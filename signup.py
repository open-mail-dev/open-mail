import json
import logging
import secrets
from datetime import datetime, timezone

import dns.exception
import dns.resolver

import catalog
import coupons
import engine
import profiles
import slack
import stores
import tokens

logger = logging.getLogger(__name__)

SOURCES = {"popup", "footer"}
RATE_LIMIT = 10          # signups per IP per hour
_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"


_DOMAIN_OK: dict[str, bool] = {}


def domain_accepts_mail(domain: str) -> bool:
    """False only when DNS says for certain the domain can't receive mail (NXDOMAIN, or no MX and no A/AAAA).
    Any lookup trouble (timeout, SERVFAIL) answers True: a slow resolver must never cost a real signup.
    Bogus domains bounce, and bounces burn the new sending domain's reputation, so this runs before anything."""
    if domain in _DOMAIN_OK:
        return _DOMAIN_OK[domain]
    r = dns.resolver.Resolver()
    r.lifetime = 2.0
    ok = True
    try:
        r.resolve(domain, "MX")
    except dns.resolver.NXDOMAIN:
        ok = False
    except dns.resolver.NoAnswer:
        ok = False
        for rtype in ("A", "AAAA"):          # RFC 5321: no MX → deliver to the A/AAAA host
            try:
                r.resolve(domain, rtype)
                ok = True
                break
            except (dns.resolver.NoAnswer, dns.resolver.NXDOMAIN):
                continue
            except dns.exception.DNSException:
                ok = True
                break
    except dns.exception.DNSException:
        ok = True
    _DOMAIN_OK[domain] = ok
    return ok


def new_code(store) -> str:
    part = lambda: "".join(secrets.choice(_ALPHABET) for _ in range(4))  # noqa: E731
    return f"{store.code_prefix}-{part()}-{part()}"


def handle(conn, form: dict, ip: str, shopify_client_factory, token_secret: str = "") -> tuple[int, dict]:
    if form.get("website"):                     # honeypot: pretend success
        return 200, {"ok": True}
    try:
        store = stores.get(form.get("store", ""))
        email = profiles.normalise(form.get("email", ""))
    except (KeyError, ValueError):
        return 400, {"error": "invalid"}
    if not store.welcome:
        return 400, {"error": "invalid"}
    source = form.get("source", "")
    if source not in SOURCES:
        return 400, {"error": "invalid"}
    if not domain_accepts_mail(email.rsplit("@", 1)[1]):
        return 400, {"error": "undeliverable"}
    # product handles this browser viewed before signing up (consent-gated in the theme); junk is dropped, not rejected
    raw = form.get("viewed")
    viewed = [h for h in raw if isinstance(h, str) and catalog.HANDLE.match(h)][:catalog.MAX_VIEWED] if isinstance(raw, list) else []

    try:
        n = conn.execute(
            "SELECT count(*) n FROM signup_rate WHERE ip=%s AND ts > now() - interval '1 hour'", (ip,)
        ).fetchone()["n"]
        if n >= RATE_LIMIT:
            return 429, {"error": "slow down"}
        conn.execute("INSERT INTO signup_rate (ip) VALUES (%s)", (ip,))

        pid = profiles.upsert(conn, email)
        # upsert's INSERT...ON CONFLICT DO UPDATE holds the profile row lock until commit, serializing concurrent same-email signups
        conn.execute("DELETE FROM suppressions WHERE email=%s AND store=%s AND reason='unsubscribed'", (email, store.key))
        profiles.set_consent(conn, pid, store.key, "subscribed", source, ip)

        row = conn.execute(
            # one welcome code per address per Shopify store: storefronts of one Shopify store share it, and a code works on all
            "SELECT code, created_at FROM coupons WHERE profile_id=%s AND store = ANY(%s) AND pool=%s FOR UPDATE",
            (pid, stores.same_shop(store), store.welcome_pool),
        ).fetchone()
        shop = shopify_client_factory(store)
        repeat = row is not None
        if row:
            # The welcome offer is once per address: no new enrolment (so no new mail) and no new code.
            # Hand back the old code only while it is still valid — an expired one would fail at checkout.
            live = coupons.expiry(row["created_at"], store.welcome_valid_days) > datetime.now(timezone.utc)
            code = row["code"] if live else None
        else:
            code = new_code(store)
            created = datetime.now(timezone.utc)   # one timestamp for Shopify's endsAt and the mails' quoted expiry
            shop.create_discount_code(code, store.welcome_amount, store.welcome_min_subtotal, f"Welcome {code}",
                                      coupons.expiry(created, store.welcome_valid_days))
            conn.execute(
                "INSERT INTO coupons (profile_id, store, pool, code, created_at) VALUES (%s,%s,%s,%s,%s)",
                (pid, store.key, store.welcome_pool, code, created),
            )
            conn.execute(
                "INSERT INTO events (store, profile_id, type, payload) VALUES (%s,%s,'signup',%s::jsonb)",
                (store.key, pid, json.dumps({"source": source, "viewed": viewed})),
            )
            engine.enrol(conn, "welcome", store.key, pid)
        conn.commit()
    except Exception:
        logger.exception("Signup error")
        conn.rollback()
        return 502, {"error": "temporarily unavailable"}

    # Best-effort: set marketing consent after commit
    try:
        shop.set_marketing_consent(email, True)
    except Exception:
        logger.exception("Failed to set marketing consent")
        domain = email.split("@", 1)[-1]
        slack.alert(f"signup: Shopify consent write-back failed for {store.key} (…@{domain}) — consent already recorded")

    # The code is never returned: it only travels by email, so a typo'd or made-up address gets nothing —
    # that keeps bogus addresses (= bounces) off the list. `enrolled` tells main.py to send email 1 right away.
    out = {"ok": True, "repeat": repeat, "code_valid": code is not None, "enrolled": not repeat}
    if token_secret:
        out["vid"] = tokens.visitor(pid, token_secret)   # lets the snippet report product views for this profile
    return 200, out
