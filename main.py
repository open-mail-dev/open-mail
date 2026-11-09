import json
import logging
import os

import boto3
import botocore.config

import checkouts
import config
import db
import notify
import orders
import ses_events
import shopify
import signup
import sitetrack
import slack
import stock
import stores
import tick
import web
import welcome_lookup
import winback

# only storefronts whose signup popup runs on this system may post signups (Store.welcome)
_ORIGINS = {f"https://{p}{s.domain}" for s in stores.all_stores() if s.welcome for p in ("", "www.")}
_TRACK_ORIGINS = {f"https://{p}{s.domain}" for s in stores.all_stores() if s.site_flows for p in ("", "www.")}
_NOTIFY_ORIGINS = {f"https://{p}{s.domain}" for s in stores.all_stores() if s.back_in_stock for p in ("", "www.")}


def client_ip(req) -> str:
    # The FIRST hop of X-Forwarded-For is whatever the caller sent — spoofable
    # (a malicious client can set it to fake a different IP for the rate
    # limiter / consent log). On a direct Cloud Functions gen2 / Cloud Run URL
    # (no load balancer in front, which is what we deploy to), Google appends
    # the true client IP as the LAST entry, so that's the one to trust. If a
    # load balancer or CDN is ever put in front of these functions, this needs
    # to change to the hop just before the entries it appends.
    fwd = req.headers.get("X-Forwarded-For", "")
    return fwd.rsplit(",", 1)[-1].strip() if fwd else req.remote_addr


def _shop_factory(store):
    token = shopify.access_token(store, os.environ[store.client_id_env].strip(), os.environ[store.client_secret_env].strip())
    return shopify.Client(store, token)


def _webhook_secret(headers) -> str:
    """Shopify signs each store's webhooks with that store's app client secret; unknown shop → "" → 401."""
    try:
        s = stores.by_shop(headers.get("X-Shopify-Shop-Domain", ""))
    except KeyError:
        return ""
    return os.environ.get(s.client_secret_env, "").strip()


def _cors(req, origins=_ORIGINS):
    o = req.headers.get("Origin", "")
    return {"Access-Control-Allow-Origin": o, "Access-Control-Allow-Methods": "POST",
            "Access-Control-Allow-Headers": "Content-Type", "Vary": "Origin"} if o in origins else {}


def ingest(req):
    if req.path == "/signup":
        h = _cors(req)
        if req.method == "OPTIONS":
            return "", (204 if h else 403), h
        if req.method != "POST":
            return "", 405, h
        form = req.get_json(silent=True) or dict(req.form)
        with db.connect(config.DB_DSN) as conn:
            status, body = signup.handle(conn, form, client_ip(req), _shop_factory, config.TOKEN_SECRET)
            if status == 200 and body.pop("enrolled", False):
                # 79% of signup-buyers order within a day, many on the spot: don't make them wait up to
                # 5 minutes for the next scheduled tick. Same send path (guards, cap, idempotency) as the
                # tick; any failure just leaves the step for the scheduler.
                try:
                    tick.run(conn, _ses_client(), config.TOKEN_SECRET, config.WEB_BASE_URL, shop_factory=_shop_factory)
                except Exception:
                    logging.exception("immediate welcome send failed — the scheduled tick will retry")
        return json.dumps(body), status, {**h, "Content-Type": "application/json"}
    if req.path == "/notify":
        h = _cors(req, _NOTIFY_ORIGINS)
        if req.method == "OPTIONS":
            return "", (204 if h else 403), h
        if req.method != "POST":
            return "", 405, h
        try:
            payload = req.get_json(silent=True) or json.loads(req.get_data() or b"null")
        except ValueError:
            return json.dumps({}), 400, {**h, "Content-Type": "application/json"}
        with db.connect(config.DB_DSN) as conn:
            status, body = notify.handle(conn, payload, client_ip(req), _shop_factory, config.TOKEN_SECRET)
        return json.dumps(body), status, {**h, "Content-Type": "application/json"}
    if req.path in ("/track", "/ident"):
        h = _cors(req, _TRACK_ORIGINS)
        if req.method == "OPTIONS" or not h:
            return "", (204 if h and req.method == "OPTIONS" else 403), h
        if req.method != "POST":
            return "", 405, h
        # the snippet posts text/plain (a simple CORS request: no preflight, so keepalive works on page hide)
        try:
            payload = json.loads(req.get_data() or b"null")
        except ValueError:
            return json.dumps({}), 400, {**h, "Content-Type": "application/json"}
        with db.connect(config.DB_DSN) as conn:
            if req.path == "/track":
                status, body = sitetrack.track(conn, payload, config.TOKEN_SECRET)
            else:
                status, body = sitetrack.ident(conn, payload, config.IDENT_SECRET, config.TOKEN_SECRET)
        return json.dumps(body), status, {**h, "Content-Type": "application/json"}
    if req.path == "/shopify" and req.method == "POST":
        with db.connect(config.DB_DSN) as conn:
            return "", orders.handle(conn, req.get_data(), dict(req.headers), _webhook_secret(req.headers)), {}
    if req.path == "/ses" and req.method == "POST":
        try:
            message = json.loads(req.get_data())
        except (TypeError, ValueError):
            return "", 400, {}
        with db.connect(config.DB_DSN) as conn:
            return "", ses_events.handle(conn, message), {}
    return "", 404, {}


def web_entry(req):
    parts = req.path.strip("/").split("/")
    if len(parts) == 2 and parts[0] == "c":
        if req.method not in ("GET", "HEAD"):
            return "", 405, {}
        with db.connect(config.DB_DSN) as conn:
            status, h = web.click(conn, parts[1], config.TOKEN_SECRET)
        return "", status, h
    if len(parts) == 2 and parts[0] == "o":
        if req.method not in ("GET", "HEAD"):
            return "", 405, {}
        token = parts[1][:-4] if parts[1].endswith(".gif") else parts[1]
        with db.connect(config.DB_DSN) as conn:
            status, h, body = web.open_pixel(conn, token, config.TOKEN_SECRET, req.headers.get("User-Agent"))
        return body, status, h
    if len(parts) == 2 and parts[0] == "u":
        with db.connect(config.DB_DSN) as conn:
            status, page = web.unsubscribe(conn, parts[1], config.TOKEN_SECRET, req.method, _shop_factory, ip=client_ip(req))
        return page, status, {"Content-Type": "text/html; charset=utf-8"}
    return "", 404, {}


def _ses_client():
    return boto3.client(
        "sesv2",
        region_name=config.SES_REGION,
        # Explicit, stripped credentials: a trailing "\n" on a Secret Manager
        # value has broken auth in this project before, and boto3's default
        # credential chain would happily pick up an unstripped env var.
        aws_access_key_id=os.environ["AWS_ACCESS_KEY_ID"].strip(),
        aws_secret_access_key=os.environ["AWS_SECRET_ACCESS_KEY"].strip(),
        # total_max_attempts=1: exactly one request, no botocore retry (botocore's
        # max_attempts counts RETRIES, so max_attempts=1 would still retry once).
        # tick.py's own backoff handles retries between ticks; a boto retry after
        # a read timeout that SES actually accepted would double-send.
        config=botocore.config.Config(connect_timeout=5, read_timeout=20, retries={"total_max_attempts": 1}),
    )


def poll_checkouts(conn, since):
    """Enrol new abandoned checkouts, once per Shopify store (several storefronts can share one)."""
    for shop in sorted({s.shop for s in stores.all_stores() if s.abandoned_checkout}):
        st = next(s for s in stores.all_stores() if s.shop == shop and s.abandoned_checkout)
        try:
            out = checkouts.poll(conn, _shop_factory(st), shop, since, since_by_store=config.AC_SINCE_BY_STORE)
            if any(out[k] for k in ("enrolled", "holdout", "exited")):
                logging.info("checkouts %s: %s", shop, out)
        except Exception:
            conn.rollback()
            logging.exception("abandoned-checkout poll failed for %s — next tick retries", shop)


def poll_winback(conn):
    """Enrol subscribers whose last order turned 120 days old, once per Shopify store with a winback market switched on."""
    live = [s for s in stores.all_stores() if s.winback and s.key in config.wb_live()]
    for shop in sorted({s.shop for s in live}):
        st = next(s for s in live if s.shop == shop)
        try:
            out = winback.poll(conn, _shop_factory(st), shop)
            if out["enrolled"] or out["holdout"]:
                logging.info("winback %s: %s", shop, out)
        except Exception:
            conn.rollback()
            logging.exception("winback poll failed for %s — next tick retries", shop)


def poll_stock(conn):
    """Back-in-stock: enrol waiting requests whose variant is buyable again, once per Shopify store with a live market."""
    live = [s for s in stores.all_stores() if s.back_in_stock and s.key in config.bis_live()]
    for shop in sorted({s.shop for s in live}):
        keys = [s.key for s in live if s.shop == shop]
        try:
            stock.poll(conn, _shop_factory(next(s for s in live if s.shop == shop)), keys)
        except Exception:
            conn.rollback()
            logging.exception("back-in-stock poll failed for %s — next tick retries", shop)


def lookup_entry(req):
    """mail-lookup: deployed --no-allow-unauthenticated, so only callers holding roles/run.invoker reach this (e.g.
    the service account your customer-service bot runs as). POST /welcome-code {"store","email"}."""
    if req.path.strip("/") != "welcome-code":
        return "", 404, {}
    if req.method != "POST":
        return "", 405, {}
    body = req.get_json(silent=True)
    if not isinstance(body, dict) or not isinstance(body.get("store"), str) or not isinstance(body.get("email"), str):
        return "", 400, {}
    with db.connect(config.DB_DSN) as conn:
        out = welcome_lookup.find(conn, body["store"], body["email"])
    return json.dumps(out), 200, {"Content-Type": "application/json"}


def tick_entry(req):
    ses = _ses_client()
    with db.connect(config.DB_DSN) as conn:
        if config.AC_SINCE:
            poll_checkouts(conn, config.AC_SINCE)
        poll_winback(conn)
        poll_stock(conn)
        out = tick.run(conn, ses, config.TOKEN_SECRET, config.WEB_BASE_URL, shop_factory=_shop_factory)
        # No per-tick alert on out["failed"]: it fires every 5 minutes for
        # ordinary retryable failures. tick.py already alerts on terminal
        # (MAX_ATTEMPTS-exhausted) failures via slack.alert.
        if not tick.heartbeat(conn):
            slack.alert("steps overdue >1h and nothing sent in 24h — tick or SES is broken")
    return json.dumps(out), 200, {"Content-Type": "application/json"}
