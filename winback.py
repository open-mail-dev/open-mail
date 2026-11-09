"""Winback: one mail to a subscriber whose last order turned 120 days old.

Polled from tick, like abandoned checkouts. Our own events only go back to the order webhook's go-live, so the order
history comes from Shopify: the orders that turned 120 days old in the last LOOKBACK, then — only for someone we would
actually mail — that customer's last orders for the product cards (what goes with everything they bought).

Left alone: anyone who ordered since (Shopify's lastOrder), is not subscribed, was on the site in the last 30 days
(view, cart add, checkout), is in another flow right now, or had a winback enrolment (either arm) in the last year.
Holdout test: 50/50 per order (checkouts.enrol_ab); readout = report.site(…, "winback", window=30 days).
"""
import json
import logging
import urllib.parse
from datetime import datetime, timedelta, timezone

import checkouts
import config
import profiles
import stores

logger = logging.getLogger(__name__)
FLOW = "winback"
AFTER = timedelta(days=120)
LOOKBACK = timedelta(days=2)          # a tick outage up to this long still catches every order
QUIET = timedelta(days=30)            # site activity this recent: they are shopping on their own
ONCE_PER = timedelta(days=365)
ACTIVITY = ("viewed_product", "added_to_cart", "checkout_started")

_ORDERS = """query($q:String,$after:String){orders(first:100, after:$after, query:$q, sortKey:CREATED_AT){
  pageInfo{hasNextPage endCursor}
  nodes{legacyResourceId createdAt cancelledAt test email statusPageUrl totalPriceSet{shopMoney{amount}}
    shippingAddress{countryCodeV2} billingAddress{countryCodeV2} customer{id lastOrder{legacyResourceId}}}}}"""
_HISTORY = """query($id:ID!){customer(id:$id){orders(first:10, sortKey:CREATED_AT, reverse:true){
  nodes{lineItems(first:20){nodes{product{legacyResourceId}}}}}}}"""


def _dt(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def _ts(d: datetime) -> str:
    return d.strftime("%Y-%m-%dT%H:%M:%SZ")


def poll(conn, client, shop: str, now=None) -> dict:
    now = now or datetime.now(timezone.utc)
    start, end = now - AFTER - LOOKBACK, now - AFTER
    out = {"enrolled": 0, "holdout": 0, "skipped": 0}
    after = None
    while True:
        page = client.gql(_ORDERS, {"q": f"created_at:>='{_ts(start)}' AND created_at:<'{_ts(end)}'", "after": after})["orders"]
        for node in page["nodes"]:
            try:
                r = _one(conn, client, node, shop, start, end, now)
                out[r] += 1
                conn.commit()
            except Exception:
                conn.rollback()
                logger.exception("winback order %s failed — retried next poll", node.get("legacyResourceId"))
        if not page["pageInfo"]["hasNextPage"]:
            return out
        after = page["pageInfo"]["endCursor"]


def _one(conn, client, node: dict, shop: str, start: datetime, end: datetime, now: datetime) -> str:
    oid = node["legacyResourceId"]
    cust = node.get("customer") or {}
    if not start <= _dt(node["createdAt"]) < end or node.get("cancelledAt") or node.get("test"):
        return "skipped"
    if float(((node.get("totalPriceSet") or {}).get("shopMoney") or {}).get("amount") or 0) <= 0:
        return "skipped"                                        # €0 = a replacement / warranty case, not a purchase
    if ((cust.get("lastOrder") or {}).get("legacyResourceId")) != oid:
        return "skipped"                                        # ordered again since
    addr = node.get("shippingAddress") or node.get("billingAddress") or {}
    store = stores.for_market(shop, addr.get("countryCodeV2"), urllib.parse.urlparse(node.get("statusPageUrl") or "").hostname)
    if store is None or not store.winback or store.key not in config.wb_live():
        return "skipped"
    try:
        known = conn.execute("SELECT id FROM profiles WHERE email=%s", (profiles.normalise(node.get("email") or ""),)).fetchone()
    except ValueError:
        return "skipped"
    if not known or not profiles.is_sendable(conn, known["id"], store.key, "subscribed"):
        return "skipped"                                        # never subscribed: no profile is created for them
    pid, keys = known["id"], stores.same_shop(store)
    busy = conn.execute(
        """SELECT EXISTS (SELECT 1 FROM enrolments WHERE profile_id=%(p)s AND store = ANY(%(k)s)
                            AND ((flow=%(f)s AND (started_at > %(y)s OR ref=%(r)s)) OR state='active')) AS enrolled,
                  EXISTS (SELECT 1 FROM events WHERE profile_id=%(p)s AND store = ANY(%(k)s) AND type = ANY(%(t)s)
                            AND ts > %(q)s) AS active""",
        {"p": pid, "k": keys, "f": FLOW, "y": now - ONCE_PER, "r": f"order:{oid}", "t": list(ACTIVITY), "q": now - QUIET},
    ).fetchone()
    if busy["enrolled"] or busy["active"]:
        return "skipped"
    if not cust.get("id"):
        return "skipped"
    history = client.gql(_HISTORY, {"id": cust["id"]})["customer"]["orders"]["nodes"]
    ids = list(dict.fromkeys(li["product"]["legacyResourceId"] for o in history for li in o["lineItems"]["nodes"] if li.get("product")))
    conn.execute(
        """INSERT INTO events (store, profile_id, type, external_id, payload, ts) VALUES (%s,%s,'winback',%s,%s::jsonb,%s)
           ON CONFLICT (store, type, external_id) DO UPDATE SET payload = EXCLUDED.payload""",
        (store.key, pid, oid, json.dumps({"product_ids": ids}), now),
    )
    r = checkouts.enrol_ab(conn, FLOW, store.key, pid, now, f"order:{oid}")
    return "skipped" if r == "skipped" else r
