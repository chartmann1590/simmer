# SPDX-License-Identifier: GPL-3.0-or-later
"""Kitchen timers: finding "simmer for 20 minutes" in a step, and keeping several timers running at once.

No GTK in here. The app ticks `Timers` once a second and is told when one rings.
"""
from __future__ import annotations

import re
import time
from dataclasses import dataclass

_NUM = r"(\d+\s+\d+/\d+|\d+/\d+|\d+(?:[.,]\d+)?|½|¼|¾|an?|one|two|three|four|five|six|eight|ten|fifteen|twenty|thirty|forty|forty-five|ninety)"
_WORDS = {"a": 1, "an": 1, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "eight": 8, "ten": 10,
          "fifteen": 15, "twenty": 20, "thirty": 30, "forty": 40, "forty-five": 45, "ninety": 90,
          "½": 0.5, "¼": 0.25, "¾": 0.75}
_UNIT = r"(?:\s*(hours?|hrs?|minutes?|mins?|seconds?|secs?)|(h|m|s))\b"   # "10 minutes", "10m"; never "2 s"
_PART = rf"{_NUM}(?:\s*(?:-|–|to|or)\s*{_NUM})?{_UNIT}"
DURATION_RE = re.compile(rf"\b{_PART}(?:,?\s*(?:and\s*)?{_PART})?", re.I)


def _n(word: str) -> float:
    w = word.lower().replace(",", ".").strip()
    if w in _WORDS:
        return float(_WORDS[w])
    if "/" in w:
        whole, _, frac = w.rpartition(" ")
        a, b = frac.split("/")
        return (float(whole) if whole else 0.0) + float(a) / float(b)
    return float(w)


def _secs(n: float, unit: str) -> float:
    u = unit.lower()
    if u.startswith("h"):
        return n * 3600
    if u.startswith("m"):
        return n * 60
    return n


@dataclass
class Found:
    seconds: int
    start: int
    end: int
    text: str


def durations(text: str) -> list[Found]:
    """Every time span in a step. A range ("8–10 minutes") times the longer end: check early, never late."""
    out = []
    for m in DURATION_RE.finditer(text or ""):
        g = m.groups()   # n1, n1b, long1, short1, n2, n2b, long2, short2
        try:
            total = _secs(_n(g[1] or g[0]), g[2] or g[3])
            if g[4]:
                total += _secs(_n(g[5] or g[4]), g[6] or g[7])
        except (ValueError, KeyError):
            continue
        if 5 <= total <= 48 * 3600:
            out.append(Found(int(total), m.start(), m.end(), m.group(0)))
    return out


def clock(seconds: float) -> str:
    s = max(0, int(round(seconds)))
    h, rem = divmod(s, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def words(seconds: float) -> str:
    s = int(seconds)
    h, rem = divmod(s, 3600)
    m, s = divmod(rem, 60)
    parts = []
    if h:
        parts.append(f"{h} h")
    if m:
        parts.append(f"{m} min")
    if s and not h:
        parts.append(f"{s} s")
    return " ".join(parts) or "0 s"


@dataclass
class Timer:
    id: int
    label: str
    seconds: float              # the full length
    ends: float = 0.0           # monotonic time it rings (running)
    left: float = 0.0           # seconds left (paused)
    rang: bool = False
    recipe: str = ""

    @property
    def running(self) -> bool:
        return self.ends > 0 and not self.rang

    def remaining(self, now: float | None = None) -> float:
        if self.rang:
            return 0.0
        if self.ends:
            return max(0.0, self.ends - (now if now is not None else time.monotonic()))
        return self.left


class Timers:
    """Several timers at once. `tick()` returns the timers that just rang."""

    def __init__(self, clock_fn=time.monotonic):
        self.now = clock_fn
        self.items: list[Timer] = []
        self._next = 1

    def start(self, label: str, seconds: float, recipe: str = "") -> Timer:
        t = Timer(self._next, label, float(seconds), ends=self.now() + float(seconds), recipe=recipe)
        self._next += 1
        self.items.append(t)
        return t

    def get(self, tid: int) -> Timer | None:
        return next((t for t in self.items if t.id == tid), None)

    def pause(self, tid: int):
        t = self.get(tid)
        if t and t.running:
            t.left, t.ends = t.remaining(self.now()), 0.0

    def resume(self, tid: int):
        t = self.get(tid)
        if t and not t.running and not t.rang and t.left > 0:
            t.ends, t.left = self.now() + t.left, 0.0

    def add(self, tid: int, seconds: float):
        t = self.get(tid)
        if t is None:
            return
        if t.rang:
            t.rang, t.ends, t.left = False, self.now() + seconds, 0.0
        elif t.ends:
            t.ends += seconds
        else:
            t.left += seconds
        t.seconds += seconds

    def cancel(self, tid: int):
        self.items = [t for t in self.items if t.id != tid]

    def tick(self) -> list[Timer]:
        now = self.now()
        rang = []
        for t in self.items:
            if t.ends and not t.rang and now >= t.ends:
                t.rang = True
                rang.append(t)
        return rang

    @property
    def ringing(self) -> list[Timer]:
        return [t for t in self.items if t.rang]

    @property
    def active(self) -> bool:
        return any(t.running for t in self.items)
