# SPDX-License-Identifier: GPL-3.0-or-later
"""Simmer: recipes and cooking for Luma.

Shape: places and content. The sidebar holds the places (Recipes, Favorites, This week, Shopping list,
Community, Cooking log) and your tags; the island holds the page; the bar holds what you do here, with the
running timers above it. Cook mode is one big step at a time, with the screen kept on, made for a tablet on
the counter or the kitchen TV.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import re
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from fractions import Fraction

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gdk, Gio, GLib, Gtk, Pango  # noqa: E402

from luma_appkit import (  # noqa: E402
    ActionCenter, AppWindow, BarAction, BarSearch, Command, CommandGroup, CommandRegistry, EmptyState, FileRequest,
    Island, NavigationSidebar, NavigationTrail, PageHeader, PanelHeading, PanelRow, ParagraphField, Place, RowLead,
    SidebarFoot, SidebarRow, SidebarToggle, TextButton, TextField, TitleIsland, Toast, ToastHost, TypeLabel,
    ask_for_file, icons,
)

from . import importer, ingredients as ING, net, timers as T  # noqa: E402
from .chime import Chime  # noqa: E402
from .community import REPORT_REASONS, Community, CommunityError, shareable  # noqa: E402
from .hsbilling import Billing  # noqa: E402
from .hsupdates import Updates  # noqa: E402
from .paywall import Paywall  # noqa: E402
from .samples import SAMPLES  # noqa: E402
from .store import Store  # noqa: E402
from .updates_ui import Updater  # noqa: E402
from .widgets import (  # noqa: E402
    Art, CheckRow, ListRow, RecipeCard, Stepper, caption, card_grid, chip, chip_row, heading, minutes_words,
    plain_list, recipe_meta,
)

APP_ID = "com.charleshartmann.Simmer"
APP_NAME = "Simmer"
VERSION = "1.0.0"
PLACES = (("recipes", "Recipes", "book-open"), ("favorites", "Favorites", "heart"), ("plan", "This week", "calendar-days"),
          ("shopping", "Shopping list", "list-checks"), ("community", "Community", "users"),
          ("cooked", "Cooking log", "history"))
PLUS = "Simmer Plus"
PLUS_PRODUCTS = ("simmer_lifetime", "simmer_plus:yearly", "simmer_plus:monthly")  # best first
PLUS_FEATURES = (
    ("calendar-days", "Meal planner", "Plan the week's dinners, then cook from the plan."),
    ("list-checks", "Smart shopping list", "One tap turns the week's plan into a list, amounts added up and sorted by aisle."),
    ("scaling", "Metric and US units", "Switch any recipe between cups and grams, with sensible rounding."),
    ("history", "Cooking log", "What you cooked and when, your favorites by the numbers, and notes for next time."),
)
SORTS = (("recent", "Recent"), ("title", "A–Z"), ("quick", "Quickest"), ("cooked", "Most cooked"))


def data_dir() -> str:
    return os.path.join(GLib.get_user_data_dir(), "simmer")


def cache_dir() -> str:
    return os.path.join(GLib.get_user_cache_dir(), "simmer")


def monday(day: dt.date) -> dt.date:
    return day - dt.timedelta(days=day.weekday())


def esc(text: str) -> str:
    return GLib.markup_escape_text(text or "")


class Window(AppWindow):
    def __init__(self, app):
        self.app_ref = app
        self.store = Store(os.path.join(data_dir(), "library.sqlite3"))
        self.billing = Billing("simmer", PLUS_PRODUCTS, os.path.join(data_dir(), "license.json"))
        self.community = Community(os.path.join(data_dir(), "community.json"))
        self.pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix="simmer")
        self.pictures: dict[str, str] = {}
        self._picture_waiters: dict[str, list] = {}
        self.timers = T.Timers()
        self._tick_id = 0
        self._ring_id = 0
        self.chime = Chime(os.path.join(cache_dir(), "bell.wav"))
        self.search_text = ""
        self.sort = self.store.get("sort") or "recent"
        self.scale: dict[int, int] = {}        # recipe id → servings chosen this session
        self.ticked: dict[int, set] = {}       # recipe id → ingredient indexes ticked off
        self.cook_step: dict[int, int] = {}
        self.week = monday(dt.date.today())
        self.community_cache: dict = {}        # (q, sort) → list
        self.community_sort = "new"
        self._inhibit = 0
        self._place_rows: dict = {}
        self._tag_rows: dict = {}
        self._populating = False
        self.search_item = None

        commands = CommandRegistry([
            CommandGroup("Recipes", (
                Command("recipe.import", "Import from a link…", self.ask_link, icon="link", shortcut=("Ctrl", "L")),
                Command("recipe.new", "Write a recipe", lambda: self.edit(None), icon="square-pen", shortcut=("Ctrl", "N")),
                Command("recipe.paste", "Paste a recipe…", self.ask_paste, icon="clipboard", shortcut=("Ctrl", "Shift", "V")),
                Command("recipe.search", "Search recipes", self.focus_search, icon="search", shortcut=("Ctrl", "F")),
                Command("recipe.random", "Surprise me", self.surprise, icon="shuffle",
                        enabled=lambda: self.store.counts()["recipes"] > 0),
            )),
            CommandGroup("Cooking", (
                Command("cook.timer", "New timer…", self.ask_timer, icon="timer", shortcut=("Ctrl", "T")),
                Command("cook.stop", "Stop the ringing timer", self.stop_ringing, icon="bell-off",
                        enabled=lambda: bool(self.timers.ringing)),
            )),
            CommandGroup("Library", (
                Command("library.backup", "Back up recipes…", self.export_backup, icon="download",
                        enabled=lambda: self.store.counts()["recipes"] > 0),
                Command("library.restore", "Restore from a backup…", self.import_backup, icon="upload"),
            )),
            CommandGroup(PLUS, (
                Command("plus.open", f"{PLUS}…", self.open_plus, icon="sparkles"),
            )),
            CommandGroup("App", (
                Command("app.updates", "Check for updates…", lambda: self.updater.open(), icon="refresh-cw"),
            )),
        ])
        super().__init__(application=app, app_id=APP_ID, title=APP_NAME, icon_name=APP_ID, commands=commands,
                         default_width=1180, default_height=820, minimum_width=360, minimum_height=294)
        self.commands_registry = commands
        self._css()

        self.sidebar = NavigationSidebar(variant="files")
        self.sidebar.append_footer(SidebarFoot(search="Search recipes", on_search=self._sidebar_search,
                                               add=("Add a recipe", "plus", self.ask_add)))
        self.sidebar.list.connect("row-activated", self._row_activated)
        self.island = Island()
        self.stack = Gtk.Stack(hexpand=True, vexpand=True, transition_type=Gtk.StackTransitionType.CROSSFADE)
        from luma_appkit.lumaui import reduced_motion
        self.stack.set_transition_duration(0 if reduced_motion() else 140)
        self.island.append(self.stack)
        self.host = ToastHost(self.island)
        body = Gtk.Box()
        body.append(self.sidebar)
        main = Gtk.Box(hexpand=True)
        main.append(self.host)
        body.append(main)
        self.title_island = TitleIsland(APP_NAME, "", lead="menu")
        self.title_island.add_trailing(TitleIsland.button("search", "Search recipes", self.focus_search))
        self.title_island.add_trailing(TitleIsland.button("ellipsis", "More", self._island_more))
        self.sidebar_toggle = SidebarToggle(self.sidebar, island=self.title_island)
        self.set_leading(self.sidebar_toggle)
        self.set_body(body)
        self.title_island.float_over(body)
        self.center = ActionCenter().attach(self.host)
        self.timer_strip = Gtk.Box(spacing=6, halign=Gtk.Align.CENTER)
        self.timer_strip.add_css_class("simmer-timers")

        self.paywall = Paywall(self, self.billing, PLUS, PLUS_FEATURES, open_uri=self.open_uri,
                               toast=lambda t, k="done": self.toast(t, k), changed=self._plus_changed)
        if self.billing.needs_revalidation():
            threading.Thread(target=self.billing.revalidate, daemon=True).start()
        self.updates = Updates(APP_ID, "chartmann1590/simmer", VERSION, os.path.join(data_dir(), "updates.json"),
                               os.path.join(cache_dir(), "updates"))
        self.updater = Updater(self, self.updates, APP_NAME, grow=lambda k, w: self.center.grow(k, w, wide=True),
                               fold=lambda: self.center.fold(),
                               toast=lambda t, k="done", action=None: self.toast(t, k, **({"action": action} if action else {})))
        GLib.timeout_add_seconds(8, self.updater.start)

        self.trail = NavigationTrail(Place("recipes", "Recipes"), on_change=lambda place, _d: self._show(place))
        self.tier_watch.connect("tier-changed", lambda *_: self._tiered())
        self.tv = False
        try:
            from luma_appkit.lumaui import watch_posture
            self._unwatch_tv = watch_posture(self._posture)
        except Exception:
            self._unwatch_tv = None
        self.connect("close-request", self._closing)
        keys = Gtk.EventControllerKey()
        keys.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
        keys.connect("key-pressed", self._key)
        self.add_controller(keys)
        self.refresh_sidebar()
        self.go("recipes")
        self._tiered()

    # ================================================================ helpers
    def _css(self):
        from luma_appkit import add_style_sheet
        path = os.path.join(os.path.dirname(__file__), "simmer.css")
        if os.path.exists(path):
            add_style_sheet(path)

    @staticmethod
    def _label(widget, text):
        widget.set_tooltip_text(text)
        widget.update_property([Gtk.AccessibleProperty.LABEL], [text])

    def toast(self, text, kind="done", **kw):
        Toast.show(self.host, text, kind=kind, **kw)

    def run(self, work, done=None, fail=None):
        """`work()` on a worker thread; `done(result)` or `fail(message)` back on the main loop."""
        def wrapped():
            try:
                result = work()
            except (net.NetError, CommunityError, importer.ImportError_) as ex:
                msg = str(ex)
                GLib.idle_add(lambda: (fail(msg) if fail else self.toast(msg, "error"), False)[1],
                              priority=GLib.PRIORITY_DEFAULT)
                return
            except Exception as ex:  # never lose a worker silently
                import traceback
                traceback.print_exc()
                msg = f"Something went wrong: {ex}"
                GLib.idle_add(lambda: (fail(msg) if fail else self.toast(msg, "error"), False)[1],
                              priority=GLib.PRIORITY_DEFAULT)
                return
            if done is not None:
                GLib.idle_add(lambda: (done(result), False)[1], priority=GLib.PRIORITY_DEFAULT)
        self.pool.submit(wrapped)

    def picture_for(self, url: str, widget: Art | None = None) -> str:
        if not url:
            return ""
        path = self.pictures.get(url)
        if path:
            if widget is not None:
                widget.set_path(path)
            return path
        cached = net.picture_path(url)
        if os.path.exists(cached) and os.path.getsize(cached) > 0:
            self.pictures[url] = cached
            if widget is not None:
                widget.set_path(cached)
            return cached
        waiters = self._picture_waiters.setdefault(url, [])
        if widget is not None:
            waiters.append(widget)
        if url not in self.pictures:
            self.pictures[url] = ""
            self.run(lambda: net.fetch_picture(url), lambda p: self._picture_ready(url, p), lambda _m: None)
        return ""

    def _picture_ready(self, url, path):
        self.pictures[url] = path
        for w in self._picture_waiters.pop(url, []):
            if path:
                w.set_path(path)

    def open_uri(self, url: str):
        Gtk.UriLauncher.new(url).launch(self, None, None, None)

    def _copy(self, text: str):
        Gdk.Display.get_default().get_clipboard().set(text)

    # ================================================================ Simmer Plus
    @property
    def plus(self) -> bool:
        return self.billing.owns()

    def open_plus(self):
        self.center.grow("plus", self.paywall.panel, wide=True)

    def need_plus(self, what: str) -> bool:
        if self.plus:
            return True
        self.toast(f"{what} comes with {PLUS}", "done")
        self.open_plus()
        return False

    def _plus_changed(self):
        self.show_bar(force=False)
        if self.trail.current.view in ("plan", "cooked", "recipe"):
            self._show(self.trail.current)

    def units(self) -> str:
        return (self.store.get("units") or "") if self.plus else ""

    def set_units(self, system: str):
        if system and not self.need_plus("Unit conversion"):
            return
        self.store.set("units", system)
        if self.trail.current.view in ("recipe", "cook"):
            self._show(self.trail.current)

    # ================================================================ navigation
    def go(self, view: str, title: str | None = None, subject=None):
        names = dict((k, t) for k, t, _ in PLACES)
        self.trail.start(Place(view, title or names.get(view, view), subject))
        if self.trail.current.view == view and self.trail.current.subject == subject:
            self._show(self.trail.current)

    def open_page(self, view: str, title: str, subject=None):
        self.trail.open(Place(view, title, subject))

    def _show(self, place: Place):
        builders = {"recipes": self._page_recipes, "favorites": self._page_favorites, "tag": self._page_tag,
                    "plan": self._page_plan, "shopping": self._page_shopping, "community": self._page_community,
                    "cooked": self._page_cooked, "recipe": self._page_recipe, "edit": self._page_edit,
                    "cook": self._page_cook, "search": self._page_search, "shared": self._page_shared}
        if place.view != "cook":
            self._keep_awake(False)
        # cooking on a TV: the step fills the screen, the sidebar steps aside
        self.sidebar.set_visible(not (self.tv and place.view == "cook"))
        try:
            page = builders[place.view](place)
        except Exception as ex:
            import traceback
            traceback.print_exc()
            page = EmptyState("Couldn't show this page", str(ex), "lumaui-circle-alert-symbolic")
        old = self.stack.get_visible_child()
        self.stack.add_named(page, f"p{time.monotonic_ns()}")
        self.stack.set_visible_child(page)
        if old is not None:
            GLib.timeout_add(400, lambda: (old.get_parent() is self.stack and self.stack.remove(old), False)[1])
        self._select_rows()
        self.show_bar()
        self.title_island.set_title(place.title, "")

    def _changed(self):
        self.refresh_sidebar()
        self._show(self.trail.current)

    def _posture(self, tv: bool):
        changed = bool(tv) != self.tv
        self.tv = bool(tv)
        if changed and getattr(self, "trail", None) is not None and self.stack.get_visible_child() is not None:
            self._show(self.trail.current)

    def _tiered(self):
        self.stack.set_margin_top(64 if self.tier == "phone" else 0)
        if getattr(self, "_last_tier", None) not in (None, self.tier):
            self._show(self.trail.current)
        self._last_tier = self.tier

    def _island_more(self):
        from luma_appkit import MenuDrawer
        MenuDrawer.present(self, self.commands_registry)

    def _card_width(self) -> int:
        return 280 if self.tv else {"phone": 150}.get(self.tier, 210)

    def page(self, place: Place, content: list[Gtk.Widget], *, meta=(), header=True, wide: int = 980):
        side = 12 if self.tier == "phone" else 22
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4, margin_start=side, margin_end=side,
                      margin_top=14, margin_bottom=150)
        if header == "back":
            if self.trail.can_go_back:
                from luma_appkit import NavigationTrailBar
                bar = NavigationTrailBar(self.trail, title=False)
                bar.set_halign(Gtk.Align.START)
                box.append(bar)
        elif header:
            box.append(PageHeader(self.trail, meta=list(meta)))
        for w in content:
            box.append(w)
        clamp = Adw.Clamp(maximum_size=wide, tightening_threshold=wide - 100, child=box)
        return Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER, vexpand=True, hexpand=True, child=clamp)

    # ================================================================ sidebar
    def refresh_sidebar(self):
        self._populating = True
        self.sidebar.clear()
        self._place_rows, self._tag_rows = {}, {}
        c = self.store.counts()
        trail = {"recipes": c["recipes"], "favorites": c["favorites"], "shopping": c["shopping"], "plan": c["planned"]}
        for key, title, icon in PLACES:
            row = SidebarRow(title, lead=RowLead.icon(icon), trail=trail.get(key) or None)
            row.place = key
            self._place_rows[key] = row
            self.sidebar.append_row(row)
        tags = self.store.tags()
        if tags:
            self.sidebar.append_section("Tags")
            for name, n in tags[:24]:
                row = SidebarRow(name, lead=RowLead.icon("tag"), trail=n)
                row.tag = name
                self._tag_rows[name.lower()] = row
                self.sidebar.append_row(row)
        self._select_rows()
        self._populating = False

    def _select_rows(self):
        if not self._place_rows:
            return
        cur = self.trail.current
        root = self.trail.places[0]
        target = self._tag_rows.get(str(cur.subject).lower()) if cur.view == "tag" else None
        if target is not None:
            self.sidebar.list.select_row(target)
        else:
            self.sidebar.list.unselect_all()
        for key, row in self._place_rows.items():
            on = key == root.view and target is None
            row.set_state_flags(Gtk.StateFlags.CHECKED, False) if on else row.unset_state_flags(Gtk.StateFlags.CHECKED)

    def _row_activated(self, _list, row):
        if self._populating:
            return
        if getattr(row, "place", None):
            self.go(row.place)
        elif getattr(row, "tag", None):
            self.go("tag", row.tag, row.tag)
        if self.tier == "phone" and hasattr(self.sidebar_toggle, "set_active"):
            self.sidebar_toggle.set_active(False)

    def _sidebar_search(self, text: str):
        if text.strip():
            self.search(text)

    # ================================================================ the bar
    def show_bar(self, force: bool = True):
        if not force and self.center.grown is not None:
            if not getattr(self, "_bar_pending", False):
                self._bar_pending = True
                GLib.timeout_add(400, self._bar_when_folded)
            return
        cur = self.trail.current
        view = cur.view
        items = []
        if view in ("recipes", "favorites", "tag", "search"):
            items.append(BarAction("plus", "Add recipe", primary=True, panel=self._add_panel, key="add"))
        elif view == "recipe":
            r = self.store.recipe(cur.subject)
            if r:
                items.append(BarAction("utensils", "Cook", primary=True, on_activate=lambda: self.cook(r["id"])))
                items.append(BarAction("heart-filled" if r["favorite"] else "heart",
                                       tooltip="Unfavorite" if r["favorite"] else "Favorite", active=bool(r["favorite"]),
                                       on_activate=lambda: self.toggle_favorite(r["id"])))
                items.append(BarAction("list-plus", tooltip="Add ingredients to the shopping list",
                                       on_activate=lambda: self.add_to_list(r["id"])))
                items.append(BarAction("calendar-days", tooltip="Plan it", panel=lambda: self._plan_panel(r["id"]), key="plan"))
                items.append(BarAction("square-pen", tooltip="Edit", on_activate=lambda: self.edit(r["id"])))
        elif view == "edit":
            items.append(BarAction("check", "Save", primary=True, on_activate=self.save_edit))
            items.append(BarAction("x", "Cancel", on_activate=self.cancel_edit))
        elif view == "cook":
            rid = cur.subject
            items.append(BarAction("chevron-left", "Back", on_activate=lambda: self.cook_move(rid, -1)))
            items.append(BarAction("chevron-right", "Next", primary=True, on_activate=lambda: self.cook_move(rid, 1)))
            items.append(BarAction("timer", tooltip="New timer", on_activate=self.ask_timer))
            items.append(BarAction("list", tooltip="Ingredients", panel=lambda: self._ingredients_panel(rid), key="ings"))
        elif view == "shopping":
            items.append(BarAction("plus", "Add item", primary=True, panel=self._shop_add_panel, key="shop-add"))
            if any(r["checked"] for r in self.store.shopping()):
                items.append(BarAction("check-check", "Clear done", on_activate=self.clear_checked))
            items.append(BarAction("calendar-days", tooltip="Shop for this week's plan", on_activate=self.shop_week))
        elif view == "plan":
            items.append(BarAction("chevron-left", tooltip="Last week", on_activate=lambda: self.move_week(-1)))
            items.append(BarAction("calendar", "This week", on_activate=lambda: self.move_week(0)))
            items.append(BarAction("chevron-right", tooltip="Next week", on_activate=lambda: self.move_week(1)))
            items.append(BarAction("list-checks", "Shop for the week", primary=True, on_activate=self.shop_week))
        elif view == "community":
            items.append(BarAction("refresh-cw", tooltip="Refresh", on_activate=self.refresh_community))
            items.append(BarAction("send", "Share a recipe", primary=True, panel=self._share_pick_panel, key="share-pick"))
        elif view == "shared":
            items.append(BarAction("download", "Save to my recipes", primary=True,
                                   on_activate=lambda: self.save_shared(cur.subject)))
            items.append(BarAction("flag", tooltip="Report", panel=lambda: self._report_panel(cur.subject), key="report"))
        if view not in ("edit", "cook"):
            items.append(BarSearch("Search", label="Search recipes", collapsed=True, on_activate=self.search))
            items.append(BarAction("timer", tooltip="New timer", on_activate=self.ask_timer))
        items.append(BarAction("settings", tooltip="Settings", panel=self._settings_panel, key="settings"))
        more = [BarAction("link", "Import from a link…", on_activate=self.ask_link),
                BarAction("clipboard", "Paste a recipe…", on_activate=self.ask_paste),
                BarAction("square-pen", "Write a recipe", on_activate=lambda: self.edit(None)),
                BarAction("shuffle", "Surprise me", on_activate=self.surprise),
                BarAction("download", "Back up recipes…", on_activate=self.export_backup),
                BarAction("upload", "Restore from a backup…", on_activate=self.import_backup),
                BarAction("sparkles", f"{PLUS}…", on_activate=self.open_plus),
                BarAction("refresh-cw", "Check for updates…", on_activate=lambda: self.updater.open())]
        if view == "recipe":
            r = self.store.recipe(cur.subject)
            if r:
                more = [BarAction("copy", "Copy as text", on_activate=lambda: self.copy_recipe(r["id"])),
                        BarAction("users", "Share to the community…", on_activate=lambda: self.ask_share(r["id"])),
                        BarAction("trash-2", "Delete recipe", danger=True, on_activate=lambda: self.delete(r["id"]))] + more
        self._fill_timer_strip()
        head = self.timer_strip if self.timers.items else None
        self.center.show_bar(items, more=more, head=head, fill=True, head_inset=False)
        self.search_item = next((i for i in items if isinstance(i, BarSearch)), None)

    def _bar_when_folded(self):
        if self.center.grown is not None:
            return True
        self._bar_pending = False
        self.show_bar()
        return False

    def focus_search(self):
        if self.search_item is not None:
            try:
                self.center.open_search(self.search_item)
                return
            except Exception:
                pass
        self.go("recipes")

    def _settings_panel(self):
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        box.append(PanelHeading(PLUS))
        if self.plus:
            box.append(PanelRow(f"{PLUS} is on", icon="sparkles", detail="Manage", on_activate=self.open_plus))
        else:
            box.append(PanelRow(f"Get {PLUS}", icon="sparkles", subtitle="Meal planner, smart list, units and cooking log",
                                on_activate=self.open_plus))
        words = {"": "As written", "us": "US (cups, ounces)", "metric": "Metric (grams, ml)"}
        box.append(PanelRow("Units", icon="scaling", detail=words.get(self.units(), "As written"), closes=False,
                            on_activate=self._cycle_units))
        box.append(PanelHeading("Cooking"))
        box.append(PanelRow("Keep the screen on in cook mode", icon="sun", toggle=self.store.get("keep_awake", True),
                            on_toggle=lambda on: self.store.set("keep_awake", on)))
        box.append(PanelRow("Ring timers out loud", icon="bell", toggle=self.store.get("chime", True),
                            on_toggle=lambda on: self.store.set("chime", on)))
        box.append(PanelHeading("Community"))
        box.append(PanelRow("Name on recipes you share", icon="user", detail=self.community.name or "Not set",
                            closes=False, on_activate=self.ask_name))
        mine = self.community.mine()
        if mine:
            box.append(PanelRow("Recipes you've shared", icon="users", detail=str(len(mine)), on_activate=self._mine_panel_open))
        box.append(PanelHeading("Updates"))
        box.append(PanelRow(f"Check for {APP_NAME} updates automatically", icon="download", toggle=self.updates.automatic,
                            on_toggle=self.updates.set_automatic))
        box.append(PanelRow("Check now", icon="refresh-cw", detail=VERSION, on_activate=lambda: self.updater.check(manual=True)))
        return box

    def _cycle_units(self):
        order = ("", "us", "metric")
        cur = self.units()
        nxt = order[(order.index(cur) + 1) % 3] if cur in order else ""
        self.center.fold()
        if nxt and not self.plus:
            self.need_plus("Unit conversion")
            return
        self.set_units(nxt)
        self.center.grow("settings", self._settings_panel)

    # ================================================================ pages: the library
    def _cards(self, recipes: list[dict]) -> Gtk.Widget:
        width = self._card_width()
        cards = []
        for r in recipes:
            card = RecipeCard(r, width=width, on_open=lambda r=r: self.open_recipe(r["id"]))
            self.picture_for(r.get("image") or "", card.art)
            cards.append(card)
        return card_grid(cards)

    def _sort_chips(self):
        return chip_row([chip(name, on_click=lambda k=key: self._set_sort(k), active=key == self.sort)
                         for key, name in SORTS])

    def _set_sort(self, key):
        self.sort = key
        self.store.set("sort", key)
        self._show(self.trail.current)

    def _page_recipes(self, place):
        recipes = self.store.recipes(order=self.sort)
        if not recipes:
            return self._welcome()
        return self.page(place, [self._sort_chips(), self._cards(recipes)])

    def _welcome(self):
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=14, valign=Gtk.Align.CENTER, halign=Gtk.Align.CENTER,
                      margin_start=24, margin_end=24, margin_bottom=120)
        empty = EmptyState("Your recipe box is empty", "Import a recipe from any cooking site, paste one, or write your own.",
                           "lumaui-utensils-symbolic", primary=("Import from a link", self.ask_link),
                           secondary=("Write a recipe", lambda: self.edit(None)))
        empty.set_vexpand(False)
        box.append(empty)
        row = Gtk.Box(spacing=8, halign=Gtk.Align.CENTER)
        row.append(TextButton("Try four sample recipes", icon="sparkles", style="plain", on_click=self.add_samples))
        row.append(TextButton("Browse the community", icon="users", style="plain", on_click=lambda: self.go("community")))
        box.append(row)
        return box

    def add_samples(self):
        have = {r["title"] for r in self.store.recipes()}
        n = 0
        for s in SAMPLES:
            if s["title"] not in have:
                self.store.add(dict(s))
                n += 1
        self.toast(f"Added {n} sample recipe" + ("" if n == 1 else "s"), "added")
        self._changed()

    def _page_favorites(self, place):
        recipes = self.store.recipes(favorites=True, order=self.sort)
        if not recipes:
            return EmptyState("No favorites yet", "Tap the heart on a recipe you love and it lives here.",
                              "lumaui-heart-symbolic", primary=("All recipes", lambda: self.go("recipes")))
        return self.page(place, [self._sort_chips(), self._cards(recipes)], meta=[f"{len(recipes)} favorite" +
                                                                                 ("" if len(recipes) == 1 else "s")])

    def _page_tag(self, place):
        recipes = self.store.recipes(tag=place.subject, order=self.sort)
        if not recipes:
            return EmptyState(f"Nothing tagged {place.subject}", "Tags come from the recipe; edit a recipe to change them.",
                              "lumaui-tag-symbolic", primary=("All recipes", lambda: self.go("recipes")))
        return self.page(place, [self._sort_chips(), self._cards(recipes)], meta=[f"{len(recipes)} recipes"])

    def search(self, text: str):
        text = (text or "").strip()
        if not text:
            return
        self.search_text = text
        if self.trail.current.view == "community":
            self.community_query = text
            self._show(self.trail.current)
            return
        self.open_page("search", f"“{text}”", text)

    def _page_search(self, place):
        recipes = self.store.recipes(search=place.subject, order=self.sort)
        content = []
        if recipes:
            content.append(self._cards(recipes))
        else:
            content.append(EmptyState("No recipes match", "Search looks at names, ingredients and tags.",
                                      "lumaui-search-symbolic", primary=("Search the community",
                                                                         lambda: self._search_community(place.subject))))
        return self.page(place, content, meta=[f"{len(recipes)} found"])

    def _search_community(self, text):
        self.community_query = text
        self.go("community")

    def surprise(self):
        import random
        recipes = self.store.recipes()
        if recipes:
            self.open_recipe(random.choice(recipes)["id"])

    # ================================================================ pages: one recipe
    def open_recipe(self, rid: int):
        r = self.store.recipe(rid)
        if r:
            self.open_page("recipe", r["title"], rid)

    def servings_for(self, r) -> int:
        return self.scale.get(r["id"]) or r["servings"] or 0

    def factor(self, r) -> Fraction:
        base = r["servings"] or 0
        want = self.servings_for(r)
        return Fraction(want, base) if base and want else Fraction(1)

    def _page_recipe(self, place):
        r = self.store.recipe(place.subject)
        if r is None:
            return EmptyState("This recipe is gone", "It may have been deleted.", "lumaui-circle-alert-symbolic",
                              primary=("All recipes", lambda: self.go("recipes")))
        phone = self.tier == "phone"
        content = []
        top = Gtk.Box(orientation=Gtk.Orientation.VERTICAL if phone else Gtk.Orientation.HORIZONTAL, spacing=20,
                      margin_bottom=6)
        art_w = 320 if not phone else 300
        art = Art(r["title"], int(art_w * 0.75), radius=18, width=art_w)
        self.picture_for(r["image"], art)
        top.append(art)
        info = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8, valign=Gtk.Align.CENTER, hexpand=True)
        title = Gtk.Label(label=r["title"], xalign=0, wrap=True, wrap_mode=Pango.WrapMode.WORD_CHAR, selectable=True)
        title.add_css_class("simmer-title")
        info.append(title)
        facts = []
        for label, mins in (("Prep", r["prep"]), ("Cook", r["cook"]), ("Total", r["total"])):
            if mins:
                facts.append(f"{label} {minutes_words(mins)}")
        if facts:
            info.append(caption(" · ".join(facts)))
        by = []
        if r["author"]:
            by.append(f"By {r['author']}")
        if r["cooked"]:
            by.append(f"Cooked {r['cooked']} time" + ("" if r["cooked"] == 1 else "s"))
        if by:
            info.append(caption(" · ".join(by)))
        if r["tags"]:
            info.append(chip_row([chip(t, icon="tag", on_click=lambda t=t: self.go("tag", t, t)) for t in r["tags"]]))
        if r["source"]:
            host = re.sub(r"^www\.", "", re.sub(r"^https?://([^/]+).*$", r"\1", r["source"]))
            link = Gtk.Button(label=f"From {host}", halign=Gtk.Align.START)
            link.add_css_class("flat")
            link.add_css_class("simmer-link")
            link.connect("clicked", lambda _b: self.open_uri(r["source"]))
            info.append(link)
        cook = TextButton("Start cooking", icon="utensils", style="key", on_click=lambda: self.cook(r["id"]))
        cook.set_halign(Gtk.Align.START)
        cook.set_margin_top(6)
        info.append(cook)
        top.append(info)
        content.append(top)

        # ingredients, scaled
        if r["ingredients"]:
            row = Gtk.Box(spacing=10, margin_top=18, margin_bottom=6)
            t = TypeLabel("Ingredients", role="section-title")
            t.set_hexpand(True)
            t.set_halign(Gtk.Align.START)
            row.append(t)
            if r["servings"]:
                row.append(Stepper(self.servings_for(r), label="servings", words=lambda v: f"{v} serving" + ("" if v == 1 else "s"),
                                   maximum=max(99, r["servings"] * 8), on_change=lambda v: self._set_servings(r, v)))
            content.append(row)
            if r["servings"] and self.servings_for(r) != r["servings"]:
                content.append(caption(f"Scaled from {r['servings']} servings"))
            ticked = self.ticked.setdefault(r["id"], set())
            rows = []
            system, factor = self.units(), self.factor(r)
            for i, line in enumerate(r["ingredients"]):
                if line.startswith("## "):
                    rows.append(heading(line[3:]))
                    continue
                text = ING.scaled(ING.parse(line), factor, system)
                rows.append(CheckRow(text, checked=i in ticked,
                                     on_toggle=lambda on, i=i: (ticked.add(i) if on else ticked.discard(i))))
            content.append(plain_list(rows))
        # steps, with their timers
        if r["steps"]:
            content.append(heading("Method"))
            n = 0
            for step in r["steps"]:
                if step.startswith("## "):
                    content.append(heading(step[3:]))
                    continue
                n += 1
                content.append(self._step_row(n, step, r))
        if r["notes"]:
            content.append(heading("Notes"))
            notes = Gtk.Label(label=r["notes"], xalign=0, wrap=True, wrap_mode=Pango.WrapMode.WORD_CHAR, selectable=True)
            notes.add_css_class("simmer-body")
            content.append(notes)
        log = self.store.log(r["id"], 5) if self.plus else []
        if log:
            content.append(heading("Cooked"))
            for e in log:
                content.append(caption(time.strftime("%A, %B %-d", time.localtime(e["at"])) +
                                       (f": {e['note']}" if e["note"] else "")))
        return self.page(place, content, header="back", wide=860)

    def _step_row(self, n: int, step: str, r: dict) -> Gtk.Widget:
        row = Gtk.Box(spacing=14, margin_top=8, margin_bottom=8)
        num = Gtk.Label(label=str(n), valign=Gtk.Align.START)
        num.add_css_class("simmer-step-number")
        row.append(num)
        col = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6, hexpand=True)
        text = Gtk.Label(label=step, xalign=0, wrap=True, wrap_mode=Pango.WrapMode.WORD_CHAR, selectable=True)
        text.add_css_class("simmer-body")
        col.append(text)
        found = T.durations(step)
        if found:
            col.append(chip_row([chip(f"Start {T.words(f.seconds)}", icon="timer",
                                      on_click=lambda f=f: self.start_timer(self._timer_label(step, f), f.seconds, r["title"]))
                                 for f in found]))
        row.append(col)
        return row

    @staticmethod
    def _timer_label(step: str, f) -> str:
        """"Simmer" from "Turn the heat down and simmer for 20 minutes": the verb before the time, else the step's start."""
        before = step[:f.start]
        verbs = re.findall(r"\b(bake|boil|simmer|roast|rest|cook|chill|fry|grill|steam|rise|proof|marinate|cool|soak|"
                           r"braise|poach|toast|reduce|knead|blend|stir|whisk|melt|sear|brown|saute|sauté)\b", before, re.I)
        if verbs:
            return verbs[-1].capitalize()
        words = before.strip().split()
        return " ".join(words[:3]).rstrip(",.").capitalize() or "Timer"

    def _set_servings(self, r, v):
        self.scale[r["id"]] = v
        GLib.idle_add(lambda: (self._show(self.trail.current), False)[1])

    def toggle_favorite(self, rid):
        r = self.store.recipe(rid)
        if r:
            self.store.update(rid, favorite=0 if r["favorite"] else 1)
            self.toast("Added to favorites" if not r["favorite"] else "Removed from favorites",
                       "unfavourite" if r["favorite"] else "favourite")
            self._changed()

    def copy_recipe(self, rid):
        r = self.store.recipe(rid)
        if r:
            self._copy(importer.to_text(r))
            self.toast("Copied as text", "copied")

    def delete(self, rid):
        saved = self.store.delete(rid)
        if not saved:
            return
        if self.trail.current.view in ("recipe", "cook") and self.trail.current.subject == rid:
            self.go("recipes")
        else:
            self._changed()

        def undo():
            self.store.restore(saved)
            self._changed()
        self.toast(f"Deleted {saved['title']}", "deleted", undo=undo)

    # ================================================================ adding recipes
    def _add_panel(self):
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        box.append(PanelHeading("Add a recipe"))
        box.append(PanelRow("Import from a link", icon="link", subtitle="Most cooking sites work", on_activate=self.ask_link))
        box.append(PanelRow("Paste a recipe", icon="clipboard", subtitle="From a message, a note or a page",
                            on_activate=self.ask_paste))
        box.append(PanelRow("Write a recipe", icon="square-pen", on_activate=lambda: self.edit(None)))
        box.append(PanelRow("Find one in the community", icon="users", on_activate=lambda: self.go("community")))
        return box

    def ask_add(self):
        self.center.grow("add", self._add_panel)

    def ask_link(self):
        def go(_t=None):
            self.center.fold()
            self.import_link(field.text)
        field = TextField("Recipe address", placeholder="https://", purpose="url", on_activate=go)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10, margin_start=4, margin_end=4)
        box.append(PanelHeading("Import from a link"))
        box.append(field)
        box.append(TypeLabel("Simmer reads the recipe the site publishes for search engines: ingredients, steps, "
                             "times and the picture. Nothing else on the page comes along.", role="caption", wrap=True))
        box.append(TextButton("Import", icon="download", style="fill", on_click=go))
        self.center.grow("link", box)
        clip = Gdk.Display.get_default().get_clipboard()

        def pasted(c, res):
            try:
                t = (c.read_text_finish(res) or "").strip()
            except GLib.Error:
                return
            if t.startswith(("http://", "https://")) and not field.text:
                field.entry.set_text(t)
        clip.read_text_async(None, pasted)
        GLib.idle_add(lambda: (field.entry.grab_focus(), False)[1])

    def import_link(self, text: str):
        url = (text or "").strip()
        if not url:
            return
        if "://" not in url:
            url = "https://" + url
        if not url.lower().startswith(("http://", "https://")):
            self.toast("That isn't a web address.", "error")
            return
        self.toast("Reading the recipe…", "done")

        def got(rec):
            partial = rec.pop("partial", False)
            rid = self.store.add(rec)
            self.refresh_sidebar()
            if partial:
                self.toast("That page had no full recipe; add the ingredients and steps.", "warning")
                self.edit(rid)
            else:
                self.toast(f"Imported {rec['title']}", "added")
                self.open_recipe(rid)
        self.run(lambda: net.fetch_recipe(url), got)

    def ask_paste(self):
        para = ParagraphField("", placeholder="Paste a recipe: the name first, then the ingredients and the steps.",
                              label="Recipe text")
        para.set_size_request(-1, 180)
        scroller = Gtk.ScrolledWindow(child=para, min_content_height=180, max_content_height=320,
                                      propagate_natural_height=True, hscrollbar_policy=Gtk.PolicyType.NEVER)

        def go():
            try:
                rec = importer.from_text(para.text)
            except importer.ImportError_ as ex:
                self.toast(str(ex), "error")
                return
            self.center.fold()
            rid = self.store.add(rec)
            self.refresh_sidebar()
            self.toast(f"Added {rec['title']}. Check it over.", "added")
            self.edit(rid)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10, margin_start=4, margin_end=4)
        box.append(PanelHeading("Paste a recipe"))
        box.append(scroller)
        box.append(TextButton("Add recipe", icon="plus", style="fill", on_click=go))
        self.center.grow("paste", box, wide=True)
        GLib.idle_add(lambda: (para.grab_focus(), False)[1])

    # ================================================================ editing
    def edit(self, rid):
        self.center.fold()
        r = self.store.recipe(rid) if rid else None
        self.open_page("edit", f"Edit {r['title']}" if r else "New recipe", rid)

    def _page_edit(self, place):
        r = self.store.recipe(place.subject) if place.subject else None
        r = r or importer.blank()
        f = {}
        f["title"] = TextField("Name", value=r["title"], placeholder="Grandma's lasagne")
        row = Gtk.Box(spacing=10, homogeneous=True)
        f["servings"] = TextField("Serves", value=str(r["servings"] or ""), placeholder="4", purpose="number")
        f["prep"] = TextField("Prep (minutes)", value=str(r["prep"] or ""), placeholder="15", purpose="number")
        f["cook"] = TextField("Cook (minutes)", value=str(r["cook"] or ""), placeholder="30", purpose="number")
        for k in ("servings", "prep", "cook"):
            row.append(f[k])
        f["tags"] = TextField("Tags", value=", ".join(r["tags"]), placeholder="Dinner, Vegetarian",
                              hint="Separate tags with commas.")
        f["image"] = TextField("Picture address", value=r["image"], placeholder="https://… (optional)", purpose="url")
        f["ingredients"] = ParagraphField("\n".join(r["ingredients"]), placeholder="One ingredient per line:\n2 cups flour\n"
                                          "1 tsp salt\n## For the sauce (a line starting ## is a heading)",
                                          label="Ingredients")
        f["steps"] = ParagraphField("\n".join(r["steps"]), placeholder="One step per line. Times like “20 minutes” "
                                    "become timers you can start with a tap.", label="Method")
        f["notes"] = ParagraphField(r["notes"], placeholder="Notes, swaps, what you'd change next time", label="Notes")
        self._edit_fields = f
        content = [f["title"], row, f["tags"]]
        for key, label in (("ingredients", "Ingredients"), ("steps", "Method"), ("notes", "Notes")):
            content.append(heading(label))
            well = Gtk.Box()
            well.add_css_class("simmer-well")
            f[key].set_size_request(-1, 140 if key != "notes" else 80)
            well.append(f[key])
            content.append(well)
        content.append(f["image"])
        return self.page(place, content, header="back", wide=760)

    def save_edit(self):
        f = getattr(self, "_edit_fields", None)
        place = self.trail.current
        if not f or place.view != "edit":
            return

        def num(k):
            m = re.search(r"\d+", f[k].text or "")
            return int(m.group(0)) if m else 0
        lines = lambda k: [" ".join(x.split()) for x in f[k].text.split("\n") if x.strip()]  # noqa: E731
        fields = {"title": f["title"].text.strip() or "Untitled recipe", "servings": min(num("servings"), 999),
                  "prep": num("prep"), "cook": num("cook"), "tags": [t.strip() for t in f["tags"].text.split(",") if t.strip()][:8],
                  "image": f["image"].text.strip() if f["image"].text.strip().startswith(("https://", "http://")) else "",
                  "ingredients": lines("ingredients"), "steps": lines("steps"), "notes": f["notes"].text.strip()}
        fields["total"] = fields["prep"] + fields["cook"]
        rid = place.subject
        if rid:
            old = self.store.recipe(rid)
            if old and old["total"] and not (fields["prep"] or fields["cook"]):
                fields["total"] = old["total"]
            self.store.update(rid, **fields)
        else:
            rid = self.store.add(fields)
        self.scale.pop(rid, None)
        self.refresh_sidebar()
        self.toast("Saved", "saved")
        self.trail.back() if place.subject else None
        if self.trail.current.view == "recipe" and self.trail.current.subject == rid:
            self._show(self.trail.current)
        else:
            self.open_recipe(rid)

    def cancel_edit(self):
        if self.trail.can_go_back:
            self.trail.back()
        else:
            self.go("recipes")

    # ================================================================ cook mode
    def cook(self, rid):
        r = self.store.recipe(rid)
        if not r:
            return
        if not [s for s in r["steps"] if not s.startswith("## ")]:
            self.toast("This recipe has no steps yet. Edit it to add some.", "warning")
            return
        self.cook_step.setdefault(rid, 0)
        self.open_page("cook", r["title"], rid)

    def _cook_steps(self, r) -> list[tuple[str, str]]:
        """[(section, step)] without the section headings."""
        out, section = [], ""
        for s in r["steps"]:
            if s.startswith("## "):
                section = s[3:]
            else:
                out.append((section, s))
        return out

    def _page_cook(self, place):
        r = self.store.recipe(place.subject)
        if r is None:
            return EmptyState("This recipe is gone", "", "lumaui-circle-alert-symbolic")
        steps = self._cook_steps(r)
        i = max(0, min(self.cook_step.get(r["id"], 0), len(steps)))
        self._keep_awake(True)
        big = self.tv or self.tier != "phone"
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=14, margin_start=24, margin_end=24,
                      margin_top=24 if self.tier != "phone" else 70, margin_bottom=150, valign=Gtk.Align.FILL)
        box.add_css_class("simmer-cook")
        if self.tv:
            box.add_css_class("tv")
        top = Gtk.Box(spacing=10)
        back = TextButton("Done cooking" if i < len(steps) else "Back to recipe", icon="x", style="plain",
                          on_click=lambda: self.trail.back())
        top.append(back)
        top.append(Gtk.Box(hexpand=True))
        top.append(caption(r["title"]))
        box.append(top)
        progress = Gtk.Box(spacing=4, homogeneous=True)
        for k in range(len(steps)):
            seg = Gtk.Box(hexpand=True)
            seg.set_size_request(-1, 5)
            seg.add_css_class("simmer-seg")
            if k < i:
                seg.add_css_class("done")
            elif k == i:
                seg.add_css_class("now")
            progress.append(seg)
        progress.update_property([Gtk.AccessibleProperty.LABEL], [f"Step {min(i + 1, len(steps))} of {len(steps)}"])
        box.append(progress)
        if i >= len(steps):
            done = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12, valign=Gtk.Align.CENTER, vexpand=True,
                           halign=Gtk.Align.CENTER)
            done.append(icons.image("utensils", pixel_size=48))
            t = Gtk.Label(label="Enjoy your meal")
            t.add_css_class("simmer-cook-step")
            done.append(t)
            note = TextField("A note for next time (optional)", placeholder="Less salt, more garlic", size="regular")
            note.set_size_request(380, -1)
            done.append(note)
            row = Gtk.Box(spacing=10, halign=Gtk.Align.CENTER)
            row.append(TextButton("Mark as cooked", icon="check", style="key",
                                  on_click=lambda: self.finish_cooking(r["id"], note.text)))
            row.append(TextButton("Start over", icon="rotate-ccw", style="plain",
                                  on_click=lambda: self.cook_goto(r["id"], 0)))
            done.append(row)
            box.append(done)
            return self._cook_scroll(box)
        section, step = steps[i]
        label = f"Step {i + 1} of {len(steps)}" + (f" · {section}" if section else "")
        lab = Gtk.Label(label=label, xalign=0)
        lab.add_css_class("simmer-cook-count")
        box.append(lab)
        text = Gtk.Label(label=step, xalign=0, wrap=True, wrap_mode=Pango.WrapMode.WORD_CHAR, selectable=True,
                         valign=Gtk.Align.START)
        text.add_css_class("simmer-cook-step")
        if big:
            text.add_css_class("big")
        if self.tv:   # read from across the kitchen: the TV theme's type scale is for menus, not recipes
            attrs = Pango.AttrList()
            attrs.insert(Pango.attr_size_new_absolute(46 * Pango.SCALE))
            text.set_attributes(attrs)
        box.append(text)
        found = T.durations(step)
        if found:
            box.append(chip_row([chip(f"Start a {T.words(f.seconds)} timer", icon="timer", css="big",
                                      on_click=lambda f=f: self.start_timer(self._timer_label(step, f), f.seconds, r["title"]))
                                 for f in found]))
        used = self._ingredients_in(step, r)
        if used:
            box.append(heading("For this step"))
            for line in used:
                lab = Gtk.Label(label=f"•  {line}", xalign=0, wrap=True)
                lab.add_css_class("simmer-cook-ing")
                if self.tv:
                    attrs = Pango.AttrList()
                    attrs.insert(Pango.attr_size_new_absolute(28 * Pango.SCALE))
                    lab.set_attributes(attrs)
                box.append(lab)
        nav = Gtk.Box(spacing=12, halign=Gtk.Align.FILL, margin_top=18, homogeneous=True)
        prev = TextButton("Previous", icon="chevron-left", style="fill", on_click=lambda: self.cook_move(r["id"], -1))
        prev.set_sensitive(i > 0)
        nxt = TextButton("Next step" if i + 1 < len(steps) else "Finish", icon="chevron-right", style="key",
                         on_click=lambda: self.cook_move(r["id"], 1))
        nav.append(prev)
        nav.append(nxt)
        box.append(nav)
        hint = caption("← and → move between steps · Space starts this step's timer", wrap=True)
        hint.set_margin_top(4)
        if self.tier != "phone":
            box.append(hint)
        return self._cook_scroll(box)

    def _cook_scroll(self, box):
        clamp = Adw.Clamp(maximum_size=1300 if self.tv else 820, child=box)
        return Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER, vexpand=True, child=clamp)

    def _ingredients_in(self, step: str, r: dict) -> list[str]:
        s = " " + step.lower() + " "
        out = []
        factor, system = self.factor(r), self.units()
        for line in r["ingredients"]:
            if line.startswith("## "):
                continue
            ing = ING.parse(line)
            key = ing.key
            words = [w for w in key.split() if len(w) > 2]
            if not words:
                continue
            main = words[-1]
            if re.search(rf"\b{re.escape(main)}(e?s)?\b", s) or (key and key in s):
                out.append(ING.scaled(ing, factor, system))
        return out[:8]

    def cook_move(self, rid, d):
        r = self.store.recipe(rid)
        if not r:
            return
        n = len(self._cook_steps(r))
        self.cook_goto(rid, max(0, min(n, self.cook_step.get(rid, 0) + d)))

    def cook_goto(self, rid, i):
        self.cook_step[rid] = i
        if self.trail.current.view == "cook":
            self._show(self.trail.current)

    def finish_cooking(self, rid, note=""):
        self.store.cooked(rid, (note or "").strip()[:300])
        self.cook_step.pop(rid, None)
        self.ticked.pop(rid, None)
        self.toast("Logged. Nice cooking!", "done")
        self.trail.back()

    def _keep_awake(self, on: bool):
        if on and not self._inhibit and self.store.get("keep_awake", True):
            try:
                self._inhibit = self.app_ref.inhibit(self, Gtk.ApplicationInhibitFlags.IDLE, "Cooking from a recipe")
            except Exception:
                self._inhibit = 0
        elif not on and self._inhibit:
            try:
                self.app_ref.uninhibit(self._inhibit)
            except Exception:
                pass
            self._inhibit = 0

    def _ingredients_panel(self, rid):
        r = self.store.recipe(rid)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        box.append(PanelHeading("Ingredients"))
        ticked = self.ticked.setdefault(rid, set())
        rows = []
        for i, line in enumerate(r["ingredients"] if r else []):
            if line.startswith("## "):
                continue
            rows.append(CheckRow(ING.scaled(ING.parse(line), self.factor(r), self.units()), checked=i in ticked,
                                 on_toggle=lambda on, i=i: (ticked.add(i) if on else ticked.discard(i))))
        scroller = Gtk.ScrolledWindow(child=plain_list(rows), max_content_height=420, propagate_natural_height=True,
                                      hscrollbar_policy=Gtk.PolicyType.NEVER)
        box.append(scroller)
        return box

    def _key(self, _c, keyval, _code, state):
        if self.trail.current.view != "cook" or self.center.grown is not None:
            return False
        focus = self.get_focus()
        if isinstance(focus, (Gtk.Text, Gtk.Entry, Gtk.TextView)):
            return False
        rid = self.trail.current.subject
        if keyval in (Gdk.KEY_Right, Gdk.KEY_Page_Down):
            self.cook_move(rid, 1)
            return True
        if keyval in (Gdk.KEY_Left, Gdk.KEY_Page_Up):
            self.cook_move(rid, -1)
            return True
        if keyval == Gdk.KEY_space:
            if self.timers.ringing:
                self.stop_ringing()
                return True
            r = self.store.recipe(rid)
            steps = self._cook_steps(r) if r else []
            i = self.cook_step.get(rid, 0)
            if i < len(steps):
                found = T.durations(steps[i][1])
                if found:
                    self.start_timer(self._timer_label(steps[i][1], found[0]), found[0].seconds, r["title"])
                    return True
        if keyval == Gdk.KEY_Escape:
            self.trail.back()
            return True
        return False

    # ================================================================ timers
    def ask_timer(self):
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        box.append(PanelHeading("New timer"))
        for mins in (1, 3, 5, 10, 15, 20, 30, 45, 60):
            box.append(PanelRow(T.words(mins * 60), icon="timer", on_activate=lambda m=mins: self.start_timer("Timer", m * 60)))
        field = TextField("Or type a time", placeholder="12 minutes, 1 h 30 min", size="panel",
                          on_activate=lambda _t: self._typed_timer(field.text))
        box.append(field)
        self.center.grow("timer", box)

    def _typed_timer(self, text):
        t = text.strip()
        if t.isdigit():
            t += " minutes"
        found = T.durations(t)
        if not found:
            self.toast("Type a time like 12 minutes or 1 h 30 min", "warning")
            return
        self.center.fold()
        self.start_timer("Timer", found[0].seconds)

    def start_timer(self, label: str, seconds: int, recipe: str = ""):
        t = self.timers.start(label, seconds, recipe)
        self.toast(f"{t.label}: {T.words(seconds)} timer started", "done")
        if not self._tick_id:
            self._tick_id = GLib.timeout_add(500, self._tick)
        self.show_bar(force=False)

    def _tick(self):
        for t in self.timers.tick():
            self._rang(t)
        self._update_timer_labels()
        if not self.timers.items:
            self._tick_id = 0
            return False
        return True

    def _rang(self, t):
        self.toast(f"{t.label} is done" + (f" ({t.recipe})" if t.recipe else ""), "notified",
                   action=("Stop", self.stop_ringing))
        if self.store.get("chime", True):
            self.chime.ring()
            if not self._ring_id:
                self._ring_count = 0
                self._ring_id = GLib.timeout_add_seconds(4, self._ring_again)
        if not self.is_active():
            n = Gio.Notification.new(f"{t.label} is done")
            n.set_body(t.recipe or f"Your {T.words(t.seconds)} timer finished.")
            n.set_priority(Gio.NotificationPriority.URGENT)
            n.set_default_action("app.activate")
            try:
                self.app_ref.send_notification(f"timer-{t.id}", n)
            except Exception:
                pass
        self.show_bar(force=False)

    def _ring_again(self):
        self._ring_count = getattr(self, "_ring_count", 0) + 1
        if not self.timers.ringing or self._ring_count > 30:   # two minutes at most
            self._ring_id = 0
            return False
        if self.store.get("chime", True):
            self.chime.ring()
        return True

    def stop_ringing(self):
        for t in list(self.timers.ringing):
            self.timers.cancel(t.id)
            try:
                self.app_ref.withdraw_notification(f"timer-{t.id}")
            except Exception:
                pass
        self.chime.stop()
        self.show_bar(force=False)

    def _fill_timer_strip(self):
        child = self.timer_strip.get_first_child()
        while child is not None:
            self.timer_strip.remove(child)
            child = self.timer_strip.get_first_child()
        self._timer_labels = {}
        for t in self.timers.items:
            b = Gtk.MenuButton()
            b.add_css_class("simmer-timer")
            if t.rang:
                b.add_css_class("ringing")
            elif not t.running:
                b.add_css_class("paused")
            inner = Gtk.Box(spacing=6)
            inner.append(icons.image("bell" if t.rang else "timer", pixel_size=14))
            lab = Gtk.Label(label=self._timer_words(t))
            lab.add_css_class("simmer-timer-time")
            inner.append(lab)
            b.set_child(inner)
            self._timer_labels[t.id] = (lab, b)
            pop = Gtk.Popover()
            menu = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2, margin_top=6, margin_bottom=6,
                           margin_start=6, margin_end=6)
            acts = []
            if t.rang:
                acts.append(("Stop", lambda t=t: self.stop_ringing()))
                acts.append(("1 more minute", lambda t=t: self._timer_act(t.id, "add", 60)))
            else:
                acts.append(("Pause" if t.running else "Resume", lambda t=t: self._timer_act(t.id, "pause" if t.running else "resume")))
                acts.append(("Add 1 minute", lambda t=t: self._timer_act(t.id, "add", 60)))
                acts.append(("Cancel timer", lambda t=t: self._timer_act(t.id, "cancel")))
            for word, fn in acts:
                btn = Gtk.Button(label=word)
                btn.add_css_class("flat")
                btn.connect("clicked", lambda _b, fn=fn, pop=pop: (pop.popdown(), fn()))
                menu.append(btn)
            pop.set_child(menu)
            b.set_popover(pop)
            self._label(b, f"{t.label}, {self._timer_words(t)}")
            self.timer_strip.append(b)

    def _timer_words(self, t) -> str:
        return f"{t.label} · " + ("done" if t.rang else T.clock(t.remaining()))

    def _update_timer_labels(self):
        for tid, (lab, b) in list(getattr(self, "_timer_labels", {}).items()):
            t = self.timers.get(tid)
            if t is not None:
                lab.set_text(self._timer_words(t))

    def _timer_act(self, tid, what, arg=None):
        if what == "pause":
            self.timers.pause(tid)
        elif what == "resume":
            self.timers.resume(tid)
        elif what == "add":
            self.timers.add(tid, arg)
            if not self.timers.ringing:
                self.chime.stop()
            if not self._tick_id:
                self._tick_id = GLib.timeout_add(500, self._tick)
        elif what == "cancel":
            self.timers.cancel(tid)
        self.show_bar(force=False)

    # ================================================================ shopping
    def add_to_list(self, rid):
        r = self.store.recipe(rid)
        if not r:
            return
        n = 0
        factor, system = self.factor(r), self.units()
        for line in r["ingredients"]:
            if line.startswith("## "):
                continue
            ing = ING.parse(line)
            text = ING.scaled(ing, factor, system)
            self.store.shop_add(text, ING.aisle_for(ing.name or line), r["title"])
            n += 1
        self.refresh_sidebar()
        self.toast(f"Added {n} item" + ("" if n == 1 else "s") + " to the shopping list", "added",
                   action=("Open", lambda: self.go("shopping")))

    def _page_shopping(self, place):
        items = self.store.shopping()
        if not items:
            return EmptyState("Your shopping list is empty", "Add a recipe's ingredients from the recipe, or the whole "
                              "week's plan at once.", "lumaui-list-checks-symbolic",
                              primary=("Plan the week", lambda: self.go("plan")),
                              secondary=("Add an item", lambda: self.center.grow("shop-add", self._shop_add_panel)))
        content = []
        groups: dict[str, list] = {}
        for it in items:
            groups.setdefault("Done" if it["checked"] else it["aisle"], []).append(it)
        order = [a for a in ING.AISLE_ORDER if a in groups] + (["Done"] if "Done" in groups else [])
        for aisle in order:
            content.append(heading(aisle))
            rows = []
            for it in groups[aisle]:
                rows.append(CheckRow(it["text"], checked=bool(it["checked"]), detail=it["recipes"],
                                     on_toggle=lambda on, it=it: self._shop_toggle(it["id"], on),
                                     on_remove=lambda it=it: self._shop_remove(it)))
            content.append(plain_list(rows))
        left = sum(1 for i in items if not i["checked"])
        return self.page(place, content, meta=[f"{left} to get"] + ([f"{len(items) - left} done"] if len(items) - left else []))

    def _shop_toggle(self, sid, on):
        self.store.shop_check(sid, on)
        GLib.timeout_add(350, lambda: (self._changed() if self.trail.current.view == "shopping" else None, False)[1])

    def _shop_remove(self, it):
        self.store.shop_remove(it["id"])
        self._changed()
        self.toast(f"Removed {it['text']}", "deleted", undo=lambda: (self.store.shop_restore([it]), self._changed()))

    def _shop_add_panel(self):
        def add(_t=None):
            text = field.text.strip()
            if not text:
                return
            self.store.shop_add(text, ING.aisle_for(ING.parse(text).name or text))
            field.entry.set_text("")
            self.refresh_sidebar()
            if self.trail.current.view == "shopping":
                self._show(self.trail.current)
                GLib.idle_add(lambda: (self.center.grow("shop-add", self._shop_add_panel), False)[1])
        field = TextField("Item", placeholder="2 lemons", size="panel", on_activate=add)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8, margin_start=4, margin_end=4)
        box.append(PanelHeading("Add to the list"))
        box.append(field)
        box.append(TypeLabel("It goes under its aisle by itself. Press Enter to add another.", role="caption", wrap=True))
        box.append(TextButton("Add", icon="plus", style="fill", on_click=add))
        GLib.idle_add(lambda: (field.entry.grab_focus(), False)[1])
        return box

    def clear_checked(self):
        rows = self.store.shop_clear(checked_only=True)
        self._changed()
        if rows:
            self.toast(f"Cleared {len(rows)} item" + ("" if len(rows) == 1 else "s"), "deleted",
                       undo=lambda: (self.store.shop_restore(rows), self._changed()))

    def shop_week(self):
        if not self.need_plus("Shopping for the week"):
            return
        first = self.week if self.trail.current.view == "plan" else monday(dt.date.today())
        plan = self.store.plan(first.isoformat(), (first + dt.timedelta(days=6)).isoformat())
        lines = []
        for day, recipes in plan.items():
            for r in recipes:
                want = r.get("planned") or r["servings"]
                factor = Fraction(want, r["servings"]) if r["servings"] and want else Fraction(1)
                lines += [(i, r["title"], factor) for i in r["ingredients"] if not i.startswith("## ")]
        if not lines:
            self.toast("Nothing planned that week yet", "done")
            return
        items = ING.combine(lines)
        system = self.store.get("units") or "us"
        for it in items:
            self.store.shop_add(it.words("metric" if system == "metric" else "us"), ING.aisle_for(it.name),
                                ", ".join(it.recipes[:3]))
        self.refresh_sidebar()
        self.toast(f"Added {len(items)} items for {len({r for _l, r, _f in lines})} recipes", "added",
                   action=("Open", lambda: self.go("shopping")))

    # ================================================================ the plan
    def move_week(self, d):
        self.week = monday(dt.date.today()) if d == 0 else self.week + dt.timedelta(days=7 * d)
        self._show(self.trail.current)

    def _page_plan(self, place):
        if not self.plus:
            return self._locked("calendar-days", "Plan the week with Simmer Plus",
                                "Put recipes on days, see the week at a glance, and turn it into one shopping list.")
        first, today = self.week, dt.date.today()
        plan = self.store.plan(first.isoformat(), (first + dt.timedelta(days=6)).isoformat())
        content = []
        for k in range(7):
            day = first + dt.timedelta(days=k)
            name = "Today" if day == today else ("Tomorrow" if day == today + dt.timedelta(days=1) else day.strftime("%A"))
            content.append(heading(f"{name} · {day.strftime('%B %-d')}",
                                   action=("Add", lambda d=day: self.center.grow("plan-day", lambda: self._plan_day_panel(d)))))
            rows = []
            for r in plan.get(day.isoformat(), []):
                art = Art(r["title"], 44, radius=10, width=58)
                self.picture_for(r["image"], art)
                rm = Gtk.Button(icon_name=icons.icon_name("x"), valign=Gtk.Align.CENTER)
                rm.add_css_class("flat")
                self._label(rm, f"Take {r['title']} off {name}")
                rm.connect("clicked", lambda _b, d=day, r=r: self._unplan(d, r))
                rows.append(ListRow(r["title"], recipe_meta(r), lead=art, trail=rm, on_open=lambda r=r: self.open_recipe(r["id"])))
            if rows:
                content.append(plain_list(rows))
            else:
                content.append(caption("Nothing planned"))
        label = f"Week of {first.strftime('%B %-d')}"
        return self.page(place, content, meta=[label])

    def _plan_day_panel(self, day: dt.date):
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        box.append(PanelHeading(day.strftime("Plan %A")))
        recipes = self.store.recipes(order="cooked")
        if not recipes:
            box.append(PanelRow("Add a recipe first", icon="plus", on_activate=self.ask_add))
            return box
        rows = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        for r in recipes[:60]:
            rows.append(PanelRow(r["title"], icon="utensils", subtitle=recipe_meta(r) or None,
                                 on_activate=lambda r=r: self._plan(day, r)))
        box.append(Gtk.ScrolledWindow(child=rows, max_content_height=380, propagate_natural_height=True,
                                      hscrollbar_policy=Gtk.PolicyType.NEVER))
        return box

    def _plan_panel(self, rid):
        if not self.plus:
            GLib.idle_add(lambda: (self.center.fold(), self.need_plus("The meal planner"), False)[2])
            return Gtk.Box()
        r = self.store.recipe(rid)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        box.append(PanelHeading("Plan it for…"))
        today = dt.date.today()
        for k in range(10):
            day = today + dt.timedelta(days=k)
            name = "Today" if k == 0 else ("Tomorrow" if k == 1 else day.strftime("%A, %B %-d"))
            box.append(PanelRow(name, icon="calendar", on_activate=lambda d=day: self._plan(d, r)))
        return box

    def _plan(self, day, r):
        self.store.plan_add(day.isoformat(), r["id"], self.scale.get(r["id"]) or 0)
        self.refresh_sidebar()
        self.toast(f"{r['title']} planned for {day.strftime('%A')}", "added")
        if self.trail.current.view == "plan":
            self._show(self.trail.current)

    def _unplan(self, day, r):
        self.store.plan_remove(day.isoformat(), r["id"])
        self._changed()
        self.toast(f"Took {r['title']} off {day.strftime('%A')}", "deleted",
                   undo=lambda: (self.store.plan_add(day.isoformat(), r["id"], r.get("planned") or 0), self._changed()))

    def _locked(self, icon, title, text):
        return EmptyState(title, text, f"lumaui-{icon}-symbolic", primary=(f"Get {PLUS}", self.open_plus))

    # ================================================================ cooking log
    def _page_cooked(self, place):
        if not self.plus:
            return self._locked("history", "Your cooking log comes with Simmer Plus",
                                "What you cooked and when, your most-made dishes, and the notes you left yourself. "
                                "It's kept for everyone, so it's all there the day you upgrade.")
        log = self.store.log(limit=300)
        if not log:
            return EmptyState("Nothing cooked yet", "Finish a recipe in cook mode and it's logged here.",
                              "lumaui-history-symbolic", primary=("Recipes", lambda: self.go("recipes")))
        month = time.strftime("%Y-%m")
        this_month = [e for e in log if time.strftime("%Y-%m", time.localtime(e["at"])) == month]
        counts: dict = {}
        for e in log:
            counts.setdefault(e["recipe"], [0, e["title"], e["image"]])[0] += 1
        tiles = Gtk.FlowBox(selection_mode=Gtk.SelectionMode.NONE, min_children_per_line=1, max_children_per_line=3,
                            column_spacing=10, row_spacing=10, homogeneous=True)
        for value, label in ((str(len(this_month)), "Cooked this month"), (str(len(log)), "Cooked in all"),
                             (str(len(counts)), "Different recipes")):
            card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
            card.add_css_class("simmer-stat")
            v = Gtk.Label(label=value, xalign=0)
            v.add_css_class("simmer-stat-value")
            card.append(v)
            card.append(TypeLabel(label, role="caption"))
            tiles.append(card)
        content = [tiles, heading("Most cooked")]
        top = sorted(counts.items(), key=lambda kv: -kv[1][0])[:5]
        rows = []
        for rid, (n, title, image) in top:
            art = Art(title, 44, radius=10, width=58)
            self.picture_for(image, art)
            rows.append(ListRow(title, f"{n} time" + ("" if n == 1 else "s"), lead=art, on_open=lambda rid=rid: self.open_recipe(rid)))
        content.append(plain_list(rows))
        content.append(heading("Recently"))
        rows = []
        for e in log[:40]:
            when = time.strftime("%a %b %-d", time.localtime(e["at"]))
            rows.append(ListRow(e["title"], when + (f" · {e['note']}" if e["note"] else ""),
                                on_open=lambda e=e: self.open_recipe(e["recipe"])))
        content.append(plain_list(rows))
        return self.page(place, content, meta=[f"{len(log)} meals"])

    # ================================================================ community
    def refresh_community(self):
        self.community_cache = {}
        self._show(self.trail.current)

    def _page_community(self, place):
        q = getattr(self, "community_query", "")
        modes = chip_row([chip("Newest", on_click=lambda: self._community_sort("new"), active=self.community_sort == "new"),
                          chip("Most saved", on_click=lambda: self._community_sort("popular"),
                               active=self.community_sort == "popular")] +
                         ([chip(f"“{q}” ×", on_click=self._clear_community_query, active=True)] if q else []))
        self._community_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        key = (q, self.community_sort)
        cached = self.community_cache.get(key)
        if cached is not None:
            self._fill_community(cached)
        else:
            spin = Gtk.Spinner(spinning=True, halign=Gtk.Align.CENTER, margin_top=60)
            spin.set_size_request(32, 32)
            self._community_box.append(spin)
            sort = self.community_sort
            self.run(lambda: self.community.browse(q=q, sort=sort), lambda doc: self._community_loaded(key, doc),
                     self._community_failed)
        about = caption("Recipes here are shared by Simmer cooks. Every one is checked automatically before it appears "
                        "(text only, no links), and anything people report is reviewed out of view.", wrap=True)
        about.set_margin_top(28)
        return self.page(place, [modes, self._community_box, about], meta=["Shared by Simmer cooks"])

    def _community_sort(self, s):
        self.community_sort = s
        self._show(self.trail.current)

    def _clear_community_query(self):
        self.community_query = ""
        self._show(self.trail.current)

    def _community_loaded(self, key, doc):
        self.community_cache[key] = doc.get("recipes") or []
        if self.trail.current.view == "community":
            box = getattr(self, "_community_box", None)
            if box is not None:
                self._clear(box)
                self._fill_community(self.community_cache[key])

    def _community_failed(self, msg):
        box = getattr(self, "_community_box", None)
        if box is None:
            return
        self._clear(box)
        box.append(EmptyState("The community isn't reachable", msg, "lumaui-wifi-low-symbolic",
                              primary=("Try again", self.refresh_community), compact=True))

    @staticmethod
    def _clear(box):
        c = box.get_first_child()
        while c is not None:
            box.remove(c)
            c = box.get_first_child()

    def _fill_community(self, recipes):
        if not recipes:
            self._community_box.append(EmptyState("No recipes here yet", "Be the first: share one of your own.",
                                                  "lumaui-users-symbolic", compact=True,
                                                  primary=("Share a recipe", lambda: self.center.grow("share-pick", self._share_pick_panel))))
            return
        rows = []
        for r in recipes:
            art = Art(r.get("title") or "", 48, radius=10, width=64)
            bits = []
            if r.get("author"):
                bits.append(f"by {r['author']}")
            meta = recipe_meta(r)
            if meta:
                bits.append(meta)
            if r.get("saves"):
                bits.append(f"saved {r['saves']}×")
            mine = self.community.is_mine(r["id"])
            if mine:
                bits.insert(0, "Yours")
            rows.append(ListRow(r.get("title") or "Recipe", " · ".join(bits), lead=art,
                                on_open=lambda r=r: self.open_page("shared", r.get("title") or "Recipe", r["id"])))
        self._community_box.append(plain_list(rows))

    def _page_shared(self, place):
        cid = place.subject
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        cached = self.community_cache.get(("full", cid))
        if cached is not None:
            return self._shared_view(place, cached)
        spin = Gtk.Spinner(spinning=True, halign=Gtk.Align.CENTER, margin_top=80)
        spin.set_size_request(32, 32)
        box.append(spin)

        def got(rec):
            self.community_cache[("full", cid)] = rec
            if self.trail.current.view == "shared" and self.trail.current.subject == cid:
                self._show(self.trail.current)

        def fail(msg):
            self._clear(box)
            box.append(EmptyState("Couldn't open this recipe", msg, "lumaui-circle-alert-symbolic",
                                  primary=("Back", lambda: self.trail.back())))
        self.run(lambda: self.community.get(cid), got, fail)
        return self.page(place, [box], header="back")

    def _shared_view(self, place, rec):
        content = []
        title = Gtk.Label(label=rec["title"], xalign=0, wrap=True, selectable=True)
        title.add_css_class("simmer-title")
        content.append(title)
        bits = []
        if rec.get("author"):
            bits.append(f"Shared by {rec['author']}")
        if rec.get("created"):
            bits.append(time.strftime("%B %-d, %Y", time.localtime(rec["created"] / 1000)))
        meta = recipe_meta(rec)
        if meta:
            bits.append(meta)
        content.append(caption(" · ".join(bits)))
        if rec.get("tags"):
            content.append(chip_row([chip(t, icon="tag") for t in rec["tags"]]))
        if self.community.is_mine(rec["id"]):
            content.append(TextButton("Take it down", icon="trash-2", style="danger",
                                      on_click=lambda: self.unshare(rec["id"])))
        content.append(heading("Ingredients"))
        content.append(plain_list([CheckRow(i) for i in rec.get("ingredients") or [] if not i.startswith("## ")]))
        content.append(heading("Method"))
        for n, s in enumerate([s for s in rec.get("steps") or [] if not s.startswith("## ")], 1):
            content.append(self._step_row(n, s, rec))
        if rec.get("notes"):
            content.append(heading("Notes"))
            content.append(Gtk.Label(label=rec["notes"], xalign=0, wrap=True, selectable=True))
        content.append(caption("Checked automatically when it was shared. If something's wrong with it, use Report.",
                               wrap=True))
        return self.page(place, content, header="back", wide=820)

    def save_shared(self, cid):
        rec = self.community_cache.get(("full", cid))
        if not rec:
            return
        existing = [r for r in self.store.recipes() if r.get("community_id") == cid]
        if existing:
            self.open_recipe(existing[0]["id"])
            return
        rid = self.store.add({**rec, "community_id": cid, "source": ""})
        self.run(lambda: self.community.save(cid))
        self.refresh_sidebar()
        self.toast(f"Saved {rec['title']} to your recipes", "added", action=("Open", lambda: self.open_recipe(rid)))

    def _report_panel(self, cid):
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        box.append(PanelHeading("What's wrong with it?"))
        for key, words in REPORT_REASONS:
            box.append(PanelRow(words, icon="flag", on_activate=lambda k=key: self.report(cid, k)))
        box.append(TypeLabel("Reports are anonymous. A recipe several people report is hidden right away.",
                             role="caption", wrap=True))
        return box

    def report(self, cid, reason):
        def done(_r):
            self.community_cache = {}
            self.toast("Thanks. The recipe was reported.", "done")
            if self.trail.can_go_back:
                self.trail.back()
        self.run(lambda: self.community.report(cid, reason), done)

    def _share_pick_panel(self):
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        box.append(PanelHeading("Share one of your recipes"))
        mine = [r for r in self.store.recipes() if shareable(r)[0]]
        if not mine:
            box.append(TypeLabel("Only recipes you wrote yourself can be shared. Imported ones belong to their site. "
                                 "Write one, then share it from its page.", role="caption", wrap=True))
            box.append(PanelRow("Write a recipe", icon="square-pen", on_activate=lambda: self.edit(None)))
            return box
        rows = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        for r in mine[:50]:
            rows.append(PanelRow(r["title"], icon="utensils", subtitle=recipe_meta(r) or None,
                                 on_activate=lambda r=r: GLib.idle_add(lambda: (self.ask_share(r["id"]), False)[1])))
        box.append(Gtk.ScrolledWindow(child=rows, max_content_height=380, propagate_natural_height=True,
                                      hscrollbar_policy=Gtk.PolicyType.NEVER))
        return box

    def ask_share(self, rid):
        r = self.store.recipe(rid)
        if not r:
            return
        ok, why = shareable(r)
        if r.get("shared_id") and r["shared_id"] in self.community.mine():
            ok, why = False, "You've shared this recipe already. Take it down from the Community page first to share it again."
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10, margin_start=4, margin_end=4)
        box.append(PanelHeading("Share to the community"))
        if not ok:
            box.append(TypeLabel(why, role="caption", wrap=True))
            self.center.fold()
            self.center.grow("share", box)
            return
        name = TextField("Your name on it (optional)", value=self.community.name, placeholder="Sam from Leeds", size="panel")
        box.append(name)
        box.append(TypeLabel("Everyone using Simmer will be able to see and save it. It's shared as text: the name, "
                             "ingredients, steps, notes and tags (no picture, and links are removed). It's checked "
                             "automatically first, and you can take it down at any time.", role="caption", wrap=True))
        box.append(TextButton(f"Share {r['title']}", icon="send", style="fill",
                              on_click=lambda: self.share(rid, name.text)))
        self.center.fold()
        self.center.grow("share", box, wide=True)

    def share(self, rid, author):
        r = self.store.recipe(rid)
        if not r:
            return
        self.center.fold()
        if author.strip() != self.community.name:
            self.community.set_name(author)
        self.toast("Checking and sharing…", "done")

        def done(doc):
            self.store.update(rid, shared_id=doc["id"])
            self.community_cache = {}
            self.toast(f"Shared {r['title']} with the community", "done",
                       action=("View", lambda: self.open_page("shared", r["title"], doc["id"])))
        self.run(lambda: self.community.share(r, author.strip()), done, lambda m: self.toast(m, "error"))

    def unshare(self, cid):
        def done(_r):
            self.community_cache = {}
            for r in self.store.recipes():
                if r.get("shared_id") == cid:
                    self.store.update(r["id"], shared_id="")
            self.toast("Taken down from the community", "deleted")
            if self.trail.can_go_back:
                self.trail.back()
        self.run(lambda: self.community.unshare(cid), done)

    def ask_name(self):
        def save(_t=None):
            self.community.set_name(field.text)
            self.center.fold()
            self.toast("Saved", "saved")
        field = TextField("Name on recipes you share", value=self.community.name, placeholder="Sam from Leeds",
                          size="panel", on_activate=save)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8, margin_start=4, margin_end=4)
        box.append(PanelHeading("Your community name"))
        box.append(field)
        box.append(TypeLabel("Optional. Shown on recipes you share; leave it empty to share without a name.",
                             role="caption", wrap=True))
        box.append(TextButton("Save", style="fill", on_click=save))
        self.center.fold()
        self.center.grow("name", box)

    def _mine_panel_open(self):
        mine = self.community.mine()
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        box.append(PanelHeading("Recipes you've shared"))
        for cid, title in mine.items():
            box.append(PanelRow(title or "Recipe", icon="users", on_activate=lambda c=cid, t=title: self.open_page("shared", t, c)))
        self.center.fold()
        GLib.idle_add(lambda: (self.center.grow("mine", box), False)[1])

    # ================================================================ backup
    def export_backup(self):
        data = json.dumps(self.store.export(), indent=1).encode()
        n = self.store.counts()["recipes"]

        def done(files):
            if not files:
                return
            f = files[0] if isinstance(files, (list, tuple)) else files
            try:
                f.replace_contents(data, None, False, Gio.FileCreateFlags.REPLACE_DESTINATION, None)
            except GLib.Error as ex:
                self.toast(f"Couldn't save: {ex.message}", "error")
                return
            self.toast(f"Backed up {n} recipe" + ("" if n == 1 else "s"), "saved")
        ask_for_file(self, FileRequest("Back up recipes", mode="save",
                                       name=f"simmer-recipes-{time.strftime('%Y-%m-%d')}.json"), done)

    def import_backup(self):
        def picked(files):
            if not files:
                return
            f = files[0] if isinstance(files, (list, tuple)) else files
            try:
                _ok, data, _etag = f.load_contents(None)
                n = self.store.import_(json.loads(data.decode("utf-8")))
            except (GLib.Error, ValueError) as ex:
                self.toast(f"Couldn't read that backup: {getattr(ex, 'message', ex)}", "error")
                return
            self.toast(f"Restored {n} recipe" + ("" if n == 1 else "s") if n else "Every recipe in that backup is already here",
                       "added" if n else "done")
            self._changed()
        ask_for_file(self, FileRequest("Restore recipes from a Simmer backup", start="downloads"), picked)

    # ================================================================ closing
    def _closing(self, *_):
        if self.timers.active:
            # a timer is running: keep it running with the window hidden, so it still rings
            self.set_visible(False)
            return True
        self._keep_awake(False)
        self.chime.stop()
        self.pool.shutdown(wait=False, cancel_futures=True)
        return False


class App(Adw.Application):
    def __init__(self):
        super().__init__(application_id=APP_ID, flags=Gio.ApplicationFlags.DEFAULT_FLAGS)
        self.window = None
        GLib.set_application_name(APP_NAME)

    def do_activate(self):
        if self.window is None:
            self.window = Window(self)
        self.window.set_visible(True)
        self.window.present()


def main() -> int:
    return App().run(sys.argv)
