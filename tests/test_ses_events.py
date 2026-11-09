import json

import profiles
import ses_events

TOPIC = "arn:aws:sns:eu-west-1:111122223333:ses-events"
OTHER_TOPIC = "arn:aws:sns:eu-west-1:999999999999:evil-topic"


def note(obj, topic=TOPIC):
    return {"Type": "Notification", "Message": json.dumps(obj), "TopicArn": topic}


def test_permanent_bounce_suppresses_everywhere(conn):
    ses_events.handle(conn, note({"eventType": "Bounce", "bounce": {"bounceType": "Permanent",
        "bouncedRecipients": [{"emailAddress": "Jan@Example.org"}]}}), verify=False, allowed_topics={TOPIC})
    row = conn.execute("SELECT email, store, reason FROM suppressions").fetchone()
    assert row == {"email": "jan@example.org", "store": "*", "reason": "hard_bounce"}


def test_transient_bounce_ignored(conn):
    ses_events.handle(conn, note({"eventType": "Bounce", "bounce": {"bounceType": "Transient",
        "bouncedRecipients": [{"emailAddress": "jan@example.org"}]}}), verify=False, allowed_topics={TOPIC})
    assert conn.execute("SELECT count(*) n FROM suppressions").fetchone()["n"] == 0


def test_complaint_suppresses(conn):
    ses_events.handle(conn, note({"eventType": "Complaint", "complaint": {
        "complainedRecipients": [{"emailAddress": "jan@example.org"}]}}), verify=False, allowed_topics={TOPIC})
    assert conn.execute("SELECT reason FROM suppressions").fetchone()["reason"] == "complaint"


def test_suppression_blocks_sendability_for_every_store(conn):
    """Fix round 1, item 7: a '*' suppression (hard bounce / complaint) must
    make is_sendable False for ANY store, not just the store in the event."""
    p = profiles.upsert(conn, "jan@example.org")
    profiles.set_consent(conn, p, "outlet.example.com", "subscribed", "popup")
    assert profiles.is_sendable(conn, p, "outlet.example.com")
    ses_events.handle(conn, note({"eventType": "Complaint", "complaint": {
        "complainedRecipients": [{"emailAddress": "jan@example.org"}]}}), verify=False, allowed_topics={TOPIC})
    assert not profiles.is_sendable(conn, p, "outlet.example.com")


def test_subscribe_url_must_be_aws(conn, monkeypatch):
    called = []
    monkeypatch.setattr(ses_events.requests, "get", lambda u, timeout: called.append(u))
    ses_events.handle(conn, {"Type": "SubscriptionConfirmation", "SubscribeURL": "https://evil.example/x", "TopicArn": TOPIC},
                       verify=False, allowed_topics={TOPIC})
    ses_events.handle(conn, {"Type": "SubscriptionConfirmation", "SubscribeURL": "https://sns.eu-west-1.amazonaws.com/?x", "TopicArn": TOPIC},
                       verify=False, allowed_topics={TOPIC})
    assert called == ["https://sns.eu-west-1.amazonaws.com/?x"]


def _no_network(*a, **k):
    raise AssertionError("verify_sns must not hit the network for a rejected message")


def test_verify_sns_rejects_non_aws_signing_cert_without_network(monkeypatch):
    monkeypatch.setattr(ses_events.requests, "get", _no_network)
    m = {
        "Type": "Notification",
        "SignatureVersion": "2",
        "SigningCertURL": "https://evil.example/cert.pem",
        "Signature": "irrelevant",
        "Message": "{}",
        "MessageId": "id",
        "Timestamp": "2026-01-01T00:00:00Z",
        "TopicArn": TOPIC,
    }
    assert ses_events.verify_sns(m) is False


def test_verify_sns_rejects_wrong_signature_version_without_network(monkeypatch):
    monkeypatch.setattr(ses_events.requests, "get", _no_network)
    m = {
        "Type": "Notification",
        "SignatureVersion": "1",
        "SigningCertURL": "https://sns.eu-west-1.amazonaws.com/cert.pem",
        "Signature": "irrelevant",
        "Message": "{}",
        "MessageId": "id",
        "Timestamp": "2026-01-01T00:00:00Z",
        "TopicArn": TOPIC,
    }
    assert ses_events.verify_sns(m) is False


def test_verify_sns_returns_false_when_cert_fetch_fails(monkeypatch):
    """Fix round 1, item 2: a cert-fetch failure must be swallowed (return
    False), never raise."""
    def boom(*a, **k):
        raise RuntimeError("network down")
    monkeypatch.setattr(ses_events.requests, "get", boom)
    m = {
        "Type": "Notification",
        "SignatureVersion": "2",
        "SigningCertURL": "https://sns.eu-west-1.amazonaws.com/test-cert-fetch-fails.pem",
        "Signature": "irrelevant",
        "Message": "{}",
        "MessageId": "id",
        "Timestamp": "2026-01-01T00:00:00Z",
        "TopicArn": TOPIC,
    }
    assert ses_events.verify_sns(m) is False


def test_handle_verify_true_rejects_bogus_message(conn):
    """Fix round 1, item 2."""
    assert ses_events.handle(conn, {"Type": "Notification", "Message": "{}", "TopicArn": TOPIC},
                              verify=True, allowed_topics={TOPIC}) == 403


def test_foreign_topic_arn_rejected(conn, monkeypatch):
    """Fix round 1, item 1 (critical): AWS-signed does not mean OUR topic.
    A Notification/Bounce arriving on a topic we didn't allowlist must be
    rejected before it ever reaches the suppression logic, and must not
    trigger any network call."""
    called = []
    monkeypatch.setattr(ses_events.requests, "get", lambda *a, **k: called.append(a))
    status = ses_events.handle(
        conn,
        note({"eventType": "Bounce", "bounce": {"bounceType": "Permanent",
              "bouncedRecipients": [{"emailAddress": "jan@example.org"}]}}, topic=OTHER_TOPIC),
        verify=False, allowed_topics={TOPIC},
    )
    assert status == 403
    assert called == []
    assert conn.execute("SELECT count(*) n FROM suppressions").fetchone()["n"] == 0


def test_empty_allowlist_rejects_everything(conn):
    """Fix round 1, item 1: fail closed when no topics are configured."""
    status = ses_events.handle(conn, note({"eventType": "Complaint", "complaint": {
        "complainedRecipients": [{"emailAddress": "jan@example.org"}]}}), verify=False, allowed_topics=set())
    assert status == 403
    assert conn.execute("SELECT count(*) n FROM suppressions").fetchone()["n"] == 0


def test_default_allowed_topics_comes_from_config(conn, monkeypatch):
    """handle() falls back to config.SNS_TOPIC_ARNS when allowed_topics is
    not passed."""
    import config
    monkeypatch.setattr(config, "SNS_TOPIC_ARNS", {TOPIC})
    status = ses_events.handle(conn, note({"eventType": "Complaint", "complaint": {
        "complainedRecipients": [{"emailAddress": "jan@example.org"}]}}), verify=False)
    assert status == 200
    assert conn.execute("SELECT count(*) n FROM suppressions").fetchone()["n"] == 1


def test_ses_topic_validation_text_is_acknowledged_not_a_500(conn):
    msg = {"Type": "Notification", "Message": "Successfully validated SNS topic for Amazon SES event publishing.", "TopicArn": TOPIC}
    assert ses_events.handle(conn, msg, verify=False, allowed_topics={TOPIC}) == 200
    assert conn.execute("SELECT count(*) n FROM suppressions").fetchone()["n"] == 0
