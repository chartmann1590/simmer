# SPDX-License-Identifier: GPL-3.0-or-later
"""Telling people about a new version, showing what's new, and getting it onto the device.

GitHub builds: Download and install fetches this device's bundle, verifies it, and opens it with the system's
installer, which shows what changes and asks the person to confirm. Depot builds: Update asks Luma's own updater
(Flatpak's update portal, the same one behind "App updates" in the app menu) to install what Depot has. Either way
the app keeps running on its current code until it is reopened, so nothing open is lost.
"""
from __future__ import annotations

import threading
import time

import gi

gi.require_version("Gtk", "4.0")
from gi.repository import Gio, GLib, Gtk, Pango  # noqa: E402

from luma_appkit import PanelHeading, PanelRow, ProgressLine, TypeLabel  # noqa: E402

from .hsupdates import UpdateError, notes_markup  # noqa: E402


def _ui(fn, *args):
    GLib.idle_add(lambda: (fn(*args), False)[1], priority=GLib.PRIORITY_DEFAULT)


def _bg(work, done, fail):
    def run():
        try:
            result = work()
        except UpdateError as ex:
            _ui(fail, str(ex))
            return
        except Exception as ex:
            _ui(fail, f"Something went wrong: {ex}")
            return
        _ui(done, result)
    threading.Thread(target=run, daemon=True).start()


def _when(iso: str) -> str:
    try:
        t = time.mktime(time.strptime(iso[:10], "%Y-%m-%d"))
        return time.strftime("%B %-d, %Y", time.localtime(t))
    except (ValueError, TypeError):
        return ""


class Updater:
    """`window` is the AppWindow (its `_app_updates` is Luma's portal updater when installed as a Flatpak);
    `grow(key, widget)` opens a bar panel; `toast(text, kind, action=None)` says what happened."""

    KEY = "updates"

    def __init__(self, window, updates, name: str, *, grow, fold, toast):
        self.window, self.updates, self.name = window, updates, name
        self.grow, self.fold, self.toast = grow, fold, toast
        self.release = None
        self.box: Gtk.Box | None = None
        self.downloading = None          # threading.Event while a download runs
        self.fraction = 0.0
        self.downloaded = ""             # verified bundle path
        self.error = ""
        self.checking = False

    # ---------------------------------------------------------------- checking
    def start(self):
        """Quietly check if it's time; announce a newer version once."""
        if self.updates.due():
            self.check(manual=False)
        return False

    def check(self, manual: bool = True):
        if self.checking:
            return
        self.checking = True

        def done(release):
            self.checking = False
            if release is None:
                if manual:
                    self.toast(f"{self.name} {self.updates.current} is the latest version", "done")
                return
            self.release = release
            if manual:
                self.open()
            elif not self.updates.skipped(release.version):
                self.toast(f"{self.name} {release.version} is available", "notified",
                           action=("What's new", self.open))

        def fail(msg):
            self.checking = False
            if manual:
                self.toast(msg, "error")
        _bg(self.updates.check, done, fail)

    # ---------------------------------------------------------------- the panel
    def open(self):
        if self.release is None:
            self.check(manual=True)
            return
        self.grow(self.KEY, self.panel)

    def panel(self) -> Gtk.Widget:
        self.box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        self.box.add_css_class("hs-updates")
        self._fill()
        return self.box

    def _clear(self):
        child = self.box.get_first_child()
        while child is not None:
            self.box.remove(child)
            child = self.box.get_first_child()

    def _fill(self):
        if self.box is None or self.release is None:
            return
        self._clear()
        r, b = self.release, self.box
        b.append(PanelHeading(f"{self.name} {r.version} is available"))
        when = _when(r.get("published", ""))
        b.append(self._words(f"You have {self.updates.current}." + (f" Released {when}." if when else "")))
        notes = notes_markup(r.get("notes", ""))
        if notes:
            label = Gtk.Label(xalign=0, wrap=True, wrap_mode=Pango.WrapMode.WORD_CHAR, selectable=True,
                              margin_start=6, margin_end=6, margin_top=4, margin_bottom=4)
            try:
                label.set_markup(notes)
            except Exception:
                label.set_text(r.get("notes", ""))
            label.add_css_class("hs-release-notes")
            label.update_property([Gtk.AccessibleProperty.LABEL], [f"What's new in {r.version}"])
            label.connect("activate-link", lambda _l, uri: (Gtk.UriLauncher.new(uri).launch(self.window, None, None, None), True)[1])
            scroller = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER, child=label,
                                          propagate_natural_height=True, max_content_height=260)
            b.append(scroller)
        if self.updates.channel == "github":
            self._github_part()
        else:
            self._depot_part()
        if r.get("url"):
            b.append(PanelRow("See the release on GitHub", icon="globe", closes=False,
                              on_activate=lambda: Gtk.UriLauncher.new(r["url"]).launch(self.window, None, None, None)))
        b.append(PanelRow("Skip this version", icon="x", closes=True, on_activate=self._skip))
        b.append(PanelRow("Check for updates automatically", icon="refresh-cw", toggle=self.updates.automatic,
                          on_toggle=self.updates.set_automatic))

    def _github_part(self):
        b, r = self.box, self.release
        found = self.updates.bundle(r)
        if self.downloaded:
            b.append(self._words("Downloaded and checked. The installer shows what changes and asks you to confirm; "
                                 f"reopen {self.name} afterwards."))
            b.append(PanelRow("Open the installer", icon="download", closes=False, on_activate=self._install))
        elif self.downloading is not None:
            b.append(self._words(f"Downloading {self.name} {r.version}…"))
            self.line = ProgressLine(self.fraction, label="Download")
            self.line.set_margin_start(6)
            self.line.set_margin_end(6)
            b.append(self.line)
            b.append(PanelRow("Cancel download", icon="x", closes=False, on_activate=self._cancel))
        elif not found:
            b.append(self._words(f"This release has no download for this device ({self.updates.arch})."))
        else:
            size = GLib.format_size(found[1]) if found[1] else ""
            if self.error:
                b.append(self._words(self.error))
            b.append(PanelRow("Download and install", icon="download", detail=size, closes=False,
                              on_activate=self._download))

    def _depot_part(self):
        b = self.box
        portal = getattr(self.window, "_app_updates", None)
        state = getattr(portal, "state", "")
        if state in ("available", "failed"):
            b.append(PanelRow("Update", icon="download", subtitle="From Depot. Reopen the app afterwards.",
                              closes=True, on_activate=self._portal_install))
        elif state == "installing":
            b.append(self._words("Installing the update from Depot…"))
        elif state == "ready":
            b.append(self._words(f"The update is installed. Reopen {self.name} to use it."))
        else:
            b.append(self._words("Depot delivers updates to this copy once it has published them. When it has, "
                                 "Update appears here and in App updates in the app menu."))

    @staticmethod
    def _words(text: str) -> Gtk.Widget:
        t = TypeLabel(text, role="caption", wrap=True)
        for side in ("start", "end"):
            getattr(t, f"set_margin_{side}")(6)
        t.set_margin_top(4)
        t.set_margin_bottom(4)
        return t

    # ---------------------------------------------------------------- actions
    def _skip(self):
        if self.release is not None:
            self.updates.skip(self.release.version)
            self.toast(f"You won't be told about {self.release.version} again", "done")

    def _download(self):
        r = self.release
        self.downloading = threading.Event()
        self.fraction, self.error = 0.0, ""
        cancel = self.downloading
        self._fill()

        def progress(done, total):
            if total:
                f = min(1.0, done / total)
                if f - self.fraction >= 0.02 or f >= 1:
                    self.fraction = f
                    _ui(self._progress, f)

        def done(path):
            self.downloading = None
            if path:
                self.downloaded = path
                self._fill()
                self._install()
            else:
                self._fill()

        def fail(msg):
            self.downloading, self.error = None, msg
            self._fill()
            self.toast(msg, "error")
        _bg(lambda: self.updates.download(r, progress, cancel), done, fail)

    def _progress(self, f):
        line = getattr(self, "line", None)
        if line is not None:
            line.set_fraction(f)

    def _cancel(self):
        if self.downloading is not None:
            self.downloading.set()

    def _install(self):
        """Hand the verified bundle to the system's installer. It, not the app, does the installing."""
        path = self.downloaded
        if not path:
            return
        launcher = Gtk.FileLauncher.new(Gio.File.new_for_path(path))

        def launched(source, result):
            try:
                source.launch_finish(result)
                self.toast(f"Confirm in the installer, then reopen {self.name}", "opening")
            except GLib.Error as ex:
                self.toast(f"Couldn't open the installer: {ex.message}", "error")
        launcher.launch(self.window, None, launched)

    def _portal_install(self):
        portal = getattr(self.window, "_app_updates", None)
        if portal is not None and portal.install():
            self.toast("Updating from Depot…", "done")
        else:
            self.toast("Open App updates in the app menu to update.", "warning")
