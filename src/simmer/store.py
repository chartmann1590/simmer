# SPDX-License-Identifier: GPL-3.0-or-later
"""Simmer's library: recipes, the week's plan, the shopping list and the cooking log, in one SQLite file in
the app's own data folder. No GTK in here."""
from __future__ import annotations

import json
import os
import sqlite3
import threading
import time

SCHEMA = """
CREATE TABLE IF NOT EXISTS recipes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL,
    source TEXT DEFAULT '',
    image TEXT DEFAULT '',
    servings INTEGER DEFAULT 0,
    yield_text TEXT DEFAULT '',
    prep INTEGER DEFAULT 0,
    cook INTEGER DEFAULT 0,
    total INTEGER DEFAULT 0,
    ingredients TEXT DEFAULT '[]',
    steps TEXT DEFAULT '[]',
    notes TEXT DEFAULT '',
    tags TEXT DEFAULT '[]',
    author TEXT DEFAULT '',
    favorite INTEGER DEFAULT 0,
    rating INTEGER DEFAULT 0,
    created REAL,
    updated REAL,
    cooked INTEGER DEFAULT 0,
    last_cooked REAL DEFAULT 0,
    community_id TEXT DEFAULT '',
    shared_id TEXT DEFAULT ''
);
CREATE TABLE IF NOT EXISTS plan (
    day TEXT NOT NULL,
    recipe INTEGER NOT NULL REFERENCES recipes(id) ON DELETE CASCADE,
    servings INTEGER DEFAULT 0,
    PRIMARY KEY (day, recipe)
);
CREATE TABLE IF NOT EXISTS shopping (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    text TEXT NOT NULL,
    aisle TEXT DEFAULT 'Other',
    checked INTEGER DEFAULT 0,
    recipes TEXT DEFAULT '',
    created REAL
);
CREATE TABLE IF NOT EXISTS cooklog (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    recipe INTEGER NOT NULL REFERENCES recipes(id) ON DELETE CASCADE,
    at REAL NOT NULL,
    note TEXT DEFAULT ''
);
CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT);
"""
JSON_FIELDS = ("ingredients", "steps", "tags")
EDITABLE = ("title", "source", "image", "servings", "yield_text", "prep", "cook", "total", "ingredients", "steps",
            "notes", "tags", "author", "community_id")


class Store:
    def __init__(self, path: str):
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        self.path = path
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA foreign_keys = ON")
        self.db.execute("PRAGMA journal_mode = WAL")
        self.db.executescript(SCHEMA)
        self.lock = threading.RLock()

    # ---------------------------------------------------------------- settings
    def get(self, key: str, default=None):
        row = self.db.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
        if row is None:
            return default
        try:
            return json.loads(row[0])
        except ValueError:
            return default

    def set(self, key: str, value) -> None:
        with self.lock, self.db:
            self.db.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", (key, json.dumps(value)))

    # ---------------------------------------------------------------- recipes
    @staticmethod
    def _recipe(row) -> dict | None:
        if row is None:
            return None
        r = dict(row)
        for k in JSON_FIELDS:
            try:
                r[k] = json.loads(r[k] or "[]")
            except ValueError:
                r[k] = []
        return r

    def add(self, rec: dict) -> int:
        now = time.time()
        vals = {k: rec.get(k) for k in EDITABLE}
        vals["title"] = (vals["title"] or "Untitled recipe").strip()[:200]
        for k in JSON_FIELDS:
            vals[k] = json.dumps([str(x) for x in (vals[k] or [])])
        for k in ("servings", "prep", "cook", "total"):
            vals[k] = int(vals[k] or 0)
        for k in ("source", "image", "yield_text", "notes", "author", "community_id"):
            vals[k] = str(vals[k] or "")
        with self.lock, self.db:
            cur = self.db.execute(
                f"INSERT INTO recipes ({', '.join(EDITABLE)}, created, updated) VALUES "
                f"({', '.join('?' for _ in EDITABLE)}, ?, ?)", [vals[k] for k in EDITABLE] + [now, now])
            return cur.lastrowid

    def update(self, rid: int, **fields) -> None:
        sets, vals = [], []
        for k, v in fields.items():
            if k not in EDITABLE + ("favorite", "rating", "shared_id"):
                raise KeyError(k)
            if k in JSON_FIELDS:
                v = json.dumps([str(x) for x in (v or [])])
            sets.append(f"{k} = ?")
            vals.append(v)
        if not sets:
            return
        with self.lock, self.db:
            self.db.execute(f"UPDATE recipes SET {', '.join(sets)}, updated = ? WHERE id = ?", vals + [time.time(), rid])

    def recipe(self, rid: int) -> dict | None:
        return self._recipe(self.db.execute("SELECT * FROM recipes WHERE id = ?", (rid,)).fetchone())

    def recipes(self, *, search: str = "", tag: str = "", favorites: bool = False, order: str = "recent") -> list[dict]:
        where, args = [], []
        if search:
            for word in search.lower().split():
                where.append("(lower(title) LIKE ? OR lower(ingredients) LIKE ? OR lower(tags) LIKE ?)")
                args += [f"%{word}%"] * 3
        if tag:
            where.append("lower(tags) LIKE ?")
            args.append(f'%"{tag.lower()}"%')
        if favorites:
            where.append("favorite = 1")
        orders = {"recent": "updated DESC", "title": "lower(title)", "cooked": "cooked DESC, last_cooked DESC",
                  "quick": "CASE WHEN total = 0 THEN 99999 ELSE total END, lower(title)"}
        sql = "SELECT * FROM recipes" + (" WHERE " + " AND ".join(where) if where else "") + \
            f" ORDER BY {orders.get(order, orders['recent'])}"
        return [self._recipe(r) for r in self.db.execute(sql, args)]

    def delete(self, rid: int) -> dict | None:
        """Delete and return everything about it, so it can be put back (undo)."""
        rec = self.recipe(rid)
        if rec is None:
            return None
        rec["_plan"] = [dict(r) for r in self.db.execute("SELECT * FROM plan WHERE recipe = ?", (rid,))]
        rec["_log"] = [dict(r) for r in self.db.execute("SELECT * FROM cooklog WHERE recipe = ?", (rid,))]
        with self.lock, self.db:
            self.db.execute("DELETE FROM recipes WHERE id = ?", (rid,))
        return rec

    def restore(self, rec: dict) -> None:
        cols = [k for k in rec if not k.startswith("_")]
        vals = [json.dumps(rec[k]) if k in JSON_FIELDS else rec[k] for k in cols]
        with self.lock, self.db:
            self.db.execute(f"INSERT INTO recipes ({', '.join(cols)}) VALUES ({', '.join('?' for _ in cols)})", vals)
            for p in rec.get("_plan", []):
                self.db.execute("INSERT OR IGNORE INTO plan (day, recipe, servings) VALUES (?, ?, ?)",
                                (p["day"], p["recipe"], p["servings"]))
            for e in rec.get("_log", []):
                self.db.execute("INSERT INTO cooklog (recipe, at, note) VALUES (?, ?, ?)", (e["recipe"], e["at"], e["note"]))

    def tags(self) -> list[tuple[str, int]]:
        counts: dict[str, int] = {}
        names: dict[str, str] = {}
        for (raw,) in self.db.execute("SELECT tags FROM recipes"):
            try:
                for t in json.loads(raw or "[]"):
                    k = t.lower()
                    counts[k] = counts.get(k, 0) + 1
                    names.setdefault(k, t)
            except ValueError:
                pass
        return sorted(((names[k], n) for k, n in counts.items()), key=lambda p: (-p[1], p[0].lower()))

    def counts(self) -> dict:
        one = lambda sql: self.db.execute(sql).fetchone()[0]  # noqa: E731
        return {"recipes": one("SELECT COUNT(*) FROM recipes"),
                "favorites": one("SELECT COUNT(*) FROM recipes WHERE favorite = 1"),
                "shopping": one("SELECT COUNT(*) FROM shopping WHERE checked = 0"),
                "planned": one("SELECT COUNT(*) FROM plan WHERE day >= date('now', 'localtime')")}

    # ---------------------------------------------------------------- cooking log
    def cooked(self, rid: int, note: str = "") -> None:
        now = time.time()
        with self.lock, self.db:
            self.db.execute("INSERT INTO cooklog (recipe, at, note) VALUES (?, ?, ?)", (rid, now, note))
            self.db.execute("UPDATE recipes SET cooked = cooked + 1, last_cooked = ? WHERE id = ?", (now, rid))

    def log(self, rid: int | None = None, limit: int = 200) -> list[dict]:
        sql = ("SELECT cooklog.*, recipes.title, recipes.image FROM cooklog JOIN recipes ON recipes.id = cooklog.recipe"
               + (" WHERE cooklog.recipe = ?" if rid else "") + " ORDER BY at DESC LIMIT ?")
        return [dict(r) for r in self.db.execute(sql, ((rid, limit) if rid else (limit,)))]

    # ---------------------------------------------------------------- the plan
    def plan(self, first: str, last: str) -> dict[str, list[dict]]:
        out: dict[str, list[dict]] = {}
        for row in self.db.execute("SELECT plan.day, plan.servings AS planned, recipes.* FROM plan JOIN recipes "
                                   "ON recipes.id = plan.recipe WHERE day BETWEEN ? AND ? ORDER BY day, lower(title)",
                                   (first, last)):
            r = self._recipe(row)
            out.setdefault(r.pop("day"), []).append(r)
        return out

    def plan_add(self, day: str, rid: int, servings: int = 0) -> None:
        with self.lock, self.db:
            self.db.execute("INSERT OR REPLACE INTO plan (day, recipe, servings) VALUES (?, ?, ?)", (day, rid, servings))

    def plan_remove(self, day: str, rid: int) -> None:
        with self.lock, self.db:
            self.db.execute("DELETE FROM plan WHERE day = ? AND recipe = ?", (day, rid))

    # ---------------------------------------------------------------- shopping
    def shopping(self) -> list[dict]:
        return [dict(r) for r in self.db.execute("SELECT * FROM shopping ORDER BY checked, id")]

    def shop_add(self, text: str, aisle: str = "Other", recipes: str = "") -> int:
        with self.lock, self.db:
            return self.db.execute("INSERT INTO shopping (text, aisle, recipes, created) VALUES (?, ?, ?, ?)",
                                   (text.strip()[:300], aisle, recipes[:300], time.time())).lastrowid

    def shop_check(self, sid: int, checked: bool) -> None:
        with self.lock, self.db:
            self.db.execute("UPDATE shopping SET checked = ? WHERE id = ?", (1 if checked else 0, sid))

    def shop_remove(self, sid: int) -> None:
        with self.lock, self.db:
            self.db.execute("DELETE FROM shopping WHERE id = ?", (sid,))

    def shop_clear(self, checked_only: bool = True) -> list[dict]:
        rows = [r for r in self.shopping() if r["checked"] or not checked_only]
        with self.lock, self.db:
            self.db.execute("DELETE FROM shopping" + (" WHERE checked = 1" if checked_only else ""))
        return rows

    def shop_restore(self, rows: list[dict]) -> None:
        with self.lock, self.db:
            for r in rows:
                self.db.execute("INSERT INTO shopping (text, aisle, checked, recipes, created) VALUES (?, ?, ?, ?, ?)",
                                (r["text"], r["aisle"], r["checked"], r["recipes"], r["created"]))

    # ---------------------------------------------------------------- backup
    def export(self) -> dict:
        return {"app": "simmer", "version": 1, "exported": time.time(),
                "recipes": [{k: v for k, v in r.items() if k != "id"} | {"_id": r["id"]} for r in self.recipes()],
                "plan": [dict(r) for r in self.db.execute("SELECT * FROM plan")],
                "shopping": self.shopping(),
                "cooklog": [dict(r) for r in self.db.execute("SELECT * FROM cooklog")]}

    def import_(self, doc: dict) -> int:
        """Add the recipes from a Simmer backup that aren't here yet (same title and source). Returns how many."""
        if not isinstance(doc, dict) or doc.get("app") != "simmer":
            raise ValueError("That isn't a Simmer backup.")
        have = {(r["title"].lower(), r["source"]) for r in self.recipes()}
        ids: dict = {}
        added = 0
        for r in doc.get("recipes") or []:
            if not isinstance(r, dict) or not r.get("title"):
                continue
            key = (str(r["title"]).lower(), str(r.get("source") or ""))
            if key in have:
                continue
            rid = self.add(r)
            if r.get("favorite") or r.get("rating"):
                self.update(rid, favorite=int(bool(r.get("favorite"))), rating=int(r.get("rating") or 0))
            ids[r.get("_id")] = rid
            have.add(key)
            added += 1
        for p in doc.get("plan") or []:
            if p.get("recipe") in ids:
                self.plan_add(p["day"], ids[p["recipe"]], int(p.get("servings") or 0))
        return added
