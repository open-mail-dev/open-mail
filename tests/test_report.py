import json
from datetime import datetime, timedelta, timezone

import profiles
import report

S = "outlet.example.com"
T0 = datetime(2026, 10, 1, 12, 0, tzinfo=timezone.utc)
H = timedelta(hours=1)
D = timedelta(days=1)


def signup(conn, email, code, at=T0):
    p = profiles.upsert(conn, email)
    conn.execute("INSERT INTO events (store, profile_id, type, ts) VALUES (%s,%s,'signup',%s)", (S, p, at))
    if code:
        conn.execute("INSERT INTO coupons (profile_id, store, pool, code, created_at) VALUES (%s,%s,'welcome10',%s,%s)", (p, S, code, at))
    return p


def sent(conn, p, step, at, clicks=()):
    m = conn.execute("INSERT INTO messages (profile_id, store, kind, sent_at) VALUES (%s,%s,%s,%s) RETURNING id",
                     (p, S, f"flow:welcome:{step}", at)).fetchone()["id"]
    for c in clicks:
        conn.execute("INSERT INTO clicks (message_id, url, ts) VALUES (%s,'https://x',%s)", (m, c))
    return m


def order(conn, p, at, total, codes=(), oid=None):
    conn.execute("INSERT INTO events (store, profile_id, type, external_id, payload, ts) VALUES (%s,%s,'order_placed',%s,%s::jsonb,%s)",
                 (S, p, str(oid or f"{p}-{at.timestamp()}"), json.dumps({"total": f"{total:.2f}", "discount_codes": list(codes)}), at))


def run(conn, **kw):
    return report.welcome(conn, S, T0 - D, T0 + 30 * D, **kw)


def test_code_order_is_credited_to_the_last_mail_before_it(conn):
    a = signup(conn, "a@x.nl", "WELCOME-AAAA-AAAA")
    sent(conn, a, 1, T0)
    sent(conn, a, 2, T0 + 2 * D)
    order(conn, a, T0 + 2 * D + H, 349.0, codes=["welcome-aaaa-aaaa"])     # code match is case-insensitive
    r = run(conn)
    assert r["signups"] == 1
    assert [o["how"] for o in r["orders"]] == ["code"]
    assert r["steps"][2]["code_orders"] == 1 and r["steps"][2]["code_revenue"] == 349.0
    assert r["steps"][1]["code_orders"] == 0


def test_click_attribution_only_inside_the_window_and_without_the_code(conn):
    b = signup(conn, "b@x.nl", "WELCOME-BBBB-BBBB")
    sent(conn, b, 1, T0, clicks=[T0 + H])
    order(conn, b, T0 + 3 * D, 200.0, oid=1)                  # clicked 3 days earlier, no code → click
    order(conn, b, T0 + 7 * D, 150.0, oid=2)                  # 7 days after the click → not attributed
    c = signup(conn, "c@x.nl", "WELCOME-CCCC-CCCC")
    sent(conn, c, 1, T0)
    order(conn, c, T0 + D, 120.0, oid=3)                      # never clicked, no code → not attributed
    r = run(conn)
    assert [(o["total"], o["how"]) for o in r["orders"]] == [(120.0, "none"), (200.0, "click"), (150.0, "none")]
    assert r["steps"][1]["click_orders"] == 1 and r["steps"][1]["click_revenue"] == 200.0


def test_orders_before_signup_and_other_stores_are_ignored(conn):
    d = signup(conn, "d@x.nl", "WELCOME-DDDD-DDDD", at=T0)
    order(conn, d, T0 - H, 99.0, codes=["WELCOME-DDDD-DDDD"])
    conn.execute("INSERT INTO events (store, profile_id, type, external_id, payload, ts) VALUES ('shop.example.com',%s,'order_placed','sp1',%s::jsonb,%s)",
                 (d, json.dumps({"total": "50.00", "discount_codes": ["WELCOME-DDDD-DDDD"]}), T0 + H))
    assert run(conn)["orders"] == []


def test_sends_clicks_and_unsubscribes_per_step(conn):
    e = signup(conn, "e@x.nl", "WELCOME-EEEE-EEEE")
    f = signup(conn, "f@x.nl", "WELCOME-FFFF-FFFF")
    sent(conn, e, 1, T0, clicks=[T0 + H, T0 + 2 * H])        # two clicks, one clicker
    sent(conn, f, 1, T0)
    sent(conn, f, 2, T0 + 2 * D)
    profiles.set_consent(conn, f, S, "unsubscribed", "unsubscribe_link")
    conn.execute("UPDATE consent_log SET ts=%s WHERE profile_id=%s", (T0 + 2 * D + H, f))
    r = run(conn)
    assert r["steps"][1] == {**r["steps"][1], "sent": 2, "clicked": 1, "unsubscribed": 0}
    assert r["steps"][2] == {**r["steps"][2], "sent": 1, "clicked": 0, "unsubscribed": 1}


def test_window_exclude_and_bounces(conn):
    signup(conn, "late@x.nl", "WELCOME-LLLL-LLLL", at=T0 + 40 * D)        # outside the window
    signup(conn, "anna@x.nl", "WELCOME-TTTT-TTTT")                         # excluded test signup
    signup(conn, "Bad@x.nl", "WELCOME-GGGG-GGGG")
    profiles.suppress(conn, "bad@x.nl", "*", "hard_bounce")
    r = run(conn, exclude=("ANNA@x.nl",))
    assert r["signups"] == 1 and r["bounces"] == 1 and r["complaints"] == 0


def test_format_report_renders(conn):
    a = signup(conn, "a@x.nl", "WELCOME-AAAA-AAAA")
    sent(conn, a, 1, T0, clicks=[T0 + H])
    order(conn, a, T0 + 2 * H, 1234.5, codes=["WELCOME-AAAA-AAAA"])
    text = report.format_report(run(conn))
    assert "Ordered with own code   1  (100.0% of signups)  €1.234,50" in text
    assert "median 2.0 h" in text
    assert report.format_report(report.welcome(conn, S, T0 + 50 * D, T0 + 60 * D)).count("Signups                 0") == 1


def test_holdout_readout_compares_arms_and_waits_a_full_week(conn):
    import engine
    def checkout(email, ref, arm, at, bought=None):
        p = profiles.upsert(conn, email)
        engine.enrol(conn, "abandoned_checkout", "shop.example.com", p, now=at, ref=ref, arm=arm)
        if bought:
            conn.execute("INSERT INTO events (store, profile_id, type, external_id, payload, ts) VALUES ('shop.example.com',%s,'order_placed',%s,%s::jsonb,%s)",
                         (p, ref + "-o", json.dumps({"total": f"{bought[1]:.2f}"}), at + bought[0]))
    old = datetime(2026, 9, 1, tzinfo=timezone.utc)
    checkout("t1@x.nl", "t1", "treatment", old, (D, 300.0))
    checkout("t2@x.nl", "t2", "treatment", old)
    checkout("h1@x.nl", "h1", "holdout", old, (10 * D, 500.0))      # bought, but after the 7-day window
    checkout("h2@x.nl", "h2", "holdout", old)
    checkout("new@x.nl", "n1", "treatment", datetime.now(timezone.utc) - D)   # not a full week old: not counted yet
    r = report.abandoned(conn, ("shop.example.com",), old - D, datetime.now(timezone.utc))
    assert r[("shop.example.com", "treatment")]["checkouts"] == 2 and r[("shop.example.com", "treatment")]["buyers"] == 1
    assert r[("shop.example.com", "treatment")]["revenue"] == 300.0
    assert r[("shop.example.com", "holdout")]["buyers"] == 0
    assert "treatment" in report.format_abandoned(r) and "50.0%" in report.format_abandoned(r)


def opened(conn, m, at, ua):
    conn.execute("INSERT INTO opens (message_id, ts, user_agent) VALUES (%s,%s,%s)", (m, at, ua))


def test_opens_counted_once_per_mail_and_machine_opens_kept_apart(conn):
    iphone = "Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Mobile/15E148"
    a = signup(conn, "a@x.nl", None); ma = sent(conn, a, 1, T0)
    opened(conn, ma, T0 + H, iphone); opened(conn, ma, T0 + 2 * H, iphone)          # human, twice → 1
    b = signup(conn, "b@x.nl", None); mb = sent(conn, b, 1, T0)
    opened(conn, mb, T0 + H, "Mozilla/5.0")                                          # Apple Mail Privacy Protection
    c = signup(conn, "c@x.nl", None); mc = sent(conn, c, 1, T0)
    opened(conn, mc, T0 + timedelta(seconds=5), iphone)                               # seconds after sending = scanner
    d = signup(conn, "d@x.nl", None); md = sent(conn, d, 1, T0)
    opened(conn, md, T0 + timedelta(seconds=5), "Mozilla/5.0")
    opened(conn, md, T0 + 3 * H, "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/42.0.2311.135 Safari/537.36 Edge/12.246 Mozilla/5.0 GoogleImageProxy")
    e = signup(conn, "e@x.nl", None); sent(conn, e, 1, T0)                            # never opened
    s = run(conn)["steps"][1]
    assert s["sent"] == 5 and s["opened"] == 4 and s["opened_human"] == 2               # a and d (Gmail proxy = a real open)


def test_format_report_shows_open_columns(conn):
    a = signup(conn, "a@x.nl", None); m = sent(conn, a, 1, T0)
    opened(conn, m, T0 + H, "Mozilla/5.0 (Macintosh) AppleWebKit Mail")
    out = report.format_report(run(conn))
    assert "Opened" in out and "Human" in out


def test_site_report_compares_arms_per_store(conn):
    import engine
    now = datetime.now(timezone.utc)
    t0 = now - 10 * D
    st = "shop.example.com"
    for email, arm, buys in (("t1@x.nl", "treatment", 300.0), ("t2@x.nl", "treatment", None), ("h1@x.nl", "holdout", None),
                             ("new@x.nl", "treatment", 50.0)):
        p = profiles.upsert(conn, email)
        start = now - 2 * D if email == "new@x.nl" else t0         # younger than the 7-day window → not counted yet
        engine.enrol(conn, "browse_abandonment", st, p, now=start, ref=f"viewed_product:{p}", arm=arm)
        if buys:
            conn.execute("INSERT INTO events (store, profile_id, type, external_id, payload, ts) VALUES (%s,%s,'order_placed',%s,%s::jsonb,%s)",
                         (st, p, f"o{p}", json.dumps({"total": f"{buys:.2f}"}), start + D))
    p = profiles.upsert(conn, "c@x.nl")
    engine.enrol(conn, "cart_reminder", st, p, now=t0, ref="added_to_cart:1", arm="treatment")   # other flow: not counted
    r = report.site(conn, (st,), "browse_abandonment", now - 30 * D, now)
    assert r[(st, "treatment")]["checkouts"] == 2 and r[(st, "treatment")]["buyers"] == 1 and r[(st, "treatment")]["revenue"] == 300.0
    assert r[(st, "holdout")]["checkouts"] == 1 and r[(st, "holdout")]["buyers"] == 0
    out = report.format_site(r, "browse_abandonment")
    assert "browse_abandonment" in out and "Enrolments" in out


def test_post_purchase_readout_does_not_count_the_triggering_order(conn):
    import engine
    now = datetime.now(timezone.utc)
    t0 = now - 70 * D
    st = "shop.example.com"
    p = profiles.upsert(conn, "pp@x.nl")
    engine.enrol(conn, "post_purchase", st, p, now=t0, ref="order:900", arm="treatment")
    for oid, at, total in (("900", t0 + timedelta(seconds=1), 500.0), ("901", t0 + 30 * D, 120.0)):   # trigger, then a real repeat
        conn.execute("INSERT INTO events (store, profile_id, type, external_id, payload, ts) VALUES (%s,%s,'order_placed',%s,%s::jsonb,%s)",
                     (st, p, oid, json.dumps({"total": f"{total:.2f}"}), at))
    r = report.site(conn, (st,), "post_purchase", now - 90 * D, now, window=60 * D)
    assert r[(st, "treatment")]["buyers"] == 1 and r[(st, "treatment")]["revenue"] == 120.0


def test_readout_title_counts_each_flows_own_mails():
    assert "treatment = 1 mail," in report.format_site({}, "winback")
    assert "treatment = 2 mails," in report.format_site({}, "post_purchase")


def test_report_counts_requests_and_orders_after_mail(conn):
    import json
    import profiles
    import stock
    from datetime import datetime, timedelta, timezone
    t = datetime(2026, 11, 1, tzinfo=timezone.utc)
    a, b = profiles.upsert(conn, "a@x.nl"), profiles.upsert(conn, "b@x.nl")
    conn.execute("INSERT INTO messages (profile_id, store, kind, marketing, sent_at) VALUES (%s,'shop.example.com','flow:back_in_stock:1',false,%s)", (a, t))
    m = conn.execute("SELECT id FROM messages").fetchone()["id"]
    r = stock.request(conn, "shop.example.com", a, "5", "55", t - timedelta(days=3))
    stock.on_sent(conn, stock.mark_enrolled(conn, r), m)
    stock.request(conn, "shop.example.com", b, "5", "55", t)
    conn.execute("INSERT INTO events (store, profile_id, type, external_id, payload, ts) VALUES ('shop.example.com',%s,'order_placed','o1',%s::jsonb,%s)",
                 (a, json.dumps({"total": "120.00", "variant_ids": ["55"]}), t + timedelta(days=2)))
    out = report.back_in_stock(conn, ("shop.example.com",), t - timedelta(days=30), t + timedelta(days=1))
    assert out["shop.example.com"] == {"requests": 2, "waiting": 1, "sent": 1, "expired": 0, "cancelled": 0, "orders_14d": 1, "revenue_14d": 120.0}
