import json
from datetime import datetime, timezone

import coupons
import main
import profiles
import stores
import welcome_lookup
from tests.test_main import FakeConnCM, Req


def _coupon(conn, email, store, code, created):
    pid = profiles.upsert(conn, email)
    conn.execute("INSERT INTO coupons (profile_id, store, pool, code, created_at) VALUES (%s,%s,'welcome10',%s,%s)",
                 (pid, store, code, created))
    return pid


def test_find_returns_code_expiry_and_on_file_email(conn):
    created = datetime(2026, 10, 1, 9, 0, tzinfo=timezone.utc)
    pid = _coupon(conn, "jan@example.nl", "shop.example.com", "WELCOME-ABCD-EFGH", created)
    conn.execute("INSERT INTO messages (profile_id, store, kind, sent_at) VALUES (%s,'shop.example.com','flow:welcome:1',%s)",
                 (pid, datetime(2026, 10, 1, 9, 1, tzinfo=timezone.utc)))
    out = welcome_lookup.find(conn, "shop.example.com", "  Jan@Example.NL ")
    assert out["found"] is True
    assert out["email"] == "jan@example.nl"
    assert out["code"] == "WELCOME-ABCD-EFGH"
    assert out["created_at"].startswith("2026-10-01T")
    assert out["expires_at"] == coupons.expiry(created, stores.get("shop.example.com").welcome_valid_days).isoformat()
    assert out["sent_at"].startswith("2026-10-01T09:01")
    assert out["from_email"] == "hello@mail.shop.example.com"


def test_find_spans_storefronts_of_the_same_shopify_store(conn):
    # one code per address per Shopify store: a .fi signup is the answer for a shop.example.com question too
    _coupon(conn, "sven@example.fi", "shop-fi.example.com", "WELCOME-ABCD-EFGH", datetime.now(timezone.utc))
    out = welcome_lookup.find(conn, "shop.example.com", "sven@example.fi")
    assert out["found"] is True and out["code"] == "WELCOME-ABCD-EFGH"
    assert out["store"] == "shop-fi.example.com" and out["from_email"] == "hello@mail.shop-fi.example.com"
    assert out["sent_at"] is None


def test_find_never_crosses_shopify_stores(conn):
    _coupon(conn, "k@example.nl", "outlet.example.com", "WELCOME-KKKK-KKKK", datetime.now(timezone.utc))
    assert welcome_lookup.find(conn, "shop.example.com", "k@example.nl") == {"found": False}


def test_find_unknown_profile_unknown_store_and_bad_email(conn):
    assert welcome_lookup.find(conn, "shop.example.com", "nobody@example.nl") == {"found": False}
    assert welcome_lookup.find(conn, "unknown-shop.example", "nobody@example.nl") == {"found": False}
    assert welcome_lookup.find(conn, "shop.example.com", "not-an-email") == {"found": False}


def test_lookup_entry_posts_json(monkeypatch):
    seen = {}
    monkeypatch.setattr(main.db, "connect", lambda dsn: FakeConnCM())
    monkeypatch.setattr(main.welcome_lookup, "find",
                        lambda conn, store, email: seen.update(store=store, email=email) or {"found": False})
    body, status, headers = main.lookup_entry(
        Req("POST", "/welcome-code", json_body={"store": "shop.example.com", "email": "a@b.nl"}))
    assert status == 200 and json.loads(body) == {"found": False}
    assert seen == {"store": "shop.example.com", "email": "a@b.nl"}
    assert headers["Content-Type"] == "application/json"


def test_lookup_entry_rejects_other_methods_paths_and_bodies():
    assert main.lookup_entry(Req("GET", "/welcome-code"))[1] == 405
    assert main.lookup_entry(Req("POST", "/other", json_body={"store": "x", "email": "y"}))[1] == 404
    assert main.lookup_entry(Req("POST", "/welcome-code", json_body=None))[1] == 400
    assert main.lookup_entry(Req("POST", "/welcome-code", json_body={"store": 1, "email": "y"}))[1] == 400
