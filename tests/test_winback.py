from datetime import datetime, timedelta, timezone

import checkouts
import engine
import profiles
import winback

SHOP = "example-shop.myshopify.com"
NOW = datetime(2026, 11, 3, 12, 0, tzinfo=timezone.utc)
ORDERED = NOW - timedelta(days=120, hours=3)


def order(oid, email="jan@example.org", country="NL", created=ORDERED, last=None, total="249.00", cancelled=None, test=False,
          host="shop.example.com"):
    return {"legacyResourceId": str(oid), "createdAt": created.isoformat().replace("+00:00", "Z"), "cancelledAt": cancelled,
            "test": test, "email": email, "statusPageUrl": f"https://{host}/123/orders/abc",
            "totalPriceSet": {"shopMoney": {"amount": total}},
            "shippingAddress": {"countryCodeV2": country} if country else None, "billingAddress": None,
            "customer": {"id": f"gid://shopify/Customer/{oid}", "lastOrder": {"legacyResourceId": str(last or oid)}}}


class Client:
    """orders page → `nodes`; a customer's order history → product ids 1, 2 (newest order) and 2, 3 (older)."""
    def __init__(self, nodes):
        self.nodes, self.queries = nodes, []

    def gql(self, q, v):
        self.queries.append(v)
        if "customer(" in q:
            lines = lambda *ids: {"lineItems": {"nodes": [{"product": {"legacyResourceId": i}} for i in ids] + [{"product": None}]}}
            return {"customer": {"orders": {"nodes": [lines("1", "2"), lines("2", "3")]}}}
        return {"orders": {"nodes": self.nodes, "pageInfo": {"hasNextPage": False, "endCursor": None}}}


def subscribed(conn, email="jan@example.org", store="shop.example.com"):
    p = profiles.upsert(conn, email)
    profiles.set_consent(conn, p, store, "subscribed", "popup")
    conn.commit()
    return p


def enrolments(conn):
    return {r["ref"]: (r["store"], r["arm"], r["state"]) for r in conn.execute("SELECT ref, store, arm, state FROM enrolments WHERE flow='winback'")}


def live(monkeypatch, markets="shop.example.com"):
    monkeypatch.setenv("MAIL_WB_LIVE", markets)
    monkeypatch.setattr(checkouts, "arm_for", lambda ref: "treatment")


def test_subscriber_whose_last_order_is_120_days_old_is_enrolled_with_everything_they_bought(conn, monkeypatch):
    live(monkeypatch)
    subscribed(conn)
    out = winback.poll(conn, Client([order(11)]), SHOP, now=NOW)
    assert out["enrolled"] == 1
    assert enrolments(conn) == {"order:11": ("shop.example.com", "treatment", "active")}
    ev = conn.execute("SELECT payload FROM events WHERE type='winback' AND external_id='11'").fetchone()
    assert ev["payload"]["product_ids"] == ["1", "2", "3"]          # newest order first, each once


def test_ordered_since_cancelled_test_and_free_orders_are_left_alone(conn, monkeypatch):
    live(monkeypatch)
    subscribed(conn)
    nodes = [order(11, last=99), order(12, cancelled="2026-07-01T00:00:00Z"), order(13, test=True), order(14, total="0.00")]
    winback.poll(conn, Client(nodes), SHOP, now=NOW)
    assert enrolments(conn) == {}


def test_only_subscribers_and_known_profiles(conn, monkeypatch):
    live(monkeypatch)
    p = profiles.upsert(conn, "ex@example.org")
    profiles.set_consent(conn, p, "shop.example.com", "unsubscribed", "link")
    conn.commit()
    winback.poll(conn, Client([order(11, email="ex@example.org"), order(12, email="unknown@example.org")]), SHOP, now=NOW)
    assert enrolments(conn) == {}
    assert conn.execute("SELECT count(*) n FROM profiles WHERE email='unknown@example.org'").fetchone()["n"] == 0


def test_market_must_be_switched_on(conn, monkeypatch):
    live(monkeypatch, markets="")
    subscribed(conn)
    winback.poll(conn, Client([order(11)]), SHOP, now=NOW)
    assert enrolments(conn) == {}


def test_other_markets_follow_their_own_switch(conn, monkeypatch):
    live(monkeypatch, markets="shop.example.com")
    subscribed(conn, store="shop-fi.example.com")
    winback.poll(conn, Client([order(11, country="FI", host="shop-fi.example.com")]), SHOP, now=NOW)
    assert enrolments(conn) == {}                                  # .fi has copy but is not switched on
    live(monkeypatch, markets="shop.example.com,shop-fi.example.com")
    winback.poll(conn, Client([order(11, country="FI", host="shop-fi.example.com")]), SHOP, now=NOW)
    assert [v[0] for v in enrolments(conn).values()] == ["shop-fi.example.com"]


def test_recent_site_activity_leaves_them_alone(conn, monkeypatch):
    live(monkeypatch)
    p = subscribed(conn)
    conn.execute("INSERT INTO events (store, profile_id, type, external_id, payload, ts) VALUES "
                 "('shop-fi.example.com', %s, 'viewed_product', 'v1', '{}'::jsonb, %s)", (p, NOW - timedelta(days=10)))
    conn.commit()
    winback.poll(conn, Client([order(11)]), SHOP, now=NOW)
    assert enrolments(conn) == {}


def test_old_site_activity_does_not_count(conn, monkeypatch):
    live(monkeypatch)
    p = subscribed(conn)
    conn.execute("INSERT INTO events (store, profile_id, type, external_id, payload, ts) VALUES "
                 "('shop.example.com', %s, 'added_to_cart', 'v1', '{}'::jsonb, %s)", (p, NOW - timedelta(days=40)))
    conn.commit()
    assert winback.poll(conn, Client([order(11)]), SHOP, now=NOW)["enrolled"] == 1


def test_someone_in_another_flow_is_left_alone(conn, monkeypatch):
    live(monkeypatch)
    p = subscribed(conn)
    engine.enrol(conn, "browse_abandonment", "shop.example.com", p, now=NOW - timedelta(hours=1), ref="x")
    conn.commit()
    winback.poll(conn, Client([order(11)]), SHOP, now=NOW)
    assert enrolments(conn) == {}


def test_once_per_customer_per_year_holdout_included(conn, monkeypatch):
    live(monkeypatch)
    p = subscribed(conn)
    monkeypatch.setattr(checkouts, "arm_for", lambda ref: "holdout")
    checkouts.enrol_ab(conn, "winback", "shop.example.com", p, NOW - timedelta(days=200), "order:5")
    conn.commit()
    winback.poll(conn, Client([order(11)]), SHOP, now=NOW)
    assert set(enrolments(conn)) == {"order:5"}
    later = NOW + timedelta(days=170)                                # 370 days after the first one
    assert winback.poll(conn, Client([order(12, created=ORDERED + timedelta(days=170))]), SHOP, now=later)["holdout"] == 1


def test_window_and_no_duplicates(conn, monkeypatch):
    live(monkeypatch)
    subscribed(conn)
    c = Client([order(11), order(12, created=NOW - timedelta(days=100))])
    winback.poll(conn, c, SHOP, now=NOW)
    assert set(enrolments(conn)) == {"order:11"}                   # the 100-day-old order is not due yet
    q = c.queries[0]["q"]
    assert "2026-07-06T12:00:00Z" in q and "2026-07-04T12:00:00Z" in q   # 120 days ago, 2 days look-back
    out = winback.poll(conn, c, SHOP, now=NOW + timedelta(minutes=5))
    assert out["enrolled"] == 0 and len(enrolments(conn)) == 1


def test_holdout_gets_no_mail(conn, monkeypatch):
    live(monkeypatch)
    monkeypatch.setattr(checkouts, "arm_for", lambda ref: "holdout")
    subscribed(conn)
    assert winback.poll(conn, Client([order(11)]), SHOP, now=NOW)["holdout"] == 1
    assert enrolments(conn) == {"order:11": ("shop.example.com", "holdout", "completed")}
