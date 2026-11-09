import base64
import hashlib
import hmac
import json
from datetime import datetime, timezone

import engine
import orders
import profiles


SECRET = "whsec"
S = "outlet.example.com"


def call(conn, payload, secret=SECRET, shop="example-outlet.myshopify.com"):
    body = json.dumps(payload).encode()
    sig = base64.b64encode(hmac.new(secret.encode(), body, hashlib.sha256).digest()).decode()
    h = {"X-Shopify-Hmac-Sha256": sig, "X-Shopify-Shop-Domain": shop, "X-Shopify-Topic": "orders/create"}
    return orders.handle(conn, body, h, SECRET)


def order(email="jan@example.org", oid=1001):
    return {"id": oid, "email": email, "total_price": "249.00", "discount_codes": [{"code": "WELCOME-AAAA-BBBB"}]}


def test_order_with_uppercase_email_stops_flow(conn):
    p = profiles.upsert(conn, "jan@example.org")
    profiles.set_consent(conn, p, S, "subscribed", "popup")
    engine.enrol(conn, "welcome", S, p, now=datetime(2026, 10, 1, tzinfo=timezone.utc))
    assert call(conn, order(email="JAN@Example.ORG")) == 200
    assert conn.execute("SELECT state FROM enrolments").fetchone()["state"] == "exited"


def test_duplicate_webhook_is_idempotent(conn):
    call(conn, order()); call(conn, order())
    assert conn.execute("SELECT count(*) n FROM events WHERE type='order_placed'").fetchone()["n"] == 1


def test_bad_hmac_401(conn):
    body = json.dumps(order()).encode()
    h = {"X-Shopify-Hmac-Sha256": "nope", "X-Shopify-Shop-Domain": "example-outlet.myshopify.com", "X-Shopify-Topic": "orders/create"}
    assert orders.handle(conn, body, h, SECRET) == 401


def test_unknown_shop_200_ignored(conn):
    assert call(conn, order(), shop="other.myshopify.com") == 200
    assert conn.execute("SELECT count(*) n FROM events").fetchone()["n"] == 0


def test_order_without_email_is_ignored(conn):
    assert call(conn, {"id": 5, "email": None, "total_price": "10"}) == 200


def test_signed_non_json_body_200_ignored(conn):
    body = b"not json at all"
    sig = base64.b64encode(hmac.new(SECRET.encode(), body, hashlib.sha256).digest()).decode()
    h = {"X-Shopify-Hmac-Sha256": sig, "X-Shopify-Shop-Domain": "example-outlet.myshopify.com", "X-Shopify-Topic": "orders/create"}
    assert orders.handle(conn, body, h, SECRET) == 200
    assert conn.execute("SELECT count(*) n FROM events").fetchone()["n"] == 0


def test_signed_json_without_id_200_ignored(conn):
    payload = {"email": "jan@example.org", "total_price": "249.00"}
    body = json.dumps(payload).encode()
    sig = base64.b64encode(hmac.new(SECRET.encode(), body, hashlib.sha256).digest()).decode()
    h = {"X-Shopify-Hmac-Sha256": sig, "X-Shopify-Shop-Domain": "example-outlet.myshopify.com", "X-Shopify-Topic": "orders/create"}
    assert orders.handle(conn, body, h, SECRET) == 200
    assert conn.execute("SELECT count(*) n FROM events").fetchone()["n"] == 0


def test_db_failure_500_rolled_back_and_retryable(conn, monkeypatch):
    def raise_error(*args, **kwargs):
        raise RuntimeError("simulated DB failure")

    monkeypatch.setattr(engine, "exit_if_ordered", raise_error)
    assert call(conn, order()) == 500
    assert conn.execute("SELECT count(*) n FROM events WHERE type='order_placed'").fetchone()["n"] == 0

    # Verify connection is still usable after rollback
    monkeypatch.undo()
    assert call(conn, order()) == 200
    assert conn.execute("SELECT count(*) n FROM events WHERE type='order_placed'").fetchone()["n"] == 1


def test_shared_shopify_store_orders_go_to_the_market_of_the_shipping_country(conn):
    t = datetime(2026, 10, 12, tzinfo=timezone.utc)
    ids = {}
    for store, email in [("shop.example.com", "nl@x.nl"), ("shop-fi.example.com", "fi@x.fi"), ("shop-ie.example.com", "ie@x.ie")]:
        p = profiles.upsert(conn, email); ids[store] = p
        engine.enrol(conn, "abandoned_checkout", store, p, now=t, ref="c-" + store)
    conn.commit()
    base = {"total_price": "100.00", "discount_codes": []}
    assert call(conn, {**base, "id": 1, "email": "fi@x.fi", "shipping_address": {"country_code": "FI"}}, shop="example-shop.myshopify.com") == 200
    assert call(conn, {**base, "id": 2, "email": "ie@x.ie", "order_status_url": "https://shop-ie.example.com/1234567890/orders/x"}, shop="example-shop.myshopify.com") == 200
    assert call(conn, {**base, "id": 3, "email": "at@x.at", "shipping_address": {"country_code": "AT"},
                       "order_status_url": "https://example-shop.at/1234567890/orders/y"}, shop="example-shop.myshopify.com") == 200
    got = {(r["store"], r["external_id"]) for r in conn.execute("SELECT store, external_id FROM events WHERE type='order_placed'")}
    assert got == {("shop-fi.example.com", "1"), ("shop-ie.example.com", "2")}          # AT is not ours: ignored
    states = {r["store"]: r["state"] for r in conn.execute("SELECT store, state FROM enrolments")}
    assert states == {"shop.example.com": "active", "shop-fi.example.com": "exited", "shop-ie.example.com": "exited"}


# --- post_purchase: subscribers who order on a market with post_purchase on get the series (50/50 holdout per order) ---
from datetime import timedelta  # noqa: E402

import checkouts  # noqa: E402
import pytest  # noqa: E402

SP, SHOP = "shop.example.com", "example-shop.myshopify.com"
T = datetime(2026, 11, 3, 10, tzinfo=timezone.utc)


@pytest.fixture
def pp(monkeypatch):
    monkeypatch.setenv("MAIL_PP_LIVE", "shop.example.com")
    monkeypatch.setattr(checkouts, "arm_for", lambda ref: "treatment")


def sp_order(oid=1001, email="jan@example.org", items=(11, 22, 11, None), name="#NL1001"):
    return {"id": oid, "name": name, "email": email, "total_price": "249.00", "discount_codes": [],
            "shipping_address": {"country_code": "NL"}, "line_items": [{"product_id": i} for i in items]}


def sp_call(conn, payload, now=T):
    body = json.dumps(payload).encode()
    sig = base64.b64encode(hmac.new(SECRET.encode(), body, hashlib.sha256).digest()).decode()
    h = {"X-Shopify-Hmac-Sha256": sig, "X-Shopify-Shop-Domain": SHOP, "X-Shopify-Topic": "orders/create"}
    return orders.handle(conn, body, h, SECRET, now=now)


def sub(conn, email="jan@example.org", store=SP):
    p = profiles.upsert(conn, email)
    profiles.set_consent(conn, p, store, "subscribed", "popup")
    return p


def pp_enrolments(conn):
    return conn.execute("SELECT id, ref, arm, state FROM enrolments WHERE flow='post_purchase' ORDER BY id").fetchall()


def test_order_stores_name_and_product_ids(conn, pp):
    assert sp_call(conn, sp_order()) == 200
    pl = conn.execute("SELECT payload FROM events WHERE type='order_placed'").fetchone()["payload"]
    assert pl["name"] == "#NL1001" and pl["product_ids"] == ["11", "22"]


def test_order_from_subscriber_enrols_post_purchase(conn, pp):
    sub(conn)
    sp_call(conn, sp_order())
    (e,) = pp_enrolments(conn)
    assert e["ref"] == "order:1001" and e["arm"] == "treatment" and e["state"] == "active"
    due = [r["due_at"] for r in conn.execute("SELECT due_at FROM flow_steps WHERE enrolment_id=%s ORDER BY step", (e["id"],))]
    assert due == [T + timedelta(days=1), T + timedelta(days=21)]


def test_no_post_purchase_without_consent_or_switch_or_flag(conn, pp, monkeypatch):
    profiles.upsert(conn, "els@example.org")
    sp_call(conn, sp_order(oid=1, email="els@example.org"))                     # not subscribed
    sub(conn)
    monkeypatch.setenv("MAIL_PP_LIVE", "")
    sp_call(conn, sp_order(oid=2))                                           # market switched off
    monkeypatch.setenv("MAIL_PP_LIVE", "shop.example.com,outlet.example.com")
    sub(conn, "kees@example.org", store=S)
    call(conn, order(email="kees@example.org", oid=3))                          # store without post_purchase
    assert pp_enrolments(conn) == []
    assert conn.execute("SELECT count(*) n FROM events WHERE type='order_placed'").fetchone()["n"] == 3


def test_holdout_order_completes_at_once(conn, pp, monkeypatch):
    monkeypatch.setattr(checkouts, "arm_for", lambda ref: "holdout")
    sub(conn)
    sp_call(conn, sp_order())
    (e,) = pp_enrolments(conn)
    assert e["arm"] == "holdout" and e["state"] == "completed"
    assert {r["last_error"] for r in conn.execute("SELECT last_error FROM flow_steps WHERE enrolment_id=%s", (e["id"],))} == {"skip:holdout"}


def test_repeat_order_restarts_the_series(conn, pp):
    sub(conn)
    sp_call(conn, sp_order(oid=1, name="#NL1"))
    sp_call(conn, sp_order(oid=2, name="#NL2"), now=T + timedelta(days=5))
    a, b = pp_enrolments(conn)
    assert (a["ref"], a["state"]) == ("order:1", "exited") and (b["ref"], b["state"]) == ("order:2", "active")


def test_same_order_webhook_twice_enrols_once(conn, pp):
    sub(conn)
    sp_call(conn, sp_order()); sp_call(conn, sp_order(), now=T + timedelta(minutes=1))
    assert len(pp_enrolments(conn)) == 1 and pp_enrolments(conn)[0]["state"] == "active"


def test_zero_value_order_gets_no_post_purchase(conn, pp):
    """€0 orders are replacements/warranty cases created by customer service: no 'thank you + cross-sell'."""
    sub(conn)
    sp_call(conn, {**sp_order(), "total_price": "0.00"})
    assert pp_enrolments(conn) == []


def test_order_of_that_variant_cancels_the_request(conn):
    import stock
    from datetime import timedelta
    p = profiles.upsert(conn, "jan@example.org")
    stock.request(conn, "shop.example.com", p, "5", "55", datetime(2026, 11, 1, tzinfo=timezone.utc))
    stock.request(conn, "shop.example.com", p, "6", "66", datetime(2026, 11, 1, tzinfo=timezone.utc))
    conn.commit()
    o = {**order(oid=2001), "shipping_address": {"country_code": "BE"}, "line_items": [{"product_id": 5, "variant_id": 55}]}
    assert call(conn, o, shop="example-shop.myshopify.com") == 200
    assert [r["state"] for r in conn.execute("SELECT state FROM stock_requests ORDER BY id")] == ["cancelled", "waiting"]


# ── checkout opt-in: "email me" ticked at Shopify checkout joins our list (no welcome, no code) ──

def consent(conn, email="jan@example.org", store=SP):
    r = conn.execute("SELECT c.status, c.source FROM consent c JOIN profiles p ON p.id=c.profile_id "
                     "WHERE p.email=%s AND c.store=%s", (email, store)).fetchone()
    return (r["status"], r["source"]) if r else None


def test_checkout_opt_in_subscribes_without_welcome_or_code(conn):
    assert sp_call(conn, {**sp_order(), "buyer_accepts_marketing": True}) == 200
    assert consent(conn) == ("subscribed", "shopify_checkout")
    for t in ("coupons", "enrolments"):
        assert conn.execute(f"SELECT count(*) n FROM {t}").fetchone()["n"] == 0, t
    assert conn.execute("SELECT count(*) n FROM events WHERE type='signup'").fetchone()["n"] == 0


def test_checkout_opt_in_gets_the_post_purchase_series_for_that_order(conn, pp):
    sp_call(conn, {**sp_order(), "buyer_accepts_marketing": True})
    (e,) = pp_enrolments(conn)
    assert e["ref"] == "order:1001"


def test_no_opt_in_ticked_adds_nothing(conn):
    sp_call(conn, {**sp_order(), "buyer_accepts_marketing": False})
    sp_call(conn, sp_order(oid=1002))
    assert consent(conn) is None


def test_checkout_opt_in_resubscribes_someone_who_unsubscribed(conn):
    p = profiles.upsert(conn, "jan@example.org")
    profiles.set_consent(conn, p, SP, "unsubscribed", "unsubscribe_link")
    profiles.suppress(conn, "jan@example.org", SP, "unsubscribed")         # what web.unsubscribe writes
    sp_call(conn, {**sp_order(), "buyer_accepts_marketing": True})
    assert consent(conn) == ("subscribed", "shopify_checkout")
    assert profiles.is_sendable(conn, p, SP, "subscribed")


def test_checkout_opt_in_does_not_lift_a_bounce_or_complaint(conn):
    p = profiles.upsert(conn, "jan@example.org")
    profiles.suppress(conn, "jan@example.org", "*", "hard_bounce")
    sp_call(conn, {**sp_order(), "buyer_accepts_marketing": True})
    assert not profiles.is_sendable(conn, p, SP, "subscribed")


def test_checkout_opt_in_keeps_an_existing_subscription_as_it_was(conn):
    sub(conn)
    sp_call(conn, {**sp_order(), "buyer_accepts_marketing": True})
    assert consent(conn) == ("subscribed", "popup")
