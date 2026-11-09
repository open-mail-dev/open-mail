from datetime import datetime, timezone

import notify
import profiles
import signup
import tokens

S = "shop.example.com"
NOW = datetime(2026, 11, 3, 12, tzinfo=timezone.utc)
SECRET = "k"


class Shop:
    """Variant 11 sold out, 12 buyable, 13 never available again; fail=True → Shopify down."""
    def __init__(self, fail=False):
        self.fail = fail

    def gql(self, query, variables):
        if self.fail:
            raise RuntimeError("Shopify down")
        def v(vid, available, ptype="furniture"):
            return {"legacyResourceId": vid, "title": "White", "availableForSale": available, "price": "99.00", "image": None,
                    "product": {"legacyResourceId": "1", "handle": "p1", "title": "Chair", "status": "ACTIVE", "onlineStoreUrl": "https://x",
                                "productType": ptype, "featuredMedia": {"preview": {"image": {"url": "https://cdn.shopify.com/x.jpg"}}}}}
        return {"nodes": [v("11", False), v("12", True), v("13", False, "never available again")]}


def body(**kw):
    return {"store": S, "product_id": "1", "variant_id": "11", "email": "jan@example.org", **kw}


def call(conn, b, shop=None, ip="203.0.113.5"):
    return notify.handle(conn, b, ip, lambda st: shop or Shop(), SECRET, now=NOW)


def requests_(conn):
    return conn.execute("SELECT store, profile_id, product_id, variant_id, state, source FROM stock_requests").fetchall()


def live(monkeypatch, v="shop.example.com"):
    monkeypatch.setenv("MAIL_BIS_LIVE", v)


def test_request_is_stored(conn, monkeypatch):
    live(monkeypatch)
    assert call(conn, body()) == (200, {"ok": True})
    [r] = requests_(conn)
    assert (r["store"], r["variant_id"], r["state"], r["source"]) == (S, "11", "waiting", "form")


def test_honeypot_pretends_success(conn, monkeypatch):
    live(monkeypatch)
    assert call(conn, body(website="x"))[0] == 200 and requests_(conn) == []


def test_market_not_live_or_store_without_back_in_stock_is_rejected(conn, monkeypatch):
    live(monkeypatch, "")
    assert call(conn, body())[0] == 400
    live(monkeypatch, "shop.example.com,outlet.example.com")
    assert call(conn, body(store="shop-fi.example.com"))[0] == 400            # has back-in-stock, not switched on
    assert call(conn, body(store="outlet.example.com"))[0] == 400     # switched on, but no back-in-stock
    assert call(conn, body(store="nope"))[0] == 400
    assert requests_(conn) == []


def test_bad_ids_and_missing_identity_are_rejected(conn, monkeypatch):
    live(monkeypatch)
    assert call(conn, body(variant_id="11; drop"))[0] == 400
    assert call(conn, body(product_id=""))[0] == 400
    assert call(conn, body(email="", vid="garbage"))[0] == 400
    assert call(conn, body(email="not-an-email"))[0] == 400


def test_domain_without_mail_is_rejected(conn, monkeypatch):
    live(monkeypatch)
    monkeypatch.setattr(signup, "domain_accepts_mail", lambda d: False)
    assert call(conn, body()) == (400, {"error": "email"})


def test_rate_limit(conn, monkeypatch):
    live(monkeypatch)
    for i in range(notify.RATE_LIMIT):
        assert call(conn, body(email=f"a{i}@example.org"))[0] == 200
    assert call(conn, body(email="late@example.org"))[0] == 429


def test_suppressed_address_gets_ok_and_nothing_is_stored(conn, monkeypatch):
    live(monkeypatch)
    profiles.suppress(conn, "jan@example.org", "*", "hard_bounce")
    conn.commit()
    assert call(conn, body()) == (200, {"ok": True})
    assert requests_(conn) == []


def test_unsubscribed_profile_is_accepted_and_consent_unchanged(conn, monkeypatch):
    live(monkeypatch)
    p = profiles.upsert(conn, "jan@example.org")
    profiles.set_consent(conn, p, S, "unsubscribed", "link")
    profiles.suppress(conn, "jan@example.org", S, "unsubscribed")         # what web.unsubscribe writes
    conn.commit()
    assert call(conn, body())[0] == 200 and len(requests_(conn)) == 1
    assert conn.execute("SELECT status FROM consent WHERE profile_id=%s", (p,)).fetchone()["status"] == "unsubscribed"


def test_buyable_variant_is_409_and_gone_variant_is_400(conn, monkeypatch):
    live(monkeypatch)
    assert call(conn, body(variant_id="12")) == (409, {"error": "available"})
    assert call(conn, body(variant_id="13"))[0] == 400
    assert requests_(conn) == []


def test_shopify_down_is_503(conn, monkeypatch):
    live(monkeypatch)
    assert call(conn, body(), shop=Shop(fail=True))[0] == 503 and requests_(conn) == []


def test_vid_identifies_without_email(conn, monkeypatch):
    live(monkeypatch)
    p = profiles.upsert(conn, "jan@example.org")
    conn.commit()
    assert call(conn, body(email=None, vid=tokens.visitor(p, SECRET)))[0] == 200
    assert requests_(conn)[0]["profile_id"] == p


def test_twice_is_one_request(conn, monkeypatch):
    live(monkeypatch)
    assert call(conn, body())[0] == 200 and call(conn, body(email=" JAN@example.org "))[0] == 200
    assert len(requests_(conn)) == 1


def test_no_coupon_no_consent_no_welcome(conn, monkeypatch):
    live(monkeypatch)
    call(conn, body())
    for t in ("coupons", "consent", "enrolments"):
        assert conn.execute(f"SELECT count(*) n FROM {t}").fetchone()["n"] == 0, t


def test_suppressed_address_cannot_be_told_apart_by_the_response(conn, monkeypatch):
    live(monkeypatch)
    profiles.suppress(conn, "gone@example.org", "*", "import")
    conn.commit()
    # same answers as any other address: 409 for a buyable variant, 400 for a gone one, 503 when Shopify is down
    assert call(conn, body(email="gone@example.org", variant_id="12"))[0] == 409
    assert call(conn, body(email="gone@example.org", variant_id="13"))[0] == 400
    assert call(conn, body(email="gone@example.org"), shop=Shop(fail=True))[0] == 503
    assert requests_(conn) == []
