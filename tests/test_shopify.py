import base64
import hashlib
import hmac

import pytest

import shopify
import stores


def test_webhook_hmac_valid_and_invalid():
    body, secret = b'{"id":1}', "shpss_x"
    good = base64.b64encode(hmac.new(secret.encode(), body, hashlib.sha256).digest()).decode()
    assert shopify.verify_webhook(body, good, secret)
    assert not shopify.verify_webhook(body, "AAAA", secret)
    assert not shopify.verify_webhook(body, "", secret)


def test_user_errors_raise(monkeypatch):
    c = shopify.Client(stores.get("outlet.example.com"), "tok")
    monkeypatch.setattr(c, "_post", lambda q, v: {"data": {"x": {"userErrors": [{"message": "Code taken"}]}}})
    with pytest.raises(shopify.ShopifyError, match="Code taken"):
        c.gql("mutation { x { userErrors { message } } }", {})


class _Resp:
    def __init__(self, body):
        self.body = body

    def raise_for_status(self):
        pass

    def json(self):
        return self.body


def test_access_token_client_credentials_cached_and_refreshed(monkeypatch):
    S = stores.get("outlet.example.com")
    shopify._TOKENS.clear()
    calls, now = [], [1000.0]
    monkeypatch.setattr(shopify.time, "monotonic", lambda: now[0])

    def post(url, data, timeout):
        calls.append((url, data))
        return _Resp({"access_token": f"tok{len(calls)}", "scope": "read_orders", "expires_in": 86399})
    monkeypatch.setattr(shopify.requests, "post", post)

    assert shopify.access_token(S, "cid", "csec") == "tok1"
    assert calls[0] == ("https://example-outlet.myshopify.com/admin/oauth/access_token",
                        {"grant_type": "client_credentials", "client_id": "cid", "client_secret": "csec"})
    now[0] += 86399 - 301            # still > 5 min left → cached
    assert shopify.access_token(S, "cid", "csec") == "tok1" and len(calls) == 1
    now[0] += 2                      # inside the 5-min margin → renewed
    assert shopify.access_token(S, "cid", "csec") == "tok2" and len(calls) == 2


_BY_ID = "customerByIdentifier"


def _consent_client(monkeypatch, existing, create_error=None):
    """Fake Admin API: `existing` = customer id the exact lookup returns (None = no customer); create may fail."""
    c = shopify.Client(stores.get("outlet.example.com"), "tok")
    calls = []

    def post(q, v):
        calls.append((q, v))
        if _BY_ID in q:
            return {"data": {_BY_ID: {"id": existing[0]} if existing[0] else None}}
        if "customerCreate" in q:
            if create_error:
                existing[0] = "gid://shopify/Customer/9"      # someone else created it in the meantime
                return {"data": {"customerCreate": {"userErrors": [{"field": ["email"], "message": create_error}]}}}
            return {"data": {"customerCreate": {"userErrors": []}}}
        return {"data": {"customerEmailMarketingConsentUpdate": {"userErrors": []}}}
    monkeypatch.setattr(c, "_post", post)
    return c, calls


def test_consent_uses_exact_lookup_and_updates_existing_customer(monkeypatch):
    c, calls = _consent_client(monkeypatch, ["gid://shopify/Customer/1"])
    c.set_marketing_consent("jan@example.org", True)
    assert _BY_ID in calls[0][0] and calls[0][1] == {"e": "jan@example.org"}
    assert "customerEmailMarketingConsentUpdate" in calls[1][0]
    assert calls[1][1]["input"]["customerId"] == "gid://shopify/Customer/1"


def test_consent_creates_customer_when_none(monkeypatch):
    c, calls = _consent_client(monkeypatch, [None])
    c.set_marketing_consent("jan@example.org", True)
    assert "customerCreate" in calls[1][0] and len(calls) == 2


def test_consent_create_race_falls_back_to_update(monkeypatch):
    """The storefront form creates the customer seconds before us: create says 'taken' → look up again and update."""
    c, calls = _consent_client(monkeypatch, [None], create_error="Email has already been taken")
    c.set_marketing_consent("jan@example.org", True)
    assert "customerEmailMarketingConsentUpdate" in calls[-1][0]
    assert calls[-1][1]["input"]["customerId"] == "gid://shopify/Customer/9"


def test_consent_other_create_errors_still_raise(monkeypatch):
    c, _ = _consent_client(monkeypatch, [None], create_error="Email is invalid")
    with pytest.raises(shopify.ShopifyError, match="invalid"):
        c.set_marketing_consent("jan@example.org", True)
