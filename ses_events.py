import base64
import json
import logging
import re
import urllib.parse

import requests
from cryptography import x509
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding

import config
import profiles

_FIELDS = {
    "Notification": ["Message", "MessageId", "Subject", "Timestamp", "TopicArn", "Type"],
    "SubscriptionConfirmation": ["Message", "MessageId", "SubscribeURL", "Timestamp", "Token", "TopicArn", "Type"],
    # UnsubscribeConfirmation is intentionally unsupported: it has no entry here,
    # so its canonical string is empty and a real AWS-signed one will always fail
    # verify_sns's signature check below (→ handle() returns 403). We never want
    # to auto-process an unsubscribe-from-the-SNS-topic confirmation.
}

_SNS_HOST = re.compile(r"^sns\.[a-z0-9-]+\.amazonaws\.com$")

# Cache fetched SigningCertURL bodies by URL — AWS reuses the same signing cert
# for long stretches, and this is called on every inbound SNS delivery.
_cert_cache: dict[str, bytes] = {}


def _aws_host(url: str) -> bool:
    u = urllib.parse.urlparse(url)
    return u.scheme == "https" and bool(_SNS_HOST.match(u.hostname or ""))


def verify_sns(m: dict) -> bool:
    if m.get("SignatureVersion") != "2" or not _aws_host(m.get("SigningCertURL", "")):
        return False
    canon = "".join(f"{k}\n{m[k]}\n" for k in _FIELDS.get(m.get("Type"), []) if m.get(k) is not None).encode()
    url = m["SigningCertURL"]
    try:
        pem = _cert_cache.get(url)
        if pem is None:
            resp = requests.get(url, timeout=10)
            resp.raise_for_status()
            pem = resp.content
            _cert_cache[url] = pem
        cert = x509.load_pem_x509_certificate(pem)
        cert.public_key().verify(base64.b64decode(m["Signature"]), canon, padding.PKCS1v15(), hashes.SHA256())
        return True
    except Exception:
        return False


def handle(conn, m: dict, verify: bool = True, allowed_topics: set[str] | None = None) -> int:
    if verify and not verify_sns(m):
        return 403
    if allowed_topics is None:
        allowed_topics = config.SNS_TOPIC_ARNS
    # Signature verification only proves AWS signed *some* SNS message, not that
    # it came from OUR topic — anyone can create a topic, subscribe our /ses URL
    # (auto-confirmed below) and publish fake Bounce/Complaint events. An empty
    # allowlist rejects everything (fail closed).
    if m.get("TopicArn") not in allowed_topics:
        return 403
    if m.get("Type") == "SubscriptionConfirmation":
        if _aws_host(m.get("SubscribeURL", "")):
            requests.get(m["SubscribeURL"], timeout=10)
        return 200
    if m.get("Type") != "Notification":
        return 200
    try:
        ev = json.loads(m["Message"])
    except (TypeError, ValueError):
        # SES posts a plain-text "Successfully validated SNS topic for Amazon SES event publishing." to the topic
        # whenever an event destination is created; a 500 here only makes SNS retry it four times
        logging.info("non-JSON SNS message ignored: %.120s", m.get("Message"))
        return 200
    t = ev.get("eventType") or ev.get("notificationType")
    if t == "Bounce" and ev["bounce"].get("bounceType") == "Permanent":
        for r in ev["bounce"]["bouncedRecipients"]:
            profiles.suppress(conn, r["emailAddress"], "*", "hard_bounce")
    elif t == "Complaint":
        for r in ev["complaint"]["complainedRecipients"]:
            profiles.suppress(conn, r["emailAddress"], "*", "complaint")
    conn.commit()
    return 200
