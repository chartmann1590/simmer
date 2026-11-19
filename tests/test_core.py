# SPDX-License-Identifier: GPL-3.0-or-later
"""Ingredients, timers, importing, the library and the community client (no GTK, no network)."""
import json
import os
import sys
import tempfile
import threading
from fractions import Fraction
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from simmer import community, importer, ingredients as ING, timers as T  # noqa: E402
from simmer.store import Store  # noqa: E402


# ---------------------------------------------------------------- ingredients
def test_parse_amounts_units_names():
    i = ING.parse("1 ½ cups all-purpose flour, sifted")
    assert (i.amount, i.unit, i.name) == (Fraction(3, 2), "cup", "all-purpose flour, sifted")
    i = ING.parse("2-3 cloves garlic (minced)")
    assert (i.amount, i.amount_to, i.unit, i.name, i.note) == (2, 3, "clove", "garlic", "minced")
    i = ING.parse("1/4 tsp red pepper flakes")
    assert (i.amount, i.unit) == (Fraction(1, 4), "tsp")
    i = ING.parse("800 g canned tomatoes")
    assert (i.amount, i.unit, i.name) == (800, "g", "canned tomatoes")
    i = ING.parse("salt and black pepper")
    assert i.amount is None and i.name == "salt and black pepper"
    i = ING.parse("a pinch of salt")
    assert (i.amount, i.unit, i.name) == (1, "pinch", "salt")
    i = ING.parse("1 T butter")
    assert i.unit == "tbsp"
    i = ING.parse("1 t salt")
    assert i.unit == "tsp"
    i = ING.parse("2 large eggs")
    assert (i.amount, i.unit, i.name) == (2, "", "large eggs")
    assert ING.parse("1 fl oz cream").unit == "fl oz"


def test_nice_amounts_and_scaling():
    assert ING.nice_amount(Fraction(3, 2)) == "1 ½"
    assert ING.nice_amount(0.333) == "⅓"
    assert ING.nice_amount(2) == "2"
    assert ING.nice_amount(Fraction(1, 10)) == "0.1"
    assert ING.scaled(ING.parse("1 ½ cups flour"), 2) == "3 cups flour"
    assert ING.scaled(ING.parse("1 cup flour"), Fraction(1, 2)) == "½ cup flour"
    assert ING.scaled(ING.parse("2-3 cloves garlic"), 2) == "4–6 cloves garlic"
    assert ING.scaled(ING.parse("salt to taste"), 3) == "salt to taste"
    assert ING.scaled(ING.parse("1 can chickpeas (400 g)"), 2) == "2 cans chickpeas (400 g)"
    assert ING.scaled(ING.parse("1 large onion, chopped"), 2) == "2 large onions, chopped"
    assert ING.scaled(ING.parse("1 tomato"), 3) == "3 tomatoes"
    assert ING.scaled(ING.parse("2 eggs"), 2) == "4 eggs"


def test_unit_conversion():
    assert ING.scaled(ING.parse("1 cup milk"), 1, "metric") == "235 ml milk"
    assert ING.scaled(ING.parse("1 lb ground beef"), 1, "metric") == "455 g ground beef"
    assert ING.scaled(ING.parse("2 lb potatoes"), 1, "metric") == "905 g potatoes"
    assert ING.scaled(ING.parse("3 lb potatoes"), 1, "metric") == "1.4 kg potatoes"
    assert ING.scaled(ING.parse("250 ml stock"), 1, "us") == "1 cup stock"
    assert ING.scaled(ING.parse("15 ml oil"), 1, "us") == "1 tbsp oil"
    assert ING.scaled(ING.parse("200 g spaghetti"), 1, "us") == "7 oz spaghetti"
    assert ING.scaled(ING.parse("2 cloves garlic"), 1, "metric") == "2 cloves garlic"   # counted things stay
    assert ING.scaled(ING.parse("1 cup milk"), 1, "us") == "1 cup milk"                 # already US


def test_aisles_and_combining():
    assert ING.aisle_for("black pepper") == "Spices and sauces"
    assert ING.aisle_for("red bell pepper") == "Produce"
    assert ING.aisle_for("chicken thighs") == "Meat and fish"
    assert ING.aisle_for("unsalted butter") == "Dairy and eggs"
    assert ING.aisle_for("mystery thing") == "Other"
    items = ING.combine([("1 cup flour", "Bread", 1), ("2 tbsp flour", "Cake", 1), ("2 onions, chopped", "Soup", 1),
                         ("1 onion", "Stew", 2), ("salt", "Soup", 1), ("salt to taste", "Stew", 1),
                         ("100 g flour", "Pie", 1)])
    by = {i.key: i for i in items}
    assert by["onion"].words() == "4 onions" and by["onion"].recipes == ["Soup", "Stew"]
    assert "flour" in by and len(by["flour"].amounts) == 2    # volume and weight side by side
    assert by["salt"].words() == "salt"
    # produce sorts before pantry
    keys = [i.key for i in items]
    assert keys.index("onion") < keys.index("flour")


# ---------------------------------------------------------------- timers
def test_durations():
    f = T.durations("Simmer for 20 minutes, then rest 5 min.")
    assert [x.seconds for x in f] == [1200, 300]
    assert [x.seconds for x in T.durations("Bake 1 hour and 15 minutes")] == [4500]
    assert [x.seconds for x in T.durations("cook 8-10 minutes")] == [600]
    assert [x.seconds for x in T.durations("Roast for 1 1/2 hours")] == [5400]
    assert T.durations("Add 2 shallots and 3 sprigs") == []
    assert [x.seconds for x in T.durations("about 30 seconds")] == [30]
    assert T.clock(65) == "1:05" and T.clock(3725) == "1:02:05"
    assert T.words(5400) == "1 h 30 min"


def test_timers_run_pause_ring():
    now = [100.0]
    ts = T.Timers(clock_fn=lambda: now[0])
    a = ts.start("Simmer", 60)
    b = ts.start("Rest", 30)
    now[0] += 20
    ts.pause(a.id)
    assert round(a.remaining()) == 40 and not a.running
    now[0] += 15
    assert [t.label for t in ts.tick()] == ["Rest"] and ts.ringing == [b]
    ts.resume(a.id)
    ts.add(a.id, 60)
    now[0] += 99
    assert ts.tick() == [] and a.running
    now[0] += 2
    assert ts.tick() == [a]
    ts.cancel(b.id)
    assert ts.ringing == [a]


# ---------------------------------------------------------------- importing
PAGE = """<html><head><title>Best Soup Ever | Food Site</title>
<meta property="og:image" content="https://img.example/og.jpg">
<script type="application/ld+json">{"@context":"https://schema.org","@graph":[{"@type":"WebPage","name":"x"},
{"@type":["Recipe"],"name":"Best Soup &amp; Bread","image":[{"url":"https://img.example/soup.jpg"}],
"recipeYield":["4","4 bowls"],"prepTime":"PT10M","cookTime":"PT1H5M","author":{"@type":"Person","name":"Jo"},
"recipeCategory":"Soup, Dinner","recipeCuisine":"Italian",
"recipeIngredient":["2 tbsp <b>olive</b> oil","1 onion"],
"recipeInstructions":[{"@type":"HowToSection","name":"Soup","itemListElement":[
  {"@type":"HowToStep","text":"Cook the onion for 8 minutes."},{"@type":"HowToStep","text":"Simmer 1 hour."}]},
  "Serve hot."]}]}</script></head><body>ads ads ads</body></html>"""


def test_import_jsonld():
    r = importer.from_html(PAGE, "https://food.example/soup")
    assert r["title"] == "Best Soup & Bread"
    assert r["image"] == "https://img.example/soup.jpg"
    assert (r["servings"], r["prep"], r["cook"], r["total"]) == (4, 10, 65, 75)
    assert r["ingredients"] == ["2 tbsp olive oil", "1 onion"]
    assert r["steps"] == ["## Soup", "Cook the onion for 8 minutes.", "Simmer 1 hour.", "Serve hot."]
    assert r["tags"] == ["Soup", "Dinner", "Italian"] and r["author"] == "Jo"


def test_import_fallback_and_errors():
    r = importer.from_html("<html><head><meta property='og:title' content='Nice Cake'>"
                           "<meta property='og:image' content='https://i.example/c.jpg'></head></html>", "https://x")
    assert r["title"] == "Nice Cake" and r.get("partial") and r["image"] == "https://i.example/c.jpg"
    try:
        importer.from_html("<html><body>nothing</body></html>")
        assert False
    except importer.ImportError_:
        pass
    assert importer.iso_minutes("PT1H30M") == 90 and importer.iso_minutes("45") == 45
    assert importer.iso_minutes("P0DT0H20M") == 20 and importer.iso_minutes(None) == 0


def test_import_text_roundtrip():
    text = "Pancakes\nServes 2\n\nIngredients\n- 1 cup flour\n- 1 egg\n\nMethod\n1. Whisk everything.\n2. Fry for 2 minutes a side."
    r = importer.from_text(text)
    assert r["title"] == "Pancakes" and r["servings"] == 2
    assert r["ingredients"] == ["1 cup flour", "1 egg"]
    assert r["steps"] == ["Whisk everything.", "Fry for 2 minutes a side."]
    again = importer.from_text(importer.to_text(r))
    assert again["ingredients"] == r["ingredients"] and again["steps"] == r["steps"] and again["title"] == r["title"]
    # without headings: amounts are ingredients, the rest steps
    r = importer.from_text("Toast\n2 slices bread\n1 tbsp butter\nToast the bread and butter it.")
    assert r["ingredients"] == ["2 slices bread", "1 tbsp butter"] and r["steps"] == ["Toast the bread and butter it."]


# ---------------------------------------------------------------- the library
def test_store_recipes_plan_shopping_backup():
    s = Store(os.path.join(tempfile.mkdtemp(), "l.sqlite3"))
    a = s.add({"title": "Soup", "ingredients": ["1 onion"], "steps": ["Cook."], "tags": ["Dinner", "Soup"], "total": 30})
    b = s.add({"title": "Cake", "ingredients": ["1 cup flour"], "steps": ["Bake."], "tags": ["Baking"], "total": 0})
    assert [r["title"] for r in s.recipes(search="onion")] == ["Soup"]
    assert [r["title"] for r in s.recipes(tag="soup")] == ["Soup"]
    assert [r["title"] for r in s.recipes(order="quick")] == ["Soup", "Cake"]
    s.update(b, favorite=1)
    assert [r["title"] for r in s.recipes(favorites=True)] == ["Cake"]
    assert dict(s.tags())["Dinner"] == 1
    s.plan_add("2026-11-20", a, 4)
    s.cooked(a, "more salt")
    assert s.recipe(a)["cooked"] == 1 and s.log(a)[0]["note"] == "more salt"
    plan = s.plan("2026-11-16", "2026-11-22")
    assert plan["2026-11-20"][0]["title"] == "Soup" and plan["2026-11-20"][0]["planned"] == 4
    sid = s.shop_add("2 lemons", "Produce", "Soup")
    s.shop_check(sid, True)
    assert s.counts()["shopping"] == 0
    rows = s.shop_clear()
    assert s.shopping() == []
    s.shop_restore(rows)
    assert len(s.shopping()) == 1
    # delete and undo keeps the plan and the log
    saved = s.delete(a)
    assert s.recipe(a) is None and s.plan("2026-11-16", "2026-11-22") == {}
    s.restore(saved)
    assert s.recipe(a)["title"] == "Soup" and s.log(a) and s.plan("2026-11-16", "2026-11-22")
    # backup into a fresh library, and again (no duplicates)
    doc = json.loads(json.dumps(s.export()))
    t = Store(os.path.join(tempfile.mkdtemp(), "l.sqlite3"))
    assert t.import_(doc) == 2 and t.import_(doc) == 0
    assert t.recipes(favorites=True)[0]["title"] == "Cake"
    try:
        t.import_({"app": "other"})
        assert False
    except ValueError:
        pass


# ---------------------------------------------------------------- community client
CALLS = []


class Fake(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, doc):
        raw = json.dumps(doc).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):
        CALLS.append(("GET", self.path, None))
        if self.path.startswith("/v1/recipes?") or self.path == "/v1/recipes":
            return self._send(200, {"recipes": [{"id": "abc234", "title": "Soup", "saves": 2}], "more": False})
        if self.path == "/v1/recipes/abc234":
            return self._send(200, {"recipe": {"id": "abc234", "title": "Soup", "ingredients": ["1 onion"], "steps": ["Cook."]}})
        self._send(404, {"error": "That recipe isn't there any more.", "code": "missing"})

    def _body(self):
        return json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")

    def do_POST(self):
        body = self._body()
        CALLS.append(("POST", self.path, body))
        if self.path == "/v1/recipes":
            if "bad" in body["recipe"]["title"]:
                return self._send(422, {"error": "The automatic safety check didn't pass this recipe.", "code": "unsafe"})
            return self._send(201, {"id": "new234", "key": "secretkey"})
        self._send(200, {"ok": True})

    def do_DELETE(self):
        CALLS.append(("DELETE", self.path, self._body()))
        self._send(200, {"ok": True})


def test_community_client():
    srv = ThreadingHTTPServer(("127.0.0.1", 0), Fake)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    community.BASE_URL = f"http://127.0.0.1:{srv.server_port}"
    path = os.path.join(tempfile.mkdtemp(), "c.json")
    c = community.Community(path)
    dev = c.device
    assert len(dev) >= 16 and community.Community(path).device == dev       # stable across restarts
    assert c.browse(q="soup", sort="popular")["recipes"][0]["id"] == "abc234"
    assert CALLS[-1][1] == "/v1/recipes?q=soup&sort=popular"
    assert c.get("abc234")["ingredients"] == ["1 onion"]
    try:
        c.get("zzz999")
        assert False
    except community.CommunityError as ex:
        assert ex.code == "missing"
    rec = {"title": "Toast", "ingredients": ["1 slice bread"], "steps": ["Toast it."], "source": "", "image": "x",
           "servings": 1, "notes": "", "tags": []}
    doc = c.share(rec, "Sam")
    sent = CALLS[-1][2]
    assert sent["device"] == dev and sent["recipe"]["author"] == "Sam"
    assert "image" not in sent["recipe"] and "source" not in sent["recipe"]     # never pictures or links
    assert c.is_mine(doc["id"]) and community.Community(path).is_mine("new234")
    try:
        c.share({**rec, "title": "bad"}, "")
        assert False
    except community.CommunityError as ex:
        assert ex.code == "unsafe"
    c.unshare("new234")
    assert CALLS[-1][0] == "DELETE" and CALLS[-1][2]["key"] == "secretkey" and not c.is_mine("new234")
    c.report("abc234", "spam")
    assert CALLS[-1][1] == "/v1/recipes/abc234/report" and CALLS[-1][2]["reason"] == "spam"
    srv.shutdown()
    community.BASE_URL = "http://127.0.0.1:1"
    try:
        c.browse()
        assert False
    except community.CommunityError as ex:
        assert ex.code == "offline"


def test_shareable():
    assert community.shareable({"source": "https://x", "ingredients": ["a"], "steps": ["b"]})[0] is False
    assert community.shareable({"source": "", "community_id": "abc", "ingredients": ["a"], "steps": ["b"]})[0] is False
    assert community.shareable({"source": "", "ingredients": [], "steps": ["b"]})[0] is False
    assert community.shareable({"source": "", "ingredients": ["a"], "steps": ["b"]})[0] is True
