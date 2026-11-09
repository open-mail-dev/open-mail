"""Optional, one-off: import consent + suppressions from a Klaviyo account when migrating to this system.
Run this ONCE per storefront before it goes live:
    KLAVIYO_KEY=pk_... python import_klaviyo.py shop.example.com "<dsn>"
For one storefront of a Klaviyo account shared by several storefronts:
    KLAVIYO_KEY=pk_... python import_klaviyo.py shop.example.com "<dsn>" \
        --exclude-lists <list id>,<list id> --exclude-locales fi,sv
Excluded profiles (members of those lists, or $locale starting with one of those) only contribute GLOBAL
suppressions (hard bounce / complaint / invalid) — their consent belongs to the other storefront.
Imported profiles are NOT enrolled into welcome (they already received one from the previous tool).
Note: consent_timestamp is intentionally not compared against local consent."""
import logging
import os
import sys
import time

import requests

import db
import profiles

logger = logging.getLogger(__name__)

# Klaviyo suppression reasons that mean the address is undeliverable/abusive
# everywhere, not just for this one store's list — these get a global ('*')
# suppression. Everything else (UNSUBSCRIBE, USER_SUPPRESSED, or any other/
# unrecognised reason) is treated as a store-scoped opt-out: reason
# "unsubscribed", so our own popup can re-subscribe the address later
# (signup.py only ever clears store-scoped 'unsubscribed' suppressions).
GLOBAL_SUPPRESSION_REASONS = {"HARD_BOUNCE", "SPAM_COMPLAINT", "INVALID_EMAIL"}


def apply(conn, store: str, rows: list[dict], excluded=lambda r: False) -> dict:
    out = {"subscribed": 0, "suppressed": 0, "skipped": 0, "errors": 0, "other_storefront": 0}
    for r in rows:
        try:
            with conn.transaction():
                try:
                    email = profiles.normalise(r.get("email") or "")
                except ValueError:
                    out["skipped"] += 1
                    continue

                if excluded(r):
                    out["other_storefront"] += 1
                    if {str(x).upper() for x in r.get("suppression") or []} & GLOBAL_SUPPRESSION_REASONS:
                        profiles.upsert(conn, email)
                        profiles.suppress(conn, email, "*", "import")
                    continue

                pid = profiles.upsert(conn, email)

                # Check if profile has suppression data (via Klaviyo)
                suppression_list = r.get("suppression") or []
                if suppression_list:
                    reasons = {str(x).upper() for x in suppression_list}
                    if reasons & GLOBAL_SUPPRESSION_REASONS:
                        # Hard bounce / spam complaint / invalid email: suppress
                        # everywhere. Also log consent if UNSUBSCRIBED.
                        if r.get("consent") == "UNSUBSCRIBED":
                            profiles.set_consent(conn, pid, store, "unsubscribed", "import_klaviyo")
                        profiles.suppress(conn, email, "*", "import")
                    else:
                        # UNSUBSCRIBE / USER_SUPPRESSED / anything else: a
                        # store-scoped opt-out, not a global bounce/complaint.
                        profiles.set_consent(conn, pid, store, "unsubscribed", "import_klaviyo")
                        profiles.suppress(conn, email, store, "unsubscribed")
                    out["suppressed"] += 1
                elif r.get("consent") == "SUBSCRIBED":
                    already_out = conn.execute(
                        "SELECT 1 FROM suppressions WHERE email=%s AND store IN (%s,'*')", (email, store)).fetchone()
                    if already_out:
                        out["skipped"] += 1
                        continue
                    profiles.set_consent(conn, pid, store, "subscribed", "import_klaviyo")
                    out["subscribed"] += 1
                elif r.get("consent") == "UNSUBSCRIBED":
                    profiles.set_consent(conn, pid, store, "unsubscribed", "import_klaviyo")
                    profiles.suppress(conn, email, store, "unsubscribed")
                    out["suppressed"] += 1
                else:
                    out["skipped"] += 1
        except Exception:
            logger.exception(f"Error processing row: {r}")
            out["errors"] += 1
            continue
    conn.commit()
    return out


def fetch_pages(key: str, sleep_fn=time.sleep):
    """Yield page-sized lists of profiles from Klaviyo with 429/5xx retry logic."""
    url = "https://a.klaviyo.com/api/profiles/?additional-fields[profile]=subscriptions&page[size]=100"
    h = {"Authorization": f"Klaviyo-API-Key {key}", "revision": "2024-10-15", "accept": "application/json"}

    while url:
        for attempt in range(5):
            try:
                resp = requests.get(url, headers=h, timeout=60)
                if resp.status_code in (429, 500, 502, 503, 504):
                    retry_after = int(resp.headers.get("Retry-After", 2 ** attempt))
                    if attempt < 4:
                        logger.info(f"Rate limited/server error {resp.status_code}, retrying after {retry_after}s")
                        sleep_fn(retry_after)
                        continue
                    resp.raise_for_status()
                resp.raise_for_status()
                d = resp.json()
                break
            except requests.RequestException:
                if attempt == 4:
                    raise
                retry_after = int(resp.headers.get("Retry-After", 2 ** attempt)) if 'resp' in locals() else 2 ** attempt
                logger.info(f"Request failed, retrying after {retry_after}s")
                sleep_fn(retry_after)

        page = []
        for p in d["data"]:
            m = ((p["attributes"].get("subscriptions") or {}).get("email") or {}).get("marketing") or {}
            page.append({"email": p["attributes"].get("email"), "consent": m.get("consent"),
                        "consent_timestamp": m.get("consent_timestamp"), "method": m.get("method"),
                        "suppression": [r.get("reason") for r in m.get("suppression") or []],
                        "locale": (p["attributes"].get("properties") or {}).get("$locale") or ""})
        yield page
        url = d["links"].get("next")


def list_emails(key: str, list_id: str) -> set[str]:
    url = f"https://a.klaviyo.com/api/lists/{list_id}/profiles/?page[size]=100"
    h = {"Authorization": f"Klaviyo-API-Key {key}", "revision": "2024-10-15", "accept": "application/json"}
    out = set()
    while url:
        resp = requests.get(url, headers=h, timeout=60)
        if resp.status_code == 429:
            time.sleep(int(resp.headers.get("Retry-After", 2)))
            continue
        resp.raise_for_status()
        d = resp.json()
        out |= {(p["attributes"].get("email") or "").strip().lower() for p in d["data"]}
        url = d["links"].get("next")
    return out


def excluder(other_list_emails: set[str], locale_prefixes: tuple[str, ...]):
    def excluded(r):
        return ((r.get("email") or "").strip().lower() in other_list_emails
                or any((r.get("locale") or "").lower().startswith(p) for p in locale_prefixes))
    return excluded


def _opt(name):
    return sys.argv[sys.argv.index(name) + 1].split(",") if name in sys.argv else []


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    store, dsn = sys.argv[1], sys.argv[2]
    key = os.environ["KLAVIYO_KEY"].strip()
    others = set().union(*[list_emails(key, lid) for lid in _opt("--exclude-lists")]) if _opt("--exclude-lists") else set()
    excluded = excluder(others, tuple(x.lower() for x in _opt("--exclude-locales")))
    logger.info(f"{len(others)} addresses on the other storefronts' lists")
    with db.connect(dsn) as c:
        total = {"subscribed": 0, "suppressed": 0, "skipped": 0, "errors": 0, "other_storefront": 0}
        for page_num, page in enumerate(fetch_pages(key), 1):
            result = apply(c, store, page, excluded)
            for key in total:
                total[key] += result[key]
            logger.info(f"Page {page_num}: {result}")
        logger.info(f"Total: {total}")
        print(total)
