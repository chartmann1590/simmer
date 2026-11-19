# SPDX-License-Identifier: GPL-3.0-or-later
"""Turning a recipe web page, or pasted text, into a recipe.

Most recipe sites describe each recipe for search engines in schema.org JSON-LD
(`<script type="application/ld+json">` with `"@type": "Recipe"`). That is read first; it holds the title,
ingredients, steps, times, servings and picture exactly as the author entered them. Pages without it fall
back to their title and picture, and you fill in the rest. No GTK in here.
"""
from __future__ import annotations

import html
import json
import re
from html.parser import HTMLParser

RECIPE_FIELDS = ("title", "source", "image", "servings", "yield_text", "prep", "cook", "total", "ingredients",
                 "steps", "notes", "tags", "author")


class ImportError_(Exception):
    """Something the person should read."""


def blank() -> dict:
    return {"title": "", "source": "", "image": "", "servings": 0, "yield_text": "", "prep": 0, "cook": 0,
            "total": 0, "ingredients": [], "steps": [], "notes": "", "tags": [], "author": ""}


# ---------------------------------------------------------------- times and text
_ISO = re.compile(r"^P(?:(\d+)D)?(?:T(?:(\d+(?:\.\d+)?)H)?(?:(\d+(?:\.\d+)?)M)?(?:(\d+(?:\.\d+)?)S)?)?$", re.I)


def iso_minutes(value) -> int:
    """"PT1H30M" → 90. Some sites write plain minutes ("45") or words ("1 hour"); those work too."""
    if value is None:
        return 0
    if isinstance(value, (int, float)):
        return int(value)
    v = str(value).strip()
    m = _ISO.match(v)
    if m and any(m.groups()):
        d, h, mi, s = (float(x) if x else 0 for x in m.groups())
        return int(round(d * 1440 + h * 60 + mi + s / 60))
    if v.isdigit():
        return int(v)
    from .timers import durations
    found = durations(v)
    return int(round(sum(f.seconds for f in found) / 60)) if found else 0


def clean(text) -> str:
    """Plain text from whatever a site put there: entities decoded, tags stripped, spaces tidied."""
    if text is None:
        return ""
    if isinstance(text, (list, tuple)):
        text = " ".join(clean(t) for t in text)
    if isinstance(text, dict):
        text = text.get("text") or text.get("name") or ""
    t = html.unescape(str(text))
    t = re.sub(r"<br\s*/?>", "\n", t, flags=re.I)
    t = re.sub(r"<[^>]+>", "", t)
    t = html.unescape(t)
    lines = [" ".join(line.split()) for line in t.splitlines()]
    return "\n".join(line for line in lines if line).strip()


def _servings(value) -> tuple[int, str]:
    vals = value if isinstance(value, list) else [value]
    text = ""
    n = 0
    for v in vals:
        s = clean(v)
        if not s:
            continue
        text = text or s
        m = re.search(r"\d+", s)
        if m and not n:
            n = int(m.group(0))
    return n, (text if text and not text.isdigit() else "")


# ---------------------------------------------------------------- JSON-LD
class _Scripts(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.ld: list[str] = []
        self.meta: dict[str, str] = {}
        self.title = ""
        self._in_ld = False
        self._in_title = False
        self._buf: list[str] = []

    def handle_starttag(self, tag, attrs):
        a = {k.lower(): (v or "") for k, v in attrs}
        if tag == "script" and "ld+json" in a.get("type", "").lower():
            self._in_ld, self._buf = True, []
        elif tag == "meta":
            key = (a.get("property") or a.get("name") or "").lower()
            if key and "content" in a and key not in self.meta:
                self.meta[key] = a["content"]
        elif tag == "title":
            self._in_title = True

    def handle_endtag(self, tag):
        if tag == "script" and self._in_ld:
            self.ld.append("".join(self._buf))
            self._in_ld = False
        elif tag == "title":
            self._in_title = False

    def handle_data(self, data):
        if self._in_ld:
            self._buf.append(data)
        elif self._in_title:
            self.title += data


def _is_recipe(node) -> bool:
    t = node.get("@type") if isinstance(node, dict) else None
    types = t if isinstance(t, list) else [t]
    return any(isinstance(x, str) and x.split("/")[-1].lower() == "recipe" for x in types)


def _find_recipe(node, depth=0):
    if depth > 8:
        return None
    if isinstance(node, list):
        for n in node:
            r = _find_recipe(n, depth + 1)
            if r is not None:
                return r
        return None
    if not isinstance(node, dict):
        return None
    if _is_recipe(node):
        return node
    for key in ("@graph", "mainEntity", "mainEntityOfPage", "itemListElement", "item"):
        if key in node:
            r = _find_recipe(node[key], depth + 1)
            if r is not None:
                return r
    return None


def _image(value) -> str:
    if isinstance(value, list):
        for v in value:
            u = _image(v)
            if u:
                return u
        return ""
    if isinstance(value, dict):
        return _image(value.get("url") or value.get("contentUrl") or "")
    u = str(value or "").strip()
    return u if u.startswith(("https://", "http://")) else ""


def _steps(value) -> list[str]:
    """recipeInstructions: a string, a list of strings, HowToSteps, or HowToSections of HowToSteps."""
    out: list[str] = []
    if value is None:
        return out
    if isinstance(value, str):
        text = clean(value)
        parts = [p.strip() for p in re.split(r"\n+|(?<=[.!?])\s+(?=\d+[.)]\s)", text) if p.strip()]
        return [re.sub(r"^\d+[.)]\s*", "", p) for p in parts]
    if isinstance(value, dict):
        kind = str(value.get("@type", "")).lower()
        if "section" in kind:
            name = clean(value.get("name"))
            inner = _steps(value.get("itemListElement"))
            if name and inner:
                out.append(f"## {name}")
            return out + inner
        text = clean(value.get("text") or value.get("name") or value.get("description"))
        return [text] if text else []
    if isinstance(value, list):
        for v in value:
            out.extend(_steps(v))
    return out


def _tags(r: dict) -> list[str]:
    tags: list[str] = []
    for key in ("recipeCategory", "recipeCuisine"):
        v = r.get(key)
        for t in (v if isinstance(v, list) else [v]):
            t = clean(t)
            for part in t.split(","):
                part = part.strip()
                if part and part.lower() not in (x.lower() for x in tags) and len(part) < 30:
                    tags.append(part)
    return tags[:6]


def _author(v) -> str:
    if isinstance(v, list):
        return ", ".join(a for a in (_author(x) for x in v) if a)
    if isinstance(v, dict):
        return clean(v.get("name"))
    return clean(v)


def from_jsonld(r: dict, source: str = "") -> dict:
    rec = blank()
    rec["title"] = clean(r.get("name"))[:200]
    rec["source"] = source
    rec["image"] = _image(r.get("image") or r.get("thumbnailUrl"))
    rec["servings"], rec["yield_text"] = _servings(r.get("recipeYield"))
    rec["prep"] = iso_minutes(r.get("prepTime"))
    rec["cook"] = iso_minutes(r.get("cookTime"))
    rec["total"] = iso_minutes(r.get("totalTime")) or (rec["prep"] + rec["cook"])
    ings = r.get("recipeIngredient") or r.get("ingredients") or []
    rec["ingredients"] = [clean(i) for i in (ings if isinstance(ings, list) else [ings]) if clean(i)][:120]
    rec["steps"] = _steps(r.get("recipeInstructions"))[:120]
    rec["notes"] = clean(r.get("description"))[:2000]
    rec["tags"] = _tags(r)
    rec["author"] = _author(r.get("author"))[:120]
    return rec


def from_html(page: str, source: str = "") -> dict:
    """The recipe on a web page. Raises ImportError_ when there's nothing recipe-like at all."""
    p = _Scripts()
    try:
        p.feed(page)
        p.close()
    except Exception:
        pass
    for raw in p.ld:
        try:
            data = json.loads(raw.strip().rstrip(";"))
        except ValueError:
            try:   # some sites leave raw newlines inside strings
                data = json.loads(re.sub(r"[\r\n\t]+", " ", raw.strip()))
            except ValueError:
                continue
        r = _find_recipe(data)
        if r is not None:
            rec = from_jsonld(r, source)
            if rec["title"] and (rec["ingredients"] or rec["steps"]):
                return rec
    title = clean(p.meta.get("og:title") or p.title)
    if not title:
        raise ImportError_("That page doesn't have a recipe Simmer can read.")
    rec = blank()
    rec["title"] = title[:200]
    rec["source"] = source
    rec["image"] = _image(p.meta.get("og:image"))
    rec["notes"] = clean(p.meta.get("og:description") or p.meta.get("description"))[:2000]
    rec["partial"] = True
    return rec


# ---------------------------------------------------------------- pasted text
_ING_HEAD = re.compile(r"^\s*(ingredients?|you(?:'ll)? need|shopping list)\s*:?\s*$", re.I)
_STEP_HEAD = re.compile(r"^\s*(method|directions?|instructions?|steps?|preparation|how to make it)\s*:?\s*$", re.I)
_NOTE_HEAD = re.compile(r"^\s*(notes?|tips?)\s*:?\s*$", re.I)
_BULLET = re.compile(r"^\s*(?:[-*•·▢□◦]|\d+[.)]|step\s*\d+[:.)]?)\s*", re.I)


def from_text(text: str) -> dict:
    """A recipe from pasted text: the first line is the title; "Ingredients" and "Method" (or similar) start
    their sections. Without headings, lines that start with an amount are ingredients and the rest steps."""
    rec = blank()
    lines = [line.rstrip() for line in (text or "").replace("\r", "").split("\n")]
    lines = [line for line in lines if line.strip()]
    if not lines:
        raise ImportError_("Paste a recipe first.")
    rec["title"] = _BULLET.sub("", lines[0]).strip()[:200]
    section = ""
    notes: list[str] = []
    for line in lines[1:]:
        if _ING_HEAD.match(line):
            section = "ing"
            continue
        if _STEP_HEAD.match(line):
            section = "step"
            continue
        if _NOTE_HEAD.match(line):
            section = "note"
            continue
        body = _BULLET.sub("", line).strip()
        if not body:
            continue
        m = re.match(r"^(serves|servings|makes|yield)\s*:?\s*(\d+)", body, re.I)
        if m and not section:
            rec["servings"] = int(m.group(2))
            continue
        if section == "ing":
            rec["ingredients"].append(body)
        elif section == "step":
            rec["steps"].append(body)
        elif section == "note":
            notes.append(body)
        else:
            from .ingredients import parse
            if parse(body).amount is not None and len(body) < 90:
                rec["ingredients"].append(body)
            else:
                rec["steps"].append(body)
    rec["notes"] = "\n".join(notes)
    return rec


def to_text(rec: dict) -> str:
    """A recipe as plain text, the same shape from_text reads back (for sharing and copying)."""
    out = [rec.get("title") or "Recipe", ""]
    if rec.get("servings"):
        out += [f"Serves {rec['servings']}", ""]
    if rec.get("ingredients"):
        out.append("Ingredients")
        out += [f"- {i}" for i in rec["ingredients"]]
        out.append("")
    if rec.get("steps"):
        out.append("Method")
        n = 0
        for s in rec["steps"]:
            if s.startswith("## "):
                out.append(s[3:])
            else:
                n += 1
                out.append(f"{n}. {s}")
        out.append("")
    if rec.get("notes"):
        out += ["Notes", rec["notes"], ""]
    if rec.get("source"):
        out.append(f"From {rec['source']}")
    return "\n".join(out).strip() + "\n"
