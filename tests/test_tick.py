import re
import threading
from datetime import datetime, timedelta, timezone

import db
import engine
import migrate
import profiles
import slack
import tick

S = "outlet.example.com"
T0 = datetime(2026, 10, 1, 12, tzinfo=timezone.utc)


class SES:
    def __init__(self, fail=False):
        self.fail, self.sent = fail, []

    def send_email(self, **kw):
        if self.fail:
            raise RuntimeError("SES down")
        self.sent.append(kw)
        return {"MessageId": f"m{len(self.sent)}"}


def setup(conn):
    p = profiles.upsert(conn, "jan@example.org")
    profiles.set_consent(conn, p, S, "subscribed", "popup")
    conn.execute("INSERT INTO coupons (profile_id, store, pool, code, created_at) VALUES (%s,%s,'welcome10','WELCOME-AAAA-BBBB',%s)", (p, S, T0))
    engine.enrol(conn, "welcome", S, p, now=T0)
    conn.commit()
    return p


def test_tick_sends_step1_once(conn):
    setup(conn)
    ses = SES()
    assert tick.run(conn, ses, "k", "https://mail.example", now=T0)["sent"] == 1
    assert tick.run(conn, ses, "k", "https://mail.example", now=T0)["sent"] == 0
    assert "WELCOME-AAAA-BBBB" in ses.sent[0]["Content"]["Simple"]["Subject"]["Data"]


def test_send_failure_leaves_step_pending(conn):
    setup(conn)
    out = tick.run(conn, SES(fail=True), "k", "https://mail.example", now=T0)
    assert out["failed"] == 1
    row = conn.execute("SELECT status, attempts, last_error FROM flow_steps WHERE step=1").fetchone()
    assert row["status"] == "pending" and row["attempts"] == 1 and "SES down" in row["last_error"]
    assert tick.run(conn, SES(), "k", "https://mail.example", now=T0 + timedelta(minutes=5))["sent"] == 1


def test_backoff_pushes_due_at_after_failure(conn):
    """Review round 1, item 5b: a non-final failure pushes due_at by
    min(5 * 2**(attempts-1), 240) minutes, not an immediate retry."""
    setup(conn)
    tick.run(conn, SES(fail=True), "k", "https://mail.example", now=T0)
    row = conn.execute("SELECT attempts, due_at FROM flow_steps WHERE step=1").fetchone()
    assert row["attempts"] == 1
    assert row["due_at"] == T0 + timedelta(minutes=5)   # min(5 * 2**0, 240)


def test_step_marked_failed_after_8_attempts(conn):
    """Review round 1, item 5c: MAX_ATTEMPTS is 8, and each non-final failure
    pushes due_at by the backoff — advance `now` to the row's own new due_at
    each round rather than a fixed step."""
    setup(conn)
    now = T0
    row = None
    for _ in range(8):
        tick.run(conn, SES(fail=True), "k", "https://mail.example", now=now)
        row = conn.execute("SELECT attempts, due_at, status FROM flow_steps WHERE step=1").fetchone()
        now = row["due_at"]
    assert row["status"] == "failed"
    assert row["attempts"] == 8


def test_failed_step_triggers_slack_alert(conn, monkeypatch):
    """Review round 1, item 5d."""
    setup(conn)
    alerts = []
    monkeypatch.setattr(slack, "alert", lambda text: alerts.append(text))
    now = T0
    row = None
    for _ in range(8):
        tick.run(conn, SES(fail=True), "k", "https://mail.example", now=now)
        row = conn.execute("SELECT due_at, status FROM flow_steps WHERE step=1").fetchone()
        now = row["due_at"]
    assert row["status"] == "failed"
    assert any("failed" in a and "step" in a for a in alerts)


def test_heartbeat_flags_stuck_queue(conn):
    setup(conn)
    assert tick.heartbeat(conn, now=T0 + timedelta(hours=2)) is False
    tick.run(conn, SES(), "k", "https://mail.example", now=T0 + timedelta(hours=2))
    assert tick.heartbeat(conn, now=T0 + timedelta(hours=2)) is True


def test_heartbeat_ignores_exited_enrolments(conn):
    """Review round 1, item 4: exit_if_ordered leaves the remaining flow_steps
    'pending' (by design — engine.py doesn't touch them), so an exited
    enrolment's stale pending steps must not trip the stuck-queue alert."""
    p = setup(conn)
    assert engine.exit_if_ordered(conn, S, p) == 1
    conn.commit()
    assert tick.heartbeat(conn, now=T0 + timedelta(hours=2)) is True


def test_unconfirmed_send_after_mark_failure_is_not_resent(conn, monkeypatch):
    """Review round 1, item 2: SES accepted the message but the DB write that
    records it then failed. The step must end up 'sent' (never pending, to
    avoid a duplicate send), with a 'unconfirmed: ...' last_error, and a
    second tick must not resend it."""
    setup(conn)
    alerts = []
    monkeypatch.setattr(slack, "alert", lambda text: alerts.append(text))
    real_mark, calls = engine.mark, []

    def flaky_mark(*a, **kw):   # the first write fails (transient DB error), the recovery's succeeds
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("db down")
        return real_mark(*a, **kw)
    monkeypatch.setattr(engine, "mark", flaky_mark)

    ses = SES()
    out = tick.run(conn, ses, "k", "https://mail.example", now=T0)
    assert out["sent"] == 1
    assert len(ses.sent) == 1

    row = conn.execute("SELECT status, last_error FROM flow_steps WHERE step=1").fetchone()
    assert row["status"] == "sent"
    assert row["last_error"].startswith("unconfirmed:")
    assert any("unconfirmed" in a for a in alerts)

    # The messages row must exist (the original INSERT was rolled back along
    # with the rest of the failed transaction) so the frequency cap and
    # heartbeat see the send.
    msg = conn.execute(
        "SELECT store, kind, marketing, ses_id FROM messages WHERE profile_id=(SELECT id FROM profiles WHERE email='jan@example.org')"
    ).fetchone()
    assert msg is not None
    assert msg["store"] == S
    assert msg["kind"] == "flow:welcome:1"
    assert msg["marketing"] is False
    assert msg["ses_id"] == "m1"

    # The delivered mail's unsubscribe/click tokens are signed with the ORIGINAL message id:
    # the re-inserted row must carry that id, or every link in the mail 404s forever.
    import tokens
    html = ses.sent[0]["Content"]["Simple"]["Body"]["Html"]["Data"]
    signed_id = tokens.verify(html.split("https://mail.example/u/")[1].split('"')[0], "k")["m"]
    assert conn.execute("SELECT count(*) n FROM messages WHERE id=%s", (signed_id,)).fetchone()["n"] == 1
    assert conn.execute("SELECT message_id FROM flow_steps WHERE step=1").fetchone()["message_id"] == signed_id

    # a second tick must not re-send: the step is no longer pending.
    ses2 = SES()
    assert tick.run(conn, ses2, "k", "https://mail.example", now=T0)["sent"] == 0
    assert len(ses2.sent) == 0


def test_lost_race_refunds_speculative_attempt(postgresql, monkeypatch):
    """Review round 1, item 5a: when a tick's per-row re-lock finds the row
    already held (or resolved) elsewhere, it must give back the attempt its
    own claim_due speculatively bumped.

    b claims the row first and commits (a stale, speculative bump). a then
    really claims + re-locks it and its send fails (non-final), leaving the
    row 'pending' after a's commit. b's lost-race re-lock — replayed against
    the stale claim via a monkeypatched claim_due — then unblocks once a
    commits, sees the row still 'pending', and refunds its speculative bump.
    """
    i = postgresql.info
    dsn = f"host={i.host} port={i.port} user={i.user} dbname={i.dbname}"
    a = db.connect(dsn)
    migrate.apply(a)
    setup(a)
    b = db.connect(dsn)
    try:
        stale = engine.claim_due(b, now=T0)   # b's speculative bump: 0 -> 1
        b.commit()

        in_send, release = threading.Event(), threading.Event()

        class HoldFailSES:
            def send_email(self, **kw):
                in_send.set()
                release.wait(2)
                raise RuntimeError("SES down")

        ta = threading.Thread(target=lambda: tick.run(a, HoldFailSES(), "k", "https://mail.example", now=T0))
        ta.start()
        assert in_send.wait(5)

        real_claim_due = engine.claim_due
        monkeypatch.setattr(engine, "claim_due", lambda conn, **kw: stale)
        b_out = tick.run(b, SES(), "k", "https://mail.example", now=T0)
        monkeypatch.setattr(engine, "claim_due", real_claim_due)

        release.set()
        ta.join(5)

        row = a.execute("SELECT attempts, status FROM flow_steps WHERE step=1").fetchone()
        assert row["status"] == "pending"
        assert row["attempts"] == 1   # a's real bump (0->1) survives; b's stale bump was refunded
        assert b_out == {"sent": 0, "skipped": 0, "deferred": 0, "failed": 0}
    finally:
        b.close()
        a.close()


def test_overlap_deterministic(postgresql, monkeypatch):
    """Review round 1, item 1: deterministic replacement for the previous
    thread-timing overlap test, which the reviewer's positive control showed
    passed even with the per-row re-lock removed (claim_due's own SKIP
    LOCKED, or plain sequential runs, made it trivially pass regardless).

    b claims the row and commits — a stale claim. While a is blocked inside
    send_email (holding the per-row lock the whole time), b replays that
    stale claim (via a monkeypatched claim_due, since a real claim_due call
    would just re-claim nothing new) straight into tick.run's per-row
    re-lock, which must find the row locked and skip it — never send.
    """
    i = postgresql.info
    dsn = f"host={i.host} port={i.port} user={i.user} dbname={i.dbname}"
    a = db.connect(dsn)
    migrate.apply(a)
    setup(a)
    b = db.connect(dsn)
    try:
        stale = engine.claim_due(b, now=T0)
        b.commit()   # b claimed in the window before a re-locks

        in_send, release = threading.Event(), threading.Event()

        class HoldSES:
            def __init__(self):
                self.calls = []

            def send_email(self, **kw):
                self.calls.append(kw)
                in_send.set()
                release.wait(5)
                return {"MessageId": "a"}

        class FastSES:
            def __init__(self):
                self.calls = []

            def send_email(self, **kw):
                self.calls.append(kw)
                return {"MessageId": "b"}

        sa, sb = HoldSES(), FastSES()
        ta = threading.Thread(target=lambda: tick.run(a, sa, "k", "https://mail.example", now=T0))
        ta.start()
        assert in_send.wait(5)

        real_claim_due = engine.claim_due
        monkeypatch.setattr(engine, "claim_due", lambda conn, **kw: stale)
        tick.run(b, sb, "k", "https://mail.example", now=T0)
        monkeypatch.setattr(engine, "claim_due", real_claim_due)

        release.set()
        ta.join(5)

        assert len(sa.calls) + len(sb.calls) == 1
        n = a.execute("SELECT count(*) n FROM messages WHERE kind='flow:welcome:1'").fetchone()["n"]
        assert n == 1
    finally:
        b.close()
        a.close()


class FakeShop:
    def __init__(self, fail=False):
        self.fail, self.calls = fail, []

    def gql(self, query, variables):
        self.calls.append(variables)
        if self.fail:
            raise RuntimeError("Shopify down")
        node = {"handle": "chair-x", "title": "Chair X", "status": "ACTIVE", "onlineStoreUrl": "https://x",
                "featuredMedia": {"preview": {"image": {"url": "https://cdn.shopify.com/x.jpg"}}},
                "variants": {"nodes": [{"availableForSale": True, "price": "249.00"}]}}
        return {"viewed": {"nodes": []}, "top": {"products": {"nodes": [node]}}}


def _html(ses):
    return ses.sent[-1]["Content"]["Simple"]["Body"]["Html"]["Data"]


def test_step1_shows_live_products(conn):
    setup(conn)
    ses, shop = SES(), FakeShop()
    assert tick.run(conn, ses, "k", "https://mail.example", now=T0, shop_factory=lambda s: shop)["sent"] == 1
    assert ">Chair X</div>" in _html(ses) and "€249.00" in _html(ses)
    assert shop.calls[0]["top"] == "best-sellers"


def test_shopify_failure_still_sends_without_products(conn):
    setup(conn)
    ses = SES()
    assert tick.run(conn, ses, "k", "https://mail.example", now=T0, shop_factory=lambda s: FakeShop(fail=True))["sent"] == 1
    assert "€249,00" not in _html(ses)


def test_viewed_handles_from_signup_reach_the_catalog_query(conn):
    p = setup(conn)
    conn.execute("INSERT INTO events (store, profile_id, type, payload) VALUES (%s,%s,'signup','{\"viewed\":[\"chair-y\"]}')", (S, p))
    conn.commit()
    shop = FakeShop()
    tick.run(conn, SES(), "k", "https://mail.example", now=T0, shop_factory=lambda s: shop)
    assert shop.calls[0]["q"] == "handle:chair-y"


def test_steps_after_code_expiry_are_skipped(conn):
    setup(conn)
    ses = SES()
    tick.run(conn, ses, "k", "https://mail.example", now=T0)
    late = T0 + timedelta(days=16)          # past 14 Oct 23:59 Amsterdam
    out = tick.run(conn, ses, "k", "https://mail.example", now=late)
    assert out["sent"] == 0 and out["skipped"] == 2
    errs = [r["last_error"] for r in conn.execute("SELECT last_error FROM flow_steps WHERE step > 1")]
    assert errs == ["skip:coupon-expired"] * 2


def test_step3_counts_down_to_expiry(conn):
    setup(conn)
    ses = SES()
    for d in (0, 2, 11):
        tick.run(conn, ses, "k", "https://mail.example", now=T0 + timedelta(days=d))
    subject = ses.sent[-1]["Content"]["Simple"]["Subject"]["Data"]
    assert subject.startswith("3 days left")   # T0+11d = 12 Oct; code valid until 15 Oct → 3 calendar days


def test_each_store_sends_through_its_own_configuration_set(conn):
    import stores
    setup(conn)
    ses = SES()
    tick.run(conn, ses, "k" * 32, "https://mail.example", now=T0)
    assert ses.sent[0]["ConfigurationSetName"] == "mail-example-outlet"
    # one set per sending domain: subfolder markets (path_prefix) send from their domain's set too
    by_domain = {}
    for s in stores.all_stores():
        by_domain.setdefault(s.sending_domain, set()).add(s.configuration_set)
    sets = [next(iter(v)) for v in by_domain.values()]
    assert all(len(v) == 1 for v in by_domain.values()) and len(set(sets)) == len(sets) and "mail-shop-example-com" in sets


def _checkout(conn, store, email, ref, arm="treatment", with_cart=True):
    import json
    p = profiles.upsert(conn, email)
    e = engine.enrol(conn, "abandoned_checkout", store, p, now=T0, ref=ref, arm=arm)
    if with_cart:
        conn.execute("INSERT INTO events (store, profile_id, type, external_id, payload, ts) VALUES (%s,%s,'checkout_started',%s,%s::jsonb,%s)",
                     (store, p, ref, json.dumps({"url": f"https://{store}/1/checkouts/ac/x", "currency": "SEK", "total": 446.0,
                                                 "items": [{"title": "Sminkväska SC 40", "quantity": 1, "line_price": 446.0, "image": None}]}), T0))
    return e


def test_abandoned_checkout_sends_through_its_market(conn):
    _checkout(conn, "shop-fi.example.com", "asiakas@x.fi", "c1")
    ses = SES()
    assert tick.run(conn, ses, "k" * 32, "https://mail.example", now=T0 + timedelta(hours=2))["sent"] == 1
    m = ses.sent[0]
    assert m["ConfigurationSetName"] == "mail-shop-fi-example-com" and m["FromEmailAddress"] == "Example Shop <hello@mail.shop-fi.example.com>"
    assert m["Destination"]["ToAddresses"] == ["asiakas@x.fi"] and "446,00 kr" in m["Content"]["Simple"]["Body"]["Html"]["Data"]


def test_abandoned_checkout_without_its_cart_is_skipped_not_sent(conn):
    _checkout(conn, "shop.example.com", "a@x.nl", "c2", with_cart=False)
    ses = SES()
    out = tick.run(conn, ses, "k" * 32, "https://mail.example", now=T0 + timedelta(hours=2))
    assert out["skipped"] == 1 and ses.sent == []


def test_sent_mail_carries_an_open_pixel_for_its_own_message(conn):
    import tokens
    setup(conn)
    ses = SES()
    tick.run(conn, ses, "k", "https://mail.example", now=T0)
    html = ses.sent[0]["Content"]["Simple"]["Body"]["Html"]["Data"]
    tok = re.search(r'<img src="https://mail\.example/o/([^"]+)\.gif"', html).group(1)
    m = conn.execute("SELECT id FROM messages").fetchone()["id"]
    assert tokens.verify(tok, "k") == {"m": m, "a": "open"}
    assert "/o/" not in ses.sent[0]["Content"]["Simple"]["Body"]["Text"]["Data"]


# --- browse_abandonment / cart_reminder content ---------------------------------------------------------------------
class IdShop:
    """Answers the viewed-products query with one live product per requested id."""
    def __init__(self, fail=False, live=True):
        self.fail, self.live, self.calls = fail, live, []

    def gql(self, query, variables):
        self.calls.append(variables)
        if self.fail:
            raise RuntimeError("Shopify down")
        ids = re.findall(r"id:(\d+)", variables["q"])
        nodes = [{"id": f"gid://shopify/Product/{i}", "legacyResourceId": i, "handle": f"p{i}", "title": f"Chair {i}", "status": "ACTIVE",
                  "onlineStoreUrl": f"https://shop.example.com/products/p{i}",
                  "featuredMedia": {"preview": {"image": {"url": "https://cdn.shopify.com/x.jpg"}}},
                  "variants": {"nodes": [{"availableForSale": True, "price": "249.00"}]}} for i in ids] if self.live else []
        return {"viewed": {"nodes": nodes}, "top": {"products": {"nodes": []}}}


def site_setup(conn, monkeypatch, flow="browse_abandonment"):
    import checkouts
    monkeypatch.setenv("MAIL_SITE_LIVE", "shop.example.com")
    import sitetrack
    import tokens
    monkeypatch.setattr(checkouts, "arm_for", lambda ref: "treatment")
    p = profiles.upsert(conn, "jan@example.org")
    profiles.set_consent(conn, p, "shop.example.com", "subscribed", "popup")
    kind = "viewed_product" if flow == "browse_abandonment" else "added_to_cart"
    for i, pid in enumerate(["1", "2", "1", "3", "4"]):           # 4 newest, 1 viewed twice
        sitetrack.track(conn, {"store": "shop.example.com", "vid": tokens.visitor(p, "k"), "type": kind, "product_id": pid,
                               "variant_id": "9" if kind == "added_to_cart" else None}, "k", now=T0 + timedelta(minutes=i))
    return p


def test_browse_mail_shows_the_four_newest_distinct_products(conn, monkeypatch):
    site_setup(conn, monkeypatch)
    ses, shop = SES(), IdShop()
    out = tick.run(conn, ses, "k", "https://mail.example", now=T0 + timedelta(hours=2, minutes=5), shop_factory=lambda s: shop)
    assert out["sent"] == 1
    assert re.findall(r"id:(\d+)", shop.calls[0]["q"]) == ["4", "3", "1", "2"]      # newest first, the repeat view once
    html = _html(ses)
    assert all(f"Chair {i}" in html for i in "1234")


def test_cart_mail_sends_with_the_added_product(conn, monkeypatch):
    site_setup(conn, monkeypatch, flow="cart_reminder")
    ses = SES()
    assert tick.run(conn, ses, "k", "https://mail.example", now=T0 + timedelta(hours=2, minutes=5), shop_factory=lambda s: IdShop())["sent"] == 1
    assert "Chair 4" in _html(ses)


def test_site_mail_without_live_products_is_skipped(conn, monkeypatch):
    site_setup(conn, monkeypatch)
    ses = SES()
    out = tick.run(conn, ses, "k", "https://mail.example", now=T0 + timedelta(hours=3), shop_factory=lambda s: IdShop(live=False))
    assert out["skipped"] == 1 and ses.sent == []
    assert conn.execute("SELECT last_error FROM flow_steps WHERE step=1").fetchone()["last_error"] == "skip:no-products"


def test_site_mail_waits_when_shopify_is_down(conn, monkeypatch):
    site_setup(conn, monkeypatch)
    ses = SES()
    out = tick.run(conn, ses, "k", "https://mail.example", now=T0 + timedelta(hours=3), shop_factory=lambda s: IdShop(fail=True))
    assert out["failed"] == 1 and ses.sent == []
    assert conn.execute("SELECT status FROM flow_steps WHERE step=1").fetchone()["status"] == "pending"


def test_site_mail_of_a_market_switched_off_is_skipped(conn, monkeypatch):
    site_setup(conn, monkeypatch)
    monkeypatch.setenv("MAIL_SITE_LIVE", "")                   # kill switch after enrolment
    ses = SES()
    out = tick.run(conn, ses, "k", "https://mail.example", now=T0 + timedelta(hours=3), shop_factory=lambda s: IdShop())
    assert out["skipped"] == 1 and ses.sent == []
    assert conn.execute("SELECT last_error FROM flow_steps WHERE step=1").fetchone()["last_error"] == "skip:market-off"


# --- post_purchase ---------------------------------------------------------------------------------------------------
class FbtShop(IdShop):
    """Ordered products each carry an FBT list of one product id + 100."""
    def gql(self, query, variables):
        data = super().gql(query, variables)
        for n in data["viewed"]["nodes"]:
            j = str(int(n["legacyResourceId"]) + 100)
            n["fbt"] = {"references": {"nodes": [{**n, "legacyResourceId": j, "handle": f"p{j}", "title": f"Chair {j}"}]}}
        return data


def pp_setup(conn, monkeypatch):
    import checkouts
    import orders
    monkeypatch.setenv("MAIL_PP_LIVE", "shop.example.com")
    monkeypatch.setattr(checkouts, "arm_for", lambda ref: "treatment")
    p = profiles.upsert(conn, "jan@example.org")
    profiles.set_consent(conn, p, "shop.example.com", "subscribed", "popup")
    import base64, hashlib, hmac, json
    body = json.dumps({"id": 77, "name": "#NL1001", "email": "jan@example.org", "total_price": "249.00", "discount_codes": [],
                       "shipping_address": {"country_code": "NL"}, "line_items": [{"product_id": 1}, {"product_id": 2}]}).encode()
    sig = base64.b64encode(hmac.new(b"w", body, hashlib.sha256).digest()).decode()
    assert orders.handle(conn, body, {"X-Shopify-Hmac-Sha256": sig, "X-Shopify-Shop-Domain": "example-shop.myshopify.com",
                                      "X-Shopify-Topic": "orders/create"}, "w", now=T0) == 200
    return p


def test_post_purchase_mail_gets_order_name_and_fbt_products(conn, monkeypatch):
    pp_setup(conn, monkeypatch)
    ses, shop = SES(), FbtShop()
    assert tick.run(conn, ses, "k", "https://mail.example", now=T0 + timedelta(days=1, minutes=5), shop_factory=lambda s: shop)["sent"] == 1
    assert "#NL1001" in _html(ses)
    assert tick.run(conn, ses, "k", "https://mail.example", now=T0 + timedelta(days=21, minutes=5), shop_factory=lambda s: shop)["sent"] == 1
    assert "Chair 101" in _html(ses) and "Chair 102" in _html(ses) and "Chair 1<" not in _html(ses)


def test_post_purchase_skipped_when_market_off(conn, monkeypatch):
    pp_setup(conn, monkeypatch)
    monkeypatch.setenv("MAIL_PP_LIVE", "")
    ses = SES()
    out = tick.run(conn, ses, "k", "https://mail.example", now=T0 + timedelta(days=1, minutes=5), shop_factory=lambda s: FbtShop())
    assert out["skipped"] == 1 and ses.sent == []
    assert conn.execute("SELECT last_error FROM flow_steps WHERE step=1").fetchone()["last_error"] == "skip:market-off"


def test_post_purchase_retries_when_shopify_is_down_and_skips_without_products(conn, monkeypatch):
    pp_setup(conn, monkeypatch)
    ses = SES()
    # mail 1 has no product cards: it goes out without live blocks when Shopify is down
    assert tick.run(conn, ses, "k", "https://mail.example", now=T0 + timedelta(days=1, minutes=5), shop_factory=lambda s: FbtShop(fail=True))["sent"] == 1
    out = tick.run(conn, ses, "k", "https://mail.example", now=T0 + timedelta(days=21, minutes=5), shop_factory=lambda s: FbtShop(fail=True))
    assert out["failed"] == 1 and conn.execute("SELECT status FROM flow_steps WHERE step=2").fetchone()["status"] == "pending"
    out = tick.run(conn, ses, "k", "https://mail.example", now=T0 + timedelta(days=22), shop_factory=lambda s: FbtShop(live=False))
    assert out["skipped"] == 1 and conn.execute("SELECT last_error FROM flow_steps WHERE step=2").fetchone()["last_error"] == "skip:no-products"


# --- winback ---------------------------------------------------------------------------------------------------------
def wb_setup(conn, monkeypatch, product_ids=("1", "2")):
    import checkouts
    import json
    monkeypatch.setenv("MAIL_WB_LIVE", "shop.example.com")
    monkeypatch.setattr(checkouts, "arm_for", lambda ref: "treatment")
    p = profiles.upsert(conn, "jan@example.org")
    profiles.set_consent(conn, p, "shop.example.com", "subscribed", "popup")
    conn.execute("INSERT INTO events (store, profile_id, type, external_id, payload) VALUES ('shop.example.com', %s, 'winback', '77', %s::jsonb)",
                 (p, json.dumps({"product_ids": list(product_ids)})))
    checkouts.enrol_ab(conn, "winback", "shop.example.com", p, T0, "order:77")
    conn.commit()
    return p


def test_winback_mail_shows_what_goes_with_everything_they_bought(conn, monkeypatch):
    wb_setup(conn, monkeypatch)
    ses = SES()
    assert tick.run(conn, ses, "k", "https://mail.example", now=T0 + timedelta(minutes=5), shop_factory=lambda s: FbtShop())["sent"] == 1
    html = _html(ses)
    assert "Chair 101" in html and "Chair 102" in html and "Chair 1<" not in html and "Chair 2<" not in html
    assert conn.execute("SELECT kind FROM messages").fetchone()["kind"] == "flow:winback:1"


def test_winback_skipped_when_market_off_and_without_products(conn, monkeypatch):
    wb_setup(conn, monkeypatch)
    monkeypatch.setenv("MAIL_WB_LIVE", "")
    ses = SES()
    assert tick.run(conn, ses, "k", "https://mail.example", now=T0 + timedelta(minutes=5), shop_factory=lambda s: FbtShop())["skipped"] == 1
    assert conn.execute("SELECT last_error FROM flow_steps").fetchone()["last_error"] == "skip:market-off"


def test_winback_waits_for_shopify_and_skips_when_nothing_is_for_sale(conn, monkeypatch):
    wb_setup(conn, monkeypatch)
    ses = SES()
    out = tick.run(conn, ses, "k", "https://mail.example", now=T0 + timedelta(minutes=5), shop_factory=lambda s: FbtShop(fail=True))
    assert out["failed"] == 1 and ses.sent == []
    out = tick.run(conn, ses, "k", "https://mail.example", now=T0 + timedelta(hours=1), shop_factory=lambda s: FbtShop(live=False))
    assert out["skipped"] == 1 and conn.execute("SELECT last_error FROM flow_steps").fetchone()["last_error"] == "skip:no-products"


def test_winback_ends_when_they_order(conn, monkeypatch):
    p = wb_setup(conn, monkeypatch)
    engine.exit_if_ordered(conn, "shop.example.com", p)
    conn.commit()
    ses = SES()
    tick.run(conn, ses, "k", "https://mail.example", now=T0 + timedelta(minutes=5), shop_factory=lambda s: FbtShop())
    assert ses.sent == []


# --- back_in_stock ---------------------------------------------------------------------------------------------------
def bis_setup(conn, monkeypatch, email="jan@example.org"):
    import stock
    from tests.test_stock import VShop
    monkeypatch.setenv("MAIL_BIS_LIVE", "shop.example.com")
    p = profiles.upsert(conn, email)
    r = stock.request(conn, "shop.example.com", p, "1", "11", T0)
    conn.commit()
    stock.poll(conn, VShop({"11": "buyable"}), ["shop.example.com"], now=T0)
    return p, r


def bis_state(conn):
    return conn.execute("SELECT state, attempt, message_id FROM stock_requests").fetchone()


def test_back_in_stock_mail_is_sent_and_the_request_closed(conn, monkeypatch):
    from tests.test_stock import VShop
    bis_setup(conn, monkeypatch)
    ses = SES()
    assert tick.run(conn, ses, "k", "https://mail.example", now=T0 + timedelta(minutes=5), shop_factory=lambda s: VShop({"11": "buyable"}))["sent"] == 1
    from tests.test_render import clicked_urls
    assert "Chair Test" in ses.sent[0]["Content"]["Simple"]["Subject"]["Data"] and any("variant=11" in u for u in clicked_urls(_html(ses)))
    st = bis_state(conn)
    assert st["state"] == "sent" and st["message_id"] is not None
    assert conn.execute("SELECT marketing FROM messages").fetchone()["marketing"] is False


def test_flicker_goes_back_to_waiting_and_retries_with_new_ref(conn, monkeypatch):
    import stock
    from tests.test_stock import VShop
    _, r = bis_setup(conn, monkeypatch)
    ses = SES()
    out = tick.run(conn, ses, "k", "https://mail.example", now=T0 + timedelta(minutes=5), shop_factory=lambda s: VShop({"11": "soldout"}))
    assert out["skipped"] == 1 and ses.sent == [] and bis_state(conn)["state"] == "waiting"
    stock.poll(conn, VShop({"11": "buyable"}), ["shop.example.com"], now=T0 + timedelta(days=2))
    assert [e["ref"] for e in conn.execute("SELECT ref FROM enrolments WHERE flow='back_in_stock' ORDER BY id")] == [f"stock:{r}:1", f"stock:{r}:2"]
    assert tick.run(conn, ses, "k", "https://mail.example", now=T0 + timedelta(days=2, minutes=5), shop_factory=lambda s: VShop({"11": "buyable"}))["sent"] == 1


def test_unsubscribed_requester_gets_the_mail_suppressed_does_not(conn, monkeypatch):
    from tests.test_stock import VShop
    p, _ = bis_setup(conn, monkeypatch)
    profiles.set_consent(conn, p, "shop.example.com", "unsubscribed", "link")
    profiles.suppress(conn, "jan@example.org", "shop.example.com", "unsubscribed")
    conn.commit()
    ses = SES()
    assert tick.run(conn, ses, "k", "https://mail.example", now=T0 + timedelta(minutes=5), shop_factory=lambda s: VShop({"11": "buyable"}))["sent"] == 1
    p2, _ = bis_setup(conn, monkeypatch, email="bounce@example.org")
    profiles.suppress(conn, "bounce@example.org", "*", "hard_bounce")
    conn.commit()
    out = tick.run(conn, ses, "k", "https://mail.example", now=T0 + timedelta(minutes=10), shop_factory=lambda s: VShop({"11": "buyable"}))
    assert out["skipped"] == 1 and len(ses.sent) == 1
    assert conn.execute("SELECT state FROM stock_requests WHERE profile_id=%s", (p2,)).fetchone()["state"] == "cancelled"


def test_back_in_stock_waits_when_shopify_is_down_and_skips_when_market_off(conn, monkeypatch):
    from tests.test_stock import VShop
    bis_setup(conn, monkeypatch)
    ses = SES()
    out = tick.run(conn, ses, "k", "https://mail.example", now=T0 + timedelta(minutes=5), shop_factory=lambda s: VShop(fail=True))
    assert out["failed"] == 1 and bis_state(conn)["state"] == "enrolled"
    monkeypatch.setenv("MAIL_BIS_LIVE", "")
    out = tick.run(conn, ses, "k", "https://mail.example", now=T0 + timedelta(hours=1), shop_factory=lambda s: VShop({"11": "buyable"}))
    assert out["skipped"] == 1 and ses.sent == [] and bis_state(conn)["state"] == "waiting"


def test_back_in_stock_mail_is_not_sent_after_the_request_was_cancelled_by_an_order(conn, monkeypatch):
    import stock
    from tests.test_stock import VShop
    p, _ = bis_setup(conn, monkeypatch)
    stock.cancel(conn, variant_ids=["11"], profile_id=p, stores=["shop.example.com"])   # what orders.handle does
    conn.commit()
    ses = SES()
    out = tick.run(conn, ses, "k", "https://mail.example", now=T0 + timedelta(minutes=5), shop_factory=lambda s: VShop({"11": "buyable"}))
    assert out["skipped"] == 1 and ses.sent == [] and bis_state(conn)["state"] == "cancelled"


def test_back_in_stock_terminal_failure_cancels_the_request(conn, monkeypatch):
    from tests.test_stock import VShop
    bis_setup(conn, monkeypatch)
    monkeypatch.setattr(slack, "alert", lambda *a, **k: None)
    conn.execute("UPDATE flow_steps SET attempts=%s", (tick.MAX_ATTEMPTS - 1,))
    conn.commit()
    out = tick.run(conn, SES(fail=True), "k", "https://mail.example", now=T0 + timedelta(minutes=5), shop_factory=lambda s: VShop({"11": "buyable"}))
    assert out["failed"] == 1 and bis_state(conn)["state"] == "cancelled"     # no new enrolment + alert every ~9 hours


def test_back_in_stock_unconfirmed_send_still_closes_the_request(conn, monkeypatch):
    import stock
    from tests.test_stock import VShop
    bis_setup(conn, monkeypatch)
    monkeypatch.setattr(slack, "alert", lambda *a, **k: None)
    real = stock.on_sent
    calls = {"n": 0}
    def boom(c, ref, mid):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("db hiccup after SES accepted")
        return real(c, ref, mid)
    monkeypatch.setattr(stock, "on_sent", boom)
    out = tick.run(conn, SES(), "k", "https://mail.example", now=T0 + timedelta(minutes=5), shop_factory=lambda s: VShop({"11": "buyable"}))
    assert out["sent"] == 1 and bis_state(conn)["state"] == "sent"
