"""Product views and cart adds reported by the storefront snippet (/track) for browsers we can identify: a signup in
that browser or a click in one of our mails (tokens.visitor). Only stored for profiles subscribed to that store —
these events exist only to send browse_abandonment / cart_reminder, which are marketing."""
import hashlib
import hmac
import json
import re
from datetime import datetime, timedelta, timezone

import checkouts
import config
import engine
import profiles
import stores
import tokens

TYPES = ("viewed_product", "added_to_cart")
RATE_PER_HOUR = 60
_DIGITS = re.compile(r"^[0-9]{1,20}$")


def track(conn, body: dict, secret: str, now=None) -> tuple[int, dict]:
    now = now or datetime.now(timezone.utc)
    if not isinstance(body, dict):
        return 400, {}
    try:
        store = stores.get(str(body.get("store", "")))
    except KeyError:
        return 400, {}
    kind, product, variant = body.get("type"), str(body.get("product_id") or ""), body.get("variant_id")
    if kind not in TYPES or not _DIGITS.match(product) or (variant is not None and not _DIGITS.match(str(variant))):
        return 400, {}
    pid = tokens.visitor_profile(str(body.get("vid") or ""), secret)
    # unknown browser, store not on these flows, or not subscribed there: say ok, store nothing
    if pid is None or not store.site_flows or store.key not in config.site_live() \
            or not profiles.is_sendable(conn, pid, store.key, "subscribed"):
        return 200, {"ok": True}
    n = conn.execute("SELECT count(*) AS n FROM events WHERE profile_id=%s AND type = ANY(%s) AND ts > %s",
                     (pid, list(TYPES), now - timedelta(hours=1))).fetchone()["n"]
    if n >= RATE_PER_HOUR:
        return 200, {"ok": True}   # dropped silently: a 429 (only reachable when subscribed) would reveal that
    eid = conn.execute("INSERT INTO events (store, profile_id, type, payload, ts) VALUES (%s,%s,%s,%s::jsonb,%s) RETURNING id",
                       (store.key, pid, kind, json.dumps({"product_id": product, "variant_id": None if variant is None else str(variant)}),
                        now)).fetchone()["id"]
    on_event(conn, store.key, pid, kind, eid, now)
    conn.commit()
    return 200, {"ok": True}


def ident(conn, body: dict, ident_secret: str, token_secret: str) -> tuple[int, dict]:
    """A logged-in customer's browser: the theme signs "email|store" with the shop's ident key (Liquid hmac_sha256, hex;
    key in shop metafield om.ident_key = secret mail-ident-secret). {"vid": token} = subscriber, {"vid": None} = someone
    we may not mail (the snippet drops any id kept in that browser), {} = can't tell (the snippet changes nothing)."""
    if not isinstance(body, dict):
        return 400, {}
    try:
        store = stores.get(str(body.get("store", "")))
    except KeyError:
        return 400, {}
    email, sig = body.get("email"), body.get("sig")
    if not isinstance(email, str) or not isinstance(sig, str):
        return 400, {}
    want = hmac.new(ident_secret.encode(), f"{email}|{store.key}".encode(), hashlib.sha256).hexdigest()
    if not ident_secret or not hmac.compare_digest(sig, want) or not store.site_flows or store.key not in config.site_live():
        return 200, {}
    row = conn.execute("SELECT id FROM profiles WHERE email=%s", (email.strip().lower(),)).fetchone()
    if not row or not profiles.is_sendable(conn, row["id"], store.key, "subscribed"):
        return 200, {"vid": None}
    return 200, {"vid": tokens.visitor(row["id"], token_secret)}


# flow → (trigger event, no new enrolment within this window of the last one, flows whose active enrolment blocks it)
_RULES = {
    "browse_abandonment": ("viewed_product", timedelta(days=14), ("welcome", "cart_reminder", "abandoned_checkout")),
    "cart_reminder": ("added_to_cart", timedelta(days=7), ("abandoned_checkout",)),
}


def on_event(conn, store: str, pid: int, kind: str, event_id: int, now: datetime) -> str:
    """Enrol (or not) for a stored view/cart add. The 50/50 holdout arm is per enrolment, like abandoned checkout."""
    # one decision per profile at a time: the snippet posts a cart's new lines in parallel. Same profile → enrolment
    # lock order as engine.decide / orders.handle / checkouts._one.
    conn.execute("SELECT 1 FROM profiles WHERE id=%s FOR UPDATE", (pid,))
    engine.exit_on_event(conn, store, pid, kind)            # a cart add ends browse_abandonment
    flow = next(f for f, r in _RULES.items() if r[0] == kind)
    _, window, blockers = _RULES[flow]
    recent = conn.execute("SELECT 1 FROM enrolments WHERE store=%s AND profile_id=%s AND flow=%s AND started_at > %s LIMIT 1",
                          (store, pid, flow, now - window)).fetchone()
    busy = conn.execute("SELECT 1 FROM enrolments WHERE store=%s AND profile_id=%s AND flow = ANY(%s) AND state='active' LIMIT 1",
                        (store, pid, list(blockers))).fetchone()
    if recent or busy:
        return "skipped"
    return checkouts.enrol_ab(conn, flow, store, pid, now, f"{kind}:{event_id}")
