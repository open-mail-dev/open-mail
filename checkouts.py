"""Abandoned checkouts → abandoned_checkout enrolments, with the holdout test.

Polled from tick (no extra webhooks): Shopify's `abandonedCheckouts` carries everything the reminder needs — email,
checkout URL on the market's own domain, lines in the checkout currency, the buyer's consent and order count.

Holdout test: a checkout of someone who never subscribed and never ordered is split 50/50 by its id into
`treatment` (3 reminders) and `holdout` (none, steps written as skipped). Everyone else gets the reminders (`always`).
"""
import hashlib
import json
import logging
import urllib.parse
from datetime import datetime, timedelta, timezone

import catalog
import engine
import profiles
import stores

logger = logging.getLogger(__name__)
FLOW = "abandoned_checkout"
LOOKBACK = timedelta(hours=24)

_QUERY = """query($q:String,$after:String){abandonedCheckouts(first:50, after:$after, query:$q, sortKey:CREATED_AT){
  pageInfo{hasNextPage endCursor}
  nodes{id createdAt completedAt abandonedCheckoutUrl
    customer{email firstName numberOfOrders emailMarketingConsent{marketingState}}
    shippingAddress{countryCodeV2 firstName} billingAddress{countryCodeV2 firstName}
    totalPriceSet{presentmentMoney{amount currencyCode}}
    lineItems(first:20){nodes{title variantTitle quantity
      discountedTotalPriceSet{presentmentMoney{amount currencyCode}}
      product{handle featuredMedia{preview{image{url}}}}}}}}}"""


def arm_for(checkout_id: str) -> str:
    """Stable 50/50 split: the same checkout always lands in the same arm, whatever process sees it first."""
    return "treatment" if hashlib.sha256(checkout_id.encode()).digest()[0] % 2 == 0 else "holdout"


def enrol_ab(conn, flow: str, store: str, pid: int, now, ref: str) -> str:
    """Enrol in a holdout-tested flow: arm per ref (50/50, arm_for); a holdout enrolment is completed at once with its
    steps skipped. "enrolled" | "holdout" | "skipped" (this ref was enrolled before)."""
    arm = arm_for(ref)
    enr = engine.enrol(conn, flow, store, pid, now=now, ref=ref, arm=arm)
    if enr is None:
        return "skipped"
    if arm == "holdout":
        conn.execute("UPDATE flow_steps SET status='skipped', last_error='skip:holdout' WHERE enrolment_id=%s", (enr,))
        conn.execute("UPDATE enrolments SET state='completed', exit_reason='holdout' WHERE id=%s", (enr,))
        return "holdout"
    return "enrolled"


def _dt(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def snapshot(node: dict, store) -> dict:
    """What the reminder shows: the cart as the buyer left it, in their checkout's currency."""
    items = []
    for li in node["lineItems"]["nodes"]:
        img = (((li.get("product") or {}).get("featuredMedia") or {}).get("preview") or {}).get("image") or {}
        money = (li.get("discountedTotalPriceSet") or {}).get("presentmentMoney") or {}
        handle = (li.get("product") or {}).get("handle")
        items.append({"title": li["title"] + (f" — {li['variantTitle']}" if li.get("variantTitle") else ""),
                      "quantity": li["quantity"], "line_price": float(money.get("amount") or 0),
                      "image": catalog._sized(img["url"], 200, store.domain) if img.get("url") else None,
                      # the product's own handle works on every market domain (SE/DK/IE 301 to their translated slug)
                      "url": f"{store.base_url}/products/{handle}" if handle else None})
    total = node["totalPriceSet"]["presentmentMoney"]
    name = ((node.get("customer") or {}).get("firstName") or (node.get("shippingAddress") or {}).get("firstName")
            or (node.get("billingAddress") or {}).get("firstName") or "").strip()
    return {"url": node["abandonedCheckoutUrl"], "currency": total["currencyCode"], "total": float(total["amount"]), "items": items,
            "first_name": name[:40]}


def poll(conn, client, shop: str, since: datetime, now=None, since_by_store: dict | None = None) -> dict:
    """Enrol new abandoned checkouts of `shop` created after `since` (the go-live moment: nothing older is ever
    reminded, so another tool's in-flight reminders are never doubled) and within LOOKBACK."""
    now = now or datetime.now(timezone.utc)
    start = max(since, now - LOOKBACK)
    out = {"enrolled": 0, "holdout": 0, "skipped": 0, "exited": 0}
    after = None
    while True:
        page = client.gql(_QUERY, {"q": f"created_at:>='{start.strftime('%Y-%m-%dT%H:%M:%SZ')}'", "after": after})["abandonedCheckouts"]
        for node in page["nodes"]:
            try:
                r = _one(conn, node, shop, start, since_by_store or {})
                out[r] += 1
                conn.commit()
            except Exception:
                conn.rollback()
                logger.exception("checkout %s failed — retried next poll", node.get("id"))
        if not page["pageInfo"]["hasNextPage"]:
            return out
        after = page["pageInfo"]["endCursor"]


def _one(conn, node: dict, shop: str, start: datetime, since_by_store: dict) -> str:
    if _dt(node["createdAt"]) < start:
        return "skipped"
    if node.get("completedAt"):
        # bought after all (the order webhook normally exits it first; this covers a missed webhook)
        n = conn.execute("UPDATE enrolments SET state='exited', exit_reason='checkout_completed' WHERE flow=%s AND ref=%s AND state='active'",
                         (FLOW, node["id"])).rowcount
        return "exited" if n else "skipped"
    cust = node.get("customer") or {}
    email = cust.get("email")
    if email:
        # whatever happens to this checkout below, starting it ends browse/cart reminders (also for a market we skip)
        try:
            known = conn.execute("SELECT id FROM profiles WHERE email=%s", (profiles.normalise(email),)).fetchone()
        except ValueError:
            known = None
        if known:
            engine.exit_on_event(conn, stores.by_shop(shop).key, known["id"], "checkout_started")
    addr = node.get("shippingAddress") or node.get("billingAddress") or {}
    country = addr.get("countryCodeV2")
    store = stores.for_market(shop, country, urllib.parse.urlparse(node.get("abandonedCheckoutUrl") or "").hostname)
    if not email or store is None or not store.abandoned_checkout or (country and country not in store.countries):
        return "skipped"                                       # no address, or a market not on our system (FI, …)
    if store.ac_own_start and (store.key not in since_by_store or _dt(node["createdAt"]) < since_by_store[store.key]):
        return "skipped"                                       # storefront added later: only from its own go-live
    try:
        pid = profiles.upsert(conn, email)
    except ValueError:
        return "skipped"
    if not profiles.is_sendable(conn, pid, store.key, "not_unsubscribed"):
        return "skipped"                                       # opted out or suppressed: never mailed, not in the test
    ours = conn.execute("SELECT status FROM consent WHERE profile_id=%s AND store=%s", (pid, store.key)).fetchone()
    subscribed = (cust.get("emailMarketingConsent") or {}).get("marketingState") == "SUBSCRIBED" or (ours and ours["status"] == "subscribed")
    customer = int(cust.get("numberOfOrders") or 0) > 0
    arm = "always" if (subscribed or customer) else arm_for(node["id"])
    created = _dt(node["createdAt"])
    eid = engine.enrol(conn, FLOW, store.key, pid, now=created, ref=node["id"], arm=arm)
    engine.exit_on_event(conn, store.key, pid, "checkout_started")   # a profile created just above (not known before)
    conn.execute(
        """INSERT INTO events (store, profile_id, type, external_id, payload, ts) VALUES (%s,%s,'checkout_started',%s,%s::jsonb,%s)
           ON CONFLICT (store, type, external_id) DO UPDATE SET payload = EXCLUDED.payload""",
        (store.key, pid, node["id"], json.dumps(snapshot(node, store)), created),
    )
    if eid is None:
        return "skipped"                                       # seen before (payload refreshed above)
    if arm == "holdout":
        conn.execute("UPDATE flow_steps SET status='skipped', last_error='skip:holdout' WHERE enrolment_id=%s", (eid,))
        conn.execute("UPDATE enrolments SET state='completed', exit_reason='holdout' WHERE id=%s", (eid,))
        return "holdout"
    return "enrolled"
