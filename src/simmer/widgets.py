# SPDX-License-Identifier: GPL-3.0-or-later
"""Simmer's own widgets: recipe pictures at any size, recipe cards, ingredient and step rows."""
from __future__ import annotations

import colorsys
import hashlib
import math
import threading
import time
from collections import OrderedDict

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")
gi.require_version("Graphene", "1.0")
gi.require_version("Gsk", "4.0")
from gi.repository import Gdk, GLib, GObject, Graphene, Gsk, Gtk, Pango  # noqa: E402

from luma_appkit import ProgressLine, TypeLabel, icons  # noqa: E402

# ---------------------------------------------------------------- pictures
_FALLBACK: OrderedDict = OrderedDict()   # path → Gdk.Texture decoded by GTK itself
_FALLBACK_BYTES = [0]
_FALLBACK_BUDGET = 160 * 1024 * 1024
_TEX_LOCK = threading.Lock()
_INFLIGHT: dict = {}                      # path → callbacks waiting for the fallback decode
_QUEUE: "queue.LifoQueue" = None          # newest first: what just scrolled into view decodes next
_WORKERS: list = []


def _ensure_workers():
    global _QUEUE
    import queue
    with _TEX_LOCK:
        if _QUEUE is None:
            _QUEUE = queue.LifoQueue()
        if _WORKERS:
            return
        for _ in range(2):
            t = threading.Thread(target=_fallback_worker, daemon=True, name="simmer-art")
            t.start()
            _WORKERS.append(t)


def _fallback_worker():
    while True:
        path = _QUEUE.get()
        try:
            t = Gdk.Texture.new_from_filename(path)
        except Exception:
            t = None
        with _TEX_LOCK:
            if t is not None:
                _FALLBACK[path] = t
                _FALLBACK_BYTES[0] += t.get_width() * t.get_height() * 4
                while _FALLBACK_BYTES[0] > _FALLBACK_BUDGET and len(_FALLBACK) > 1:
                    _p, old = _FALLBACK.popitem(last=False)
                    _FALLBACK_BYTES[0] -= old.get_width() * old.get_height() * 4
            callbacks = _INFLIGHT.pop(path, [])
        GLib.idle_add(lambda cbs=callbacks, t=t: ([cb(t) for cb in cbs], False)[1])


def load_texture(path: str, px: int, done) -> None:
    """A texture for `path` sized for `px`, delivered to `done(texture | None)` on the main loop.

    Luma's shared TextureLoader decodes scaled copies off the main loop and keeps them on disk. Where its
    image loader can't run, GTK's own decoder is the fallback, within a memory budget."""
    try:
        from luma_appkit.media_style import TextureLoader
        loader = TextureLoader.shared()
    except Exception:
        loader = None

    def fallback():
        with _TEX_LOCK:
            tex = _FALLBACK.get(path)
            if tex is not None:
                _FALLBACK.move_to_end(path)
            waiting = _INFLIGHT.get(path)
            if tex is None:
                if waiting is not None:
                    waiting.append(done)
                    return
                _INFLIGHT[path] = [done]
        if tex is not None:
            done(tex)
            return
        _ensure_workers()
        _QUEUE.put(path)

    if loader is None:
        fallback()
        return

    def got(texture):
        if texture is not None:
            done(texture)
        else:
            fallback()
    try:
        loader.request(path, px, got)
    except Exception:
        fallback()


def hue_for(text: str) -> float:
    return int(hashlib.md5(text.encode()).hexdigest()[:4], 16) / 65535


class Art(Gtk.Widget):
    """A recipe's picture, square with round corners. Until (or unless) the picture loads it is a
    colour from the title with the recipe's initials, so a list never jumps or shows holes."""

    __gtype_name__ = "SimmerArt"

    def __init__(self, title: str = "", size: int = 56, radius: float | None = None, width: int | None = None):
        super().__init__(accessible_role=Gtk.AccessibleRole.IMG, halign=Gtk.Align.START, valign=Gtk.Align.START)
        self.size = size
        self.width_px = width or size
        self.radius = radius if radius is not None else max(6.0, size * 0.12)
        self.title = title
        self.texture = None
        self._want = ""
        self.set_overflow(Gtk.Overflow.HIDDEN)
        self.update_property([Gtk.AccessibleProperty.LABEL], [f"{title} picture" if title else "Recipe picture"])
        # no picture yet: a warm, food-coloured gradient (tomato, saffron, olive, herb, paprika, plum, teal) and a glyph
        palette = ((0.01, 0.55, 0.62), (0.09, 0.56, 0.75), (0.16, 0.45, 0.5), (0.3, 0.42, 0.45), (0.04, 0.48, 0.6),
                   (0.92, 0.42, 0.38), (0.47, 0.38, 0.42), (0.12, 0.6, 0.7))
        hh, ll, ss = palette[int(hue_for(title or "?") * len(palette)) % len(palette)]
        r1, g1, b1 = colorsys.hls_to_rgb(hh, ll + 0.08, ss)
        r2, g2, b2 = colorsys.hls_to_rgb((hh + 0.03) % 1, ll - 0.12, ss)
        self._c1, self._c2 = Gdk.RGBA(red=r1, green=g1, blue=b1, alpha=1), Gdk.RGBA(red=r2, green=g2, blue=b2, alpha=1)
        self._initials = ""
        self._glyph = None

    def set_size(self, size: int, width: int | None = None):
        self.size = size
        self.width_px = width or size
        self.radius = max(6.0, size * 0.12)
        self.queue_resize()

    def set_path(self, path: str):
        if not path or path == self._want:
            return
        self._want = path
        load_texture(path, int(max(self.size, self.width_px) * (self.get_scale_factor() or 1)), self._loaded)

    def _loaded(self, texture):
        if texture is not None:
            self.texture = texture
            self.queue_draw()

    def _draw_glyph(self, s, w, h):
        if self._glyph is None:
            try:
                from luma_appkit import icons as _icons
                theme = Gtk.IconTheme.get_for_display(self.get_display())
                self._glyph = theme.lookup_icon(_icons.icon_name("utensils"), None, 64, 1, Gtk.TextDirection.NONE, 0)
            except Exception:
                self._glyph = False
        if not self._glyph:
            return
        size = min(w, h) * 0.34
        s.save()
        s.translate(Graphene.Point().init((w - size) / 2, (h - size) / 2))
        try:
            self._glyph.snapshot_symbolic(s, size, size, [Gdk.RGBA(red=1, green=1, blue=1, alpha=0.85)])
        except Exception:
            self._glyph.snapshot(s, size, size)
        s.restore()

    def do_measure(self, orientation, for_size):
        n = self.width_px if orientation == Gtk.Orientation.HORIZONTAL else self.size
        return n, n, -1, -1

    def do_snapshot(self, s):
        w, h = self.get_width(), self.get_height()
        if w <= 0 or h <= 0:
            return
        rect = Graphene.Rect().init(0, 0, w, h)
        rr = Gsk.RoundedRect()
        rr.init_from_rect(rect, self.radius)
        s.push_rounded_clip(rr)
        if self.texture is not None:
            # cover: fill the frame, cropping the longer side, never stretching the food
            tw, th = self.texture.get_width(), self.texture.get_height()
            scale = max(w / tw, h / th) if tw and th else 1
            dw, dh = tw * scale, th * scale
            s.append_scaled_texture(self.texture, Gsk.ScalingFilter.TRILINEAR,
                                    Graphene.Rect().init((w - dw) / 2, (h - dh) / 2, dw, dh))
        else:
            stops = [Gsk.ColorStop(), Gsk.ColorStop()]
            stops[0].offset, stops[0].color = 0.0, self._c1
            stops[1].offset, stops[1].color = 1.0, self._c2
            s.append_linear_gradient(rect, Graphene.Point().init(0, 0), Graphene.Point().init(w, h), stops)
            if w >= 24:
                self._draw_glyph(s, w, h)
        s.pop()
        # a hairline so white artwork doesn't melt into a white page
        border = Gsk.RoundedRect()
        border.init_from_rect(rect, self.radius)
        line = Gdk.RGBA(red=0, green=0, blue=0, alpha=0.08)
        s.append_border(border, [0.5] * 4, [line] * 4)




# ---------------------------------------------------------------- words
def minutes_words(m: int) -> str:
    m = int(m or 0)
    if m <= 0:
        return ""
    h, mm = divmod(m, 60)
    return (f"{h} h" + (f" {mm} min" if mm else "")) if h else f"{mm} min"


def recipe_meta(r: dict) -> str:
    parts = []
    if r.get("total"):
        parts.append(minutes_words(r["total"]))
    if r.get("servings"):
        parts.append(f"serves {r['servings']}")
    elif r.get("yield_text"):
        parts.append(r["yield_text"])
    return " · ".join(parts)


def caption(text: str, *, ellipsize: bool = True, wrap: bool = False) -> Gtk.Widget:
    lab = Gtk.Label(label=text, xalign=0, wrap=wrap)
    if ellipsize and not wrap:
        lab.set_ellipsize(Pango.EllipsizeMode.END)
    if wrap:
        lab.set_wrap_mode(Pango.WrapMode.WORD_CHAR)
    lab.add_css_class("dim-label")
    lab.add_css_class("caption")
    return lab


def heading(text: str, *, action: tuple[str, object] | None = None) -> Gtk.Widget:
    row = Gtk.Box(spacing=8, margin_top=18, margin_bottom=6)
    t = TypeLabel(text, role="section-title")
    t.set_hexpand(True)
    t.set_halign(Gtk.Align.START)
    row.append(t)
    if action is not None:
        from luma_appkit import TextButton
        row.append(TextButton(action[0], on_click=action[1], small=True))
    return row


# ---------------------------------------------------------------- cards and grids
class RecipeCard(Gtk.Button):
    """A recipe in the grid: its picture (4:3), its name, and how long and how many it serves."""

    __gtype_name__ = "SimmerRecipeCard"

    def __init__(self, r: dict, *, width: int = 220, on_open=None, badge: str = ""):
        super().__init__(valign=Gtk.Align.START)
        self.add_css_class("flat")
        self.add_css_class("simmer-card")
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        self.art = Art(r.get("title") or "", int(width * 0.72), radius=14, width=width)
        over = Gtk.Overlay(child=self.art, halign=Gtk.Align.START)
        if r.get("favorite"):
            heart = icons.image("heart-filled", pixel_size=16)
            heart.add_css_class("simmer-card-heart")
            heart.set_halign(Gtk.Align.END)
            heart.set_valign(Gtk.Align.START)
            heart.set_margin_top(8)
            heart.set_margin_end(8)
            over.add_overlay(heart)
        if badge:
            b = Gtk.Label(label=badge, halign=Gtk.Align.START, valign=Gtk.Align.END, margin_start=8, margin_bottom=8)
            b.add_css_class("simmer-card-badge")
            over.add_overlay(b)
        box.append(over)
        t = Gtk.Label(label=r.get("title") or "Untitled", xalign=0, wrap=True, lines=2,
                      ellipsize=Pango.EllipsizeMode.END, max_width_chars=1)
        t.set_size_request(width, -1)
        t.add_css_class("simmer-card-title")
        box.append(t)
        meta = recipe_meta(r)
        if meta:
            m = caption(meta)
            m.set_size_request(width, -1)
            m.set_max_width_chars(1)
            box.append(m)
        self.set_child(box)
        self.update_property([Gtk.AccessibleProperty.LABEL], [", ".join(p for p in (r.get("title"), meta) if p)])
        if on_open is not None:
            self.connect("clicked", lambda _b: on_open())


def card_grid(cards: list[Gtk.Widget]) -> Gtk.FlowBox:
    flow = Gtk.FlowBox(selection_mode=Gtk.SelectionMode.NONE, homogeneous=True, column_spacing=14, row_spacing=20,
                       min_children_per_line=1, max_children_per_line=6, valign=Gtk.Align.START)
    flow.add_css_class("simmer-grid")
    for c in cards:
        flow.append(c)
    return flow


def plain_list(rows: list[Gtk.Widget]) -> Gtk.Box:
    box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
    box.add_css_class("simmer-list")
    for i, r in enumerate(rows):
        if i:
            sep = Gtk.Separator()
            sep.add_css_class("simmer-sep")
            box.append(sep)
        box.append(r)
    return box


class ListRow(Gtk.Button):
    """A row that opens something: a lead picture, a title and a caption, focusable for keyboards and remotes."""

    __gtype_name__ = "SimmerListRow"

    def __init__(self, title: str, subtitle: str = "", *, lead: Gtk.Widget | None = None, trail: Gtk.Widget | None = None,
                 on_open=None):
        super().__init__()
        self.add_css_class("flat")
        self.add_css_class("simmer-row")
        box = Gtk.Box(spacing=12)
        if lead is not None:
            box.append(lead)
        words = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2, hexpand=True, valign=Gtk.Align.CENTER)
        t = Gtk.Label(label=title, xalign=0, ellipsize=Pango.EllipsizeMode.END)
        t.add_css_class("simmer-row-title")
        words.append(t)
        if subtitle:
            words.append(caption(subtitle))
        box.append(words)
        if trail is not None:
            box.append(trail)
        self.set_child(box)
        self.update_property([Gtk.AccessibleProperty.LABEL], [", ".join(p for p in (title, subtitle) if p)])
        if on_open is not None:
            self.connect("clicked", lambda _b: on_open())


class CheckRow(Gtk.Box):
    """An ingredient or a shopping item you can tick off. The whole row toggles (click, Space, Enter)."""

    __gtype_name__ = "SimmerCheckRow"

    def __init__(self, text: str, *, checked: bool = False, detail: str = "", on_toggle=None, on_remove=None,
                 markup: bool = False):
        super().__init__(spacing=12, accessible_role=Gtk.AccessibleRole.CHECKBOX, focusable=True)
        self.add_css_class("simmer-check")
        from luma_appkit import Checkbox
        self.box = Checkbox(checked=checked)
        self.append(self.box)
        words = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=1, hexpand=True, valign=Gtk.Align.CENTER)
        self.label = Gtk.Label(xalign=0, wrap=True, wrap_mode=Pango.WrapMode.WORD_CHAR)
        (self.label.set_markup if markup else self.label.set_text)(text)
        self.label.add_css_class("simmer-check-text")
        words.append(self.label)
        if detail:
            words.append(caption(detail))
        self.append(words)
        if on_remove is not None:
            from luma_appkit import IconOnlyButton
            rm = IconOnlyButton("x", "Remove", on_click=on_remove)
            rm.set_valign(Gtk.Align.CENTER)
            self.append(rm)
        self.on_toggle = on_toggle
        self.checked = checked
        self._sync()
        self.update_property([Gtk.AccessibleProperty.LABEL], [self.label.get_text()])
        click = Gtk.GestureClick()
        click.connect("released", lambda *_a: self.toggle())   # the remove button claims its own clicks
        self.add_controller(click)
        keys = Gtk.EventControllerKey()
        keys.connect("key-pressed", self._key)
        self.add_controller(keys)

    def _key(self, _c, keyval, _code, _state):
        if keyval in (Gdk.KEY_space, Gdk.KEY_Return, Gdk.KEY_KP_Enter):
            self.toggle()
            return True
        return False

    def toggle(self):
        self.checked = not self.checked
        self._sync()
        if self.on_toggle is not None:
            self.on_toggle(self.checked)

    def _sync(self):
        self.box.set_checked(self.checked)
        (self.add_css_class if self.checked else self.remove_css_class)("done")
        self.update_state([Gtk.AccessibleState.CHECKED],
                          [int(Gtk.AccessibleTristate.TRUE if self.checked else Gtk.AccessibleTristate.FALSE)])


class Stepper(Gtk.Box):
    """− value + : servings."""

    __gtype_name__ = "SimmerStepper"

    def __init__(self, value: int, *, label: str, words=None, minimum: int = 1, maximum: int = 99, on_change=None):
        super().__init__(spacing=4, valign=Gtk.Align.CENTER)
        self.add_css_class("simmer-stepper")
        from luma_appkit import IconOnlyButton
        self.value, self.minimum, self.maximum, self.on_change = value, minimum, maximum, on_change
        self.words = words or (lambda v: str(v))
        self.minus = IconOnlyButton("minus", f"Fewer {label}", raised=True, on_click=lambda: self.nudge(-1))
        self.plus = IconOnlyButton("plus", f"More {label}", raised=True, on_click=lambda: self.nudge(1))
        self.text = Gtk.Label(label=self.words(value), width_chars=9)
        self.text.add_css_class("simmer-stepper-value")
        self.append(self.minus)
        self.append(self.text)
        self.append(self.plus)
        self._sync()

    def nudge(self, d: int):
        v = max(self.minimum, min(self.maximum, self.value + d))
        if v != self.value:
            self.value = v
            self._sync()
            if self.on_change:
                self.on_change(v)

    def _sync(self):
        self.text.set_text(self.words(self.value))
        self.minus.set_sensitive(self.value > self.minimum)
        self.plus.set_sensitive(self.value < self.maximum)


def chip(label: str, *, icon: str | None = None, on_click=None, active: bool = False, css: str = "") -> Gtk.Button:
    b = Gtk.Button()
    b.add_css_class("simmer-chip")
    if css:
        b.add_css_class(css)
    if active:
        b.add_css_class("active")
    inner = Gtk.Box(spacing=6)
    if icon:
        inner.append(icons.image(icon, pixel_size=14))
    inner.append(Gtk.Label(label=label))
    b.set_child(inner)
    b.update_property([Gtk.AccessibleProperty.LABEL], [label])
    if on_click is not None:
        b.connect("clicked", lambda _b: on_click())
    return b


def chip_row(chips: list[Gtk.Widget]) -> Gtk.Widget:
    box = Gtk.FlowBox(selection_mode=Gtk.SelectionMode.NONE, column_spacing=6, row_spacing=6, max_children_per_line=12,
                      min_children_per_line=1, homogeneous=False, valign=Gtk.Align.START)
    for c in chips:
        box.append(c)
    return box
