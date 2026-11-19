# SPDX-License-Identifier: GPL-3.0-or-later
"""Drives the real Simmer window inside the Luma runtime, offline (needs a display; run with xvfb-run).

    xvfb-run flatpak run --command=python3 org.projectluma.Sdk//44 tests/test_ui.py

The community server and GitHub are local fakes; the license server is unreachable on purpose.
"""
import json
import os
import sys
import tempfile
import threading
import time
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
TMP = tempfile.mkdtemp()
os.environ["XDG_DATA_HOME"] = os.path.join(TMP, "data")
os.environ["XDG_CACHE_HOME"] = os.path.join(TMP, "cache")
os.environ["SIMMER_AUDIO_SINK"] = "fakesink"
os.environ["HS_BILLING_URL"] = "http://127.0.0.1:1"   # no store in tests: the panel must cope
os.environ["HS_UPDATES_API"] = "http://127.0.0.1:1"   # never reach GitHub from tests

import gi  # noqa: E402

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, GLib, Gtk  # noqa: E402

RESULTS = []
SHARED = {}


class Community(BaseHTTPRequestHandler):
    """Answers like the Simmer community Worker, with the safety check faked (titles containing "spam" fail)."""

    def log_message(self, *a):
        pass

    def _send(self, code, doc):
        raw = json.dumps(doc).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _body(self):
        return json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")

    def do_GET(self):
        if self.path.startswith("/v1/recipes/"):
            r = SHARED.get(self.path.split("/")[3])
            return self._send(200, {"recipe": r}) if r else self._send(404, {"error": "Gone.", "code": "missing"})
        return self._send(200, {"recipes": [{k: v for k, v in r.items() if k not in ("steps",)} for r in SHARED.values()
                                            if not r.get("hidden")], "more": False})

    def do_POST(self):
        b = self._body()
        if self.path == "/v1/recipes":
            if "spam" in b["recipe"]["title"].lower():
                return self._send(422, {"error": "The automatic safety check didn't pass this recipe, so it wasn't shared.",
                                        "code": "unsafe"})
            rid = f"r{len(SHARED) + 1:05d}x"
            SHARED[rid] = {**b["recipe"], "id": rid, "saves": 0, "created": int(time.time() * 1000)}
            return self._send(201, {"id": rid, "key": "k" + rid})
        if self.path.endswith("/report"):
            SHARED[self.path.split("/")[3]]["hidden"] = True
        return self._send(200, {"ok": True})

    def do_DELETE(self):
        SHARED.pop(self.path.split("/")[3], None)
        self._send(200, {"ok": True})


SRV = ThreadingHTTPServer(("127.0.0.1", 0), Community)
threading.Thread(target=SRV.serve_forever, daemon=True).start()
os.environ["SIMMER_COMMUNITY_URL"] = f"http://127.0.0.1:{SRV.server_port}"
SHARED["seed00001"] = {"id": "seed00001", "title": "Community Chili", "author": "Pat", "servings": 6, "total": 90,
                       "tags": ["chili"], "saves": 12, "created": 1763500000000,
                       "ingredients": ["500 g beef", "2 cans beans", "1 onion"],
                       "steps": ["Brown the beef for 10 minutes.", "Add everything and simmer for 1 hour."]}


def pump(seconds=0.3):
    ctx = GLib.MainContext.default()
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        ctx.iteration(False)
        time.sleep(0.005)


def wait_for(cond, seconds=15):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        pump(0.05)
        if cond():
            return True
    return False


def texts(widget):
    out, stack = [], [widget]
    while stack:
        w = stack.pop()
        if isinstance(w, Gtk.Label):
            out.append(w.get_text())
        c = w.get_first_child()
        while c is not None:
            stack.append(c)
            c = c.get_next_sibling()
    return " | ".join(out)


def page_text(w):
    return texts(w.stack.get_visible_child())


def check(name, fn):
    try:
        fn()
        RESULTS.append((name, None))
    except Exception:
        RESULTS.append((name, traceback.format_exc()))


def run(w):
    def welcome_and_samples():
        assert w.store.counts()["recipes"] == 0
        assert "Your recipe box is empty" in page_text(w)
        w.add_samples()
        pump(0.3)
        assert w.store.counts()["recipes"] == 4
        assert "Weeknight Tomato Soup" in page_text(w)
        for view in ("recipes", "favorites", "plan", "shopping", "cooked"):
            w.go(view)
            pump(0.2)
            assert w.trail.current.view == view
        tag = w.store.tags()[0][0]
        w.go("tag", tag, tag)
        pump(0.2)
        assert w.trail.current.view == "tag"
        w.search("garlic")
        pump(0.2)
        assert w.trail.current.view == "search" and "Lemon Garlic Butter Pasta" in page_text(w)

    def recipe_and_scaling():
        soup = next(r for r in w.store.recipes() if r["title"] == "Weeknight Tomato Soup")
        w.open_recipe(soup["id"])
        pump(0.3)
        t = page_text(w)
        assert "800 g canned whole tomatoes" in t and "Start 20 min" in t, t
        w._set_servings(soup, 8)
        pump(0.4)
        t = page_text(w)
        assert "1600 g canned whole tomatoes" in t and "4 tbsp olive oil" in t, t
        w.toggle_favorite(soup["id"])
        pump(0.2)
        assert w.store.recipe(soup["id"])["favorite"] == 1
        w.copy_recipe(soup["id"])
        w.add_to_list(soup["id"])
        assert w.store.counts()["shopping"] == 10
        items = {r["text"]: r for r in w.store.shopping()}
        assert items["1600 g canned whole tomatoes"]["aisle"] == "Pantry"
        assert items["2 large onions, chopped"]["aisle"] == "Produce"

    def cook_mode_and_timers():
        soup = next(r for r in w.store.recipes() if r["title"] == "Weeknight Tomato Soup")
        w.cook(soup["id"])
        pump(0.3)
        assert w.trail.current.view == "cook" and "Step 1 of 5" in page_text(w)
        assert "For this step" in page_text(w) and "olive oil" in page_text(w)
        w.cook_move(soup["id"], 1)
        pump(0.2)
        assert "Step 2 of 5" in page_text(w)
        # keyboard: right arrow moves on, space starts this step's timer
        from gi.repository import Gdk
        assert w._key(None, Gdk.KEY_Right, 0, 0) and "Step 3 of 5" in page_text(w)
        w.cook_goto(soup["id"], 3)
        pump(0.2)
        assert w._key(None, Gdk.KEY_space, 0, 0)
        assert w.timers.items and w.timers.items[-1].seconds == 1200 and w.timers.items[-1].label == "Simmer"
        assert w.timer_strip.get_parent() is not None
        # a short timer rings, is shown, and stops
        w.start_timer("Test", 1)
        assert wait_for(lambda: bool(w.timers.ringing), 5), "timer never rang"
        assert "done" in texts(w.timer_strip)
        w.stop_ringing()
        assert not w.timers.ringing
        for t in list(w.timers.items):
            w._timer_act(t.id, "cancel")
        assert not w.timers.items
        w.cook_goto(soup["id"], 5)
        pump(0.2)
        assert "Enjoy your meal" in page_text(w)
        w.finish_cooking(soup["id"], "great")
        pump(0.2)
        assert w.store.recipe(soup["id"])["cooked"] == 1 and w.store.log(soup["id"])[0]["note"] == "great"
        assert w.trail.current.view == "recipe"

    def adding_and_editing():
        w.edit(None)
        pump(0.3)
        assert w.trail.current.view == "edit"
        f = w._edit_fields
        f["title"].entry.set_text("Garlic Toast")
        f["servings"].entry.set_text("2")
        f["prep"].entry.set_text("5")
        f["tags"].entry.set_text("Snack, Quick")
        f["ingredients"].get_buffer().set_text("2 slices bread\n1 tbsp butter\n1 clove garlic")
        f["steps"].get_buffer().set_text("Toast the bread for 3 minutes.\nRub with garlic and butter.")
        w.save_edit()
        pump(0.3)
        r = next(r for r in w.store.recipes() if r["title"] == "Garlic Toast")
        assert r["servings"] == 2 and r["tags"] == ["Snack", "Quick"] and len(r["steps"]) == 2
        assert w.trail.current.view == "recipe" and w.trail.current.subject == r["id"]
        # paste a recipe
        from simmer import importer
        rec = importer.from_text("Iced Tea\nIngredients\n- 4 tea bags\n- 1 l water\nMethod\n1. Steep for 5 minutes.")
        rid = w.store.add(rec)
        assert w.store.recipe(rid)["ingredients"] == ["4 tea bags", "1 l water"]
        # delete and undo
        saved = w.store.delete(rid)
        assert w.store.recipe(rid) is None
        w.store.restore(saved)
        assert w.store.recipe(rid)["title"] == "Iced Tea"

    def plus_locked_and_owned():
        assert not w.plus
        w.go("plan")
        pump(0.3)
        assert "Simmer Plus" in page_text(w)
        w.shop_week()
        pump(0.4)
        assert w.center.grown == "plus"
        ok = wait_for(lambda: w.paywall.catalog_error != "", 30)
        assert ok, w.paywall.catalog_error
        w.center.fold()
        w.set_units("metric")
        assert w.units() == ""
        w.center.fold()
        # owned, as the license server answers after a paid checkout
        w.billing._store({"email": "t@example.com", "entitlements": ["simmer_plus:yearly"], "licenseToken": "",
                          "revalidateAfter": 0})
        pump(0.3)
        assert w.plus
        import datetime as dt
        recipes = {r["title"]: r for r in w.store.recipes()}
        today = dt.date.today()
        w._plan(today, recipes["Weeknight Tomato Soup"])
        w._plan(today + dt.timedelta(days=1), recipes["Lemon Garlic Butter Pasta"])
        w.go("plan")
        pump(0.3)
        assert w.trail.current.view == "plan" and "Weeknight Tomato Soup" in page_text(w)
        w.store.shop_clear(checked_only=False)
        w.week = __import__("simmer.app", fromlist=["x"]).monday(today)
        w.shop_week()
        items = [r["text"] for r in w.store.shopping()]
        assert any("garlic" in i for i in items) and len([i for i in items if "garlic" in i]) == 1, items  # merged
        w.set_units("metric")
        assert w.units() == "metric"
        soup = recipes["Weeknight Tomato Soup"]
        w.open_recipe(soup["id"])
        pump(0.3)
        assert "30 ml olive oil" in page_text(w) or "ml olive oil" in page_text(w), page_text(w)
        w.set_units("")
        w.go("cooked")
        pump(0.3)
        assert "Cooked this month" in page_text(w)
        w.billing.forget()
        pump(0.2)
        assert not w.plus

    def community_flow():
        w.go("community")
        assert wait_for(lambda: "Community Chili" in page_text(w), 10), page_text(w)
        w.open_page("shared", "Community Chili", "seed00001")
        assert wait_for(lambda: "Brown the beef" in page_text(w), 10)
        w.save_shared("seed00001")
        pump(0.2)
        saved = [r for r in w.store.recipes() if r["community_id"] == "seed00001"]
        assert saved and saved[0]["source"] == ""
        # imported recipes can't be shared; your own can
        from simmer.community import shareable
        toast = next(r for r in w.store.recipes() if r["title"] == "Garlic Toast")
        assert shareable(toast)[0] and not shareable(saved[0])[0]
        w.share(toast["id"], "Tester")
        assert wait_for(lambda: bool(w.store.recipe(toast["id"])["shared_id"]), 10)
        cid = w.store.recipe(toast["id"])["shared_id"]
        assert SHARED[cid]["author"] == "Tester" and "image" not in SHARED[cid]
        assert w.community.is_mine(cid)
        # a recipe the safety check refuses is not shared and says why
        bad = w.store.add({"title": "Spam Special", "ingredients": ["1 egg"], "steps": ["Fry it."]})
        before = len(SHARED)
        w.share(bad, "")
        pump(1.0)
        assert len(SHARED) == before
        # report
        w.report("seed00001", "spam")
        assert wait_for(lambda: SHARED["seed00001"].get("hidden"), 10)
        # take your own down
        w.unshare(cid)
        assert wait_for(lambda: cid not in SHARED, 10)
        assert not w.community.is_mine(cid)
        w.center.grow("settings", w._settings_panel)
        pump(0.2)
        w.center.fold()

    def backup():
        doc = json.loads(json.dumps(w.store.export()))
        from simmer.store import Store
        t = Store(os.path.join(TMP, "restore.sqlite3"))
        assert t.import_(doc) == w.store.counts()["recipes"]

    for name, fn in (("welcome, samples, places", welcome_and_samples), ("recipe and scaling", recipe_and_scaling),
                     ("cook mode and timers", cook_mode_and_timers), ("adding and editing", adding_and_editing),
                     ("Simmer Plus", plus_locked_and_owned), ("community", community_flow), ("backup", backup),
                     ("updates", lambda: updates_flow(w))):
        check(name, fn)


def updates_flow(w):
    """The update panel against a LOCAL FAKE GitHub: GitHub build downloads + verifies; Depot build doesn't."""
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import test_updates as tu
    srv, _u = tu.setup("github")
    try:
        tu.STATE["tag"] = "v1.3.0"
        w.updates.channel, w.updates.state = "github", {}
        w.updater.release = None
        w.updater.check(manual=True)
        assert wait_for(lambda: w.center.grown == "updates", 15), "panel didn't open"
        text = texts(w.updater.box)
        assert "1.3.0 is available" in text and "Download and install" in text, text
        w.updater._download()
        assert wait_for(lambda: bool(w.updater.downloaded), 20), (w.updater.error, w.updater.downloading)
        assert open(w.updater.downloaded, "rb").read() == tu.BUNDLE
        w.center.fold()
        pump(0.3)
        w.updates.channel, w.updater.downloaded = "depot", ""
        w.updater.open()
        pump(0.5)
        text = texts(w.updater.box)
        assert "Depot" in text and "Download and install" not in text, text
        w.center.fold()
    finally:
        srv.shutdown()


class TestApp(Adw.Application):
    def __init__(self):
        super().__init__(application_id="com.charleshartmann.SimmerTest")
        self.window = None

    def do_activate(self):
        from simmer import app as A
        self.window = A.Window(self)
        self.window.present()
        pump(0.5)
        try:
            run(self.window)
        finally:
            self.window.chime.stop()
            self.quit()


TestApp().run([])
bad = 0
for name, err in RESULTS:
    print(("ok   " if err is None else "FAIL ") + name)
    if err:
        bad += 1
        print(err)
print(f"{len(RESULTS) - bad} passed, {bad} failed")
sys.exit(1 if bad else 0)
