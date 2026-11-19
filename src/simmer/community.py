# SPDX-License-Identifier: GPL-3.0-or-later
"""The Simmer community: browse recipes other people shared, save them, share your own, report problems.

Everything shared is text only (no pictures, no links) and is checked automatically by the server before
anyone else can see it: a word filter, a "is this really a recipe" check and an AI safety model. A recipe that
three different people report is hidden. No accounts: the app makes a random device id once, and the server
keeps only a salted hash of it. No GTK in here.
"""
from __future__ import annotations

import json
import os
import ssl
import urllib.error
import urllib.parse
import urllib.request
import uuid

BASE_URL = os.environ.get("SIMMER_COMMUNITY_URL", "https://simmer-community.charles-h-hartmann1.workers.dev")
TIMEOUT = 20
_CTX = ssl.create_default_context()
REPORT_REASONS = (("spam", "Spam or advertising"), ("offensive", "Hateful or offensive"),
                  ("unsafe", "Unsafe food advice"), ("copied", "Copied from someone else"), ("other", "Something else"))


class CommunityError(Exception):
    def __init__(self, message: str, code: str = ""):
        super().__init__(message)
        self.code = code


def _request(method: str, path: str, body: dict | None = None) -> dict:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(BASE_URL + path, data=data, method=method,
                                 headers={"Content-Type": "application/json", "Accept": "application/json",
                                          "User-Agent": "Simmer-Luma/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT, context=_CTX) as resp:
            raw, status = resp.read(2 * 1024 * 1024), resp.status
    except urllib.error.HTTPError as ex:
        raw, status = ex.read(64 * 1024), ex.code
    except (urllib.error.URLError, TimeoutError, OSError):
        raise CommunityError("Couldn't reach the Simmer community. Check your connection.", "offline") from None
    try:
        doc = json.loads(raw.decode("utf-8", "replace") or "{}")
    except ValueError:
        doc = {}
    if status >= 400:
        raise CommunityError(str(doc.get("error") or f"The community server answered with an error ({status})."),
                             str(doc.get("code") or status))
    return doc


class Community:
    """`state_path` keeps this device's id and the delete keys of recipes shared from here."""

    def __init__(self, state_path: str):
        self.path = state_path
        try:
            with open(state_path, encoding="utf-8") as f:
                self.state = json.load(f)
        except (OSError, ValueError):
            self.state = {}
        if not self.state.get("device"):
            self.state["device"] = str(uuid.uuid4())
            self._save()

    def _save(self):
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        tmp = self.path + ".part"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self.state, f)
        os.chmod(tmp, 0o600)
        os.replace(tmp, self.path)

    @property
    def device(self) -> str:
        return self.state["device"]

    @property
    def name(self) -> str:
        return self.state.get("name", "")

    def set_name(self, name: str):
        self.state["name"] = name.strip()[:40]
        self._save()

    def mine(self) -> dict:
        """{community id: title} for recipes shared from this device."""
        return {k: v.get("title", "") for k, v in (self.state.get("shared") or {}).items()}

    def is_mine(self, cid: str) -> bool:
        return cid in (self.state.get("shared") or {})

    # ---------------------------------------------------------------- blocking calls (run on a worker)
    def browse(self, q: str = "", tag: str = "", sort: str = "new", before: int = 0) -> dict:
        params = {k: v for k, v in (("q", q), ("tag", tag), ("sort", sort), ("before", before or "")) if v}
        return _request("GET", "/v1/recipes" + ("?" + urllib.parse.urlencode(params) if params else ""))

    def get(self, cid: str) -> dict:
        return _request("GET", f"/v1/recipes/{urllib.parse.quote(cid)}")["recipe"]

    def share(self, rec: dict, author: str) -> dict:
        payload = {k: rec.get(k) for k in ("title", "servings", "prep", "cook", "total", "ingredients", "steps",
                                            "notes", "tags")}
        payload["author"] = author
        doc = _request("POST", "/v1/recipes", {"device": self.device, "recipe": payload})
        self.state.setdefault("shared", {})[doc["id"]] = {"key": doc["key"], "title": payload["title"]}
        self._save()
        return doc

    def unshare(self, cid: str) -> None:
        entry = (self.state.get("shared") or {}).get(cid)
        if not entry:
            raise CommunityError("Only recipes shared from this device can be taken down here.", "forbidden")
        try:
            _request("DELETE", f"/v1/recipes/{urllib.parse.quote(cid)}", {"device": self.device, "key": entry["key"]})
        except CommunityError as ex:
            if ex.code != "missing":
                raise
        self.state["shared"].pop(cid, None)
        self._save()

    def save(self, cid: str) -> None:
        try:
            _request("POST", f"/v1/recipes/{urllib.parse.quote(cid)}/save", {"device": self.device})
        except CommunityError:
            pass   # a save count is a nicety; never stop someone keeping a recipe

    def report(self, cid: str, reason: str) -> None:
        _request("POST", f"/v1/recipes/{urllib.parse.quote(cid)}/report", {"device": self.device, "reason": reason})


def shareable(rec: dict) -> tuple[bool, str]:
    """Can this recipe go to the community? Imported ones belong to their site's author, so only recipes
    written here (or saved from the community and changed) can be shared."""
    if rec.get("source"):
        return False, "Recipes imported from a website belong to their author, so they can't be shared to the community."
    if rec.get("community_id"):
        return False, "This recipe came from the community already."
    if not rec.get("ingredients") or not rec.get("steps"):
        return False, "Add ingredients and steps before sharing."
    return True, ""
