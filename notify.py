"""POST /notify: "Houd mij op de hoogte" on a sold-out product page → a back-in-stock request (stock.py).

Never touches consent, coupons or the welcome flow: the request itself is the permission for that one mail (also for
someone unsubscribed from marketing). A suppressed address (bounce, complaint) gets the same 200 as everyone, so the
form can't be used to probe which addresses we hold."""
import logging
import re
from datetime import datetime, timezone

import catalog
import config
import profiles
import signup
import stock
import stores
import tokens

logger = logging.getLogger(__name__)
RATE_LIMIT = 10          # requests per IP per hour (shares signup_rate with /signup)
_DIGITS = re.compile(r"^[0-9]{1,20}$")


def handle(conn, body: dict, ip: str, shop_factory, token_secret: str, now=None) -> tuple[int, dict]:
    now = now or datetime.now(timezone.utc)
    if not isinstance(body, dict):
        return 400, {"error": "invalid"}
    if body.get("website"):                       # honeypot
        return 200, {"ok": True}
    try:
        store = stores.get(str(body.get("store", "")))
    except KeyError:
        return 400, {"error": "invalid"}
    if not store.back_in_stock or store.key not in config.bis_live():
        return 400, {"error": "invalid"}
    product, variant = str(body.get("product_id") or ""), str(body.get("variant_id") or "")
    if not _DIGITS.match(product) or not _DIGITS.match(variant):
        return 400, {"error": "invalid"}

    pid, email = None, None
    if body.get("email"):
        try:
            email = profiles.normalise(str(body["email"]))
        except ValueError:
            return 400, {"error": "email"}
        if not signup.domain_accepts_mail(email.rsplit("@", 1)[1]):
            return 400, {"error": "email"}
    else:
        pid = tokens.visitor_profile(str(body.get("vid") or ""), token_secret)
        row = conn.execute("SELECT email FROM profiles WHERE id=%s", (pid,)).fetchone() if pid else None
        if not row:
            return 400, {"error": "email"}
        email = row["email"]

    n = conn.execute("SELECT count(*) n FROM signup_rate WHERE ip=%s AND ts > now() - interval '1 hour'", (ip,)).fetchone()["n"]
    if n >= RATE_LIMIT:
        return 429, {"error": "slow down"}
    conn.execute("INSERT INTO signup_rate (ip) VALUES (%s)", (ip,))
    conn.commit()

    # the product checks come first: whatever they answer is the same for every address
    try:
        live = catalog.variants(shop_factory(store), store, [variant])
    except Exception:
        logger.exception("notify: Shopify lookup failed for variant %s", variant)
        return 503, {"error": "unavailable"}
    if live["cards"].get(variant):
        return 409, {"error": "available"}
    if variant in live["gone"]:
        return 400, {"error": "invalid"}
    if profiles.is_blocked(conn, email, store.key):
        return 200, {"ok": True}
    pid = pid or profiles.upsert(conn, email)
    stock.request(conn, store.key, pid, product, variant, now)
    conn.commit()
    return 200, {"ok": True}
