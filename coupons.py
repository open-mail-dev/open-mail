"""Welcome-code validity. One function, used both when the Shopify code is created (endsAt) and when a
mail quotes the expiry date, so the two can never disagree."""
import os
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

# The shop's time zone: codes end at the end of a calendar day here, and mails quote dates in it.
AMS = ZoneInfo(os.environ.get("MAIL_TIMEZONE", "Europe/Amsterdam"))


def expiry(created_at: datetime, valid_days: int) -> datetime:
    """End of the shop's calendar day `valid_days` after creation, so "valid until 13 October"
    holds for all of 13 October."""
    last_day = created_at.astimezone(AMS).date() + timedelta(days=valid_days)
    return datetime.combine(last_day, time(23, 59, 59), tzinfo=AMS)
