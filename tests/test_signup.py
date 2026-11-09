import threading
import time

import pytest

import db
import engine
import migrate
import signup


class FakeShopify:
    def __init__(self):
        self.codes, self.consents = [], []
        self._lock = threading.Lock()
        self.raise_on_create_discount_code = False
        self.raise_on_set_marketing_consent = False

    def create_discount_code(self, code, amount, min_subtotal, title, ends_at):
        if self.raise_on_create_discount_code:
            raise Exception("Shopify API error: create_discount_code failed")
        with self._lock:
            self.codes.append((code, amount, min_subtotal))
            self.ends_at = ends_at

    def set_marketing_consent(self, email, subscribed):
        if self.raise_on_set_marketing_consent:
            raise Exception("Shopify API error: set_marketing_consent failed")
        with self._lock:
            self.consents.append((email, subscribed))


@pytest.fixture
def fake(monkeypatch):
    f = FakeShopify()
    enrolled = []
    monkeypatch.setattr(engine, "enrol", lambda conn, flow, store, pid, **kw: enrolled.append((flow, store, pid)) or 1)
    f.enrolled = enrolled
    return f


def form(**over):
    d = {"store": "outlet.example.com", "email": "Jan@Example.org", "source": "popup", "website": ""}
    d.update(over)
    return d


def test_signup_creates_consent_code_and_enrolment(conn, fake):
    status, body = signup.handle(conn, form(), "203.0.113.5", lambda s: fake)
    assert status == 200 and "code" not in body   # the code only travels by email
    assert fake.codes[0][1:] == (10, 100)
    assert fake.consents == [("jan@example.org", True)]
    assert fake.enrolled[0][:2] == ("welcome", "outlet.example.com")


def test_signup_twice_is_idempotent(conn, fake):
    _, a = signup.handle(conn, form(), "203.0.113.5", lambda s: fake)
    _, b = signup.handle(conn, form(email="jan@example.org"), "203.0.113.6", lambda s: fake)
    assert "code" not in a and "code" not in b and (a["enrolled"], b["enrolled"], b["code_valid"]) == (True, False, True)
    assert (a["repeat"], b["repeat"]) == (False, True)   # the popup only promises a mail on the first
    assert len(fake.codes) == 1
    assert conn.execute("SELECT count(*) n FROM profiles").fetchone()["n"] == 1


def test_honeypot_returns_200_but_does_nothing(conn, fake):
    status, _ = signup.handle(conn, form(website="http://spam"), "203.0.113.5", lambda s: fake)
    assert status == 200 and fake.codes == [] and fake.enrolled == []


@pytest.mark.parametrize("bad", [{"store": "evil.example"}, {"email": "nope"}, {"source": "admin"}])
def test_bad_input_400(conn, fake, bad):
    status, _ = signup.handle(conn, form(**bad), "203.0.113.5", lambda s: fake)
    assert status == 400


def test_rate_limit_per_ip(conn, fake):
    for i in range(10):
        signup.handle(conn, form(email=f"a{i}@example.org"), "203.0.113.9", lambda s: fake)
    status, _ = signup.handle(conn, form(email="z@example.org"), "203.0.113.9", lambda s: fake)
    assert status == 429


def test_suppressed_address_can_resubscribe_via_form(conn, fake):
    import profiles
    profiles.suppress(conn, "jan@example.org", "outlet.example.com", "unsubscribed")
    signup.handle(conn, form(), "203.0.113.5", lambda s: fake)
    pid = profiles.upsert(conn, "jan@example.org")
    assert profiles.is_sendable(conn, pid, "outlet.example.com")


def test_concurrent_signup_same_email_creates_one_code(postgresql, monkeypatch):
    """Verify that concurrent signups for the same new email create only one coupon.

    The profiles.upsert INSERT … ON CONFLICT DO UPDATE holds a row lock on the
    profile until commit, which serializes concurrent signups on the same email.
    """
    # Set up two connections from the same DB
    i = postgresql.info
    dsn = f"host={i.host} port={i.port} user={i.user} dbname={i.dbname}"
    conn1 = db.connect(dsn)
    conn2 = db.connect(dsn)
    migrate.apply(conn1)

    # Shared FakeShopify for both threads (thread-safe via _lock)
    fake = FakeShopify()
    enrolled = []
    enrolled_lock = threading.Lock()

    def enrolled_append(item):
        with enrolled_lock:
            enrolled.append(item)

    monkeypatch.setattr(engine, "enrol", lambda conn, flow, store, pid, **kw: enrolled_append((flow, store, pid)) or 1)

    # Barrier to synchronize both threads
    barrier = threading.Barrier(2)
    results = {}
    errors = {}

    def thread_func(conn_id):
        try:
            barrier.wait()
            conn = conn1 if conn_id == 1 else conn2
            status, body = signup.handle(
                conn,
                form(email="concurrent@example.org"),
                "203.0.113.7",
                lambda s: fake
            )
            results[conn_id] = (status, body)
        except Exception as e:
            errors[conn_id] = e

    t1 = threading.Thread(target=thread_func, args=(1,))
    t2 = threading.Thread(target=thread_func, args=(2,))
    t1.start()
    t2.start()
    t1.join()
    t2.join()

    # Check for exceptions
    assert not errors, f"Thread exceptions: {errors}"

    try:
        # Both should return 200 with code
        assert results[1][0] == 200 and results[2][0] == 200
        assert all("code" not in r[1] for r in results.values())

        # Both should return the same code (idempotency)
        assert sorted(r[1]["enrolled"] for r in results.values()) == [False, True]

        # Only one coupon should be created
        assert len(fake.codes) == 1

        # Only one profile should exist
        assert conn1.execute("SELECT count(*) n FROM profiles WHERE email='concurrent@example.org'").fetchone()["n"] == 1

        # Only one coupon row should exist
        assert conn1.execute("SELECT count(*) n FROM coupons WHERE profile_id IN (SELECT id FROM profiles WHERE email='concurrent@example.org')").fetchone()["n"] == 1
    finally:
        conn1.close()
        conn2.close()


def test_create_discount_code_error_returns_502_and_rolls_back(conn, fake):
    """Verify that if create_discount_code raises, signup returns 502 and rolls back."""
    fake.raise_on_create_discount_code = True
    status, body = signup.handle(conn, form(), "203.0.113.10", lambda s: fake)
    assert status == 502 and body["error"] == "temporarily unavailable"
    # No profiles or consent rows should be persisted
    assert conn.execute("SELECT count(*) n FROM profiles").fetchone()["n"] == 0
    assert conn.execute("SELECT count(*) n FROM consent").fetchone()["n"] == 0


def test_set_marketing_consent_error_returns_200_with_code(conn, fake):
    """Verify that if set_marketing_consent raises (after commit), signup still returns 200 with code."""
    fake.raise_on_set_marketing_consent = True
    status, body = signup.handle(conn, form(), "203.0.113.11", lambda s: fake)
    assert status == 200 and "code" not in body   # the code only travels by email
    # Coupon should be persisted despite the error
    assert conn.execute("SELECT count(*) n FROM coupons").fetchone()["n"] == 1
    # Consent should be persisted
    assert conn.execute("SELECT count(*) n FROM consent").fetchone()["n"] == 1


def test_set_marketing_consent_error_alerts_slack_with_redacted_email(conn, fake, monkeypatch):
    """Like web.py's unsubscribe write-back failure, a failed post-commit
    Shopify consent write-back must alert Slack — with the email redacted to
    its domain, never the full address."""
    import signup as signup_mod

    alerts = []
    monkeypatch.setattr(signup_mod.slack, "alert", lambda text: alerts.append(text))
    fake.raise_on_set_marketing_consent = True
    status, body = signup.handle(conn, form(), "203.0.113.12", lambda s: fake)
    assert status == 200 and "code" not in body   # the code only travels by email
    assert len(alerts) == 1
    assert "example.org" in alerts[0]
    assert "jan@example.org" not in alerts[0]


def test_code_expires_end_of_day_14_days_after_signup_and_matches_mail(conn, fake):
    import coupons
    signup.handle(conn, form(), "203.0.113.5", lambda s: fake)
    created = conn.execute("SELECT created_at FROM coupons").fetchone()["created_at"]
    assert fake.ends_at == coupons.expiry(created, 14)
    ams = fake.ends_at.astimezone(coupons.AMS)
    assert (ams.hour, ams.minute) == (23, 59)
    assert (ams.date() - created.astimezone(coupons.AMS).date()).days == 14


def test_viewed_products_stored_and_junk_dropped(conn, fake):
    viewed = ["chair-a", "<script>", 5, "Chair-B", "chair-c", "d", "e", "f", "g"]
    status, _ = signup.handle(conn, form(viewed=viewed), "203.0.113.5", lambda s: fake)
    assert status == 200
    payload = conn.execute("SELECT payload FROM events WHERE type='signup'").fetchone()["payload"]
    assert payload == {"source": "popup", "viewed": ["chair-a", "chair-c", "d", "e", "f", "g"]}


def test_viewed_not_a_list_is_ignored(conn, fake):
    status, _ = signup.handle(conn, form(viewed="chair-a"), "203.0.113.5", lambda s: fake)
    assert status == 200
    assert conn.execute("SELECT payload FROM events WHERE type='signup'").fetchone()["payload"]["viewed"] == []


def test_repeat_signup_after_expiry_gets_no_dead_code(conn, fake):
    signup.handle(conn, form(), "203.0.113.5", lambda s: fake)
    conn.execute("UPDATE coupons SET created_at = created_at - interval '30 days'")
    conn.commit()
    status, body = signup.handle(conn, form(), "203.0.113.6", lambda s: fake)
    assert status == 200 and body["repeat"] is True and body["code_valid"] is False
    assert len(fake.codes) == 1   # the welcome offer is once per address


def test_undeliverable_domain_rejected_before_anything(conn, fake, monkeypatch):
    monkeypatch.setattr(signup, "domain_accepts_mail", lambda d: d != "gmial.com")
    status, body = signup.handle(conn, form(email="jan@gmial.com"), "203.0.113.5", lambda s: fake)
    assert (status, body) == (400, {"error": "undeliverable"})
    assert fake.codes == [] and fake.enrolled == []
    assert conn.execute("SELECT count(*) n FROM profiles").fetchone()["n"] == 0


class _Res:
    def __init__(self, table):
        self.table, self.lifetime = table, None

    def resolve(self, domain, rtype):
        out = self.table.get((domain, rtype), "noanswer")
        if out == "nx":
            raise signup.dns.resolver.NXDOMAIN()
        if out == "noanswer":
            raise signup.dns.resolver.NoAnswer()
        if out == "timeout":
            raise signup.dns.exception.Timeout()
        return out


@pytest.mark.parametrize("table,ok", [
    ({("x.nl", "MX"): ["mx"]}, True),                              # normal
    ({("x.nl", "MX"): "nx"}, False),                               # domain doesn't exist
    ({("x.nl", "A"): ["1.2.3.4"]}, True),                          # no MX but an A record (RFC 5321 fallback)
    ({}, False),                                                   # no MX, no A, no AAAA
    ({("x.nl", "MX"): "timeout"}, True),                           # resolver trouble → fail open
])
def test_domain_accepts_mail(monkeypatch, table, ok):
    signup._DOMAIN_OK.clear()
    monkeypatch.setattr(signup.dns.resolver, "Resolver", lambda: _Res(table))
    assert signup.domain_accepts_mail("x.nl") is ok


def test_storefront_without_our_welcome_cannot_sign_up(conn, monkeypatch):
    import dataclasses
    import stores
    monkeypatch.setitem(stores._STORES, "shop-ie.example.com", dataclasses.replace(stores.get("shop-ie.example.com"), welcome=False))
    status, body = signup.handle(conn, {"store": "shop-ie.example.com", "email": "a@x.se", "source": "popup", "website": ""}, "1.2.3.4", lambda s: None)
    assert status == 400 and body == {"error": "invalid"}


def test_a_second_storefront_of_the_same_shop_gives_no_second_code(conn, fake):
    """shop.example.com, shop-ie.example.com and shop-fi.example.com are one Shopify store: a welcome code works on all of them, so one per address."""
    _, a = signup.handle(conn, form(store="shop.example.com", email="a@x.nl"), "203.0.113.5", lambda s: fake)
    _, b = signup.handle(conn, form(store="shop-ie.example.com", email="a@x.nl"), "203.0.113.5", lambda s: fake)
    assert a["repeat"] is False
    assert b == {"ok": True, "repeat": True, "code_valid": True, "enrolled": False}
    assert len(fake.codes) == 1 and [e[1] for e in fake.enrolled] == ["shop.example.com"]


def test_market_codes_use_the_default_prefix(conn, fake):
    signup.handle(conn, form(store="shop-fi.example.com", email="b@x.fi"), "203.0.113.5", lambda s: fake)
    assert fake.codes[0][0].startswith("WELCOME-") and fake.codes[0][1:] == (10, 100)


def test_code_prefix_is_per_storefront(conn, fake, monkeypatch):
    import dataclasses
    import stores
    monkeypatch.setitem(stores._STORES, "outlet.example.com", dataclasses.replace(stores.get("outlet.example.com"), code_prefix="HELLO"))
    signup.handle(conn, form(store="outlet.example.com", email="c@x.nl"), "203.0.113.5", lambda s: fake)
    assert fake.codes[0][0].startswith("HELLO-")


def test_signup_returns_a_visitor_token_for_the_profile(conn, fake):
    import tokens
    _, a = signup.handle(conn, form(), "203.0.113.5", lambda s: fake, token_secret="k")
    _, b = signup.handle(conn, form(), "203.0.113.6", lambda s: fake, token_secret="k")
    pid = conn.execute("SELECT id FROM profiles").fetchone()["id"]
    assert tokens.visitor_profile(a["vid"], "k") == pid and tokens.visitor_profile(b["vid"], "k") == pid
    _, h = signup.handle(conn, form(website="x"), "203.0.113.7", lambda s: fake, token_secret="k")
    assert "vid" not in h
