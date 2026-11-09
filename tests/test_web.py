import profiles
import slack
import tokens
import web

S = "outlet.example.com"


class FakeShop:
    def __init__(self): self.calls = []
    def set_marketing_consent(self, email, subscribed): self.calls.append((email, subscribed))


def msg(conn):
    p = profiles.upsert(conn, "jan@example.org")
    profiles.set_consent(conn, p, S, "subscribed", "popup")
    return conn.execute("INSERT INTO messages (profile_id, store, kind) VALUES (%s,%s,'flow:welcome:2') RETURNING id", (p, S)).fetchone()["id"], p


def test_click_redirects_and_logs(conn):
    m, _ = msg(conn)
    t = tokens.sign({"m": m, "u": "https://outlet.example.com/collections/all?utm_source=mail"}, "k")
    status, h = web.click(conn, t, "k")
    assert status == 302 and h["Location"].startswith("https://outlet.example.com/")
    assert conn.execute("SELECT count(*) n FROM clicks").fetchone()["n"] == 1


def test_click_refuses_foreign_domain(conn):
    m, _ = msg(conn)
    assert web.click(conn, tokens.sign({"m": m, "u": "https://evil.example/"}, "k"), "k")[0] == 404


def test_click_bad_token_404(conn):
    assert web.click(conn, "garbage", "k")[0] == 404


def test_unsubscribe_get_does_not_unsubscribe(conn):
    m, p = msg(conn)
    status, page = web.unsubscribe(conn, tokens.sign({"m": m, "a": "unsub"}, "k"), "k", "GET", lambda s: FakeShop())
    assert status == 200 and "<form method=\"post\"" in page
    assert profiles.is_sendable(conn, p, S)


def test_unsubscribe_post_suppresses_and_writes_back(conn):
    m, p = msg(conn)
    shop = FakeShop()
    status, _ = web.unsubscribe(conn, tokens.sign({"m": m, "a": "unsub"}, "k"), "k", "POST", lambda s: shop)
    assert status == 200 and not profiles.is_sendable(conn, p, S)
    assert shop.calls == [("jan@example.org", False)]


def test_unsubscribe_survives_shopify_error(conn):
    m, p = msg(conn)

    class Broken:
        def set_marketing_consent(self, *a): raise RuntimeError("shopify down")

    status, _ = web.unsubscribe(conn, tokens.sign({"m": m, "a": "unsub"}, "k"), "k", "POST", lambda s: Broken())
    assert status == 200 and not profiles.is_sendable(conn, p, S)


def test_unsubscribe_shopify_error_alerts_slack_with_email_redacted(conn, monkeypatch):
    """Fix round 1, item 6: a write-back failure must be reconcilable via Slack,
    without leaking the full email address."""
    m, p = msg(conn)
    alerts = []
    monkeypatch.setattr(slack, "alert", lambda text: alerts.append(text))

    class Broken:
        def set_marketing_consent(self, *a): raise RuntimeError("shopify down")

    status, _ = web.unsubscribe(conn, tokens.sign({"m": m, "a": "unsub"}, "k"), "k", "POST", lambda s: Broken())
    assert status == 200
    assert len(alerts) == 1
    assert S in alerts[0] and "example.org" in alerts[0]
    assert "jan@" not in alerts[0]


def test_unsubscribe_records_ip_in_consent(conn):
    """Fix round 1, item 3."""
    m, p = msg(conn)
    web.unsubscribe(conn, tokens.sign({"m": m, "a": "unsub"}, "k"), "k", "POST", lambda s: FakeShop(), ip="203.0.113.5")
    row = conn.execute("SELECT ip FROM consent WHERE profile_id=%s AND store=%s", (p, S)).fetchone()
    assert str(row["ip"]) == "203.0.113.5"


def test_click_rejects_domain_of_a_different_store(conn):
    """Fix round 1, item 5: the redirect must match THIS message's own store,
    not merely any https URL — a token for a outlet.example.com message must
    not redirect to another registered-looking domain."""
    m, _ = msg(conn)
    t = tokens.sign({"m": m, "u": "https://shop.example.com/collections/all"}, "k")
    assert web.click(conn, t, "k")[0] == 404


def test_click_rejects_userinfo_domain_confusion(conn):
    """A URL like https://<real-domain>@evil.example/ must resolve on the
    PARSED hostname (evil.example), not be fooled by the real domain sitting
    in the userinfo component."""
    m, _ = msg(conn)
    t = tokens.sign({"m": m, "u": "https://outlet.example.com@evil.example/"}, "k")
    assert web.click(conn, t, "k")[0] == 404


def test_unsubscribe_rejects_click_token(conn):
    """Fix round 1, item 7: a click token (no 'a': 'unsub') must not be usable
    to unsubscribe."""
    m, _ = msg(conn)
    t = tokens.sign({"m": m, "u": "https://outlet.example.com/"}, "k")
    status, _ = web.unsubscribe(conn, t, "k", "POST", lambda s: FakeShop())
    assert status == 404


GIF = b"GIF89a"


def test_open_records_and_returns_a_gif(conn):
    m, _ = msg(conn)
    status, h, body = web.open_pixel(conn, tokens.sign({"m": m, "a": "open"}, "k"), "k", "Mozilla/5.0 (iPhone) Mail")
    assert status == 200 and h["Content-Type"] == "image/gif" and body.startswith(GIF)
    assert "no-store" in h["Cache-Control"]
    row = conn.execute("SELECT message_id, user_agent FROM opens").fetchone()
    assert row["message_id"] == m and row["user_agent"] == "Mozilla/5.0 (iPhone) Mail"


def test_open_with_a_bad_or_click_token_still_shows_the_image_but_records_nothing(conn):
    m, _ = msg(conn)
    for t in ("garbage", tokens.sign({"m": m, "u": "https://outlet.example.com/"}, "k"), tokens.sign({"m": 999999, "a": "open"}, "k")):
        status, h, body = web.open_pixel(conn, t, "k", "x")
        assert status == 200 and body.startswith(GIF)
    assert conn.execute("SELECT count(*) n FROM opens").fetchone()["n"] == 0


def test_open_user_agent_is_capped(conn):
    m, _ = msg(conn)
    web.open_pixel(conn, tokens.sign({"m": m, "a": "open"}, "k"), "k", "x" * 5000)
    assert len(conn.execute("SELECT user_agent FROM opens").fetchone()["user_agent"]) == 300


def test_click_appends_visitor_fragment(conn):
    m, p = msg(conn)
    status, h = web.click(conn, tokens.sign({"m": m, "u": "https://outlet.example.com/x?utm_source=mail"}, "k"), "k")
    url, frag = h["Location"].split("#", 1)
    assert status == 302 and url == "https://outlet.example.com/x?utm_source=mail"
    assert tokens.visitor_profile(frag.removeprefix("om="), "k") == p
    assert conn.execute("SELECT url FROM clicks").fetchone()["url"] == url    # the logged URL carries no token


def test_click_keeps_an_existing_fragment(conn):
    m, _ = msg(conn)
    status, h = web.click(conn, tokens.sign({"m": m, "u": "https://outlet.example.com/x#reviews"}, "k"), "k")
    assert h["Location"] == "https://outlet.example.com/x#reviews"
