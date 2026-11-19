# SPDX-License-Identifier: GPL-3.0-or-later
"""Everything that touches the network, as plain blocking functions run on worker threads.

TLS certificates are always checked. Every read is bounded. Simmer contacts only the recipe pages you ask it
to import (and their pictures), the Simmer community server when you browse or share there, the Hartmann
Studios license server when you buy or restore Simmer Plus, and GitHub to check for a new version.
"""
from __future__ import annotations

import hashlib
import os
import ssl
import urllib.error
import urllib.request

from . import importer

USER_AGENT = "Mozilla/5.0 (compatible; Simmer/1.0; Luma recipe app; +https://github.com/chartmann1590/simmer)"
TIMEOUT = 25
PAGE_LIMIT = 6 * 1024 * 1024
IMAGE_LIMIT = 8 * 1024 * 1024
_CTX = ssl.create_default_context()


class NetError(Exception):
    """A network failure; the message is safe to show."""


def _http_message(code: int) -> str:
    return {404: "That page isn't there (404).", 410: "That page has been taken down (410).",
            401: "That page needs a sign-in.", 403: "The site refused to share that page (403).",
            429: "The site says too many requests. Try again in a minute."}.get(
        code, f"The site answered with an error ({code}).")


def get_bytes(url: str, limit: int, accept: str = "*/*") -> tuple[bytes, str]:
    """(body, final address)."""
    if not url.lower().startswith(("https://", "http://")):
        raise NetError("Only web addresses can be opened.")
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": accept,
                                               "Accept-Encoding": "identity", "Accept-Language": "en"})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT, context=_CTX) as resp:
            data = resp.read(limit + 1)
            final = resp.geturl()
    except urllib.error.HTTPError as ex:
        raise NetError(_http_message(ex.code)) from None
    except urllib.error.URLError as ex:
        if isinstance(getattr(ex, "reason", None), ssl.SSLError):
            raise NetError("The site's security certificate couldn't be checked.") from None
        raise NetError("Couldn't reach the site. Check your connection.") from None
    except (TimeoutError, OSError):
        raise NetError("The site took too long to answer.") from None
    if len(data) > limit:
        raise NetError("That page is too large.")
    return data, final


def fetch_recipe(url: str) -> dict:
    data, final = get_bytes(url, PAGE_LIMIT, "text/html,application/xhtml+xml")
    text = data.decode("utf-8", "replace")
    try:
        return importer.from_html(text, final)
    except importer.ImportError_ as ex:
        raise NetError(str(ex)) from None


def cache_dir() -> str:
    base = os.environ.get("XDG_CACHE_HOME") or os.path.join(os.path.expanduser("~"), ".cache")
    return os.path.join(base, "simmer")


def picture_path(url: str) -> str:
    return os.path.join(cache_dir(), "pictures", hashlib.sha1(url.encode()).hexdigest()[:24] + ".img")


def fetch_picture(url: str) -> str:
    """Download a picture once into the cache; its path, or "" when it can't be had."""
    if not url:
        return ""
    path = picture_path(url)
    if os.path.exists(path) and os.path.getsize(path) > 0:
        return path
    try:
        data, _ = get_bytes(url, IMAGE_LIMIT, "image/*")
    except NetError:
        return ""
    if not (data[:3] == b"\xff\xd8\xff" or data[:8] == b"\x89PNG\r\n\x1a\n" or data[:4] == b"RIFF"
            or data[:6] in (b"GIF87a", b"GIF89a")):
        return ""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".part"
    with open(tmp, "wb") as f:
        f.write(data)
    os.replace(tmp, path)
    return path
