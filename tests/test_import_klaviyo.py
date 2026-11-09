import pytest
from unittest.mock import patch

import import_klaviyo
import profiles

S = "outlet.example.com"


def test_import_maps_consent_and_suppressions(conn):
    out = import_klaviyo.apply(conn, S, [
        {"email": "a@x.nl", "consent": "SUBSCRIBED", "consent_timestamp": "2026-05-01T10:00:00Z", "method": "KLAVIYO_FORM"},
        {"email": "b@x.nl", "consent": "UNSUBSCRIBED", "consent_timestamp": "2026-06-01T10:00:00Z", "method": "PREFERENCE_PAGE"},
        {"email": "c@x.nl", "consent": "NEVER_SUBSCRIBED", "consent_timestamp": None, "method": None},
        {"email": "not-an-email", "consent": "SUBSCRIBED", "consent_timestamp": None, "method": None},
    ])
    assert out == {"subscribed": 1, "suppressed": 1, "skipped": 2, "errors": 0, "other_storefront": 0}
    a = profiles.upsert(conn, "a@x.nl"); b = profiles.upsert(conn, "b@x.nl")
    assert profiles.is_sendable(conn, a, S) and not profiles.is_sendable(conn, b, S)


def test_import_never_enrols_into_welcome(conn):
    import_klaviyo.apply(conn, S, [{"email": "a@x.nl", "consent": "SUBSCRIBED", "consent_timestamp": None, "method": "KLAVIYO_FORM"}])
    assert conn.execute("SELECT count(*) n FROM enrolments").fetchone()["n"] == 0


def test_import_does_not_override_newer_local_unsubscribe(conn):
    p = profiles.upsert(conn, "a@x.nl")
    profiles.suppress(conn, "a@x.nl", S, "unsubscribed")
    import_klaviyo.apply(conn, S, [{"email": "a@x.nl", "consent": "SUBSCRIBED", "consent_timestamp": None, "method": "KLAVIYO_FORM"}])
    assert not profiles.is_sendable(conn, p, S)


def test_import_suppresses_profiles_with_suppression_data(conn):
    """Profiles with a global suppression reason should be suppressed with '*' store."""
    out = import_klaviyo.apply(conn, S, [
        {"email": "bounced@x.nl", "consent": "SUBSCRIBED", "consent_timestamp": "2026-05-01T10:00:00Z", "method": "KLAVIYO_FORM", "suppression": ["HARD_BOUNCE"]},
        {"email": "complained@x.nl", "consent": "SUBSCRIBED", "consent_timestamp": "2026-05-01T10:00:00Z", "method": "KLAVIYO_FORM", "suppression": ["SPAM_COMPLAINT"]},
        {"email": "clean@x.nl", "consent": "SUBSCRIBED", "consent_timestamp": "2026-05-01T10:00:00Z", "method": "KLAVIYO_FORM", "suppression": []},
    ])
    assert out == {"subscribed": 1, "suppressed": 2, "skipped": 0, "errors": 0, "other_storefront": 0}
    bounced = profiles.upsert(conn, "bounced@x.nl")
    complained = profiles.upsert(conn, "complained@x.nl")
    clean = profiles.upsert(conn, "clean@x.nl")
    assert not profiles.is_sendable(conn, bounced, S)
    assert not profiles.is_sendable(conn, complained, S)
    assert profiles.is_sendable(conn, clean, S)
    row = conn.execute("SELECT store FROM suppressions WHERE email=%s", ("bounced@x.nl",)).fetchone()
    assert row["store"] == "*"


def test_import_hard_bounce_suppresses_globally(conn):
    """(a) HARD_BOUNCE -> a '*' (global) suppression row, reason 'import'."""
    out = import_klaviyo.apply(conn, S, [
        {"email": "bounced2@x.nl", "consent": "SUBSCRIBED", "consent_timestamp": None, "method": None, "suppression": ["HARD_BOUNCE"]},
    ])
    assert out["suppressed"] == 1
    row = conn.execute("SELECT store, reason FROM suppressions WHERE email=%s", ("bounced2@x.nl",)).fetchone()
    assert row["store"] == "*" and row["reason"] == "import"


def test_import_unsubscribe_reason_suppresses_store_scoped_only(conn):
    """(b) UNSUBSCRIBE -> a store-scoped row with reason 'unsubscribed', no '*' row."""
    out = import_klaviyo.apply(conn, S, [
        {"email": "optedout@x.nl", "consent": "UNSUBSCRIBED", "consent_timestamp": None, "method": None, "suppression": ["UNSUBSCRIBE"]},
    ])
    assert out["suppressed"] == 1
    rows = {r["store"]: r["reason"] for r in conn.execute(
        "SELECT store, reason FROM suppressions WHERE email=%s", ("optedout@x.nl",)).fetchall()}
    assert rows == {S: "unsubscribed"}
    assert "*" not in rows


def test_import_user_suppressed_reason_is_store_scoped(conn):
    """USER_SUPPRESSED, like UNSUBSCRIBE, is a store-scoped opt-out, not global."""
    import_klaviyo.apply(conn, S, [
        {"email": "usersup@x.nl", "consent": "UNSUBSCRIBED", "consent_timestamp": None, "method": None, "suppression": ["USER_SUPPRESSED"]},
    ])
    rows = {r["store"]: r["reason"] for r in conn.execute(
        "SELECT store, reason FROM suppressions WHERE email=%s", ("usersup@x.nl",)).fetchall()}
    assert rows == {S: "unsubscribed"}


def test_import_then_popup_resignup_resubscribes(conn):
    """(c) After importing an UNSUBSCRIBED row (store-scoped 'unsubscribed'
    suppression), a popup re-signup for that email must succeed in making the
    profile sendable again — signup.py's DELETE only clears store-scoped
    reason='unsubscribed' rows, so import must never use reason='import' for
    a plain per-store unsubscribe."""
    import signup

    class FakeShopify:
        def create_discount_code(self, *a, **kw):
            pass

        def set_marketing_consent(self, *a, **kw):
            pass

    import_klaviyo.apply(conn, S, [
        {"email": "resub@x.nl", "consent": "UNSUBSCRIBED", "consent_timestamp": None, "method": None},
    ])
    pid = profiles.upsert(conn, "resub@x.nl")
    assert not profiles.is_sendable(conn, pid, S)

    status, body = signup.handle(
        conn,
        {"store": S, "email": "resub@x.nl", "source": "popup", "website": ""},
        "203.0.113.20",
        lambda store: FakeShopify(),
    )
    assert status == 200
    assert profiles.is_sendable(conn, pid, S)


def test_import_handles_row_errors_and_continues(conn):
    """Bad rows should be counted as errors but not abort the import."""
    out = import_klaviyo.apply(conn, S, [
        {"email": "good1@x.nl", "consent": "SUBSCRIBED", "consent_timestamp": None, "method": None},
        {"email": 12345, "consent": "SUBSCRIBED", "consent_timestamp": None, "method": None},  # non-string email
        {"email": "good2@x.nl", "consent": "UNSUBSCRIBED", "consent_timestamp": None, "method": None},
    ])
    assert out == {"subscribed": 1, "suppressed": 1, "skipped": 0, "errors": 1, "other_storefront": 0}
    # Both good rows should be persisted despite the error row
    good1 = profiles.upsert(conn, "good1@x.nl")
    good2 = profiles.upsert(conn, "good2@x.nl")
    assert profiles.is_sendable(conn, good1, S)
    assert not profiles.is_sendable(conn, good2, S)


def test_import_unsubscribed_with_suppression_records_also_logs_consent(conn):
    """UNSUBSCRIBED + suppression should both suppress and log consent."""
    out = import_klaviyo.apply(conn, S, [
        {"email": "unsubscribed@x.nl", "consent": "UNSUBSCRIBED", "consent_timestamp": "2026-06-01T10:00:00Z", "method": "PREFERENCE_PAGE", "suppression": ["HARD_BOUNCE"]},
    ])
    assert out == {"subscribed": 0, "suppressed": 1, "skipped": 0, "errors": 0, "other_storefront": 0}
    p = profiles.upsert(conn, "unsubscribed@x.nl")
    # Should be suppressed and not sendable
    assert not profiles.is_sendable(conn, p, S)
    # Consent should be logged
    log_entries = conn.execute(
        "SELECT status FROM consent_log WHERE profile_id=%s ORDER BY ts DESC LIMIT 1",
        (p,)
    ).fetchone()
    assert log_entries["status"] == "unsubscribed"


def test_fetch_pages_retries_429_then_succeeds(conn):
    """fetch_pages should retry on 429 with Retry-After header."""
    sleep_calls = []

    def mock_sleep(duration):
        sleep_calls.append(duration)

    # Mock requests.get to return 429 once, then success
    call_count = [0]

    class MockResponse:
        def __init__(self, status_code):
            self.status_code = status_code
            self.headers = {"Retry-After": "1"}
            self.text = "error"

        def raise_for_status(self):
            if self.status_code >= 400:
                raise Exception("HTTP error")

        def json(self):
            return {
                "data": [
                    {
                        "attributes": {
                            "email": "test@x.nl",
                            "subscriptions": {
                                "email": {
                                    "marketing": {
                                        "consent": "SUBSCRIBED",
                                        "consent_timestamp": "2026-05-01T10:00:00Z",
                                        "method": "FORM",
                                        "suppression": []
                                    }
                                }
                            }
                        }
                    }
                ],
                "links": {"next": None}
            }

    def mock_get(url, headers=None, timeout=None):
        call_count[0] += 1
        if call_count[0] == 1:
            return MockResponse(429)
        return MockResponse(200)

    with patch("import_klaviyo.requests.get", side_effect=mock_get):
        pages = list(import_klaviyo.fetch_pages("test_key", sleep_fn=mock_sleep))
        assert len(pages) == 1
        assert len(pages[0]) == 1
        assert pages[0][0]["email"] == "test@x.nl"
        assert len(sleep_calls) == 1
        assert sleep_calls[0] == 1


def test_fetch_pages_follows_pagination_links(conn):
    """fetch_pages should follow multiple pages via links.next."""
    page_num = [0]

    class MockResponse:
        def __init__(self, status_code, email, next_link=None):
            self.status_code = status_code
            self.headers = {}
            self.email = email
            self.next_link = next_link

        def raise_for_status(self):
            if self.status_code >= 400:
                raise Exception("HTTP error")

        def json(self):
            return {
                "data": [
                    {
                        "attributes": {
                            "email": self.email,
                            "subscriptions": {
                                "email": {
                                    "marketing": {
                                        "consent": "SUBSCRIBED",
                                        "consent_timestamp": "2026-05-01T10:00:00Z",
                                        "method": "FORM",
                                        "suppression": []
                                    }
                                }
                            }
                        }
                    }
                ],
                "links": {"next": self.next_link}
            }

    def mock_get(url, headers=None, timeout=None):
        page_num[0] += 1
        if page_num[0] == 1:
            return MockResponse(200, "profile_page_1@x.nl", next_link="https://api.klaviyo.com/page2")
        else:
            return MockResponse(200, f"profile_page_2@x.nl", next_link=None)

    with patch("import_klaviyo.requests.get", side_effect=mock_get):
        pages = list(import_klaviyo.fetch_pages("test_key"))
        assert len(pages) == 2
        assert pages[0][0]["email"] == "profile_page_1@x.nl"
        assert pages[1][0]["email"] == "profile_page_2@x.nl"


def test_other_storefront_profiles_only_bring_global_suppressions(conn):
    ex = import_klaviyo.excluder({"se@x.se"}, ("sv", "da"))
    out = import_klaviyo.apply(conn, "shop.example.com", [
        {"email": "nl@x.nl", "consent": "SUBSCRIBED", "locale": ""},
        {"email": "SE@x.se", "consent": "SUBSCRIBED", "locale": ""},                         # on the SE popup list
        {"email": "dk@x.dk", "consent": "SUBSCRIBED", "locale": "da-DK"},
        {"email": "bad@x.dk", "consent": "UNSUBSCRIBED", "locale": "da", "suppression": ["HARD_BOUNCE"]},
        {"email": "quit@x.se", "consent": "UNSUBSCRIBED", "locale": "sv-SE", "suppression": ["UNSUBSCRIBE"]},
    ], ex)
    assert out["subscribed"] == 1 and out["other_storefront"] == 4
    consent = {r["email"] for r in conn.execute("SELECT p.email FROM consent c JOIN profiles p ON p.id=c.profile_id")}
    assert consent == {"nl@x.nl"}
    sup = {(r["email"], r["store"]) for r in conn.execute("SELECT email, store FROM suppressions")}
    assert sup == {("bad@x.dk", "*")}
