import threading
import time
from datetime import datetime, timedelta, timezone

import db
import engine
import profiles

S = "outlet.example.com"
T0 = datetime(2026, 10, 1, 12, 0, tzinfo=timezone.utc)


def subscribed(conn, email="jan@example.org"):
    p = profiles.upsert(conn, email)
    profiles.set_consent(conn, p, S, "subscribed", "popup")
    return p


def test_enrol_schedules_three_steps(conn):
    p = subscribed(conn)
    e = engine.enrol(conn, "welcome", S, p, now=T0)
    dues = [r["due_at"] for r in conn.execute("SELECT due_at FROM flow_steps WHERE enrolment_id=%s ORDER BY step", (e,))]
    assert dues == [T0, T0 + timedelta(days=2), T0 + timedelta(days=11)]


def test_enrol_twice_returns_none(conn):
    p = subscribed(conn)
    assert engine.enrol(conn, "welcome", S, p, now=T0)
    assert engine.enrol(conn, "welcome", S, p, now=T0) is None


def test_claim_only_due_steps(conn):
    p = subscribed(conn)
    engine.enrol(conn, "welcome", S, p, now=T0)
    assert [r["step"] for r in engine.claim_due(conn, now=T0)] == [1]


def test_claim_due_is_exclusive_while_uncommitted(conn, postgresql):
    p = subscribed(conn)
    engine.enrol(conn, "welcome", S, p, now=T0)
    conn.commit()
    i = postgresql.info
    other = db.connect(f"host={i.host} port={i.port} user={i.user} dbname={i.dbname}")
    try:
        first = engine.claim_due(conn, now=T0)          # tx still open → row locked
        second = engine.claim_due(other, now=T0)
        assert len(first) == 1 and second == []
    finally:
        other.close()


def test_suppressed_profile_is_skipped_at_send_time(conn):
    p = subscribed(conn)
    engine.enrol(conn, "welcome", S, p, now=T0)
    profiles.suppress(conn, "jan@example.org", S, "unsubscribed")
    row = engine.claim_due(conn, now=T0)[0]
    assert engine.decide(conn, row, now=T0) == "skip:unsendable"


def test_order_exits_flow(conn):
    p = subscribed(conn)
    engine.enrol(conn, "welcome", S, p, now=T0)
    assert engine.exit_if_ordered(conn, S, p) == 1
    assert engine.claim_due(conn, now=T0 + timedelta(days=5)) == []


def test_frequency_cap_defers_marketing_step(conn):
    p = subscribed(conn)
    engine.enrol(conn, "welcome", S, p, now=T0)
    conn.execute("INSERT INTO messages (profile_id, store, kind, marketing, sent_at) VALUES (%s,'shop.example.com','campaign:x',true,%s)",
                 (p, T0 + timedelta(hours=44)))
    row = [r for r in engine.claim_due(conn, now=T0 + timedelta(hours=48)) if r["step"] == 2][0]
    assert engine.decide(conn, row, now=T0 + timedelta(hours=48)) == "defer"
    due = conn.execute("SELECT due_at FROM flow_steps WHERE enrolment_id=%s AND step=2", (row["enrolment_id"],)).fetchone()["due_at"]
    assert due == T0 + timedelta(hours=60)


def test_step_one_is_exempt_from_cap(conn):
    p = subscribed(conn)
    conn.execute("INSERT INTO messages (profile_id, store, kind, marketing, sent_at) VALUES (%s,'shop.example.com','campaign:x',true,%s)",
                 (p, T0 - timedelta(hours=1)))
    engine.enrol(conn, "welcome", S, p, now=T0)
    row = engine.claim_due(conn, now=T0)[0]
    assert engine.decide(conn, row, now=T0) == "send"


def test_last_step_completes_enrolment(conn):
    p = subscribed(conn)
    e = engine.enrol(conn, "welcome", S, p, now=T0)
    for step in (1, 2, 3):
        engine.mark(conn, {"enrolment_id": e, "step": step, "flow": "welcome"}, "sent")
    assert conn.execute("SELECT state FROM enrolments WHERE id=%s", (e,)).fetchone()["state"] == "completed"


def test_mark_twice_second_call_returns_false_and_leaves_message_id(conn):
    p = subscribed(conn)
    e = engine.enrol(conn, "welcome", S, p, now=T0)
    row = {"enrolment_id": e, "step": 1, "flow": "welcome"}
    assert engine.mark(conn, row, "sent", message_id=111) is True
    assert engine.mark(conn, row, "sent", message_id=222) is False
    got = conn.execute(
        "SELECT message_id FROM flow_steps WHERE enrolment_id=%s AND step=1", (e,)
    ).fetchone()["message_id"]
    assert got == 111


def test_defer_does_not_consume_attempt(conn):
    p = subscribed(conn)
    e = engine.enrol(conn, "welcome", S, p, now=T0)
    conn.execute(
        "INSERT INTO messages (profile_id, store, kind, marketing, sent_at) VALUES (%s,'shop.example.com','campaign:x',true,%s)",
        (p, T0 + timedelta(hours=44)),
    )
    rows = engine.claim_due(conn, now=T0 + timedelta(hours=48))
    row = [r for r in rows if r["step"] == 2][0]
    before = conn.execute(
        "SELECT attempts FROM flow_steps WHERE enrolment_id=%s AND step=2", (e,)
    ).fetchone()["attempts"]
    assert before == 1  # claim_due bumped it from 0
    assert engine.decide(conn, row, now=T0 + timedelta(hours=48)) == "defer"
    after = conn.execute(
        "SELECT attempts FROM flow_steps WHERE enrolment_id=%s AND step=2", (e,)
    ).fetchone()["attempts"]
    assert after == 0  # back to its pre-claim value
    # not returned again before its new due_at
    assert [r for r in engine.claim_due(conn, now=T0 + timedelta(hours=48)) if r["step"] == 2] == []
    # returned once its new due_at (T0 + 36h) arrives
    later = [r for r in engine.claim_due(conn, now=T0 + timedelta(hours=60)) if r["step"] == 2]
    assert len(later) == 1


def test_decide_unknown_flow_returns_skip_unknown_step(conn):
    p = subscribed(conn)
    eid = conn.execute(
        "INSERT INTO enrolments (flow, store, profile_id, started_at) VALUES ('gone',%s,%s,%s) RETURNING id",
        (S, p, T0),
    ).fetchone()["id"]
    conn.execute("INSERT INTO flow_steps (enrolment_id, step, due_at) VALUES (%s,1,%s)", (eid, T0))
    row = {"enrolment_id": eid, "step": 1, "flow": "gone", "store": S, "profile_id": p}
    assert engine.decide(conn, row, now=T0) == "skip:unknown-step"


def test_cross_store_frequency_cap_blocks_concurrent_decide(conn, postgresql):
    p = subscribed(conn)
    profiles.set_consent(conn, p, "shop.example.com", "subscribed", "popup")

    def make_enrolment(store):
        eid = conn.execute(
            "INSERT INTO enrolments (flow, store, profile_id, started_at) VALUES ('welcome',%s,%s,%s) RETURNING id",
            (store, p, T0),
        ).fetchone()["id"]
        conn.execute("INSERT INTO flow_steps (enrolment_id, step, due_at) VALUES (%s,2,%s)", (eid, T0))
        return eid

    e1 = make_enrolment(S)
    e2 = make_enrolment("shop.example.com")
    conn.commit()

    i = postgresql.info
    dsn = f"host={i.host} port={i.port} user={i.user} dbname={i.dbname}"
    conn_b = db.connect(dsn)
    try:
        row_a = {"enrolment_id": e1, "step": 2, "flow": "welcome", "store": S, "profile_id": p}
        row_b = {"enrolment_id": e2, "step": 2, "flow": "welcome", "store": "shop.example.com", "profile_id": p}

        assert engine.decide(conn, row_a, now=T0) == "send"
        conn.execute(
            "INSERT INTO messages (profile_id, store, kind, marketing, sent_at) VALUES (%s,%s,'flow:welcome:2',true,%s)",
            (p, S, T0),
        )
        # conn's transaction is still open here — it holds the FOR UPDATE lock on the profile row

        result = {}

        def call_b():
            result["b"] = engine.decide(conn_b, row_b, now=T0)

        t = threading.Thread(target=call_b)
        t.start()
        time.sleep(0.3)
        assert t.is_alive()  # B is blocked on conn's uncommitted profile lock

        conn.commit()
        t.join(timeout=5)
        assert not t.is_alive()
        assert result["b"] == "defer"
    finally:
        conn_b.close()


def test_decide_locks_profile_before_enrolment(conn):
    """orders.handle locks profile → enrolment; decide() must use the same order or the two deadlock."""
    p = subscribed(conn)
    engine.enrol(conn, "welcome", S, p, now=T0)
    row = [r for r in engine.claim_due(conn, now=T0 + timedelta(days=2)) if r["step"] == 2][0]

    seen = []

    class Spy:
        def __init__(self, c):
            self.c = c

        def execute(self, sql, *a, **kw):
            if "FOR UPDATE" in sql or "FOR SHARE" in sql:
                seen.append("profile" if "FROM profiles" in sql else "enrolment" if "FROM enrolments" in sql else sql)
            return self.c.execute(sql, *a, **kw)

    engine.decide(Spy(conn), row, now=T0 + timedelta(days=2))
    assert seen[:2] == ["profile", "enrolment"]


def test_abandoned_checkout_enrols_once_per_checkout_not_once_per_profile(conn):
    p = profiles.upsert(conn, "koper@example.org")
    assert engine.enrol(conn, "abandoned_checkout", S, p, now=T0, ref="c1", arm="treatment")
    assert engine.enrol(conn, "abandoned_checkout", S, p, now=T0, ref="c1", arm="treatment") is None
    assert engine.enrol(conn, "abandoned_checkout", S, p, now=T0 + timedelta(days=3), ref="c2", arm="treatment")
    dues = [r["due_at"] for r in conn.execute(
        "SELECT due_at FROM flow_steps fs JOIN enrolments e ON e.id = fs.enrolment_id WHERE e.ref='c1' ORDER BY step")]
    assert dues == [T0 + timedelta(hours=1), T0 + timedelta(hours=21), T0 + timedelta(hours=61)]   # all relative to the checkout


def test_welcome_still_once_per_profile(conn):
    p = subscribed(conn)
    assert engine.enrol(conn, "welcome", S, p, now=T0)
    assert engine.enrol(conn, "welcome", S, p, now=T0 + timedelta(days=30)) is None


def _claim_one(conn, flow, p, now):
    engine.enrol(conn, flow, S, p, now=now, ref="c1" if flow == "abandoned_checkout" else "")
    return [r for r in engine.claim_due(conn, now=now + timedelta(days=1)) if r["flow"] == flow and r["profile_id"] == p][0]


def test_abandoned_checkout_reaches_never_subscribed_but_not_unsubscribed(conn):
    never = profiles.upsert(conn, "nooit@example.org")
    assert engine.decide(conn, _claim_one(conn, "abandoned_checkout", never, T0)) == "send"
    out = profiles.upsert(conn, "afgemeld@example.org")
    profiles.set_consent(conn, out, S, "unsubscribed", "unsubscribe_link")
    assert engine.decide(conn, _claim_one(conn, "abandoned_checkout", out, T0)) == "skip:unsendable"
    bounced = profiles.upsert(conn, "weg@example.org")
    profiles.suppress(conn, "weg@example.org", "*", "hard_bounce")
    assert engine.decide(conn, _claim_one(conn, "abandoned_checkout", bounced, T0)) == "skip:unsendable"


def test_welcome_still_needs_subscribed_consent(conn):
    never = profiles.upsert(conn, "nooit@example.org")
    assert engine.decide(conn, _claim_one(conn, "welcome", never, T0)) == "skip:unsendable"


def test_cart_reminder_is_not_held_back_by_the_marketing_frequency_cap(conn):
    p = subscribed(conn)
    conn.execute("INSERT INTO messages (profile_id, store, kind, marketing, sent_at) VALUES (%s,%s,'flow:welcome:2',true,%s)",
                 (p, S, T0 + timedelta(hours=23)))
    assert engine.decide(conn, _claim_one(conn, "abandoned_checkout", p, T0), now=T0 + timedelta(days=1)) == "send"
