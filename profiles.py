import re

_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_DOMAIN = re.compile(r"^[^.]+(\.[^.]+)+$")   # no empty labels: gmail..com is unsendable (SES and Shopify reject it)


def normalise(email: str) -> str:
    e = (email or "").strip().lower()
    if len(e) > 254 or not _EMAIL.match(e) or not _DOMAIN.match(e.rsplit("@", 1)[-1]):
        raise ValueError(f"invalid email: {email!r}")
    return e


def upsert(conn, email: str) -> int:
    e = normalise(email)
    row = conn.execute(
        "INSERT INTO profiles (email) VALUES (%s) ON CONFLICT (email) DO UPDATE SET email = EXCLUDED.email RETURNING id",
        (e,),
    ).fetchone()
    return row["id"]


def email_of(conn, profile_id: int) -> str:
    return conn.execute("SELECT email FROM profiles WHERE id=%s", (profile_id,)).fetchone()["email"]


def set_consent(conn, profile_id: int, store: str, status: str, source: str, ip: str | None = None) -> None:
    conn.execute(
        """INSERT INTO consent (profile_id, store, status, source, ip) VALUES (%s,%s,%s,%s,%s)
           ON CONFLICT (profile_id, store) DO UPDATE
           SET status=EXCLUDED.status, source=EXCLUDED.source, ip=EXCLUDED.ip, ts=now()""",
        (profile_id, store, status, source, ip),
    )
    conn.execute(
        "INSERT INTO consent_log (profile_id, store, status, source, ip) VALUES (%s,%s,%s,%s,%s)",
        (profile_id, store, status, source, ip),
    )


def suppress(conn, email: str, store: str, reason: str) -> None:
    conn.execute(
        "INSERT INTO suppressions (email, store, reason) VALUES (%s,%s,%s) ON CONFLICT (email, store) DO UPDATE SET reason=EXCLUDED.reason, ts=now()",
        (normalise(email), store, reason),
    )


def is_sendable(conn, profile_id: int, store: str, audience: str = "subscribed") -> bool:
    """audience "subscribed": needs a subscribed consent row; "not_unsubscribed": anyone without an unsubscribed
    row. Suppressions (store or global) block both. "requested": only is_blocked() stops it (consent is ignored)."""
    if audience == "requested":
        row = conn.execute("SELECT email FROM profiles WHERE id=%s", (profile_id,)).fetchone()
        return bool(row) and not is_blocked(conn, row["email"], store)
    row = conn.execute(
        """SELECT c.status,
                  EXISTS (SELECT 1 FROM suppressions s WHERE s.email = p.email AND s.store IN (%s, '*')) AS suppressed
           FROM profiles p LEFT JOIN consent c ON c.profile_id = p.id AND c.store = %s
           WHERE p.id = %s""",
        (store, store, profile_id),
    ).fetchone()
    if not row or row["suppressed"]:
        return False
    if audience == "not_unsubscribed":
        return row["status"] != "unsubscribed"
    return row["status"] == "subscribed"


def is_blocked(conn, email: str, store: str) -> bool:
    """True when nothing at all may be mailed to this address for `store`: a hard bounce, complaint or imported
    suppression (store or '*'). An unsubscribe (reason 'unsubscribed') only ends marketing, so it does not count."""
    return conn.execute(
        "SELECT 1 FROM suppressions WHERE email=%s AND store IN (%s, '*') AND reason <> 'unsubscribed'", (email, store)
    ).fetchone() is not None
