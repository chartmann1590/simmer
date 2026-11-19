# SPDX-License-Identifier: GPL-3.0-or-later
"""App updates against a LOCAL FAKE GitHub (releases API + release files). No network."""
import hashlib
import json
import os
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
PKG = next(p for p in ("earshot", "hoffice", "simmer") if os.path.isdir(os.path.join(os.path.dirname(__file__), "..", "src", p)))
hs = __import__(f"{PKG}.hsupdates", fromlist=["x"])
APP_ID = {"earshot": "com.charleshartmann.Earshot", "hoffice": "com.charleshartmann.Office",
          "simmer": "com.charleshartmann.Simmer"}[PKG]
BUNDLE = b"flatpak bundle bytes " * 5000
STATE = {"requests": 0, "not_modified": 0, "tag": "v1.2.0", "draft": False, "sums_ok": True}


class GitHub(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, body=b"", ctype="application/json", headers=None):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        base = f"http://127.0.0.1:{self.server.server_port}"
        arch = hs.device_arch()
        name = f"{APP_ID}-{STATE['tag'].lstrip('v')}-{arch}.flatpak"
        if self.path.endswith("/releases/latest"):
            STATE["requests"] += 1
            etag = f'"{STATE["tag"]}"'
            if self.headers.get("If-None-Match") == etag:
                STATE["not_modified"] += 1
                return self._send(304)
            doc = {"tag_name": STATE["tag"], "name": f"Release {STATE['tag']}", "draft": STATE["draft"], "prerelease": False,
                   "body": "## What's new\n- **Faster** start\n- See [notes](https://example.com/n)\n\n<script>x</script>",
                   "html_url": f"{base}/releases/tag/{STATE['tag']}", "published_at": "2026-11-20T10:00:00Z",
                   "assets": [{"name": name, "size": len(BUNDLE), "browser_download_url": f"{base}/dl/{name}"},
                              {"name": "SHA256SUMS", "size": 100, "browser_download_url": f"{base}/dl/SHA256SUMS"}]}
            return self._send(200, json.dumps(doc).encode(), headers={"ETag": etag})
        if self.path == "/dl/SHA256SUMS":
            digest = hashlib.sha256(BUNDLE if STATE["sums_ok"] else b"other").hexdigest()
            return self._send(200, f"{digest}  {name}\n".encode(), "text/plain")
        if self.path.startswith("/dl/"):
            return self._send(200, BUNDLE, "application/octet-stream")
        self._send(404, b"{}")


def setup(channel="github"):
    srv = ThreadingHTTPServer(("127.0.0.1", 0), GitHub)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    hs.API = f"http://127.0.0.1:{srv.server_port}"
    hs.DOWNLOAD_PREFIX = f"http://127.0.0.1:{srv.server_port}/"
    hs.ALLOWED_DOWNLOAD_HOSTS = ("127.0.0.1",)
    os.environ["HS_CHANNEL"] = channel
    d = tempfile.mkdtemp()
    STATE.update(requests=0, not_modified=0, tag="v1.2.0", draft=False, sums_ok=True)
    return srv, hs.Updates(APP_ID, "chartmann1590/x", "1.1.1", os.path.join(d, "updates.json"), os.path.join(d, "cache"))


def test_versions_and_arch():
    assert hs.newer("1.2.0", "1.1.1") and hs.newer("v1.10.0", "1.9.9") and hs.newer("1.2", "1.1.9")
    assert not hs.newer("1.1.1", "1.1.1") and not hs.newer("1.1.0", "1.1.1") and not hs.newer("junk", "1.0")
    assert hs.newer("1.2.0.1", "1.2.0") and not hs.newer("1.2.0", "1.2")
    assert hs.device_arch() in ("x86_64", "aarch64")


def test_check_etag_skip_and_drafts():
    srv, u = setup()
    r = u.check()
    assert r and r.version == "1.2.0" and r["url"].endswith("/releases/tag/v1.2.0")
    assert u.check().version == "1.2.0" and STATE["not_modified"] == 1      # cached by ETag
    assert not u.due()                                                       # checked just now
    u.skip("1.2.0")
    assert u.skipped("1.2.0") and not u.skipped("1.3.0")
    u.set_automatic(False)
    assert not u.automatic and not u.due()
    STATE["tag"] = "v1.1.1"                                                  # same as running: nothing new
    assert u.check() is None
    STATE["tag"], STATE["draft"] = "v9.0.0", True                            # drafts never count
    assert u.check() is None
    srv.shutdown()


def test_download_verifies_checksum():
    srv, u = setup()
    r = u.check()
    seen = []
    path = u.download(r, lambda d, t: seen.append((d, t)))
    assert open(path, "rb").read() == BUNDLE and path.endswith(f"-1.2.0-{hs.device_arch()}.flatpak")
    assert seen and seen[-1][0] == len(BUNDLE)
    STATE["sums_ok"] = False
    os.remove(path)
    try:
        u.download(r)
        raise AssertionError("a bad checksum must be refused")
    except hs.UpdateError as ex:
        assert "checksum" in str(ex)
    assert not os.path.exists(path) and not os.path.exists(path + ".part")
    srv.shutdown()


def test_download_cancel_and_depot_channel():
    srv, u = setup()
    r = u.check()
    stop = threading.Event()
    stop.set()
    assert u.download(r, cancel=stop) == ""
    srv.shutdown()
    srv, d = setup("depot")
    r = d.check()
    assert r is not None                                   # Depot builds still see what's new
    try:
        d.download(r)
        raise AssertionError("Depot builds never download bundles")
    except hs.UpdateError as ex:
        assert "Depot" in str(ex)
    srv.shutdown()


def test_unexpected_host_is_refused():
    srv, u = setup()
    r = u.check()
    hs.ALLOWED_DOWNLOAD_HOSTS = ("github.com",)
    try:
        u.download(r)
        raise AssertionError("downloads from other hosts must be refused")
    except hs.UpdateError as ex:
        assert "unexpected" in str(ex)
    srv.shutdown()


def test_notes_markup_is_safe():
    m = hs.notes_markup("## What's new\n- **Faster** start & more\n- See [notes](https://example.com/n)\n"
                        "<script>alert(1)</script>\n[bad](javascript:x)\n```\ncode <b>\n```")
    assert "<b>What's new</b>" in m and "•  <b>Faster</b> start &amp; more" in m
    assert '<a href="https://example.com/n">notes</a>' in m
    assert "<script>" not in m and "script&gt;" not in m and "alert(1)" in m   # tags dropped, words kept
    assert 'href="javascript' not in m and "[bad](javascript:x)" in m   # only https links become links
    assert "<tt>code &lt;b&gt;</tt>" in m
