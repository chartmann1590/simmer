# SPDX-License-Identifier: GPL-3.0-or-later
"""Hartmann Studios billing for Luma apps: Stripe Checkout through the shared Hartmann Studios license server.

No GTK and no Stripe key in here. The flow, the same as the studio's Android "direct" builds:

1. `checkout(product, email)` asks the license server for a Stripe Checkout page and returns its address and
   the Checkout session id (it's in the address). The app opens that page in the person's own browser.
2. `wait_for_session(session_id)` asks the server, every few seconds, whether that session has been paid. The
   server checks with Stripe itself before it answers, so a paid session is proof of purchase.
3. The answer (owned products, a signed license, when to check again) is kept in a small file in the app's
   own folder. `restore(email)` asks again by email on another device; the server rate-limits that.
4. `portal()` returns Stripe's own page for cancelling or changing a subscription.

Promotion codes are typed on Stripe's checkout page, never in the app: the server turns them on for the products it
allows (the catalog marks those `promotion_codes`), Stripe checks each code, and a completed order (paid, or free with
a 100%-off code) unlocks exactly as a paid one does.

Offline, a license keeps working until 14 days past its expiry, then the extras lock until it can be renewed.
"""
from __future__ import annotations

import json
import os
import re
import ssl
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

BASE_URL = os.environ.get("HS_BILLING_URL", "https://livecaptionn-premium.charles-h-hartmann1.workers.dev")
OFFLINE_GRACE = 14 * 24 * 3600
TIMEOUT = 20
SESSION_RE = re.compile(r"cs_(?:live|test)_[A-Za-z0-9]+")
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_CTX = ssl.create_default_context()


class BillingError(Exception):
    """Something the person should read (no connection, too many restores, store not open yet)."""

    def __init__(self, message: str, code: str = ""):
        super().__init__(message)
        self.code = code


def _request(method: str, path: str, body: dict | None = None, user_agent: str = "HartmannStudios-Luma/1.0") -> dict:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(BASE_URL + path, data=data, method=method,
                                 headers={"Content-Type": "application/json", "Accept": "application/json",
                                          "User-Agent": user_agent})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT, context=_CTX) as resp:
            raw = resp.read(256 * 1024)
            status = resp.status
    except urllib.error.HTTPError as ex:
        raw, status = ex.read(64 * 1024), ex.code
    except (urllib.error.URLError, TimeoutError, OSError):
        raise BillingError("Couldn't reach the store. Check your connection and try again.", "offline") from None
    try:
        doc = json.loads(raw.decode("utf-8", "replace") or "{}")
    except ValueError:
        doc = {}
    if status == 429:
        raise BillingError("Too many tries for now. Try again later.", "rate_limited")
    if status >= 400 or "error" in doc and not doc.get("entitlements"):
        msg = doc.get("error") or f"The store answered with an error ({status})."
        code = doc.get("code") or ("unknown_app" if "Unknown app" in str(msg) else "error")
        if code == "unknown_app":
            msg = "Purchases aren't open for this app yet."
        raise BillingError(str(msg), code)
    return doc


class Billing:
    """One app's purchases. `app` is its key on the license server ("earshot", "hartmann-office", "simmer");
    `products` its product keys, best first; `path` where the license is kept."""

    def __init__(self, app: str, products: tuple[str, ...], path: str):
        self.app, self.products, self.path = app, products, path
        self._lock = threading.Lock()
        self.state = self._load()
        self.listeners: list = []
        self._expire_if_stale()

    # ---------------------------------------------------------------- state
    def _load(self) -> dict:
        try:
            with open(self.path, encoding="utf-8") as f:
                doc = json.load(f)
            return doc if isinstance(doc, dict) else {}
        except (OSError, ValueError):
            return {}

    def _save(self) -> None:
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        tmp = self.path + ".part"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self.state, f)
        os.chmod(tmp, 0o600)
        os.replace(tmp, self.path)

    def _expire_if_stale(self) -> None:
        exp = int(self.state.get("license_exp") or 0)
        if exp and time.time() > exp + OFFLINE_GRACE and self.state.get("entitlements"):
            self.state["entitlements"] = []
            self._save()

    @property
    def email(self) -> str:
        return self.state.get("email") or ""

    def entitlements(self) -> set[str]:
        return set(self.state.get("entitlements") or [])

    def owns(self, *keys: str) -> bool:
        """True when any of `keys` (default: any of this app's products) is owned."""
        owned = self.entitlements()
        return bool(owned & set(keys or self.products))

    def owned_product(self) -> str:
        owned = self.entitlements()
        return next((p for p in self.products if p in owned), "")

    def needs_revalidation(self) -> bool:
        return bool(self.email) and time.time() >= float(self.state.get("revalidate_after") or 0)

    def _store(self, doc: dict, email: str = "") -> bool:
        token = doc.get("licenseToken") or ""
        with self._lock:
            self.state.update({
                "email": doc.get("email") or email or self.email,
                "entitlements": [str(x) for x in (doc.get("entitlements") or [])],
                "license": token,
                "license_exp": token_exp(token),
                "revalidate_after": float(doc.get("revalidateAfter") or 0),
                "checked": time.time(),
            })
            self._save()
        for fn in list(self.listeners):
            try:
                fn()
            except Exception:
                pass
        return self.owns()

    def forget(self) -> None:
        """Sign this device out of its purchases (they stay with the email at the store)."""
        with self._lock:
            self.state = {}
            try:
                os.remove(self.path)
            except OSError:
                pass
        for fn in list(self.listeners):
            fn()

    # ---------------------------------------------------------------- the store (blocking: run on a worker)
    def catalog(self) -> dict:
        """{product key: {"label", "amount" (cents), "interval", "trial_days", "mode"}} from the server."""
        doc = _request("GET", "/v2/catalog?" + urllib.parse.urlencode({"app": self.app}))
        out = {}
        for key, p in (doc.get("products") or {}).items():
            d = p.get("display")
            d = d if isinstance(d, dict) else {"label": str(d or key)}
            out[key] = {"label": d.get("label") or key, "amount": d.get("amount"), "interval": d.get("interval"),
                        "trial_days": int(p.get("trial_days") or 0), "mode": p.get("mode") or "",
                        # The server says so when its Stripe Checkout for this product accepts a promotion code.
                        # Stripe checks the code; the app only tells people where to type it.
                        "promo_codes": bool(p.get("promotion_codes") or d.get("promotion_codes"))}
        return out

    def checkout(self, product: str, email: str) -> tuple[str, str]:
        """(checkout page address, session id)."""
        email = email.strip()
        if not EMAIL_RE.match(email):
            raise BillingError("Enter the email you want your receipt and license sent to.", "email")
        doc = _request("POST", "/v2/checkout", {"app": self.app, "product": product, "email": email})
        url = doc.get("checkoutUrl") or ""
        if not url.startswith("https://"):
            raise BillingError("Couldn't open checkout. Try again.", "checkout")
        m = SESSION_RE.search(url)
        with self._lock:
            self.state["pending_email"] = email
        return url, (m.group(0) if m else "")

    def session(self, session_id: str) -> bool:
        """Ask once whether a Checkout session is paid. True when it unlocked something."""
        try:
            doc = _request("POST", "/v2/entitlement", {"app": self.app, "sessionId": session_id})
        except BillingError as ex:
            if ex.code in ("rate_limited", "offline", "unknown_app"):
                raise
            return False
        if not doc.get("entitlements"):
            return False
        return self._store(doc, self.state.get("pending_email", ""))

    def wait_for_session(self, session_id: str, cancel: threading.Event, *, every: float = 3.0,
                         limit: float = 30 * 60) -> bool:
        """Poll until the session is paid, cancelled, or `limit` seconds pass."""
        end = time.monotonic() + limit
        while not cancel.is_set() and time.monotonic() < end:
            try:
                if self.session(session_id):
                    return True
            except BillingError as ex:
                if ex.code != "offline":
                    raise
            cancel.wait(every)
        return False

    def restore(self, email: str = "") -> bool:
        email = (email or self.email).strip()
        if not EMAIL_RE.match(email):
            raise BillingError("Enter the email you used at checkout.", "email")
        doc = _request("POST", "/v2/entitlement", {"app": self.app, "email": email})
        if "entitlements" not in doc:
            raise BillingError(doc.get("error") or "Couldn't check your purchases.", doc.get("code") or "error")
        return self._store(doc, email)

    def revalidate(self) -> None:
        """Quietly renew the license (subscription renewals, cancellations). Errors are swallowed."""
        if not self.email:
            return
        try:
            self.restore(self.email)
        except BillingError:
            pass

    def portal(self) -> str:
        doc = _request("GET", "/v2/portal")
        url = doc.get("portalUrl") or ""
        if not url.startswith("https://"):
            raise BillingError("Couldn't open subscription management.", "portal")
        return url


def token_exp(token: str) -> int:
    """The `exp` in a license token's payload (base64url JSON before the first dot); 0 when unreadable."""
    import base64
    try:
        body = token.split(".", 1)[0]
        body += "=" * (-len(body) % 4)
        return int(json.loads(base64.urlsafe_b64decode(body)).get("exp") or 0)
    except Exception:
        return 0


def price_words(p: dict) -> str:
    """"$1.99 a month", "$14.99 a year, first 7 days free", "$29.99 once"."""
    amount = p.get("amount")
    if not isinstance(amount, (int, float)):
        return p.get("label") or ""
    money = f"${amount / 100:,.2f}"
    interval = p.get("interval")
    words = f"{money} a {interval}" if interval else f"{money} once"
    if p.get("trial_days"):
        words += f", first {p['trial_days']} days free"
    return words
