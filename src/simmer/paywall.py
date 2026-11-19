# SPDX-License-Identifier: GPL-3.0-or-later
"""The upgrade panel: what the extras are, what they cost (from the store, never hard-coded), and buying,
restoring or managing them. It grows out of the app's bar (LumaUI ActionCenter), like every other panel.

Honest by design (Luma review 3.4 and 3.6): prices and trial lengths come from the store; nothing is
pre-selected or counted down; cancelling is one tap to Stripe's own page; the panel says, before anything
is sent, that checkout is run by Stripe and where the email goes.
"""
from __future__ import annotations

import threading

import gi

gi.require_version("Gtk", "4.0")
from gi.repository import GLib, Gtk, Pango  # noqa: E402

from luma_appkit import PanelHeading, PanelRow, TextButton, TypeLabel, icons  # noqa: E402

from .hsbilling import BillingError, price_words  # noqa: E402


def _ui(fn, *args):
    # default priority, not idle: a spinning spinner redraws constantly and would starve idle callbacks
    GLib.idle_add(lambda: (fn(*args), False)[1], priority=GLib.PRIORITY_DEFAULT)


def _bg(work, done, fail):
    def run():
        try:
            result = work()
        except BillingError as ex:
            _ui(fail, str(ex))
            return
        except Exception as ex:  # never leave the panel spinning
            _ui(fail, f"Something went wrong: {ex}")
            return
        _ui(done, result)
    threading.Thread(target=run, daemon=True).start()


def badge(word: str = "Plus") -> Gtk.Widget:
    """A small pill that marks an extra in a menu, a heading or a settings row."""
    label = Gtk.Label(label=word, valign=Gtk.Align.CENTER)
    label.add_css_class("hs-badge")
    return label


class Paywall:
    """`billing` is an hsbilling.Billing; `name` is the extras' name ("Simmer Plus"); `features` is
    [(icon, title, detail)]; `open_uri(url)` opens a web page; `toast(text, kind)` says what happened;
    `changed()` runs when what you own changes."""

    def __init__(self, window, billing, name: str, features, *, open_uri, toast, changed):
        self.window, self.billing, self.name, self.features = window, billing, name, features
        self.open_uri, self.toast, self.changed = open_uri, toast, changed
        self.catalog: dict | None = None
        self.catalog_error = ""
        self.cancel = threading.Event()
        self.box: Gtk.Box | None = None
        self.email_field = None
        self.waiting_for = ""
        billing.listeners.append(lambda: _ui(self._owned_changed))

    # ---------------------------------------------------------------- the panel
    def panel(self) -> Gtk.Widget:
        self.box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        self.box.add_css_class("hs-paywall")
        self._fill()
        if self.catalog is None and not self.billing.owns():
            self._load_catalog()
        return self.box

    def _clear(self):
        child = self.box.get_first_child()
        while child is not None:
            self.box.remove(child)
            child = self.box.get_first_child()

    def _fill(self):
        if self.box is None:
            return
        self._clear()
        b = self.box
        owned = self.billing.owns()
        b.append(PanelHeading(f"You have {self.name}" if owned else self.name))
        for icon, title, detail in self.features:
            b.append(self._feature(icon, title, detail))
        if owned:
            self._owned_part()
        elif self.waiting_for:
            self._waiting_part()
        else:
            self._buy_part()

    def _owned_part(self):
        b = self.box
        product = self.billing.owned_product()
        word = "Thank you for supporting Hartmann Studios."
        if self.billing.email:
            word += f" Your purchase is tied to {self.billing.email}; use it to restore on another device."
        b.append(self._words(word))
        if ":" in product:  # a subscription, not a one-time unlock
            b.append(PanelRow("Manage or cancel your subscription", icon="settings", closes=True,
                              on_activate=self.manage))
        b.append(PanelRow("Check my purchase again", icon="refresh-cw", closes=False, on_activate=lambda: self.restore()))

    def _buy_part(self):
        b = self.box
        from luma_appkit import TextField
        self.email_field = TextField("Email for your receipt", value=self.billing.email, placeholder="you@example.com",
                                     purpose="email", size="panel")
        if self.catalog is None and not self.catalog_error:
            spin = Gtk.Spinner(spinning=True, halign=Gtk.Align.CENTER, margin_top=10, margin_bottom=10)
            b.append(spin)
        elif self.catalog_error:
            b.append(self._words(self.catalog_error))
            b.append(PanelRow("Try again", icon="refresh-cw", closes=False, on_activate=self._retry_catalog))
        else:
            b.append(PanelHeading("Choose"))
            promo = []
            for key in self.billing.products[::-1]:  # monthly, yearly, then once (cheapest first)
                p = self.catalog.get(key)
                if p is None:
                    continue
                words = price_words(p)
                if p.get("promo_codes"):
                    words += " · codes accepted"
                    promo.append(p["label"])
                b.append(PanelRow(p["label"], icon="sparkles", subtitle=words, closes=False,
                                  on_activate=lambda k=key: self.buy(k)))
            b.append(self.email_field)
            if promo:
                b.append(self._words(f"Have a promotion code? Choose {promo[0] if len(promo) == 1 else 'an option marked codes accepted'} "
                                     "and enter the code on Stripe's checkout page, under the price."))
            b.append(self._words("Checkout opens in your browser and is run by Stripe. Your email goes to the "
                                 "Hartmann Studios license server and to Stripe, for your receipt and license. "
                                 "Subscriptions renew until you cancel, which you can do here at any time."))
        b.append(PanelRow("Restore purchases", icon="download", closes=False, on_activate=lambda: self.restore()))

    def _waiting_part(self):
        b = self.box
        row = Gtk.Box(spacing=10, margin_top=8, margin_bottom=8, margin_start=6)
        spin = Gtk.Spinner(spinning=True)
        row.append(spin)
        row.append(self._words("Finish paying in your browser. This unlocks by itself as soon as Stripe confirms it."))
        b.append(row)
        b.append(PanelRow("Cancel", icon="x", closes=False, on_activate=self._stop_waiting))

    @staticmethod
    def _feature(icon: str, title: str, detail: str) -> Gtk.Widget:
        row = Gtk.Box(spacing=12, margin_top=6, margin_bottom=6, margin_start=6, margin_end=6,
                      accessible_role=Gtk.AccessibleRole.LIST_ITEM)
        img = icons.image(icon, pixel_size=18)
        img.set_valign(Gtk.Align.START)
        img.add_css_class("hs-feature-icon")
        row.append(img)
        words = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=1, hexpand=True)
        t = Gtk.Label(label=title, xalign=0, wrap=True)
        t.add_css_class("hs-feature-title")
        words.append(t)
        d = Gtk.Label(label=detail, xalign=0, wrap=True, wrap_mode=Pango.WrapMode.WORD_CHAR)
        d.add_css_class("dim-label")
        d.add_css_class("caption")
        words.append(d)
        row.append(words)
        row.update_property([Gtk.AccessibleProperty.LABEL], [f"{title}: {detail}"])
        return row

    @staticmethod
    def _words(text: str) -> Gtk.Widget:
        t = TypeLabel(text, role="caption", wrap=True)
        t.set_margin_start(6)
        t.set_margin_end(6)
        t.set_margin_top(6)
        t.set_margin_bottom(4)
        return t

    # ---------------------------------------------------------------- actions
    def _load_catalog(self):
        def done(cat):
            self.catalog, self.catalog_error = cat, ("" if cat else "Nothing to buy right now.")
            self._fill()

        def fail(msg):
            self.catalog_error = msg
            self._fill()
        _bg(self.billing.catalog, done, fail)

    def _retry_catalog(self):
        self.catalog, self.catalog_error = None, ""
        self._fill()
        self._load_catalog()

    def buy(self, product: str):
        email = self.email_field.text if self.email_field is not None else self.billing.email

        def opened(result):
            url, session = result
            self.open_uri(url)
            if not session:
                self.toast("Finish paying in your browser, then use Restore purchases.", "opening")
                return
            self.waiting_for = session
            self.cancel = threading.Event()
            self._fill()
            cancel = self.cancel
            _bg(lambda: self.billing.wait_for_session(session, cancel), self._waited, self._wait_failed)

        _bg(lambda: self.billing.checkout(product, email), opened, lambda m: self.toast(m, "error"))

    def _waited(self, ok: bool):
        self.waiting_for = ""
        if ok:
            self.toast(f"{self.name} is unlocked. Thank you!", "done")
        elif not self.cancel.is_set():
            self.toast("Still waiting on the payment. If you paid, use Restore purchases in a minute.", "warning")
        self._fill()

    def _wait_failed(self, msg: str):
        self.waiting_for = ""
        self.toast(msg, "error")
        self._fill()

    def _stop_waiting(self):
        self.cancel.set()
        self.waiting_for = ""
        self._fill()

    def restore(self, email: str = ""):
        email = email or (self.email_field.text if self.email_field is not None and self.email_field.text else "") \
            or self.billing.email

        def done(owned):
            self.toast(f"{self.name} restored. Thank you!" if owned else
                       f"No {self.name} purchase found for {email}.", "done" if owned else "warning")
            self._fill()
        _bg(lambda: self.billing.restore(email), done, lambda m: self.toast(m, "error"))

    def manage(self):
        _bg(self.billing.portal, self.open_uri, lambda m: self.toast(m, "error"))

    def _owned_changed(self):
        self._fill()
        self.changed()
