# SPDX-License-Identifier: GPL-3.0-or-later
"""App updates for Hartmann Studios Luma apps, watched from the app's GitHub releases. No GTK in here.

Two kinds of build, decided when the package is built (never at run time):

* **Depot** (the default manifest): Depot delivers updates. Luma's own updater (Flatpak's update portal, wired into
  every LumaUI window as "App updates") installs them. This module only fetches the release notes to show.
* **GitHub** (`flatpak/github/<app id>.json` writes `channel` = `github` into the package): the person installed a
  bundle from GitHub, so the app downloads the next bundle for this device, checks it against the release's
  SHA256SUMS, and hands the file to the system's installer, which asks the person to confirm. The app never
  installs, replaces or runs anything itself (Luma review 1.3 and 1.4).

Requests go to GitHub only: the releases API (at most every 12 hours, with an ETag so an unchanged answer is cheap)
and, when the person chooses to download, the release's files.
"""
from __future__ import annotations

import hashlib
import html
import json
import os
import platform
import re
import ssl
import threading
import time
import urllib.error
import urllib.request

API = os.environ.get("HS_UPDATES_API", "https://api.github.com")
CHECK_EVERY = 12 * 3600
TIMEOUT = 20
ALLOWED_DOWNLOAD_HOSTS = ("github.com", "objects.githubusercontent.com", "release-assets.githubusercontent.com")
DOWNLOAD_PREFIX = "https://github.com/"   # release files must come from GitHub (tests point both at a local fake)
MAX_BUNDLE = 200 * 1024 * 1024
_CTX = ssl.create_default_context()


class UpdateError(Exception):
    """Something the person should read."""


def parse_version(text: str) -> tuple[int, ...]:
    """"v1.2.0" → (1, 2, 0); anything unreadable → ()."""
    m = re.match(r"^v?(\d+(?:\.\d+)*)", (text or "").strip())
    return tuple(int(x) for x in m.group(1).split(".")) if m else ()


def newer(candidate: str, current: str) -> bool:
    a, b = parse_version(candidate), parse_version(current)
    if not a or not b:
        return False
    n = max(len(a), len(b))
    return a + (0,) * (n - len(a)) > b + (0,) * (n - len(b))


def device_arch() -> str:
    m = platform.machine().lower()
    return {"amd64": "x86_64", "x64": "x86_64", "arm64": "aarch64", "armv8": "aarch64"}.get(m, m)


def read_channel(app_id: str) -> str:
    """"github" when this package was built from the GitHub manifest, else "depot"."""
    forced = os.environ.get("HS_CHANNEL")
    if forced in ("github", "depot"):
        return forced
    try:
        with open(f"/app/share/{app_id}/channel", encoding="utf-8") as f:
            return "github" if f.read().strip() == "github" else "depot"
    except OSError:
        return "depot"


def _get(url: str, headers: dict, limit: int = 2 * 1024 * 1024, user_agent: str = "HartmannStudios-Luma-Updates/1.0"):
    req = urllib.request.Request(url, headers={"User-Agent": user_agent, **headers})
    try:
        resp = urllib.request.urlopen(req, timeout=TIMEOUT, context=_CTX)
    except urllib.error.HTTPError as ex:
        if ex.code == 304:
            return 304, b"", {}
        if ex.code in (403, 429):
            raise UpdateError("GitHub asked us to slow down. Try again later.") from None
        if ex.code == 404:
            raise UpdateError("No releases are published yet.") from None
        raise UpdateError(f"GitHub answered with an error ({ex.code}).") from None
    except (urllib.error.URLError, TimeoutError, OSError):
        raise UpdateError("Couldn't reach GitHub. Check your connection and try again.") from None
    with resp:
        data = resp.read(limit + 1)
        if len(data) > limit:
            raise UpdateError("The answer was too large.")
        return resp.status, data, dict(resp.headers)


class Release(dict):
    """version, name, notes (Markdown), url, published (ISO), assets {name: (url, size)}."""

    @property
    def version(self) -> str:
        return self["version"]


def _release(doc: dict) -> Release | None:
    if not isinstance(doc, dict) or doc.get("draft") or doc.get("prerelease"):
        return None
    version = (doc.get("tag_name") or "").lstrip("v")
    if not parse_version(version):
        return None
    assets = {}
    for a in doc.get("assets") or []:
        url = a.get("browser_download_url") or ""
        if url.startswith(DOWNLOAD_PREFIX):
            assets[a.get("name") or ""] = (url, int(a.get("size") or 0))
    url = doc.get("html_url") or ""
    return Release(version=version, name=doc.get("name") or f"Version {version}", notes=(doc.get("body") or "")[:20000],
                   url=url if url.startswith(DOWNLOAD_PREFIX) else "", published=doc.get("published_at") or "",
                   assets=assets)


class Updates:
    def __init__(self, app_id: str, repo: str, current: str, state_path: str, cache_dir: str):
        self.app_id, self.repo, self.current = app_id, repo, current
        self.state_path, self.cache_dir = state_path, cache_dir
        self.channel = read_channel(app_id)
        self.arch = device_arch()
        self.state = self._load()
        self._lock = threading.Lock()

    # ---------------------------------------------------------------- settings and memory
    def _load(self) -> dict:
        try:
            with open(self.state_path, encoding="utf-8") as f:
                d = json.load(f)
            return d if isinstance(d, dict) else {}
        except (OSError, ValueError):
            return {}

    def _save(self) -> None:
        os.makedirs(os.path.dirname(self.state_path), exist_ok=True)
        tmp = self.state_path + ".part"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self.state, f)
        os.replace(tmp, self.state_path)

    @property
    def automatic(self) -> bool:
        return self.state.get("automatic", True)

    def set_automatic(self, on: bool) -> None:
        self.state["automatic"] = bool(on)
        self._save()

    def due(self) -> bool:
        return self.automatic and time.time() - float(self.state.get("checked") or 0) >= CHECK_EVERY

    def skip(self, version: str) -> None:
        self.state["skipped"] = version
        self._save()

    def skipped(self, version: str) -> bool:
        return self.state.get("skipped") == version

    # ---------------------------------------------------------------- checking (blocking: run on a worker)
    def latest(self) -> Release | None:
        """The latest published release (cached by ETag), or None when there is none."""
        headers = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
        if self.state.get("etag") and self.state.get("release"):
            headers["If-None-Match"] = self.state["etag"]
        status, data, info = _get(f"{API}/repos/{self.repo}/releases/latest", headers)
        with self._lock:
            self.state["checked"] = time.time()
            if status != 304:
                try:
                    doc = json.loads(data.decode("utf-8", "replace") or "{}")
                except ValueError:
                    raise UpdateError("GitHub's answer couldn't be read.") from None
                rel = _release(doc)
                self.state["release"] = dict(rel) if rel else None
                self.state["etag"] = info.get("ETag") or info.get("Etag") or ""
            self._save()
        r = self.state.get("release")
        return Release(r) if r else None

    def check(self) -> Release | None:
        """A release newer than the running version, or None."""
        r = self.latest()
        return r if r and newer(r.version, self.current) else None

    # ---------------------------------------------------------------- the GitHub build: download and verify
    def bundle_name(self, release: Release) -> str:
        return f"{self.app_id}-{release.version}-{self.arch}.flatpak"

    def bundle(self, release: Release) -> tuple[str, int] | None:
        return release["assets"].get(self.bundle_name(release)) and tuple(release["assets"][self.bundle_name(release)])

    def expected_sha256(self, release: Release) -> str:
        sums = release["assets"].get("SHA256SUMS")
        if not sums:
            raise UpdateError("This release has no checksums, so it can't be verified.")
        _status, data, _ = _get(sums[0], {"Accept": "application/octet-stream"}, limit=64 * 1024)
        name = self.bundle_name(release)
        for line in data.decode("utf-8", "replace").splitlines():
            parts = line.strip().split()
            if len(parts) == 2 and parts[1].lstrip("*") == name and re.fullmatch(r"[0-9a-f]{64}", parts[0]):
                return parts[0]
        raise UpdateError("The checksums don't list the file for this device.")

    def download(self, release: Release, progress=lambda done, total: None, cancel: threading.Event | None = None) -> str:
        """Download this device's bundle into the app's cache and verify it. Returns its path ("" if cancelled)."""
        if self.channel != "github":
            raise UpdateError("Updates for this copy come through Depot.")
        found = self.bundle(release)
        if not found:
            raise UpdateError(f"There's no download for this device ({self.arch}) in {release.version}.")
        url, size = found
        want = self.expected_sha256(release)
        os.makedirs(self.cache_dir, exist_ok=True)
        path = os.path.join(self.cache_dir, self.bundle_name(release))
        tmp = path + ".part"
        req = urllib.request.Request(url, headers={"User-Agent": "HartmannStudios-Luma-Updates/1.0",
                                                   "Accept": "application/octet-stream"})
        h = hashlib.sha256()
        done = 0
        try:
            with urllib.request.urlopen(req, timeout=60, context=_CTX) as resp:
                host = urllib.request.urlparse(resp.geturl()).hostname or ""
                if not any(host == a or host.endswith("." + a) for a in ALLOWED_DOWNLOAD_HOSTS):
                    raise UpdateError("The download came from an unexpected place, so it was stopped.")
                total = int(resp.headers.get("Content-Length") or size or 0)
                if total > MAX_BUNDLE:
                    raise UpdateError("That file is larger than an update should be.")
                with open(tmp, "wb") as f:
                    while True:
                        if cancel is not None and cancel.is_set():
                            break
                        chunk = resp.read(256 * 1024)
                        if not chunk:
                            break
                        done += len(chunk)
                        if done > MAX_BUNDLE:
                            raise UpdateError("That file is larger than an update should be.")
                        h.update(chunk)
                        f.write(chunk)
                        progress(done, total)
        except UpdateError:
            _unlink(tmp)
            raise
        except (urllib.error.URLError, TimeoutError, OSError):
            _unlink(tmp)
            raise UpdateError("The download stopped. Check your connection and try again.") from None
        if cancel is not None and cancel.is_set():
            _unlink(tmp)
            return ""
        if h.hexdigest() != want:
            _unlink(tmp)
            raise UpdateError("The download didn't match its checksum, so it was thrown away.")
        os.replace(tmp, path)
        return path


def _unlink(path: str) -> None:
    try:
        os.remove(path)
    except OSError:
        pass


def notes_markup(markdown: str) -> str:
    """Release notes (GitHub Markdown) as safe Pango markup: headings, bullets, bold, inline code and links."""
    out = []
    in_code = False
    for raw in (markdown or "").replace("\r\n", "\n").split("\n"):
        line = raw.rstrip()
        if line.strip().startswith("```"):
            in_code = not in_code
            continue
        if in_code:
            out.append("<tt>" + html.escape(line, quote=False) + "</tt>")
            continue
        line = re.sub(r"<[^>]{1,200}>", "", line)       # HTML in release notes: keep the words, drop the tags
        if not line.strip():
            if out and out[-1] != "":
                out.append("")
            continue
        if re.match(r"^\s*\|", line):          # tables: keep as plain text rows
            cells = [c.strip() for c in line.strip().strip("|").split("|")]
            if all(re.fullmatch(r":?-{2,}:?", c) for c in cells if c):
                continue
            out.append(_inline("  ·  ".join(c for c in cells if c)))
            continue
        m = re.match(r"^\s*(#{1,6})\s+(.*)$", line)
        if m:
            out.append(f"<b>{_inline(m.group(2))}</b>")
            continue
        m = re.match(r"^(\s*)[-*+]\s+(.*)$", line)
        if m:
            out.append(("    " if len(m.group(1)) >= 2 else "") + "•  " + _inline(m.group(2)))
            continue
        m = re.match(r"^\s*>\s?(.*)$", line)
        if m:
            out.append(f"<i>{_inline(m.group(1))}</i>")
            continue
        out.append(_inline(line.strip()))
    while out and out[-1] == "":
        out.pop()
    return "\n".join(out)


def _inline(text: str) -> str:
    """Escape, then bring back **bold**, `code` and [links](https://…) only."""
    parts, pos = [], 0
    for m in re.finditer(r"\[([^\]]+)\]\((https?://[^)\s]+)\)|\*\*([^*]+)\*\*|`([^`]+)`", text):
        parts.append(html.escape(text[pos:m.start()], quote=False))
        if m.group(1):
            parts.append(f'<a href="{html.escape(m.group(2), quote=True)}">{html.escape(m.group(1), quote=False)}</a>')
        elif m.group(3):
            parts.append(f"<b>{html.escape(m.group(3), quote=False)}</b>")
        else:
            parts.append(f"<tt>{html.escape(m.group(4), quote=False)}</tt>")
        pos = m.end()
    parts.append(html.escape(text[pos:], quote=False))
    return "".join(parts)
