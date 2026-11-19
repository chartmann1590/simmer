# SPDX-License-Identifier: GPL-3.0-or-later
"""Development only: run Simmer at a size and walk it through steps, for screenshots and smoke tests.

    W=1280 H=800 STEPS="w.go('discover')|wait 4|..." python3 tests/devshot.py
Each step is Python with `w` (the window) and `app`, or "wait N" seconds. Not installed with the app.
"""
import os
import sys
import traceback
import faulthandler

if os.environ.get("DUMP_AFTER"):
    faulthandler.dump_traceback_later(int(os.environ["DUMP_AFTER"]), repeat=True)

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
from gi.repository import GLib  # noqa: E402

from simmer import app as A  # noqa: E402

W, H = int(os.environ.get("W", 1280)), int(os.environ.get("H", 800))
steps = [s.strip() for s in os.environ.get("STEPS", "").split("|") if s.strip()]


class DevApp(A.App):
    def do_activate(self):
        if self.window is None:
            self.window = A.Window(self)
            self.window.set_default_size(W, H)
            if os.environ.get("MAXIMIZE"):
                self.window.maximize()
            run_steps(self.window)
        self.window.present()




def run_steps(w):
    def shot(name):
        """Render the window in-process to shots/<name>.png."""
        from gi.repository import Gtk
        paintable = Gtk.WidgetPaintable.new(w)
        width, height = w.get_width(), w.get_height()
        snap = Gtk.Snapshot()
        paintable.snapshot(snap, width, height)
        node = snap.to_node()
        if node is None:
            print("SHOT EMPTY", name, flush=True)
            return
        tex = w.get_renderer().render_texture(node, None)
        os.makedirs("shots", exist_ok=True)
        tex.save_to_png(os.path.join("shots", name + ".png"))
        print("SHOT", name, width, height, flush=True)

    scope = {"w": w, "app": application, "A": A, "GLib": GLib, "shot": shot}

    def nxt():
        if not steps:
            print("STEPS DONE", flush=True)
            return False
        s = steps.pop(0)
        if s.startswith("wait "):
            GLib.timeout_add(int(float(s[5:]) * 1000), nxt)
            return False
        try:
            exec(s, scope)
        except Exception:
            traceback.print_exc()
        GLib.timeout_add(200, nxt)
        return False
    GLib.timeout_add(800, nxt)


application = DevApp()
sys.exit(application.run([]))
