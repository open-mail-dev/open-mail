import json

import main


class Req:
    def __init__(self, method, path, headers=None, form=None, json_body=None, data=b""):
        self.method, self.path, self.headers = method, path, headers or {}
        self.form, self._json, self._data = form or {}, json_body, data
        self.remote_addr = "203.0.113.5"

    def get_json(self, silent=True):
        return self._json

    def get_data(self):
        return self._data


class FakeConn:
    def commit(self):
        pass

    def rollback(self):
        pass


class FakeConnCM:
    def __enter__(self):
        return FakeConn()

    def __exit__(self, exc_type, exc, tb):
        return False


def test_cors_preflight_allows_store_origin():
    body, status, headers = main.ingest(Req("OPTIONS", "/signup", {"Origin": "https://outlet.example.com"}))
    assert status == 204 and headers["Access-Control-Allow-Origin"] == "https://outlet.example.com"


def test_cors_preflight_rejects_foreign_origin():
    _, status, headers = main.ingest(Req("OPTIONS", "/signup", {"Origin": "https://evil.example"}))
    assert status == 403 and "Access-Control-Allow-Origin" not in headers


def test_unknown_path_404():
    assert main.ingest(Req("POST", "/nope"))[1] == 404
    assert main.web_entry(Req("GET", "/nope"))[1] == 404


def test_client_ip_uses_last_forwarded_hop():
    """On a direct Cloud Functions gen2 / Cloud Run URL (no load balancer in
    front), Google appends the true client IP as the LAST X-Forwarded-For
    entry — that's the one to trust, not the first (see fix round 1, item 1)."""
    assert main.client_ip(Req("POST", "/signup", {"X-Forwarded-For": "198.51.100.7, 203.0.113.9"})) == "203.0.113.9"


def test_client_ip_ignores_a_spoofed_first_hop():
    """A client can set X-Forwarded-For itself to fake an IP for the rate
    limiter / consent log — that spoofed first entry must be ignored."""
    assert main.client_ip(Req("POST", "/signup", {"X-Forwarded-For": "1.2.3.4, 203.0.113.9"})) == "203.0.113.9"


def test_ses_non_json_body_returns_400():
    """A malformed /ses body must be rejected with 400, not crash (and must
    never reach db.connect — no fixture/monkeypatch of db is set up here)."""
    body, status, headers = main.ingest(Req("POST", "/ses", data=b"not-json-at-all"))
    assert status == 400


def test_web_entry_passes_client_ip_to_unsubscribe(monkeypatch):
    captured = {}

    def fake_connect(dsn):
        return FakeConnCM()

    def fake_unsubscribe(conn, token, secret, method, shopify_client_factory, ip=None):
        captured["ip"] = ip
        return 200, "<html></html>"

    monkeypatch.setattr(main.db, "connect", fake_connect)
    monkeypatch.setattr(main.web, "unsubscribe", fake_unsubscribe)

    req = Req("GET", "/u/sometoken", {"X-Forwarded-For": "1.2.3.4, 198.51.100.7"})
    body, status, headers = main.web_entry(req)

    assert captured["ip"] == "198.51.100.7"
    assert status == 200


def test_web_entry_unsubscribe_uses_remote_addr_without_forwarded_header(monkeypatch):
    captured = {}

    def fake_connect(dsn):
        return FakeConnCM()

    def fake_unsubscribe(conn, token, secret, method, shopify_client_factory, ip=None):
        captured["ip"] = ip
        return 200, "<html></html>"

    monkeypatch.setattr(main.db, "connect", fake_connect)
    monkeypatch.setattr(main.web, "unsubscribe", fake_unsubscribe)

    req = Req("POST", "/u/sometoken")
    main.web_entry(req)

    assert captured["ip"] == "203.0.113.5"


def test_signup_rejects_non_post_non_options_method_but_still_applies_cors():
    body, status, headers = main.ingest(Req("GET", "/signup", {"Origin": "https://outlet.example.com"}))
    assert status == 405
    assert headers["Access-Control-Allow-Origin"] == "https://outlet.example.com"


def test_signup_rejects_non_post_non_options_method_no_cors_for_foreign_origin():
    body, status, headers = main.ingest(Req("PUT", "/signup", {"Origin": "https://evil.example"}))
    assert status == 405
    assert "Access-Control-Allow-Origin" not in headers


def test_click_rejects_non_get_head_method():
    _, status, headers = main.web_entry(Req("POST", "/c/sometoken"))
    assert status == 405


def test_click_allows_head(monkeypatch):
    """HEAD is allowed alongside GET — it must pass the method gate and reach
    web.click (never a 405)."""
    monkeypatch.setattr(main.db, "connect", lambda dsn: FakeConnCM())
    monkeypatch.setattr(main.web, "click", lambda conn, token, secret: (404, {}))
    _, status, _ = main.web_entry(Req("HEAD", "/c/bogus-token-not-in-db"))
    assert status != 405


class FakeSesClient:
    pass


def test_tick_entry_strips_and_passes_aws_credentials(monkeypatch):
    captured = {}

    def fake_boto3_client(service, **kwargs):
        captured.update(kwargs)
        return FakeSesClient()

    def fake_connect(dsn):
        return FakeConnCM()

    def fake_run(conn, ses, secret, base_url, shop_factory=None):
        return {"sent": 0, "skipped": 0, "deferred": 0, "failed": 0}

    def fake_heartbeat(conn):
        return True

    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "AKIAEXAMPLE\n")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "supersecret\n")
    monkeypatch.setattr(main.boto3, "client", fake_boto3_client)
    monkeypatch.setattr(main.db, "connect", fake_connect)
    monkeypatch.setattr(main.tick, "run", fake_run)
    monkeypatch.setattr(main.tick, "heartbeat", fake_heartbeat)

    main.tick_entry(Req("POST", "/"))

    assert captured["aws_access_key_id"] == "AKIAEXAMPLE"
    assert captured["aws_secret_access_key"] == "supersecret"


def test_tick_entry_disables_botocore_retries(monkeypatch):
    """A boto retry after a read timeout SES actually accepted would
    double-send — tick.py's own backoff handles retries instead, so botocore
    must be configured with max_attempts=1 (no retries of its own)."""
    captured = {}

    def fake_boto3_client(service, **kwargs):
        captured.update(kwargs)
        return FakeSesClient()

    def fake_connect(dsn):
        return FakeConnCM()

    def fake_run(conn, ses, secret, base_url, shop_factory=None):
        return {"sent": 0, "skipped": 0, "deferred": 0, "failed": 0}

    def fake_heartbeat(conn):
        return True

    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "AKIAEXAMPLE")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "supersecret")
    monkeypatch.setattr(main.boto3, "client", fake_boto3_client)
    monkeypatch.setattr(main.db, "connect", fake_connect)
    monkeypatch.setattr(main.tick, "run", fake_run)
    monkeypatch.setattr(main.tick, "heartbeat", fake_heartbeat)

    main.tick_entry(Req("POST", "/"))

    assert captured["config"].retries == {"total_max_attempts": 1}


def test_tick_entry_does_not_alert_on_retryable_failed_count(monkeypatch):
    """tick.py already alerts on terminal (MAX_ATTEMPTS-exhausted) failures;
    a per-tick alert on any out['failed'] fires every 5 minutes for ordinary
    retryable failures and must be removed. The heartbeat alert stays."""
    alerts = []

    def fake_connect(dsn):
        return FakeConnCM()

    def fake_run(conn, ses, secret, base_url, shop_factory=None):
        return {"sent": 0, "skipped": 0, "deferred": 0, "failed": 3}

    def fake_heartbeat(conn):
        return True

    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "AKIAEXAMPLE")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "supersecret")
    monkeypatch.setattr(main.boto3, "client", lambda service, **kw: FakeSesClient())
    monkeypatch.setattr(main.db, "connect", fake_connect)
    monkeypatch.setattr(main.tick, "run", fake_run)
    monkeypatch.setattr(main.tick, "heartbeat", fake_heartbeat)
    monkeypatch.setattr(main.slack, "alert", lambda text: alerts.append(text))

    main.tick_entry(Req("POST", "/"))

    assert alerts == []


def test_tick_entry_still_alerts_on_bad_heartbeat(monkeypatch):
    alerts = []

    def fake_connect(dsn):
        return FakeConnCM()

    def fake_run(conn, ses, secret, base_url, shop_factory=None):
        return {"sent": 0, "skipped": 0, "deferred": 0, "failed": 0}

    def fake_heartbeat(conn):
        return False

    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "AKIAEXAMPLE")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "supersecret")
    monkeypatch.setattr(main.boto3, "client", lambda service, **kw: FakeSesClient())
    monkeypatch.setattr(main.db, "connect", fake_connect)
    monkeypatch.setattr(main.tick, "run", fake_run)
    monkeypatch.setattr(main.tick, "heartbeat", fake_heartbeat)
    monkeypatch.setattr(main.slack, "alert", lambda text: alerts.append(text))

    main.tick_entry(Req("POST", "/"))

    assert len(alerts) == 1
    assert "overdue" in alerts[0]


class _Conn:
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _SignupReq:
    path, method, remote_addr = "/signup", "POST", "203.0.113.9"
    headers = {"Origin": "https://outlet.example.com"}

    form = {}

    def get_json(self, silent=False):
        return {"store": "outlet.example.com"}


def test_first_signup_sends_email_1_immediately(monkeypatch):
    import json as _json
    import main
    calls = []
    monkeypatch.setattr(main.db, "connect", lambda dsn: _Conn())
    monkeypatch.setattr(main, "_ses_client", lambda: "ses")
    monkeypatch.setattr(main.tick, "run", lambda *a, **kw: calls.append("tick") or {})
    for resp, expect in (({"ok": True, "repeat": False, "code_valid": True, "enrolled": True}, ["tick"]),
                         ({"ok": True, "repeat": True, "code_valid": True, "enrolled": False}, [])):
        calls.clear()
        monkeypatch.setattr(main.signup, "handle", lambda *a, r=resp: (200, dict(r)))
        body, status, _ = main.ingest(_SignupReq())
        assert status == 200 and calls == expect and "enrolled" not in _json.loads(body)


def test_immediate_send_failure_still_returns_200(monkeypatch):
    import main
    monkeypatch.setattr(main.db, "connect", lambda dsn: _Conn())
    monkeypatch.setattr(main, "_ses_client", lambda: (_ for _ in ()).throw(RuntimeError("SES down")))
    monkeypatch.setattr(main.signup, "handle", lambda *a: (200, {"ok": True, "repeat": False, "code_valid": True, "enrolled": True}))
    _, status, _ = main.ingest(_SignupReq())
    assert status == 200


def test_cors_allows_every_registered_store_domain():
    for o in ("https://shop.example.com", "https://www.shop.example.com", "https://www.outlet.example.com"):
        assert main.ingest(Req("OPTIONS", "/signup", {"Origin": o}))[1] == 204, o
    # a storefront that is not on our system
    assert main.ingest(Req("OPTIONS", "/signup", {"Origin": "https://shop-fr.example.com"}))[1] == 403


def test_webhook_secret_is_the_sending_stores_own(monkeypatch):
    monkeypatch.setenv("SHOPIFY_CLIENT_SECRET_EXAMPLE_OUTLET", "k-secret\n")
    monkeypatch.setenv("SHOPIFY_CLIENT_SECRET_EXAMPLE_SHOP", "s-secret")
    assert main._webhook_secret({"X-Shopify-Shop-Domain": "example-outlet.myshopify.com"}) == "k-secret"
    assert main._webhook_secret({"X-Shopify-Shop-Domain": "example-shop.myshopify.com"}) == "s-secret"
    assert main._webhook_secret({"X-Shopify-Shop-Domain": "evil.myshopify.com"}) == ""


def test_checkout_poll_runs_once_per_shopify_store_and_survives_a_failure(monkeypatch):
    calls = []
    monkeypatch.setattr(main, "_shop_factory", lambda st: st.key)
    def poll(conn, client, shop, since, **kw):
        calls.append((client, shop))
        raise RuntimeError("Shopify down")
    monkeypatch.setattr(main.checkouts, "poll", poll)
    main.poll_checkouts(FakeConn(), "since")
    assert calls == [("shop.example.com", "example-shop.myshopify.com")]      # .ie/.fi share the store: polled once


def test_cors_allows_the_market_domains():
    for d in ("shop-ie.example.com", "www.shop-ie.example.com", "shop-fi.example.com"):
        assert f"https://{d}" in main._ORIGINS


def test_open_pixel_route(monkeypatch):
    seen = {}
    monkeypatch.setattr(main.db, "connect", lambda dsn: FakeConnCM())
    def fake(conn, token, secret, ua):
        seen["token"], seen["ua"] = token, ua
        return 200, {"Content-Type": "image/gif", "Cache-Control": "no-store"}, b"GIF89a"
    monkeypatch.setattr(main.web, "open_pixel", fake)
    body, status, headers = main.web_entry(Req("GET", "/o/tok123.gif", {"User-Agent": "Mozilla/5.0"}))
    assert status == 200 and body == b"GIF89a" and headers["Content-Type"] == "image/gif"
    assert seen == {"token": "tok123", "ua": "Mozilla/5.0"}


def test_open_pixel_rejects_post():
    assert main.web_entry(Req("POST", "/o/tok.gif"))[1] == 405


def test_track_rejects_foreign_origin():
    _, status, headers = main.ingest(Req("OPTIONS", "/track", {"Origin": "https://evil.example"}))
    assert status == 403 and "Access-Control-Allow-Origin" not in headers


def test_track_preflight_allows_a_market_domain():
    _, status, headers = main.ingest(Req("OPTIONS", "/track", {"Origin": "https://shop-fi.example.com"}))
    assert status == 204 and headers["Access-Control-Allow-Origin"] == "https://shop-fi.example.com"


def test_track_passes_body_to_sitetrack(monkeypatch):
    seen = {}
    monkeypatch.setattr(main.db, "connect", lambda dsn: FakeConnCM())
    monkeypatch.setattr(main.sitetrack, "track", lambda conn, b, secret: seen.setdefault("b", b) and (200, {"ok": True}))
    body, status, headers = main.ingest(Req("POST", "/track", {"Origin": "https://shop.example.com", "Content-Type": "application/json"},
                                            data=b'{"store": "shop.example.com"}'))
    assert status == 200 and seen["b"] == {"store": "shop.example.com"} and headers["Access-Control-Allow-Origin"] == "https://shop.example.com"


def test_track_reads_a_text_plain_body(monkeypatch):
    """text/plain keeps the browser's POST a simple CORS request: no preflight, so keepalive works on page hide."""
    seen = {}
    monkeypatch.setattr(main.db, "connect", lambda dsn: FakeConnCM())
    monkeypatch.setattr(main.sitetrack, "track", lambda conn, b, secret: seen.setdefault("b", b) and (200, {"ok": True}))
    _, status, _ = main.ingest(Req("POST", "/track", {"Origin": "https://shop.example.com", "Content-Type": "text/plain;charset=UTF-8"},
                                   data=b'{"store": "shop.example.com", "type": "viewed_product"}'))
    assert status == 200 and seen["b"] == {"store": "shop.example.com", "type": "viewed_product"}
    _, status, _ = main.ingest(Req("POST", "/track", {"Origin": "https://shop.example.com"}, data=b"not json"))
    assert status == 400


def test_ident_passes_a_text_plain_body_and_both_secrets(monkeypatch):
    seen = {}
    monkeypatch.setattr(main.db, "connect", lambda dsn: FakeConnCM())
    monkeypatch.setattr(main.config, "IDENT_SECRET", "ik")
    monkeypatch.setattr(main.sitetrack, "ident", lambda conn, b, ik, tk: seen.update(b=b, ik=ik) or (200, {"vid": "t"}))
    body, status, headers = main.ingest(Req("POST", "/ident", {"Origin": "https://shop.example.com", "Content-Type": "text/plain;charset=UTF-8"},
                                            data=b'{"store": "shop.example.com"}'))
    assert status == 200 and seen == {"b": {"store": "shop.example.com"}, "ik": "ik"} and json.loads(body) == {"vid": "t"}
    assert headers["Access-Control-Allow-Origin"] == "https://shop.example.com"
    _, status, _ = main.ingest(Req("POST", "/ident", {"Origin": "https://shop.example.com"}, data=b"not json"))
    assert status == 400
    _, status, headers = main.ingest(Req("POST", "/ident", {"Origin": "https://evil.example"}, data=b"{}"))
    assert status == 403 and "Access-Control-Allow-Origin" not in headers


def test_notify_cors_only_for_back_in_stock_stores():
    _, status, headers = main.ingest(Req("OPTIONS", "/notify", {"Origin": "https://shop.example.com"}))
    assert status == 204 and headers["Access-Control-Allow-Origin"] == "https://shop.example.com"
    _, status, headers = main.ingest(Req("OPTIONS", "/notify", {"Origin": "https://outlet.example.com"}))
    assert status == 403 and "Access-Control-Allow-Origin" not in headers


def test_poll_stock_runs_once_per_shopify_store_for_live_markets(monkeypatch):
    import stock
    calls = []
    monkeypatch.setattr(main, "_shop_factory", lambda st: f"client:{st.key}")
    monkeypatch.setattr(stock, "poll", lambda conn, client, keys: calls.append((client, sorted(keys))) or {"enrolled": 0})
    monkeypatch.setenv("MAIL_BIS_LIVE", "")
    main.poll_stock(object())
    assert calls == []
    monkeypatch.setenv("MAIL_BIS_LIVE", "shop.example.com,outlet.example.com")
    main.poll_stock(object())
    assert calls == [("client:shop.example.com", ["shop.example.com"])]          # example-outlet has no back_in_stock
