import logging
from datetime import datetime, timedelta, timezone

import catalog
import config
import coupons
import engine
import profiles
import render
import sender
import slack
import stock
import stores
from flows import FLOWS

MAX_ATTEMPTS = 8


SITE_FLOWS = {"browse_abandonment": "viewed_product", "cart_reminder": "added_to_cart"}


def _ctx(conn, row, store) -> dict:
    if row["flow"] in SITE_FLOWS:
        # the products behind this enrolment's trigger type since it started: newest first, each once, max 4
        # (the card grid shows an even number: 3 would render as 2)
        ids = []
        for r in conn.execute("""SELECT payload->>'product_id' AS id FROM events WHERE store=%s AND profile_id=%s AND type=%s
                                 AND ts >= %s ORDER BY ts DESC, id DESC""",
                              (row["store"], row["profile_id"], SITE_FLOWS[row["flow"]], row["started_at"])):
            if r["id"] and r["id"] not in ids:
                ids.append(r["id"])
        return {"viewed": ids[:4]}
    if row["flow"] == "post_purchase":
        # the order that started it (ref "order:<id>"): its number for mail 1, its products for mail 2's FBT cards
        ev = conn.execute("SELECT payload FROM events WHERE store=%s AND type='order_placed' AND external_id=%s",
                          (row["store"], (row.get("ref") or "").removeprefix("order:"))).fetchone()
        return {"order_name": ev["payload"].get("name") or "", "viewed": ev["payload"].get("product_ids") or []} if ev else {}
    if row["flow"] == "winback":
        # everything they bought (winback.poll stored it under the order that turned 120 days old): the FBT cards' anchors
        ev = conn.execute("SELECT payload FROM events WHERE store=%s AND type='winback' AND external_id=%s",
                          (row["store"], (row.get("ref") or "").removeprefix("order:"))).fetchone()
        return {"viewed": ev["payload"].get("product_ids") or []} if ev else {}
    if row["flow"] == "back_in_stock":
        # the variant stock.poll saw come back (event under the enrolment's ref)
        ev = conn.execute("SELECT payload FROM events WHERE store=%s AND type='back_in_stock' AND external_id=%s",
                          (row["store"], row.get("ref"))).fetchone()
        return dict(ev["payload"]) if ev else {}
    if row["flow"] == "abandoned_checkout":
        ev = conn.execute("SELECT payload FROM events WHERE store=%s AND type='checkout_started' AND external_id=%s",
                          (row["store"], row.get("ref"))).fetchone()
        return {"cart": ev["payload"]} if ev else {}
    c = conn.execute(
        "SELECT code, created_at FROM coupons WHERE profile_id=%s AND store=%s ORDER BY created_at LIMIT 1",
        (row["profile_id"], row["store"]),
    ).fetchone()
    if not c:
        return {}
    ev = conn.execute(
        "SELECT payload FROM events WHERE profile_id=%s AND store=%s AND type='signup' ORDER BY id DESC LIMIT 1",
        (row["profile_id"], row["store"]),
    ).fetchone()
    viewed = ((ev or {}).get("payload") or {}).get("viewed") or []
    return {"code": c["code"], "expires_at": coupons.expiry(c["created_at"], store.welcome_valid_days), "viewed": viewed}


def _catalog(shop_factory, store, row, ctx) -> dict | None:
    if shop_factory is None:
        return None
    want_products, categories, n = render.wants(row["flow"], row["step"], store)
    try:
        return catalog.fetch(shop_factory(store), store, ctx.get("viewed", []), categories, want_products, n=n)
    except Exception:
        # live blocks (products, categories, review score) are a nice-to-have: send without them rather than hold the mail up
        logging.exception("catalog fetch failed for %s — sending without live blocks", row)
        return None


def _backoff(attempts: int) -> timedelta:
    return timedelta(minutes=min(5 * 2 ** (attempts - 1), 240))


def run(conn, ses_client, secret: str, base_url: str, now=None, shop_factory=None) -> dict:
    now = now or datetime.now(timezone.utc)
    out = {"sent": 0, "skipped": 0, "deferred": 0, "failed": 0}
    rows = engine.claim_due(conn, now=now)
    conn.commit()                                     # persist attempt bumps; SKIP LOCKED protects concurrent ticks per row below
    for row in rows:
        locked = None
        sent_ok = False
        ses_id = None
        try:
            locked = conn.execute(
                "SELECT attempts FROM flow_steps WHERE enrolment_id=%s AND step=%s AND status='pending' FOR UPDATE SKIP LOCKED",
                (row["enrolment_id"], row["step"]),
            ).fetchone()
            if not locked:
                # Another tick holds this row right now (still processing it) or
                # has already resolved it — either way, this tick's own
                # claim_due bumped `attempts` for a row it will never process,
                # so give the attempt back. The `status='pending'` guard makes
                # this a no-op once the row has reached a terminal state.
                conn.execute(
                    "UPDATE flow_steps SET attempts = attempts - 1 WHERE enrolment_id=%s AND step=%s AND status='pending'",
                    (row["enrolment_id"], row["step"]),
                )
                conn.commit()
                continue
            verdict = engine.decide(conn, row, now=now)
            if verdict == "defer":
                out["deferred"] += 1; conn.commit(); continue
            if verdict.startswith("skip"):
                engine.mark(conn, row, "skipped", error=verdict)
                if row["flow"] == "back_in_stock":
                    (stock.on_blocked if verdict == "skip:unsendable" else stock.on_skipped)(conn, row["ref"])
                out["skipped"] += 1; conn.commit(); continue
            store = stores.get(row["store"])
            step = next(s for s in FLOWS[row["flow"]].steps if s.n == row["step"])
            ctx = _ctx(conn, row, store)
            if row["flow"] == "abandoned_checkout" and "cart" not in ctx:
                engine.mark(conn, row, "skipped", error="skip:no-cart"); out["skipped"] += 1; conn.commit(); continue
            if ctx.get("expires_at") and ctx["expires_at"] <= now:
                # every welcome step is about the code: once it has lapsed, the rest of the series is moot
                engine.mark(conn, row, "skipped", error="skip:coupon-expired"); out["skipped"] += 1; conn.commit(); continue
            if row["flow"] in ("post_purchase", "winback") and "viewed" not in ctx:
                engine.mark(conn, row, "skipped", error="skip:no-order"); out["skipped"] += 1; conn.commit(); continue
            if row["flow"] == "back_in_stock":
                # the variant must still be buyable now (same check as stock.poll); it is the whole mail
                if not stock.is_open(conn, row["ref"]):          # cancelled (they ordered it) while this step waited
                    engine.mark(conn, row, "skipped", error="skip:request-closed"); out["skipped"] += 1; conn.commit(); continue
                if store.key not in config.bis_live() or "variant_id" not in ctx:
                    engine.mark(conn, row, "skipped", error="skip:market-off" if "variant_id" in ctx else "skip:no-request")
                    stock.on_skipped(conn, row["ref"]); out["skipped"] += 1; conn.commit(); continue
                card = catalog.variants(shop_factory(store), store, [ctx["variant_id"]])["cards"][ctx["variant_id"]]
                if not card:
                    engine.mark(conn, row, "skipped", error="skip:no-products")
                    stock.on_skipped(conn, row["ref"]); out["skipped"] += 1; conn.commit(); continue
                ctx = {"product": card["title"]}
            if (row["flow"] in SITE_FLOWS and store.key not in config.site_live()) or \
                    (row["flow"] == "post_purchase" and store.key not in config.pp_live()) or \
                    (row["flow"] == "winback" and store.key not in config.wb_live()):
                engine.mark(conn, row, "skipped", error="skip:market-off"); out["skipped"] += 1; conn.commit(); continue
            cat = {"products": [card], "n_viewed": 1, "category_images": {}, "rating": None} if row["flow"] == "back_in_stock" \
                else _catalog(shop_factory, store, row, ctx)
            if row["flow"] in SITE_FLOWS or row["flow"] == "winback" or \
                    (row["flow"] == "post_purchase" and render.wants(row["flow"], row["step"], store)[0]):
                # the products ARE the mail: retry while Shopify is unreachable, skip when none is for sale any more
                if cat is None and shop_factory is not None:
                    raise RuntimeError("catalog unavailable")
                if not (cat or {}).get("products"):
                    engine.mark(conn, row, "skipped", error="skip:no-products"); out["skipped"] += 1; conn.commit(); continue
            msg_id = conn.execute(
                "INSERT INTO messages (profile_id, store, kind, marketing, sent_at) VALUES (%s,%s,%s,%s,%s) RETURNING id",
                (row["profile_id"], store.key, f"flow:{row['flow']}:{row['step']}", step.marketing, now),
            ).fetchone()["id"]
            links = render.LinkMaker(msg_id, secret, base_url)
            rendered = render.with_open_pixel(render.render(row["flow"], row["step"], store, ctx, links, catalog=cat, now=now),
                                              links.open())
            ses_id = sender.send(ses_client, store, profiles.email_of(conn, row["profile_id"]), rendered,
                                 links.unsubscribe(), store.configuration_set)
            sent_ok = True   # SES has accepted the message — this step must never go back to 'pending' now
            conn.execute("UPDATE messages SET ses_id=%s WHERE id=%s", (ses_id, msg_id))
            engine.mark(conn, row, "sent", message_id=msg_id)
            if row["flow"] == "back_in_stock":
                stock.on_sent(conn, row["ref"], msg_id)
            conn.commit(); out["sent"] += 1
        except Exception as e:
            conn.rollback()
            logging.exception("send failed %s", row)
            try:
                if sent_ok:
                    # SES already accepted the message but the DB write that
                    # records the send then failed. Resending would double-mail
                    # the profile, so mark the step sent (unconfirmed) instead
                    # of letting it fall back to pending for a retry.
                    err = f"unconfirmed: {ses_id}: {e}"[:500]
                    # The messages row inserted earlier in this transaction was
                    # rolled back along with everything else — re-insert it so
                    # the frequency cap and heartbeat both see this send. It
                    # MUST keep msg_id: the delivered mail's click and
                    # unsubscribe tokens are signed with it, and a fresh
                    # sequence id would 404 them forever.
                    conn.execute(
                        "INSERT INTO messages (id, profile_id, store, kind, marketing, sent_at, ses_id) VALUES (%s,%s,%s,%s,%s,%s,%s)",
                        (msg_id, row["profile_id"], store.key, f"flow:{row['flow']}:{row['step']}", step.marketing, now, ses_id),
                    )
                    # via mark(): links message_id and runs the enrolment-completion check
                    engine.mark(conn, row, "sent", message_id=msg_id, error=err)
                    if row["flow"] == "back_in_stock":
                        stock.on_sent(conn, row["ref"], msg_id)
                    conn.commit()
                    out["sent"] += 1
                    slack.alert(
                        f"step recorded sent-but-unconfirmed after a post-send DB failure — "
                        f"enrolment={row['enrolment_id']} step={row['step']} ses_id={ses_id}: {e}"
                    )
                elif locked and locked["attempts"] >= MAX_ATTEMPTS:
                    conn.execute(
                        "UPDATE flow_steps SET last_error=%s, status='failed' WHERE enrolment_id=%s AND step=%s AND status='pending'",
                        (str(e)[:500], row["enrolment_id"], row["step"]),
                    )
                    if row["flow"] == "back_in_stock":
                        # a failure that outlived 8 attempts would just repeat on every restock (and alert each time)
                        stock.on_blocked(conn, row["ref"])
                    conn.commit()
                    out["failed"] += 1
                    slack.alert(
                        f"step failed after {MAX_ATTEMPTS} attempts — "
                        f"enrolment={row['enrolment_id']} step={row['step']}: {e}"
                    )
                else:
                    attempts = locked["attempts"] if locked else 1
                    conn.execute(
                        "UPDATE flow_steps SET last_error=%s, due_at=%s WHERE enrolment_id=%s AND step=%s AND status='pending'",
                        (str(e)[:500], now + _backoff(attempts), row["enrolment_id"], row["step"]),
                    )
                    conn.commit()
                    out["failed"] += 1
            except Exception:
                logging.exception("failed to record send outcome for %s — leaving it for the next tick", row)
                try:
                    conn.rollback()
                except Exception:
                    logging.exception("rollback after a failed error-handler write also failed")
    return out


def heartbeat(conn, now=None) -> bool:
    now = now or datetime.now(timezone.utc)
    stuck = conn.execute(
        """SELECT 1 FROM flow_steps fs JOIN enrolments e ON e.id = fs.enrolment_id
           WHERE fs.status = 'pending' AND e.state = 'active' AND fs.due_at < %s LIMIT 1""",
        (now - timedelta(hours=1),),
    ).fetchone()
    recent = conn.execute("SELECT 1 FROM messages WHERE sent_at > %s LIMIT 1", (now - timedelta(hours=24),)).fetchone()
    return not (stuck and not recent)
