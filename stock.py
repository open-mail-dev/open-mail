"""Back-in-stock requests ("mail me when it's back").

Lifecycle: waiting → enrolled (ref stock:<id>:<attempt>) → sent. A variant that sold out again before the mail went
out sends the request back to waiting (on_skipped); the next restock enrols it again under a new ref. Requests expire
EXPIRY after they were made; cancel() ends them when the product will not come back or the person ordered it."""
import json
from datetime import datetime, timezone
from datetime import timedelta

import catalog
import engine
import stores as stores_mod

EXPIRY = timedelta(days=183)
_OPEN = ("waiting", "enrolled")


def request(conn, store: str, pid: int, product_id: str, variant_id: str, now, source: str = "form", created_at=None) -> int:
    """The open request for this person + variant (an existing one is kept as it is), else a new one."""
    made = created_at or now
    row = conn.execute(
        """INSERT INTO stock_requests (store, profile_id, product_id, variant_id, created_at, expires_at, source)
           VALUES (%s,%s,%s,%s,%s,%s,%s)
           ON CONFLICT (store, profile_id, variant_id) WHERE state IN ('waiting','enrolled') DO NOTHING RETURNING id""",
        (store, pid, product_id, variant_id, made, made + EXPIRY, source),
    ).fetchone()
    if row:
        return row["id"]
    return conn.execute("SELECT id FROM stock_requests WHERE store=%s AND profile_id=%s AND variant_id=%s AND state = ANY(%s)",
                        (store, pid, variant_id, list(_OPEN))).fetchone()["id"]


def waiting(conn, stores: list[str], now) -> list[dict]:
    return conn.execute(
        """SELECT id, store, profile_id, product_id, variant_id, attempt FROM stock_requests
           WHERE state='waiting' AND store = ANY(%s) AND expires_at > %s ORDER BY id""", (list(stores), now)).fetchall()


def expire(conn, now) -> int:
    return conn.execute("UPDATE stock_requests SET state='expired' WHERE state='waiting' AND expires_at <= %s", (now,)).rowcount


def mark_enrolled(conn, request_id: int) -> str:
    n = conn.execute("UPDATE stock_requests SET state='enrolled', attempt = attempt + 1 WHERE id=%s AND state='waiting' RETURNING attempt",
                     (request_id,)).fetchone()["attempt"]
    return f"stock:{request_id}:{n}"


def _id(ref: str) -> int:
    return int(ref.split(":")[1])


def on_sent(conn, ref: str, message_id: int) -> None:
    conn.execute("UPDATE stock_requests SET state='sent', message_id=%s WHERE id=%s AND state='enrolled'", (message_id, _id(ref)))


def on_skipped(conn, ref: str) -> None:
    conn.execute("UPDATE stock_requests SET state='waiting' WHERE id=%s AND state='enrolled'", (_id(ref),))


def cancel(conn, *, product_ids=(), variant_ids=(), profile_id=None, stores=()) -> int:
    """Cancel open requests in `stores` for these products or variants, optionally only this profile's."""
    if not product_ids and not variant_ids:
        return 0
    return conn.execute(
        """UPDATE stock_requests SET state='cancelled' WHERE state = ANY(%(o)s) AND store = ANY(%(s)s)
             AND (product_id = ANY(%(p)s) OR variant_id = ANY(%(v)s)) AND (%(pid)s::bigint IS NULL OR profile_id = %(pid)s)""",
        {"o": list(_OPEN), "s": list(stores), "p": list(product_ids), "v": list(variant_ids), "pid": profile_id},
    ).rowcount


def on_blocked(conn, ref: str) -> None:
    """The address can't be mailed at all any more (bounce/complaint): the request ends."""
    conn.execute("UPDATE stock_requests SET state='cancelled' WHERE id=%s AND state IN ('waiting','enrolled')", (_id(ref),))


def poll(conn, client, stores: list[str], now=None) -> dict:
    """Expire old requests, then enrol every waiting request whose variant is buyable on its storefront and cancel
    those whose product will not come back. One catalogue call per storefront; a Shopify error propagates before any
    request changes (the next tick retries)."""
    now = now or datetime.now(timezone.utc)
    out = {"enrolled": 0, "cancelled": 0, "expired": expire(conn, now)}
    conn.commit()
    rows = waiting(conn, stores, now)
    by_store = {}
    for r in rows:
        by_store.setdefault(r["store"], []).append(r)
    live = {k: catalog.variants(client, stores_mod.get(k), sorted({r["variant_id"] for r in rs})) for k, rs in by_store.items()}
    for r in rows:
        found = live[r["store"]]
        if r["variant_id"] in found["gone"]:
            conn.execute("UPDATE stock_requests SET state='cancelled' WHERE id=%s AND state='waiting'", (r["id"],))
            out["cancelled"] += 1
        elif found["cards"].get(r["variant_id"]):
            ref = mark_enrolled(conn, r["id"])
            engine.enrol(conn, "back_in_stock", r["store"], r["profile_id"], now=now, ref=ref)
            conn.execute("INSERT INTO events (store, profile_id, type, external_id, payload, ts) VALUES (%s,%s,'back_in_stock',%s,%s::jsonb,%s)",
                         (r["store"], r["profile_id"], ref, json.dumps({"variant_id": r["variant_id"], "product_id": r["product_id"]}), now))
            out["enrolled"] += 1
        conn.commit()
    return out


def is_open(conn, ref: str) -> bool:
    """Still enrolled under this ref (not cancelled by an order meanwhile, not already sent)."""
    return conn.execute("SELECT 1 FROM stock_requests WHERE id=%s AND state='enrolled'", (_id(ref),)).fetchone() is not None
