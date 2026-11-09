"""Read-only welcome-code lookup for customer-service tooling (e.g. a support bot that checks or
resends a customer's welcome code), served by the private mail-lookup function.

The full code goes back to the calling bot service only: the bot shows the customer a prefix + length and emails the
code to `email` — the on-file profile address from this database, never the string typed in a chat."""
from datetime import timezone

import coupons
import profiles
import stores


def find(conn, store_key: str, email: str) -> dict:
    try:
        store = stores.get(store_key)
        e = profiles.normalise(email)
    except (KeyError, ValueError):
        return {"found": False}
    row = conn.execute(
        # one welcome code per address per Shopify store (signup.handle): look across its storefronts
        "SELECT p.id, p.email, c.store, c.code, c.created_at FROM coupons c JOIN profiles p ON p.id = c.profile_id "
        "WHERE p.email=%s AND c.store = ANY(%s) AND c.pool=%s ORDER BY c.created_at DESC LIMIT 1",
        (e, stores.same_shop(store), store.welcome_pool),
    ).fetchone()
    if not row:
        return {"found": False}
    issuer = stores.get(row["store"])
    sent = conn.execute(
        "SELECT min(sent_at) t FROM messages WHERE profile_id=%s AND store=%s AND kind='flow:welcome:1'",
        (row["id"], row["store"]),
    ).fetchone()["t"]
    return {
        "found": True,
        "email": row["email"],
        "store": row["store"],
        "code": row["code"],
        "created_at": row["created_at"].astimezone(timezone.utc).isoformat(),
        # end of day in coupons.AMS (the shop time zone), so its date part is the "valid until" date the welcome mail quoted
        "expires_at": coupons.expiry(row["created_at"], issuer.welcome_valid_days).isoformat(),
        "sent_at": sent.astimezone(timezone.utc).isoformat() if sent else None,
        "from_email": issuer.from_email,
    }
