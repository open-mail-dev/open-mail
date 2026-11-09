import logging
from datetime import datetime, timedelta, timezone

import config
import profiles
import stores
from flows import FLOWS

logger = logging.getLogger(__name__)


def _now(now):
    return now or datetime.now(timezone.utc)


def enrol(conn, flow: str, store: str, profile_id: int, now=None, ref: str = "", arm: str | None = None) -> int | None:
    """None if this (flow, store, profile, ref) was enrolled before. ref = what the enrolment is about (a checkout id);
    "" for once-per-profile flows like welcome."""
    now = _now(now)
    row = conn.execute(
        "INSERT INTO enrolments (flow, store, profile_id, started_at, ref, arm) VALUES (%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING RETURNING id",
        (flow, store, profile_id, now, ref, arm),
    ).fetchone()
    if not row:
        return None
    for s in FLOWS[flow].steps:
        conn.execute(
            "INSERT INTO flow_steps (enrolment_id, step, due_at) VALUES (%s,%s,%s)",
            (row["id"], s.n, now + s.delay),
        )
    return row["id"]


def claim_due(conn, limit: int = 50, now=None) -> list[dict]:
    """Claim up to `limit` due steps.

    This is NOT a lease: exclusive only until commit; bumps attempts.
    Callers must re-lock the row `FOR UPDATE SKIP LOCKED AND status =
    'pending'` before decide/send/mark, and do the messages insert + mark +
    commit in that same transaction — once this transaction commits, the
    SKIP LOCKED exclusivity is gone and another worker could otherwise
    re-claim (or see as still pending) the same row.
    """
    return conn.execute(
        """WITH c AS (
               SELECT fs2.enrolment_id, fs2.step FROM flow_steps fs2
               JOIN enrolments e2 ON e2.id = fs2.enrolment_id
               WHERE fs2.status = 'pending' AND fs2.due_at <= %s AND e2.state = 'active'
               ORDER BY fs2.due_at LIMIT %s FOR UPDATE OF fs2 SKIP LOCKED
           )
           UPDATE flow_steps fs SET attempts = attempts + 1
           FROM c, enrolments e
           WHERE fs.enrolment_id = c.enrolment_id AND fs.step = c.step AND e.id = fs.enrolment_id
           RETURNING fs.enrolment_id, fs.step, e.flow, e.store, e.profile_id, e.started_at, e.ref""",
        (_now(now), limit),
    ).fetchall()


def _step(flow: str, n: int):
    return next(s for s in FLOWS[flow].steps if s.n == n)


def decide(conn, row: dict, now=None) -> str:
    """Decide whether a claimed step should send, defer, or be skipped.

    For marketing steps, the frequency-cap check first takes a
    `SELECT ... FOR UPDATE` lock on the profile row. That lock is held until
    the caller commits (i.e. after the send/messages-insert is recorded), so
    a concurrent decide() for the same profile in another store/flow blocks
    on this lock until this one's transaction finishes — preventing two
    stores from both deciding "send" inside the same frequency-cap window.
    """
    now = _now(now)
    try:
        step = _step(row["flow"], row["step"])
    except (KeyError, StopIteration):
        logger.warning(
            "decide: unknown flow/step flow=%s step=%s enrolment_id=%s",
            row["flow"], row["step"], row["enrolment_id"],
        )
        return "skip:unknown-step"
    # Lock order is profile → enrolment, the same as orders.handle (profiles.upsert, then
    # exit_if_ordered); the reverse order here deadlocked against an orders/create webhook.
    if step.marketing:
        conn.execute("SELECT 1 FROM profiles WHERE id=%s FOR UPDATE", (row["profile_id"],))
    state = conn.execute(
        "SELECT state FROM enrolments WHERE id=%s FOR SHARE", (row["enrolment_id"],)
    ).fetchone()["state"]
    if state != "active":
        return "skip:exited"
    if not profiles.is_sendable(conn, row["profile_id"], row["store"], FLOWS[row["flow"]].audience):
        return "skip:unsendable"
    if step.marketing:
        last = conn.execute(
            "SELECT max(sent_at) AS t FROM messages WHERE profile_id=%s AND marketing",
            (row["profile_id"],),
        ).fetchone()["t"]
        cap = timedelta(hours=config.FREQUENCY_CAP_HOURS)
        if last and now - last < cap:
            conn.execute(
                "UPDATE flow_steps SET due_at=%s, attempts = attempts - 1 WHERE enrolment_id=%s AND step=%s",
                (last + cap, row["enrolment_id"], row["step"]),
            )
            return "defer"
    return "send"


def mark(conn, row: dict, status: str, message_id: int | None = None, error: str | None = None) -> bool:
    """Record a claimed step's outcome. Only transitions a still-pending row,
    so a duplicate mark() (e.g. after a re-claim elsewhere already marked it)
    is a no-op. Returns True iff this call made the transition; the
    enrolment-completion check only runs when it does.
    """
    n = conn.execute(
        """UPDATE flow_steps SET status=%s, message_id=COALESCE(%s, message_id), last_error=COALESCE(%s, last_error)
           WHERE enrolment_id=%s AND step=%s AND status='pending'""",
        (status, message_id, error, row["enrolment_id"], row["step"]),
    ).rowcount
    if n != 1:
        return False
    remaining = conn.execute(
        "SELECT count(*) n FROM flow_steps WHERE enrolment_id=%s AND status='pending'", (row["enrolment_id"],)
    ).fetchone()["n"]
    if remaining == 0:
        conn.execute("UPDATE enrolments SET state='completed' WHERE id=%s AND state='active'", (row["enrolment_id"],))
    return True


def exit_on_event(conn, store: str, profile_id: int, event: str) -> int:
    """End this profile's active enrolments whose flow has `event` in exit_on, in `store` and every storefront of the
    same Shopify store: orders and checkouts are filed under the shipping country's market (shop-fi.example.com), a view or
    a welcome code under the domain it happened on (shop.example.com)."""
    names = [f.name for f in FLOWS.values() if event in f.exit_on]
    try:
        keys = stores.same_shop(stores.get(store))
    except KeyError:
        keys = [store]
    return conn.execute(
        "UPDATE enrolments SET state='exited', exit_reason=%s WHERE store = ANY(%s) AND profile_id=%s AND state='active' AND flow = ANY(%s)",
        (event, keys, profile_id, names),
    ).rowcount


def exit_if_ordered(conn, store: str, profile_id: int) -> int:
    return exit_on_event(conn, store, profile_id, "order_placed")
