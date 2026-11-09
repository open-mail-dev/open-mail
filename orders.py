import json
import logging
import urllib.parse
from datetime import datetime, timezone

import checkouts
import config
import engine
import profiles
import shopify
import stock
import stores

logger = logging.getLogger(__name__)


def handle(conn, body: bytes, headers: dict, webhook_secret: str, now=None) -> int:
    if not shopify.verify_webhook(body, headers.get("X-Shopify-Hmac-Sha256", ""), webhook_secret):
        return 401
    shop = headers.get("X-Shopify-Shop-Domain", "")
    if stores.for_market(shop) is None and not any(s.shop == shop for s in stores.all_stores()):
        return 200                                   # not a store we handle yet; ack so Shopify stops retrying
    if headers.get("X-Shopify-Topic") != "orders/create":
        return 200

    try:
        o = json.loads(body)
        order_id = o["id"]
    except (ValueError, KeyError, TypeError):
        logger.warning("handle: poison payload (non-JSON or missing id)")
        conn.rollback()
        return 200
    addr = o.get("shipping_address") or o.get("billing_address") or {}
    store = stores.for_market(shop, addr.get("country_code"), urllib.parse.urlparse(o.get("order_status_url") or "").hostname)
    if store is None:
        return 200                                   # a storefront of this shop that is not on our system (e.g. .fi)

    try:
        try:
            pid = profiles.upsert(conn, o.get("email") or "")
        except ValueError:
            return 200
        payload = {
            "order_id": order_id,
            "total": o.get("total_price"),
            "discount_codes": [d.get("code") for d in o.get("discount_codes") or []],
            "name": o.get("name"),
            "product_ids": list(dict.fromkeys(str(li["product_id"]) for li in o.get("line_items") or []
                                              if isinstance(li, dict) and li.get("product_id") is not None)),
            "variant_ids": list(dict.fromkeys(str(li["variant_id"]) for li in o.get("line_items") or []
                                              if isinstance(li, dict) and li.get("variant_id") is not None)),
        }
        new = conn.execute(
            """INSERT INTO events (store, profile_id, type, external_id, payload)
               VALUES (%s,%s,'order_placed',%s,%s::jsonb) ON CONFLICT DO NOTHING""",
            (store.key, pid, str(order_id), json.dumps(payload)),
        ).rowcount
        if not new:
            # Shopify redelivered an order we already stored: its exits and enrolment were committed with that event.
            # Running them again would end the post_purchase series this very order started.
            conn.commit()
            return 200
        engine.exit_if_ordered(conn, store.key, pid)          # also ends a running post_purchase: a new order restarts it
        # bought it after all (CS, backorder, another colour page): no "weer leverbaar" mail for it
        stock.cancel(conn, variant_ids=[str(li["variant_id"]) for li in o.get("line_items") or []
                                        if isinstance(li, dict) and li.get("variant_id") is not None],
                     profile_id=pid, stores=stores.same_shop(store))
        # "email me" ticked at checkout: joins our list (no welcome, no code — they just bought). A tick is fresh
        # consent, so it also re-subscribes an earlier unsubscribe (as signup does); bounces/complaints still block.
        if o.get("buyer_accepts_marketing") is True:
            ours = conn.execute("SELECT status FROM consent WHERE profile_id=%s AND store=%s", (pid, store.key)).fetchone()
            if not ours or ours["status"] != "subscribed":
                conn.execute("DELETE FROM suppressions WHERE email=%s AND store=%s AND reason='unsubscribed'",
                             (profiles.email_of(conn, pid), store.key))
                profiles.set_consent(conn, pid, store.key, "subscribed", "shopify_checkout")
        # €0 orders are customer-service replacements / warranty cases: no "thank you + cross-sell"
        if store.post_purchase and store.key in config.pp_live() and _paid(o) and profiles.is_sendable(conn, pid, store.key, "subscribed"):
            checkouts.enrol_ab(conn, "post_purchase", store.key, pid, now or datetime.now(timezone.utc), f"order:{order_id}")
        conn.commit()
    except Exception:
        logger.exception("handle: DB failure")
        conn.rollback()
        return 500

    return 200


def _paid(o: dict) -> bool:
    try:
        return float(o.get("total_price") or 0) > 0
    except (TypeError, ValueError):
        return False
