from datetime import datetime, timedelta, timezone

import profiles
import sitetrack
import tokens

S = "shop.example.com"
T0 = datetime(2026, 11, 3, 12, tzinfo=timezone.utc)


import pytest  # noqa: E402


@pytest.fixture(autouse=True)
def _site_live(monkeypatch):
    monkeypatch.setenv("MAIL_SITE_LIVE", "shop.example.com,shop-fi.example.com,outlet.example.com")


def subscriber(conn, email="jan@example.org", store=S):
    p = profiles.upsert(conn, email)
    profiles.set_consent(conn, p, store, "subscribed", "popup")
    return p


def body(p, **over):
    d = {"store": S, "vid": tokens.visitor(p, "k"), "type": "viewed_product", "product_id": "8812345678901"}
    d.update(over)
    return d


def events(conn):
    return conn.execute("SELECT store, profile_id, type, payload FROM events WHERE type IN ('viewed_product','added_to_cart')").fetchall()


def test_stores_a_product_view_for_a_subscriber(conn):
    p = subscriber(conn)
    assert sitetrack.track(conn, body(p), "k", now=T0) == (200, {"ok": True})
    [e] = events(conn)
    assert (e["store"], e["profile_id"], e["type"]) == (S, p, "viewed_product")
    assert e["payload"] == {"product_id": "8812345678901", "variant_id": None}


def test_stores_a_cart_add_with_its_variant(conn):
    p = subscriber(conn)
    sitetrack.track(conn, body(p, type="added_to_cart", variant_id="4711"), "k", now=T0)
    assert events(conn)[0]["payload"] == {"product_id": "8812345678901", "variant_id": "4711"}


def test_nothing_stored_for_unsubscribed_or_other_store_or_bad_token(conn):
    p = subscriber(conn)
    profiles.set_consent(conn, p, S, "unsubscribed", "unsubscribe_link")
    q = subscriber(conn, "els@x.fi", store="shop-fi.example.com")              # subscribed in .fi only
    for b in (body(p), body(q), body(p, vid="garbage"), body(p, vid=tokens.sign({"m": 1, "a": "open"}, "k"))):
        assert sitetrack.track(conn, b, "k", now=T0) == (200, {"ok": True})
    assert events(conn) == []


def test_store_without_site_flows_stores_nothing(conn):
    p = subscriber(conn, store="outlet.example.com")
    assert sitetrack.track(conn, body(p, store="outlet.example.com"), "k", now=T0)[0] == 200
    assert events(conn) == []


def test_bad_input_400(conn):
    p = subscriber(conn)
    for b in (body(p, product_id="abc"), body(p, type="purchase"), body(p, variant_id="x1"), body(p, store="evil.example"),
              body(p, product_id=None)):
        assert sitetrack.track(conn, b, "k", now=T0)[0] == 400
    assert events(conn) == []


def test_rate_limit_60_per_hour_drops_silently(conn):
    p = subscriber(conn)
    for i in range(60):
        assert sitetrack.track(conn, body(p, product_id=str(i + 1)), "k", now=T0 + timedelta(seconds=i))[0] == 200
    assert sitetrack.track(conn, body(p, product_id="999"), "k", now=T0 + timedelta(minutes=2)) == (200, {"ok": True})
    assert len(events(conn)) == 60          # dropped silently: a 429 would tell an outsider the address is subscribed
    assert sitetrack.track(conn, body(p, product_id="999"), "k", now=T0 + timedelta(minutes=61))[0] == 200


# --- Task 3: enrolment rules + exits -------------------------------------------------------------------------------
import checkouts  # noqa: E402
import engine  # noqa: E402
import pytest  # noqa: E402


@pytest.fixture
def treatment(monkeypatch):
    monkeypatch.setattr(checkouts, "arm_for", lambda ref: "treatment")


def enrols(conn, flow=None):
    q = "SELECT flow, state, arm, exit_reason, started_at FROM enrolments" + (" WHERE flow=%s" if flow else "") + " ORDER BY id"
    return conn.execute(q, (flow,) if flow else ()).fetchall()


def view(conn, p, at, product="1"):
    return sitetrack.track(conn, body(p, product_id=product), "k", now=at)


def add(conn, p, at, product="1", variant="11"):
    return sitetrack.track(conn, body(p, type="added_to_cart", product_id=product, variant_id=variant), "k", now=at)


def test_many_views_make_one_browse_enrolment(conn, treatment):
    p = subscriber(conn)
    for i in range(20):
        view(conn, p, T0 + timedelta(seconds=15 * i), product=str(i + 1))
    [e] = enrols(conn, "browse_abandonment")
    assert (e["state"], e["arm"], e["started_at"]) == ("active", "treatment", T0)
    steps = conn.execute("SELECT step, due_at FROM flow_steps ORDER BY step").fetchall()
    assert [(s["step"], s["due_at"] - T0) for s in steps] == [(1, timedelta(hours=2)), (2, timedelta(hours=26))]


def test_browse_again_only_after_14_days(conn, treatment):
    p = subscriber(conn)
    view(conn, p, T0)
    view(conn, p, T0 + timedelta(days=13))
    assert len(enrols(conn, "browse_abandonment")) == 1
    view(conn, p, T0 + timedelta(days=15))
    assert len(enrols(conn, "browse_abandonment")) == 2


def test_no_browse_during_welcome_but_cart_reminder_yes(conn, treatment):
    p = subscriber(conn)
    engine.enrol(conn, "welcome", S, p, now=T0 - timedelta(days=1))
    view(conn, p, T0)
    add(conn, p, T0 + timedelta(minutes=5))
    assert enrols(conn, "browse_abandonment") == [] and len(enrols(conn, "cart_reminder")) == 1


def test_cart_add_ends_browse_and_starts_one_cart_reminder_per_7_days(conn, treatment):
    p = subscriber(conn)
    view(conn, p, T0)
    add(conn, p, T0 + timedelta(minutes=10))
    add(conn, p, T0 + timedelta(days=6), product="2", variant="22")
    [b] = enrols(conn, "browse_abandonment")
    assert (b["state"], b["exit_reason"]) == ("exited", "added_to_cart")
    assert len(enrols(conn, "cart_reminder")) == 1
    add(conn, p, T0 + timedelta(days=8), product="3", variant="33")
    assert len(enrols(conn, "cart_reminder")) == 2


def test_no_cart_reminder_while_an_abandoned_checkout_runs(conn, treatment):
    p = subscriber(conn)
    engine.enrol(conn, "abandoned_checkout", S, p, now=T0, ref="gid://c1", arm="always")
    add(conn, p, T0 + timedelta(minutes=5))
    assert enrols(conn, "cart_reminder") == []


def test_holdout_enrolment_is_completed_without_mails(conn, monkeypatch):
    monkeypatch.setattr(checkouts, "arm_for", lambda ref: "holdout")
    p = subscriber(conn)
    view(conn, p, T0)
    [e] = enrols(conn, "browse_abandonment")
    assert (e["state"], e["arm"], e["exit_reason"]) == ("completed", "holdout", "holdout")
    assert {r["last_error"] for r in conn.execute("SELECT last_error FROM flow_steps")} == {"skip:holdout"}
    view(conn, p, T0 + timedelta(days=2))                       # the holdout still blocks re-entry for 14 days
    assert len(enrols(conn, "browse_abandonment")) == 1


def test_starting_a_checkout_ends_browse_and_cart(conn, treatment):
    p = subscriber(conn, "nieuw@example.org")
    view(conn, p, T0 - timedelta(hours=3), product="5")
    add(conn, p, T0 - timedelta(hours=3), product="6", variant="66")
    from tests.test_checkouts import SHOP, Client, node
    checkouts.poll(conn, Client([node("gid://x1", "nieuw@example.org", created=T0 - timedelta(hours=2))]), SHOP, T0 - timedelta(hours=6), now=T0)
    by = {e["flow"]: (e["state"], e["exit_reason"]) for e in enrols(conn)}
    assert by["cart_reminder"] == ("exited", "checkout_started")
    assert by["browse_abandonment"] == ("exited", "added_to_cart")


def test_an_order_ends_browse_and_cart(conn, treatment):
    p = subscriber(conn)
    view(conn, p, T0)
    q = subscriber(conn, "els@example.org")
    add(conn, q, T0)
    engine.exit_if_ordered(conn, S, p)
    engine.exit_if_ordered(conn, S, q)
    assert {e["exit_reason"] for e in enrols(conn)} == {"order_placed"}


def test_market_not_switched_on_stores_nothing(conn, monkeypatch):
    monkeypatch.setenv("MAIL_SITE_LIVE", "shop-fi.example.com")
    p = subscriber(conn)
    assert sitetrack.track(conn, body(p), "k", now=T0) == (200, {"ok": True})
    assert events(conn) == []


def test_non_object_body_is_400(conn):
    for b in ([1], "x", None):
        assert sitetrack.track(conn, b, "k", now=T0)[0] == 400


def test_parallel_cart_adds_make_one_enrolment(conn, postgresql, treatment):
    import threading
    import db
    i = postgresql.info
    dsn = f"host={i.host} port={i.port} user={i.user} dbname={i.dbname}"
    p = subscriber(conn); conn.commit()
    a, b = db.connect(dsn), db.connect(dsn)

    def add_in(c, v):
        eid = c.execute("INSERT INTO events (store, profile_id, type, payload, ts) VALUES (%s,%s,'added_to_cart','{}'::jsonb,%s) RETURNING id",
                        (S, p, T0)).fetchone()["id"]
        return sitetrack.on_event(c, S, p, "added_to_cart", eid, T0)

    assert add_in(a, "1") == "enrolled"                        # a holds its locks, not committed yet
    out = {}
    t = threading.Thread(target=lambda: out.setdefault("b", (add_in(b, "2"), b.commit())))
    t.start(); t.join(0.5)
    assert t.is_alive()                                        # b waits for a's profile lock …
    a.commit(); t.join(5)
    assert out["b"][0] == "skipped"                            # … and then sees a's enrolment
    assert len(enrols(conn, "cart_reminder")) == 1
    a.close(); b.close()


def test_order_or_checkout_on_another_market_of_the_same_shop_ends_reminders(conn, treatment):
    p = subscriber(conn)
    add(conn, p, T0)
    engine.exit_if_ordered(conn, "shop-fi.example.com", p)             # order shipped to FI → routed to the .fi store key
    assert enrols(conn, "cart_reminder")[0]["exit_reason"] == "order_placed"


def test_checkout_to_a_country_we_do_not_serve_still_ends_reminders(conn, treatment):
    p = subscriber(conn, "nieuw@example.org")
    add(conn, p, T0 - timedelta(hours=3))
    from tests.test_checkouts import SHOP, Client, node
    for cid, country in (("gid://at1", "AT"), ("gid://se1", "SE")):
        conn.execute("UPDATE enrolments SET state='active', exit_reason=NULL")
        checkouts.poll(conn, Client([node(cid, "nieuw@example.org", country=country, host="shop.example.com", created=T0 - timedelta(hours=2))]),
                       SHOP, T0 - timedelta(hours=6), now=T0)
        assert enrols(conn, "cart_reminder")[0]["exit_reason"] == "checkout_started", country


# --- logged-in customers: the theme signs "email|store" with the shop's ident key (Liquid hmac_sha256, hex) ---
import hashlib  # noqa: E402
import hmac  # noqa: E402


def sig(email, store=S, key="ik"):
    return hmac.new(key.encode(), f"{email}|{store}".encode(), hashlib.sha256).hexdigest()


def ident(conn, b, key="ik"):
    return sitetrack.ident(conn, b, key, "k")


def test_ident_gives_a_logged_in_subscriber_their_visitor_id(conn):
    p = subscriber(conn)
    assert ident(conn, {"store": S, "email": "jan@example.org", "sig": sig("jan@example.org")}) == (200, {"vid": tokens.visitor(p, "k")})


def test_ident_says_no_id_for_a_logged_in_customer_who_is_not_subscribed(conn):
    """vid None = this browser now belongs to someone we may not mail: the snippet drops any id it kept (shared PC)."""
    profiles.upsert(conn, "els@example.org")
    subscriber(conn, "piet@example.org", store="shop-fi.example.com")
    assert ident(conn, {"store": S, "email": "els@example.org", "sig": sig("els@example.org")}) == (200, {"vid": None})
    assert ident(conn, {"store": S, "email": "nieuw@example.org", "sig": sig("nieuw@example.org")}) == (200, {"vid": None})
    assert ident(conn, {"store": S, "email": "piet@example.org", "sig": sig("piet@example.org")}) == (200, {"vid": None})
    assert conn.execute("SELECT count(*) AS n FROM profiles WHERE email='nieuw@example.org'").fetchone()["n"] == 0


def test_ident_without_a_valid_signature_says_nothing(conn):
    """{} = can't tell: the snippet keeps whatever it had. Covers forged email, another store's signature, no key set."""
    subscriber(conn)
    assert ident(conn, {"store": S, "email": "jan@example.org", "sig": sig("jan@example.org", key="other")}) == (200, {})
    assert ident(conn, {"store": S, "email": "jan@example.org", "sig": sig("jan@example.org", store="shop-fi.example.com")}) == (200, {})
    assert ident(conn, {"store": S, "email": "jan@example.org", "sig": sig("jan@example.org", key="")}, key="") == (200, {})


def test_ident_says_nothing_when_the_market_is_switched_off(conn, monkeypatch):
    subscriber(conn)
    monkeypatch.setenv("MAIL_SITE_LIVE", "shop-fi.example.com")
    assert ident(conn, {"store": S, "email": "jan@example.org", "sig": sig("jan@example.org")}) == (200, {})


def test_ident_bad_input_400(conn):
    assert ident(conn, ["x"])[0] == 400
    assert ident(conn, {"store": "nope", "email": "a@b.nl", "sig": "00"})[0] == 400
    assert ident(conn, {"store": S, "email": 5, "sig": "00"})[0] == 400
    assert ident(conn, {"store": S, "email": "a@b.nl"})[0] == 400
