"""Env-driven settings. On Cloud Functions, secrets arrive via Secret Manager (--update-secrets)."""
import json
import os
from datetime import datetime

DB_DSN = os.environ.get("MAIL_DB_DSN", "").strip()
TOKEN_SECRET = os.environ.get("MAIL_TOKEN_SECRET", "").strip()
IDENT_SECRET = os.environ.get("MAIL_IDENT_SECRET", "").strip()   # = shop metafield lsp.ident_key (sitetrack.ident)
WEB_BASE_URL = os.environ.get("MAIL_WEB_BASE_URL", "").rstrip("/")
SES_REGION = os.environ.get("MAIL_SES_REGION", "eu-west-1")
SLACK_WEBHOOK_URL = os.environ.get("SLACK_WEBHOOK_URL", "").strip()
# SNS topic ARNs we accept Notification/SubscriptionConfirmation messages from.
# Empty allowlist rejects everything — fail closed (see ses_events.handle).
SNS_TOPIC_ARNS = {a.strip() for a in os.environ.get("MAIL_SNS_TOPIC_ARNS", "").split(",") if a.strip()}
SHOPIFY_API_VERSION = "2026-07"
FREQUENCY_CAP_HOURS = 16
# Abandoned-checkout poller go-live (ISO 8601, UTC). Unset = poller off. Only checkouts created after this moment are
# ever reminded, so switching over from another tool never doubles a reminder it already started.
_ac = os.environ.get("MAIL_AC_SINCE", "").strip()
AC_SINCE = datetime.fromisoformat(_ac.replace("Z", "+00:00")) if _ac else None
# Per-storefront go-live for storefronts with Store.ac_own_start, e.g. {"shop-ie.example.com": "2026-10-13T09:00:00Z"};
# such a storefront is skipped until it is listed here.
AC_SINCE_BY_STORE = {k: datetime.fromisoformat(v.replace("Z", "+00:00"))
                     for k, v in json.loads(os.environ.get("MAIL_AC_SINCE_BY_STORE", "") or "{}").items()}


def site_live() -> set[str]:
    """Store keys whose browse/cart reminders are switched on (env MAIL_SITE_LIVE, comma-separated, on mail-ingest AND
    mail-tick). Read per call: the server-side kill switch, `gcloud run services update … --update-env-vars`."""
    return {k.strip() for k in os.environ.get("MAIL_SITE_LIVE", "").split(",") if k.strip()}


def pp_live() -> set[str]:
    """Store keys whose post_purchase series is switched on (env MAIL_PP_LIVE, comma-separated, on mail-ingest AND
    mail-tick). Read per call, like site_live()."""
    return {k.strip() for k in os.environ.get("MAIL_PP_LIVE", "").split(",") if k.strip()}


def wb_live() -> set[str]:
    """Store keys whose winback mail is switched on (env MAIL_WB_LIVE, comma-separated, on mail-tick). Read per call,
    like site_live()."""
    return {k.strip() for k in os.environ.get("MAIL_WB_LIVE", "").split(",") if k.strip()}


def bis_live() -> set[str]:
    """Store keys whose back-in-stock is switched on (env MAIL_BIS_LIVE, comma-separated, on mail-ingest AND mail-tick).
    Read per call, like site_live()."""
    return {k.strip() for k in os.environ.get("MAIL_BIS_LIVE", "").split(",") if k.strip()}
