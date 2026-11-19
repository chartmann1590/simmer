# SPDX-License-Identifier: GPL-3.0-or-later
"""Promotion codes on the lifetime offer, against a LOCAL FAKE license server (no Stripe, no network).

The fake answers the way the patched worker does: the catalog marks only the allowlisted lifetime product as
accepting codes, and /v2/entitlement unlocks only a completed Checkout session (payment_status "paid" or
"no_payment_required") for this app's own product. Stripe itself (code validity, expiry, use limits, product
restriction) is not simulated here; from the app's side every failed or abandoned code is a session that
never completes, which is what these tests drive.
"""
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
APP, PRODUCTS = {
    "earshot": ("earshot", ("earshot_lifetime", "earshot_plus:yearly", "earshot_plus:monthly")),
    "hoffice": ("hartmann-office", ("office_lifetime", "office_pro:yearly", "office_pro:monthly")),
    "simmer": ("simmer", ("simmer_lifetime", "simmer_plus:yearly", "simmer_plus:monthly")),
}[PKG]
OTHER_APP_PRODUCT = "office_lifetime" if PKG != "hoffice" else "earshot_lifetime"
LIFETIME = PRODUCTS[0]

# session id -> (Stripe status, payment_status, product, app)
SESSIONS = {}
CHECKOUTS = []          # every /v2/checkout body the app sent
EMAIL_OWNS = {}         # email -> entitlements (what restore returns)


def token():
    body = base64.urlsafe_b64encode(json.dumps({"exp": int(time.time()) + 86400}).encode()).decode().rstrip("=")
    return body + ".sig"


class Worker(BaseHTTPRequestHandler):
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
        if self.path.startswith("/v2/catalog") and f"app={APP}" in self.path:
            products = {}
            for key in PRODUCTS:
                life = key == LIFETIME
                products[key] = {"mode": "payment" if life else "subscription", "trial_days": 7 if "yearly" in key else 0,
                                 "implies": [], "display": {"label": key, "amount": 2999 if life else 199,
                                                            "interval": None if life else "month"}}
                if life:
                    products[key]["promotion_codes"] = True      # the worker's allowlist, nothing else
            return self._send(200, {"app": APP, "products": products})
        self._send(404, {"error": "Unknown app"})

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
        if self.path == "/v2/checkout":
            CHECKOUTS.append(body)
            sid = f"cs_test_{len(CHECKOUTS):04d}x"
            SESSIONS[sid] = ("open", "unpaid", body["product"], body["app"])
            return self._send(200, {"checkoutUrl": f"https://checkout.stripe.com/c/pay/{sid}#x"})
        if self.path == "/v2/entitlement":
            if "sessionId" in body:
                status, pay, product, app = SESSIONS.get(body["sessionId"], ("missing", "", "", ""))
                if app != body.get("app"):
                    return self._send(403, {"error": "That checkout belongs to another app."})
                if status != "complete" or pay not in ("paid", "no_payment_required"):
                    return self._send(200, {"entitlements": [], "pending": status == "open"})
                EMAIL_OWNS.setdefault("qa@example.com", set()).add(product)
                return self._send(200, {"email": "qa@example.com", "entitlements": [product],
                                        "licenseToken": token(), "revalidateAfter": 0})
            owned = sorted(EMAIL_OWNS.get(body.get("email"), set()))
            return self._send(200, {"email": body.get("email"), "entitlements": owned, "licenseToken": token(),
                                    "revalidateAfter": int(time.time()) + 3600})
        self._send(404, {"error": "Not found"})


def setup():
    SESSIONS.clear(), CHECKOUTS.clear(), EMAIL_OWNS.clear()
    srv = ThreadingHTTPServer(("127.0.0.1", 0), Worker)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    hs = __import__(f"{PKG}.hsbilling", fromlist=["x"])
    hs.BASE_URL = f"http://127.0.0.1:{srv.server_port}"
    return srv, hs


def test_catalog_marks_only_the_lifetime_offer():
    srv, hs = setup()
    b = hs.Billing(APP, PRODUCTS, os.path.join(tempfile.mkdtemp(), "l.json"))
    cat = b.catalog()
    assert cat[LIFETIME]["promo_codes"] is True
    assert [k for k, p in cat.items() if p["promo_codes"]] == [LIFETIME]   # monthly and yearly unchanged
    srv.shutdown()


def test_checkout_request_shape_is_unchanged():
    """The app never sends a code or a price: the same {app, product, email} for every offer."""
    srv, hs = setup()
    b = hs.Billing(APP, PRODUCTS, os.path.join(tempfile.mkdtemp(), "l.json"))
    for key in PRODUCTS:
        b.checkout(key, "qa@example.com")
    assert CHECKOUTS == [{"app": APP, "product": k, "email": "qa@example.com"} for k in PRODUCTS]
    srv.shutdown()


def test_only_a_completed_session_unlocks():
    srv, hs = setup()
    path = os.path.join(tempfile.mkdtemp(), "l.json")
    b = hs.Billing(APP, PRODUCTS, path)
    _url, sid = b.checkout(LIFETIME, "qa@example.com")
    # open (code not yet entered, or rejected by Stripe: invalid, expired, used up, wrong product)
    assert b.session(sid) is False and not b.owns()
    assert b.wait_for_session(sid, threading.Event(), every=0.01, limit=0.1) is False and not b.owns()
    # abandoned / cancelled: Stripe expires the session
    SESSIONS[sid] = ("expired", "unpaid", LIFETIME, APP)
    assert b.session(sid) is False and not b.owns()
    # complete but not paid (shouldn't happen, but never unlock on it)
    SESSIONS[sid] = ("complete", "unpaid", LIFETIME, APP)
    assert b.session(sid) is False and not b.owns()
    # complete with a 100%-off code: $0, no card
    SESSIONS[sid] = ("complete", "no_payment_required", LIFETIME, APP)
    assert b.session(sid) is True and b.owns(LIFETIME) and b.owned_product() == LIFETIME
    # survives a restart
    assert hs.Billing(APP, PRODUCTS, path).owns(LIFETIME)
    # restores on a clean profile with the same email
    clean = hs.Billing(APP, PRODUCTS, os.path.join(tempfile.mkdtemp(), "l.json"))
    assert not clean.owns() and clean.restore("qa@example.com") and clean.owns(LIFETIME)
    srv.shutdown()


def test_ordinary_paid_checkout_still_unlocks():
    srv, hs = setup()
    b = hs.Billing(APP, PRODUCTS, os.path.join(tempfile.mkdtemp(), "l.json"))
    _url, sid = b.checkout(PRODUCTS[2], "qa@example.com")   # monthly, no code
    SESSIONS[sid] = ("complete", "paid", PRODUCTS[2], APP)
    assert b.session(sid) and b.owns(PRODUCTS[2]) and not b.owns(LIFETIME)
    srv.shutdown()


def test_apps_stay_separate():
    srv, hs = setup()
    b = hs.Billing(APP, PRODUCTS, os.path.join(tempfile.mkdtemp(), "l.json"))
    # a license that only names the other app's lifetime product unlocks nothing here
    b._store({"email": "qa@example.com", "entitlements": [OTHER_APP_PRODUCT], "licenseToken": token()})
    assert not b.owns()
    # a session made for another app is refused by the server (403) and unlocks nothing
    SESSIONS["cs_test_other"] = ("complete", "no_payment_required", OTHER_APP_PRODUCT, "someone-else")
    assert b.session("cs_test_other") is False and not b.owns()
    srv.shutdown()
