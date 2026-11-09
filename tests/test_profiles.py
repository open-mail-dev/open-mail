import pytest

import profiles

S = "outlet.example.com"


def test_email_is_normalised():
    assert profiles.normalise("  Jan@Example.ORG ") == "jan@example.org"


@pytest.mark.parametrize("bad", ["", "jan", "jan@", "@example.org", "a b@c.nl", "x" * 250 + "@c.nl",
                                 "jan@gmail..com", "jan@.gmail.com", "jan@gmail.com."])
def test_invalid_email_rejected(bad):
    with pytest.raises(ValueError):
        profiles.normalise(bad)


def test_upsert_is_idempotent_across_case(conn):
    a = profiles.upsert(conn, "Jan@Example.org")
    b = profiles.upsert(conn, "jan@example.org")
    assert a == b


def test_subscribed_profile_is_sendable(conn):
    p = profiles.upsert(conn, "jan@example.org")
    profiles.set_consent(conn, p, S, "subscribed", "popup", "203.0.113.5")
    assert profiles.is_sendable(conn, p, S)


def test_never_subscribed_is_not_sendable(conn):
    p = profiles.upsert(conn, "jan@example.org")
    assert not profiles.is_sendable(conn, p, S)


def test_store_suppression_blocks(conn):
    p = profiles.upsert(conn, "jan@example.org")
    profiles.set_consent(conn, p, S, "subscribed", "popup")
    profiles.suppress(conn, "jan@example.org", S, "unsubscribed")
    assert not profiles.is_sendable(conn, p, S)


def test_global_suppression_blocks_every_store(conn):
    p = profiles.upsert(conn, "jan@example.org")
    profiles.set_consent(conn, p, S, "subscribed", "popup")
    profiles.suppress(conn, "JAN@example.org", "*", "hard_bounce")
    assert not profiles.is_sendable(conn, p, S)


def test_consent_change_is_logged(conn):
    p = profiles.upsert(conn, "jan@example.org")
    profiles.set_consent(conn, p, S, "subscribed", "popup")
    profiles.set_consent(conn, p, S, "unsubscribed", "unsubscribe_link")
    n = conn.execute("SELECT count(*) AS n FROM consent_log WHERE profile_id=%s", (p,)).fetchone()["n"]
    assert n == 2


def test_blocked_means_bounce_complaint_or_import_never_an_unsubscribe(conn):
    import profiles
    for email, store, reason in [("u@x.nl", "shop.example.com", "unsubscribed"), ("b@x.nl", "*", "hard_bounce"),
                                 ("c@x.nl", "*", "complaint"), ("i@x.nl", "*", "import"), ("o@x.nl", "outlet.example.com", "hard_bounce")]:
        profiles.suppress(conn, email, store, reason)
    assert [profiles.is_blocked(conn, e, "shop.example.com") for e in ("u@x.nl", "b@x.nl", "c@x.nl", "i@x.nl", "o@x.nl", "n@x.nl")] == \
        [False, True, True, True, False, False]


def test_is_sendable_requested_ignores_unsubscribe_not_suppression(conn):
    import profiles
    p = profiles.upsert(conn, "u@x.nl")
    profiles.set_consent(conn, p, "shop.example.com", "unsubscribed", "link")
    profiles.suppress(conn, "u@x.nl", "shop.example.com", "unsubscribed")
    assert profiles.is_sendable(conn, p, "shop.example.com", "requested")
    assert not profiles.is_sendable(conn, p, "shop.example.com", "not_unsubscribed")
    profiles.suppress(conn, "u@x.nl", "*", "complaint")
    assert not profiles.is_sendable(conn, p, "shop.example.com", "requested")
