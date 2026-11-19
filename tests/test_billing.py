# SPDX-License-Identifier: GPL-3.0-or-later
"""The billing client against a fake license server that speaks the same /v2 API (no network, no Stripe)."""
import base64
import json
import os
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
PKG = next(p for p in ("earshot", "hoffice", "simmer") if os.path.isdir(os.path.join(os.path.dirname(__file__), "..", "src", p)))

STATE = {"paid": False, "polls": 0, "restores": 0}


def token(exp):
    body = base64.urlsafe_b64encode(json.dumps({"exp": exp, "app": "demo"}).encode()).decode().rstrip("=")
    return body + ".signature"


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
        if self.path.startswith("/v2/catalog"):
            if "app=demo" not in self.path:
                return self._send(404, {"error": "Unknown app"})
            return self._send(200, {"app": "demo", "products": {
                "demo_plus:monthly": {"mode": "subscription", "trial_days": 0,
                                      "display": {"label": "Plus (Monthly)", "amount": 199, "interval": "month"}},
                "demo_plus:yearly": {"mode": "subscription", "trial_days": 7,
                                     "display": {"label": "Plus (Yearly)", "amount": 1499, "interval": "year"}},
                "demo_lifetime": {"mode": "payment", "trial_days": 0,
                                  "display": {"label": "Lifetime", "amount": 2999, "interval": None}}}})
        if self.path == "/v2/portal":
            return self._send(200, {"portalUrl": "https://billing.stripe.com/p/login/test"})
        self._send(404, {"error": "Not found"})

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
        if self.path == "/v2/checkout":
            return self._send(200, {"checkoutUrl": "https://checkout.stripe.com/c/pay/cs_test_ABC123#frag"})
        if self.path == "/v2/entitlement":
            if body.get("sessionId"):
                STATE["polls"] += 1
                if STATE["polls"] < 3:
                    return self._send(200, {"entitlements": [], "pending": True})
                STATE["paid"] = True
                return self._send(200, {"email": "a@b.co", "entitlements": ["demo_plus:yearly"],
                                        "licenseToken": token(int(time.time()) + 86400), "revalidateAfter": 0})
            STATE["restores"] += 1
            if STATE["restores"] > 2:
                return self._send(429, {"error": "slow down"})
            owned = ["demo_plus:yearly"] if STATE["paid"] else []
            return self._send(200, {"email": body.get("email"), "entitlements": owned,
                                    "licenseToken": token(int(time.time()) + 86400),
                                    "revalidateAfter": int(time.time()) + 3600})
        self._send(404, {"error": "Not found"})


def server():
    srv = ThreadingHTTPServer(("127.0.0.1", 0), Fake)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def test_purchase_restore_and_offline_grace():
    srv = server()
    hs = __import__(f"{PKG}.hsbilling", fromlist=["x"])
    hs.BASE_URL = f"http://127.0.0.1:{srv.server_port}"
    path = os.path.join(tempfile.mkdtemp(), "license.json")
    b = hs.Billing("demo", ("demo_lifetime", "demo_plus:yearly", "demo_plus:monthly"), path)
    assert not b.owns()
    cat = b.catalog()
    assert hs.price_words(cat["demo_plus:yearly"]) == "$14.99 a year, first 7 days free"
    assert hs.price_words(cat["demo_lifetime"]) == "$29.99 once"
    try:
        b.checkout("demo_plus:yearly", "not an email")
        raise AssertionError("expected BillingError")
    except hs.BillingError as ex:
        assert ex.code == "email"
    url, session = b.checkout("demo_plus:yearly", "a@b.co")
    assert url.startswith("https://checkout.stripe.com/") and session == "cs_test_ABC123"
    changed = []
    b.listeners.append(lambda: changed.append(1))
    assert b.wait_for_session(session, threading.Event(), every=0.01, limit=5)
    assert b.owns() and b.owned_product() == "demo_plus:yearly" and b.email == "a@b.co" and changed
    # the license survives a restart, and its file is private
    again = hs.Billing("demo", b.products, path)
    assert again.owns() and oct(os.stat(path).st_mode & 0o777) == "0o600"
    # restore on another device, then the server's rate limit reads as a sentence
    other = hs.Billing("demo", b.products, path + "2")
    assert other.restore("a@b.co") and other.owns()
    other.restore("a@b.co")
    try:
        other.restore("a@b.co")
        raise AssertionError("expected rate limit")
    except hs.BillingError as ex:
        assert ex.code == "rate_limited" and "Too many" in str(ex)
    # offline: a license 15 days past expiry locks the extras
    other.state["license_exp"] = int(time.time()) - 15 * 86400
    other._save()
    assert not hs.Billing("demo", b.products, path + "2").owns()
    # an app the server doesn't know yet says so plainly
    try:
        hs.Billing("nope", ("x",), path + "3").catalog()
        raise AssertionError("expected unknown app")
    except hs.BillingError as ex:
        assert ex.code == "unknown_app" and "aren't open" in str(ex)
    assert b.portal().startswith("https://billing.stripe.com/")
    b.forget()
    assert not b.owns() and not os.path.exists(path)
    srv.shutdown()
