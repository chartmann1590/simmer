# SPDX-License-Identifier: GPL-3.0-or-later
"""Ingredient lines: reading the amount, unit and name out of "1 ½ cups flour, sifted", scaling them,
writing amounts the way a cook would ("1 ½ cups", not "1.5 cups"), converting between US and metric,
sorting a shopping list by aisle and adding up the same thing from several recipes.

No GTK in here, so it is tested on its own.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from fractions import Fraction

UNICODE_FRACTIONS = {"½": "1/2", "⅓": "1/3", "⅔": "2/3", "¼": "1/4", "¾": "3/4", "⅕": "1/5", "⅖": "2/5", "⅗": "3/5",
                     "⅘": "4/5", "⅙": "1/6", "⅚": "5/6", "⅛": "1/8", "⅜": "3/8", "⅝": "5/8", "⅞": "7/8"}
NICE = ((Fraction(1, 8), "⅛"), (Fraction(1, 4), "¼"), (Fraction(1, 3), "⅓"), (Fraction(3, 8), "⅜"),
        (Fraction(1, 2), "½"), (Fraction(5, 8), "⅝"), (Fraction(2, 3), "⅔"), (Fraction(3, 4), "¾"),
        (Fraction(7, 8), "⅞"))

# canonical unit → (kind, size in the base unit of its kind: ml for volume, g for weight), plus spellings
UNITS: dict[str, tuple[str, float, tuple[str, ...]]] = {
    "tsp": ("volume", 4.92892, ("teaspoon", "teaspoons", "tsp", "tsps", "tsp.", "t")),
    "tbsp": ("volume", 14.7868, ("tablespoon", "tablespoons", "tbsp", "tbsps", "tbsp.", "tbs", "tbl", "T")),
    "fl oz": ("volume", 29.5735, ("fluid ounce", "fluid ounces", "fl oz", "fl. oz.", "fl.oz")),
    "cup": ("volume", 236.588, ("cup", "cups", "c", "c.")),
    "pint": ("volume", 473.176, ("pint", "pints", "pt")),
    "quart": ("volume", 946.353, ("quart", "quarts", "qt")),
    "gallon": ("volume", 3785.41, ("gallon", "gallons", "gal")),
    "ml": ("volume", 1.0, ("ml", "mL", "milliliter", "milliliters", "millilitre", "millilitres")),
    "l": ("volume", 1000.0, ("l", "L", "liter", "liters", "litre", "litres")),
    "oz": ("weight", 28.3495, ("ounce", "ounces", "oz", "oz.")),
    "lb": ("weight", 453.592, ("pound", "pounds", "lb", "lbs", "lb.", "lbs.")),
    "g": ("weight", 1.0, ("g", "gram", "grams", "gr")),
    "kg": ("weight", 1000.0, ("kg", "kilogram", "kilograms", "kilo", "kilos")),
    # counted things: kept as they are, never converted
    "clove": ("count", 1, ("clove", "cloves")),
    "can": ("count", 1, ("can", "cans", "tin", "tins")),
    "pinch": ("count", 1, ("pinch", "pinches")),
    "dash": ("count", 1, ("dash", "dashes")),
    "slice": ("count", 1, ("slice", "slices")),
    "stick": ("count", 1, ("stick", "sticks")),
    "bunch": ("count", 1, ("bunch", "bunches")),
    "sprig": ("count", 1, ("sprig", "sprigs")),
    "handful": ("count", 1, ("handful", "handfuls")),
    "package": ("count", 1, ("package", "packages", "pkg", "packet", "packets")),
    "head": ("count", 1, ("head", "heads")),
}
PLURAL = {"cup": "cups", "pint": "pints", "quart": "quarts", "gallon": "gallons", "clove": "cloves", "can": "cans",
          "pinch": "pinches", "dash": "dashes", "slice": "slices", "stick": "sticks", "bunch": "bunches",
          "sprig": "sprigs", "handful": "handfuls", "package": "packages", "head": "heads"}
_SPELLING: dict[str, str] = {}
for _canon, (_k, _s, _names) in UNITS.items():
    for _n in _names:
        _SPELLING[_n if _n in ("T", "t", "L") else _n.lower()] = _canon

_NUM = r"(?:\d+\s+\d+/\d+|\d+/\d+|\d+(?:[.,]\d+)?)"
_AMOUNT_RE = re.compile(rf"^\s*(?P<a>{_NUM})(?:\s*(?:-|–|to)\s*(?P<b>{_NUM}))?\s*")


def _norm_fractions(text: str) -> str:
    out = []
    for i, ch in enumerate(text):
        if ch in UNICODE_FRACTIONS:
            prev = text[i - 1] if i else ""
            out.append((" " if prev.isdigit() else "") + UNICODE_FRACTIONS[ch])
        elif ch == "⁄":
            out.append("/")
        else:
            out.append(ch)
    return "".join(out)


def _number(s: str) -> Fraction:
    s = s.strip().replace(",", ".")
    if " " in s:
        whole, frac = s.split(None, 1)
        return Fraction(int(whole)) + Fraction(frac)
    return Fraction(s).limit_denominator(1000)


@dataclass
class Ingredient:
    text: str                       # the line as written
    amount: Fraction | None = None
    amount_to: Fraction | None = None   # "2–3 cloves"
    unit: str = ""                  # canonical unit, "" for none
    name: str = ""                  # "flour, sifted"
    note: str = ""                  # anything in parentheses

    @property
    def kind(self) -> str:
        return UNITS[self.unit][0] if self.unit in UNITS else ""

    @property
    def key(self) -> str:
        """What this is, for adding the same thing up: "flour" from "all-purpose flour, sifted"."""
        return item_key(self.name)


def parse(line: str) -> Ingredient:
    """Read one ingredient line. Anything that isn't an amount or a unit stays in the name."""
    text = " ".join((line or "").split())
    ing = Ingredient(text=text)
    rest = _norm_fractions(text)
    note = re.findall(r"\(([^)]*)\)", rest)
    if note:
        ing.note = "; ".join(n.strip() for n in note if n.strip())
        rest = re.sub(r"\s*\([^)]*\)", "", rest)
    m = _AMOUNT_RE.match(rest)
    if m:
        try:
            ing.amount = _number(m.group("a"))
            if m.group("b"):
                ing.amount_to = _number(m.group("b"))
            rest = rest[m.end():]
        except (ValueError, ZeroDivisionError):
            ing.amount = ing.amount_to = None
    elif re.match(r"^\s*(a|an|one)\s+", rest, re.I) and re.match(r"^\s*(a|an|one)\s+(\S+)", rest, re.I):
        word = re.match(r"^\s*(?:a|an|one)\s+(\S+)", rest, re.I).group(1)
        if _unit_of(word):
            ing.amount = Fraction(1)
            rest = re.sub(r"^\s*(a|an|one)\s+", "", rest, flags=re.I)
    # the unit: one or two words
    words = rest.split()
    if words:
        two = " ".join(words[:2]).rstrip(",")
        if len(words) > 1 and _unit_of(two):
            ing.unit, words = _unit_of(two), words[2:]
        elif _unit_of(words[0].rstrip(",")) and (ing.amount is not None or words[0].lower() in ("pinch", "dash")):
            ing.unit, words = _unit_of(words[0].rstrip(",")), words[1:]
    if words and words[0].lower() == "of":
        words = words[1:]
    ing.name = " ".join(words).strip(" ,")
    return ing


def _unit_of(word: str) -> str:
    if word in ("T", "t", "L"):
        return _SPELLING[word]
    return _SPELLING.get(word.lower(), "")


def nice_amount(x: Fraction | float) -> str:
    """1.5 → "1 ½", 0.333 → "⅓", 2 → "2", 0.1 → "0.1", 12.5 → "12 ½", 450.0 → "450"."""
    x = Fraction(x).limit_denominator(1000)
    if x <= 0:
        return "0"
    whole, frac = divmod(x, 1)
    whole = int(whole)
    if frac == 0:
        return str(whole)
    best, glyph = min(NICE, key=lambda n: abs(n[0] - frac))
    if abs(best - frac) <= min(Fraction(1, 24), frac * Fraction(8, 100)):
        return f"{whole} {glyph}" if whole else glyph
    if frac > Fraction(23, 24):
        return str(whole + 1)
    if frac < Fraction(1, 24) and whole:
        return str(whole)
    f = float(x)
    return f"{f:.2f}".rstrip("0").rstrip(".") if f < 10 else f"{f:.1f}".rstrip("0").rstrip(".")


METRIC = ("ml", "l", "g", "kg")


def decimal_amount(x: Fraction | float) -> str:
    """Metric amounts read as decimals: 1.4 kg, 250 g, 0.5 l."""
    f = float(x)
    if f >= 100 or f == int(f):
        return str(int(round(f)))
    return f"{f:.2f}".rstrip("0").rstrip(".") if f < 1 else f"{f:.1f}".rstrip("0").rstrip(".")


def unit_words(unit: str, amount: Fraction | None) -> str:
    if not unit:
        return ""
    if amount is not None and amount > 1 and unit in PLURAL:
        return PLURAL[unit]
    return unit


def scaled(ing: Ingredient, factor: Fraction | float, system: str = "") -> str:
    """The line with its amount multiplied by `factor`, and converted to `system` ("us", "metric", "")."""
    if ing.amount is None:
        return ing.text
    factor = Fraction(factor).limit_denominator(1000)
    amount = ing.amount * factor
    to = ing.amount_to * factor if ing.amount_to is not None else None
    unit = ing.unit
    if system and ing.kind in ("volume", "weight"):
        amount, unit, to = convert(amount, unit, system, to)
    name = ing.name
    if not unit and ing.amount <= 1 < amount:
        name = pluralise(name)
    fmt = decimal_amount if unit in METRIC else nice_amount
    words = fmt(amount) + (f"–{fmt(to)}" if to is not None else "")
    u = unit_words(unit, to if to is not None else amount)
    out = " ".join(p for p in (words, u, name) if p)
    if ing.note:
        out += f" ({ing.note})"
    return out


def pluralise(name: str) -> str:
    """"large onion, chopped" → "large onions, chopped"; leaves names that are already plural alone."""
    head, sep, tail = name.partition(",")
    words = head.split()
    if not words:
        return name
    w = words[-1]
    lw = w.lower()
    if lw.endswith("s") or not lw.isalpha() or lw in ("garlic", "rice", "flour", "salt", "pepper", "water", "milk", "butter"):
        return name
    if lw.endswith(("ch", "sh", "x")) or lw in ("tomato", "potato", "mango"):
        w += "es"
    elif lw.endswith("y") and lw[-2:-1] not in "aeiou":
        w = w[:-1] + "ies"
    elif lw.endswith("f") and lw in ("leaf", "loaf", "half"):
        w = w[:-1] + "ves"
    else:
        w += "s"
    return " ".join(words[:-1] + [w]) + sep + tail


def convert(amount: Fraction, unit: str, system: str, to: Fraction | None = None):
    """(amount, unit, to) in the other system, picking a unit that reads naturally."""
    kind, size, _ = UNITS[unit]
    metric_units = {"ml", "l", "g", "kg"}
    if (system == "metric") == (unit in metric_units):
        return amount, unit, to
    base = float(amount) * size
    base_to = float(to) * size if to is not None else None
    if system == "metric":
        if kind == "volume":
            target = "l" if base >= 1000 else "ml"
        else:
            target = "kg" if base >= 1000 else "g"
    else:
        if kind == "volume":
            target = ("tsp" if base < 14 else "tbsp" if base < 59 else "cup")
        else:
            target = "lb" if base >= 453 else "oz"
    tsize = UNITS[target][1]

    def rounded(v):
        q = v / tsize
        if target in ("ml", "g"):
            return Fraction(round(q / 5) * 5 if q >= 20 else round(q)).limit_denominator(1)
        if target in ("l", "kg"):
            return Fraction(round(q * 10), 10)
        return Fraction(max(1, round(q * 8)), 8)   # cooks measure in eighths
    return rounded(base), target, (rounded(base_to) if base_to is not None else None)


# ---------------------------------------------------------------- the shopping list
AISLES = (
    ("Produce", ("apple", "avocado", "banana", "basil", "bean sprout", "bell pepper", "berries", "blueberr", "broccoli",
                 "cabbage", "carrot", "cauliflower", "celery", "chard", "chili", "chive", "cilantro", "coriander leaves",
                 "corn", "cucumber", "dill", "eggplant", "fennel", "garlic", "ginger", "grape", "green bean", "green onion",
                 "herb", "jalape", "kale", "leek", "lemon", "lettuce", "lime", "mango", "mint", "mushroom", "onion",
                 "orange", "parsley", "peach", "pear", "pea", "pepper", "potato", "pumpkin", "radish", "rosemary",
                 "sage", "scallion", "shallot", "spinach", "squash", "strawberr", "sweet potato", "thyme", "tomato",
                 "zucchini", "arugula")),
    ("Meat and fish", ("bacon", "beef", "chicken", "chorizo", "cod", "fish", "ham", "lamb", "pork", "prawn", "salmon",
                       "sausage", "shrimp", "steak", "tuna", "turkey", "anchov")),
    ("Dairy and eggs", ("butter", "buttermilk", "cheddar", "cheese", "cream", "egg", "feta", "milk", "mozzarella",
                        "parmesan", "ricotta", "sour cream", "yogurt", "yoghurt", "crème fraîche", "ghee")),
    ("Bakery", ("bread", "baguette", "bun", "pita", "tortilla", "naan", "roll")),
    ("Pantry", ("baking powder", "baking soda", "bean", "breadcrumb", "broth", "chickpea", "chocolate", "cocoa",
                "coconut milk", "cornstarch", "couscous", "flour", "honey", "lentil", "maple", "noodle", "oat", "oil",
                "pasta", "peanut butter", "quinoa", "rice", "stock", "sugar", "syrup", "tahini", "tomato paste",
                "vanilla", "vinegar", "yeast", "nut", "almond", "walnut", "raisin", "canned", "passata", "sun-dried",
                "tinned")),
    ("Spices and sauces", ("cinnamon", "cumin", "curry", "mustard", "nutmeg", "oregano", "paprika", "salt",
                           "black pepper", "soy sauce", "fish sauce", "sriracha", "hot sauce", "ketchup", "mayo",
                           "spice", "turmeric", "chili flake", "red pepper flake", "bay lea", "garam masala",
                           "worcestershire", "miso", "harissa", "pesto", "cayenne", "coriander")),
    ("Frozen", ("frozen", "ice cream")),
    ("Drinks", ("wine", "beer", "juice", "coffee", "tea", "sparkling water")),
)
AISLE_ORDER = [a for a, _ in AISLES] + ["Other"]
# more specific words win over the one-word guesses ("black pepper" is a spice, "pepper" produce)
_AISLE_WORDS = sorted(((w, a) for a, ws in AISLES for w in ws), key=lambda p: -len(p[0]))

_DROP = re.compile(r"\b(fresh|freshly|large|small|medium|chopped|diced|minced|sliced|finely|roughly|thinly|grated|"
                   r"peeled|softened|melted|room temperature|to taste|optional|divided|packed|plus more|extra|"
                   r"boneless|skinless|ground|whole|organic|ripe|cold|warm|hot|unsalted|salted|dried|crushed|"
                   r"about|approximately|for serving|for garnish|cut into [^,]*|at room temperature)\b", re.I)


def item_key(name: str) -> str:
    n = name.lower().split(",")[0]
    n = _DROP.sub(" ", n)
    n = re.sub(r"[^a-zà-ÿ\s-]", " ", n)
    n = " ".join(n.split())
    if n.endswith("es") and n[:-2].endswith(("tomato", "potato")):
        n = n[:-2]
    elif n.endswith("s") and not n.endswith(("ss", "us", "is")) and len(n) > 3:
        n = n[:-1]
    return n


def aisle_for(name: str) -> str:
    n = " " + name.lower() + " "
    if re.search(r"\b(canned|tinned|jarred|can of|tin of)\b", n):
        return "Pantry"
    for word, aisle in _AISLE_WORDS:
        if word in n:
            return aisle
    return "Other"


@dataclass
class ShopItem:
    key: str
    name: str
    amounts: dict = field(default_factory=dict)   # (kind or unit) → Fraction in base units / count
    loose: list = field(default_factory=list)     # lines with no amount ("salt to taste")
    recipes: list = field(default_factory=list)

    def words(self, system: str = "us") -> str:
        parts = []
        for unit, total in self.amounts.items():
            if unit == "volume" or unit == "weight":
                base_unit = "ml" if unit == "volume" else "g"
                a, u, _ = convert(Fraction(total), base_unit, system) if system == "us" else \
                    convert(Fraction(total), base_unit, "metric")
                if system == "us" and u in ("ml", "g"):
                    a, u = Fraction(total), base_unit
                if u in ("ml", "g") and a >= 20:
                    a = Fraction(round(a / 5) * 5)
                parts.append(f"{(decimal_amount if u in METRIC else nice_amount)(a)} {unit_words(u, a)}")
            elif unit:
                parts.append(f"{nice_amount(total)} {unit_words(unit, total)}")
            else:
                parts.append(nice_amount(total))
        amount = " + ".join(parts)
        return f"{amount} {self.name}".strip() if amount else self.name


def combine(lines: list[tuple[str, str, Fraction | float]]) -> list[ShopItem]:
    """[(ingredient line, recipe title, scale factor)] → one ShopItem per thing, amounts added up where the
    units allow (cups and tablespoons add; cups and grams are kept side by side)."""
    items: dict[str, ShopItem] = {}
    for line, recipe, factor in lines:
        ing = parse(line)
        key = ing.key or item_key(line)
        if not key:
            continue
        it = items.get(key)
        if it is None:
            it = items[key] = ShopItem(key=key, name=_display_name(ing.name or line))
        if recipe and recipe not in it.recipes:
            it.recipes.append(recipe)
        if ing.amount is None:
            it.loose.append(line)
            continue
        amount = ing.amount * Fraction(factor).limit_denominator(1000)
        kind = ing.kind
        if kind in ("volume", "weight"):
            it.amounts[kind] = it.amounts.get(kind, Fraction(0)) + amount * Fraction(UNITS[ing.unit][1]).limit_denominator(10000)
        else:
            it.amounts[ing.unit] = it.amounts.get(ing.unit, Fraction(0)) + amount
    return sorted(items.values(), key=lambda i: (AISLE_ORDER.index(aisle_for(i.name)), i.name.lower()))


def _display_name(name: str) -> str:
    n = name.split(",")[0]
    n = _DROP.sub(" ", n)
    return " ".join(n.split()) or name.strip()
