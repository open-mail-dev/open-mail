from datetime import datetime, timedelta, timezone

import config
import profiles
import stock

S = "shop.example.com"
T0 = datetime(2026, 11, 3, 12, tzinfo=timezone.utc)


def pid(conn, email="jan@example.org"):
    return profiles.upsert(conn, email)


def states(conn):
    return [r["state"] for r in conn.execute("SELECT state FROM stock_requests ORDER BY id")]


def test_request_is_idempotent_per_variant(conn):
    p = pid(conn)
    a = stock.request(conn, S, p, "1", "11", T0)
    assert stock.request(conn, S, p, "1", "11", T0 + timedelta(hours=1)) == a
    assert stock.request(conn, S, p, "1", "12", T0) != a
    assert len(states(conn)) == 2


def test_expiry_is_six_months_and_expired_requests_are_not_waiting(conn):
    p = pid(conn)
    stock.request(conn, S, p, "1", "11", T0)
    assert [r["variant_id"] for r in stock.waiting(conn, [S], T0 + timedelta(days=182))] == ["11"]
    assert stock.expire(conn, T0 + timedelta(days=184)) == 1
    assert stock.waiting(conn, [S], T0 + timedelta(days=184)) == []
    assert states(conn) == ["expired"]


def test_imported_requests_expire_from_their_original_date(conn):
    p = pid(conn)
    stock.request(conn, S, p, "1", "11", T0, source="import", created_at=T0 - timedelta(days=180))
    assert stock.expire(conn, T0 + timedelta(days=4)) == 1


def test_enrolled_ref_counts_attempts(conn):
    p = pid(conn)
    r = stock.request(conn, S, p, "1", "11", T0)
    ref = stock.mark_enrolled(conn, r)
    assert ref == f"stock:{r}:1" and stock.waiting(conn, [S], T0) == []
    stock.on_skipped(conn, ref)
    assert [w["id"] for w in stock.waiting(conn, [S], T0)] == [r]
    assert stock.mark_enrolled(conn, r) == f"stock:{r}:2"


def test_sent_and_cancelled_are_final(conn):
    p = pid(conn)
    conn.execute("INSERT INTO messages (profile_id, store, kind, marketing, sent_at) VALUES (%s,%s,'x',false,%s)", (p, S, T0))
    m = conn.execute("SELECT id FROM messages").fetchone()["id"]
    r = stock.request(conn, S, p, "1", "11", T0)
    stock.on_sent(conn, stock.mark_enrolled(conn, r), m)
    assert states(conn) == ["sent"]
    assert stock.request(conn, S, p, "1", "11", T0) != r                 # asking again after the mail = a new request
    stock.on_skipped(conn, f"stock:{r}:1")                               # a late skip never reopens a sent request
    assert states(conn) == ["sent", "waiting"]


def test_cancel_by_variant_and_profile(conn):
    a, b = pid(conn), pid(conn, "piet@example.org")
    stock.request(conn, S, a, "1", "11", T0)
    stock.request(conn, S, b, "1", "11", T0)
    stock.request(conn, S, a, "2", "21", T0)
    assert stock.cancel(conn, variant_ids=["11"], profile_id=a, stores=[S]) == 1
    assert stock.cancel(conn, product_ids=["2"], stores=[S]) == 1
    assert states(conn) == ["cancelled", "waiting", "cancelled"]


def test_bis_live_reads_env(monkeypatch):
    monkeypatch.setenv("MAIL_BIS_LIVE", " shop.example.com ,")
    assert config.bis_live() == {"shop.example.com"}


# --- poll (Task 4) ----------------------------------------------------------------------------------------------------
class VShop:
    """variant id → "buyable" | "soldout" | "gone"; fail=True → Shopify down."""
    def __init__(self, states=None, fail=False):
        self.states, self.fail, self.calls = states or {}, fail, 0

    def gql(self, query, variables):
        self.calls += 1
        if self.fail:
            raise RuntimeError("Shopify down")
        out = []
        for gid in variables["ids"]:
            vid = gid.rsplit("/", 1)[-1]
            st = self.states.get(vid, "soldout")
            out.append({"legacyResourceId": vid, "title": "White", "availableForSale": st == "buyable", "price": "99.00", "image": None,
                        "product": {"legacyResourceId": "1", "handle": "p1", "title": "Chair Test", "status": "ACTIVE",
                                    "onlineStoreUrl": "https://x", "productType": "never available again" if st == "gone" else "furniture",
                                    "featuredMedia": {"preview": {"image": {"url": "https://cdn.shopify.com/x.jpg"}}}}})
        return {"nodes": out}


def enrolled(conn):
    return [(r["ref"], r["state"]) for r in conn.execute("SELECT ref, state FROM enrolments WHERE flow='back_in_stock' ORDER BY id")]


def test_poll_enrols_only_buyable_variants_and_cancels_gone_ones(conn):
    p = pid(conn)
    a = stock.request(conn, S, p, "1", "11", T0)
    stock.request(conn, S, p, "1", "12", T0)
    stock.request(conn, S, p, "1", "13", T0)
    conn.commit()
    out = stock.poll(conn, VShop({"11": "buyable", "13": "gone"}), [S], now=T0 + timedelta(hours=1))
    assert out == {"enrolled": 1, "cancelled": 1, "expired": 0}
    assert enrolled(conn) == [(f"stock:{a}:1", "active")]
    assert states(conn) == ["enrolled", "waiting", "cancelled"]
    ev = conn.execute("SELECT payload FROM events WHERE type='back_in_stock'").fetchone()["payload"]
    assert ev == {"variant_id": "11", "product_id": "1"}


def test_poll_leaves_state_alone_when_shopify_is_down(conn):
    import pytest
    stock.request(conn, S, pid(conn), "1", "11", T0)
    conn.commit()
    with pytest.raises(RuntimeError):
        stock.poll(conn, VShop(fail=True), [S], now=T0)
    assert states(conn) == ["waiting"] and enrolled(conn) == []


def test_poll_with_nothing_waiting_asks_shopify_nothing(conn):
    shop = VShop()
    assert stock.poll(conn, shop, [S], now=T0) == {"enrolled": 0, "cancelled": 0, "expired": 0} and shop.calls == 0
