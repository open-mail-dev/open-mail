"""Welcome-flow report: signups → sends → clicks → orders, per step. Read-only.

Attribution: an order that uses the person's own welcome code is the flow's, exactly. Any other
order by a signup within CLICK_WINDOW after a click in a welcome mail counts separately as click-attributed.
Each attributed order is credited to the last welcome mail sent before it. Opens are not tracked.

    MAIL_DB_DSN=... python report.py --store shop.example.com --since 2026-10-01 [--until 2026-11-01] [--exclude test@example.com]
"""
import argparse
import os
import statistics
from datetime import datetime, timedelta, timezone

CLICK_WINDOW = timedelta(days=5)

# Opens are an estimate, only good for comparing markets and mails with each other. A pixel fetch is "machine" when
# it is Apple Mail Privacy Protection (which prefetches every mail; its user agent is exactly "Mozilla/5.0"), has no
# user agent, or comes within SCANNER_WINDOW of sending (spam filters / link scanners). Gmail's and Yahoo's image
# proxies only fetch when the mail is opened, so those count as human.
SCANNER_WINDOW = timedelta(seconds=30)
_MACHINE_UA = ("", "mozilla/5.0")


def _human_open(ua: str | None, ts: datetime, sent_at: datetime) -> bool:
    return (ua or "").strip().lower() not in _MACHINE_UA and ts - sent_at > SCANNER_WINDOW

# One row per signup in the window: the profile, when, and their welcome code (if one was issued).
_COHORT = """
SELECT DISTINCT ON (e.profile_id) e.profile_id, e.ts AS signed_up, p.email, c.code
FROM events e JOIN profiles p ON p.id = e.profile_id
LEFT JOIN coupons c ON c.profile_id = e.profile_id AND c.store = e.store
WHERE e.store = %(store)s AND e.type = 'signup' AND e.ts >= %(since)s AND e.ts < %(until)s
  AND NOT (p.email = ANY(%(exclude)s))
ORDER BY e.profile_id, e.ts
"""


def welcome(conn, store: str, since: datetime, until: datetime, exclude: tuple = ()) -> dict:
    args = {"store": store, "since": since, "until": until, "exclude": [e.lower() for e in exclude]}
    cohort = {r["profile_id"]: r for r in conn.execute(_COHORT, args)}
    ids = list(cohort)
    out = {"store": store, "since": since, "until": until, "signups": len(ids), "steps": {}, "orders": [],
           "bounces": 0, "complaints": 0}
    if not ids:
        return out

    msgs = conn.execute(
        """SELECT m.id, m.profile_id, m.kind, m.sent_at, EXISTS (SELECT 1 FROM clicks k WHERE k.message_id = m.id) AS clicked,
                  (SELECT min(k.ts) FROM clicks k WHERE k.message_id = m.id) AS first_click,
                  (SELECT coalesce(json_agg(json_build_object('ua', o.user_agent, 'ts', o.ts)), '[]'::json)
                   FROM opens o WHERE o.message_id = m.id) AS opens
           FROM messages m WHERE m.store = %s AND m.kind LIKE 'flow:welcome:%%' AND m.profile_id = ANY(%s)
           ORDER BY m.sent_at""", (store, ids)).fetchall()
    by_profile = {}
    for m in msgs:
        by_profile.setdefault(m["profile_id"], []).append(m)

    def step_of(m):
        return int(m["kind"].rsplit(":", 1)[1])

    def last_before(pid, ts):
        prior = [m for m in by_profile.get(pid, []) if m["sent_at"] <= ts]
        return prior[-1] if prior else None

    for m in msgs:
        s = out["steps"].setdefault(step_of(m), {"sent": 0, "opened": 0, "opened_human": 0, "clicked": 0, "unsubscribed": 0,
                                                 "code_orders": 0, "code_revenue": 0.0, "click_orders": 0, "click_revenue": 0.0})
        s["sent"] += 1
        s["clicked"] += m["clicked"]
        opens = m["opens"] or []
        s["opened"] += bool(opens)
        s["opened_human"] += any(_human_open(o["ua"], datetime.fromisoformat(o["ts"]), m["sent_at"]) for o in opens)

    # an unsubscribe is credited to the last welcome mail before it (the link doesn't say which mail it came from)
    for u in conn.execute(
            """SELECT profile_id, ts FROM consent_log WHERE store = %s AND status = 'unsubscribed'
               AND source = 'unsubscribe_link' AND profile_id = ANY(%s)""", (store, ids)):
        m = last_before(u["profile_id"], u["ts"])
        if m:
            out["steps"][step_of(m)]["unsubscribed"] += 1

    for o in conn.execute(
            """SELECT profile_id, ts, payload FROM events WHERE store = %s AND type = 'order_placed' AND profile_id = ANY(%s)
               ORDER BY ts""", (store, ids)):
        who = cohort[o["profile_id"]]
        if o["ts"] < who["signed_up"]:
            continue
        p = o["payload"] or {}
        codes = {(c or "").upper() for c in p.get("discount_codes") or []}
        total = float(p.get("total") or 0)
        if who["code"] and who["code"].upper() in codes:
            how = "code"
        elif any(m["first_click"] and m["first_click"] <= o["ts"] <= m["first_click"] + CLICK_WINDOW
                 for m in by_profile.get(o["profile_id"], [])):
            how = "click"
        else:
            how = "none"
        m = last_before(o["profile_id"], o["ts"])
        out["orders"].append({"profile_id": o["profile_id"], "ts": o["ts"], "total": total, "how": how,
                              "step": step_of(m) if m else None, "hours": (o["ts"] - who["signed_up"]).total_seconds() / 3600})
        if how != "none" and m:
            s = out["steps"][step_of(m)]
            s[f"{how}_orders"] += 1
            s[f"{how}_revenue"] += total

    emails = [r["email"] for r in cohort.values()]
    for r in conn.execute("SELECT reason, count(*) AS n FROM suppressions WHERE store = '*' AND email = ANY(%s) GROUP BY reason",
                          (emails,)):
        if r["reason"] == "hard_bounce":
            out["bounces"] = r["n"]
        elif r["reason"] == "complaint":
            out["complaints"] = r["n"]
    return out


def _pct(a, b):
    return f"{100 * a / b:.1f}%" if b else "-"


def _eur(x):
    return "€" + f"{x:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")


def format_report(r: dict) -> str:
    n = r["signups"]
    orders = r["orders"]
    code = [o for o in orders if o["how"] == "code"]
    click = [o for o in orders if o["how"] == "click"]
    first_code = {}
    for o in code:
        first_code.setdefault(o["profile_id"], o)
    lines = [
        f"Welcome flow · {r['store']} · signups {r['since']:%Y-%m-%d} → {r['until']:%Y-%m-%d}",
        "",
        f"Signups                 {n}",
        f"Ordered with own code   {len(first_code)}  ({_pct(len(first_code), n)} of signups)  {_eur(sum(o['total'] for o in code))}",
        f"Click-attributed orders {len(click)}  {_eur(sum(o['total'] for o in click))}",
        f"Any order after signup  {len({o['profile_id'] for o in orders})}  ({_pct(len({o['profile_id'] for o in orders}), n)})",
    ]
    if first_code:
        hrs = sorted(o["hours"] for o in first_code.values())
        lines.append(f"Signup → code order     median {statistics.median(hrs):.1f} h · within 24 h: "
                     f"{_pct(sum(h <= 24 for h in hrs), len(hrs))}")
    lines += ["", "Step  Sent  Opened  Human%  Clicked  Click%  Code orders  Code rev     Click orders  Click rev    Unsubs"]
    for k in sorted(r["steps"]):
        s = r["steps"][k]
        lines.append(f"{k:>4}  {s['sent']:>4}  {_pct(s['opened'], s['sent']):>6}  {_pct(s['opened_human'], s['sent']):>6}  "
                     f"{s['clicked']:>7}  {_pct(s['clicked'], s['sent']):>6}  {s['code_orders']:>11}  "
                     f"{_eur(s['code_revenue']):>11}  {s['click_orders']:>12}  {_eur(s['click_revenue']):>11}  {s['unsubscribed']:>6}")
    lines += ["", f"Hard bounces {r['bounces']} · complaints {r['complaints']}",
              "Orders are credited to the last welcome mail before them. Revenue = order total incl. VAT and shipping.",
              "Opened = any pixel fetch (incl. Apple Mail prefetch + scanners); Human% = estimate without those. "
              "Compare between markets/mails only; mails sent before opens were tracked count as unopened."]
    return "\n".join(lines)


def abandoned(conn, stores_: tuple, since: datetime, until: datetime, window: timedelta = timedelta(days=7),
              flow: str = "abandoned_checkout") -> dict:
    """Holdout test readout: per store and arm, checkouts, buyers (an order by the same profile in that store within
    `window` of the checkout) and their revenue. Only checkouts at least `window` old count as finished, so the
    newest week never drags a group's rate down."""
    rows = conn.execute(
        """SELECT e.store, e.arm, e.ref, e.started_at,
                  (SELECT coalesce(sum((o.payload->>'total')::numeric), 0) FROM events o
                    WHERE o.type='order_placed' AND o.store=e.store AND o.profile_id=e.profile_id
                      AND o.ts > e.started_at AND o.ts <= e.started_at + %(w)s
                      AND o.external_id IS DISTINCT FROM (CASE WHEN e.ref LIKE 'order:%%' THEN substr(e.ref, 7) END)) AS revenue,
                  (SELECT count(*) FROM events o
                    WHERE o.type='order_placed' AND o.store=e.store AND o.profile_id=e.profile_id
                      AND o.ts > e.started_at AND o.ts <= e.started_at + %(w)s
                      AND o.external_id IS DISTINCT FROM (CASE WHEN e.ref LIKE 'order:%%' THEN substr(e.ref, 7) END)) AS orders,
                  (SELECT count(*) FROM flow_steps fs WHERE fs.enrolment_id=e.id AND fs.status='sent') AS sent,
                  (SELECT count(*) FROM clicks k JOIN flow_steps fs ON fs.message_id=k.message_id WHERE fs.enrolment_id=e.id) AS clicks
           FROM enrolments e
           WHERE e.flow=%(f)s AND e.store = ANY(%(s)s) AND e.started_at >= %(a)s AND e.started_at < %(b)s
             AND e.started_at <= now() - %(w)s""",
        {"f": flow, "s": list(stores_), "a": since, "b": until, "w": window}).fetchall()
    out = {}
    for r in rows:
        g = out.setdefault((r["store"], r["arm"]), {"checkouts": 0, "buyers": 0, "revenue": 0.0, "sent": 0, "clicks": 0})
        g["checkouts"] += 1
        g["buyers"] += r["orders"] > 0
        g["revenue"] += float(r["revenue"])
        g["sent"] += r["sent"]
        g["clicks"] += r["clicks"]
    return out


def site(conn, stores_: tuple, flow: str, since: datetime, until: datetime, window: timedelta = timedelta(days=7)) -> dict:
    """browse_abandonment / cart_reminder / post_purchase holdout readout (post_purchase: window=60 days; the order
    that started it never counts): same counting as abandoned(), one row per enrolment
    (`checkouts` = enrolments), orders within `window` of the view/cart add that started it."""
    return abandoned(conn, stores_, since, until, window, flow=flow)


def back_in_stock(conn, stores_: tuple, since: datetime, until: datetime) -> dict:
    """Per store: requests made in [since, until) and where they stand now, plus orders of the requested variant by the
    same profile within 14 days of its "weer leverbaar" mail (no holdout: a service mail, see the spec)."""
    rows = conn.execute(
        """SELECT r.store, r.state,
                  (SELECT count(*) FROM events o WHERE o.type='order_placed' AND o.profile_id=r.profile_id
                     AND o.payload->'variant_ids' ? r.variant_id AND o.ts > m.sent_at AND o.ts <= m.sent_at + interval '14 days') AS orders,
                  (SELECT coalesce(sum((o.payload->>'total')::numeric), 0) FROM events o WHERE o.type='order_placed' AND o.profile_id=r.profile_id
                     AND o.payload->'variant_ids' ? r.variant_id AND o.ts > m.sent_at AND o.ts <= m.sent_at + interval '14 days') AS revenue
           FROM stock_requests r LEFT JOIN messages m ON m.id = r.message_id
           WHERE r.store = ANY(%s) AND r.created_at >= %s AND r.created_at < %s""",
        (list(stores_), since, until)).fetchall()
    out = {}
    for r in rows:
        g = out.setdefault(r["store"], {"requests": 0, "waiting": 0, "sent": 0, "expired": 0, "cancelled": 0, "orders_14d": 0, "revenue_14d": 0.0})
        g["requests"] += 1
        state = "waiting" if r["state"] == "enrolled" else r["state"]
        g[state] += 1
        g["orders_14d"] += r["orders"]
        g["revenue_14d"] += float(r["revenue"])
    return out


def format_site(res: dict, flow: str) -> str:
    from flows import FLOWS
    n = len(FLOWS[flow].steps)
    return format_abandoned(res, title=f"{flow} · holdout test (treatment = {n} mail{'s' if n > 1 else ''}, holdout = none)",
                            unit="Enrolments")


def format_abandoned(res: dict, title: str = "Abandoned checkout · holdout test (treatment = 3 reminders, holdout = none; "
                     "always = subscribers/customers, not in the test)", unit: str = "Checkouts") -> str:
    lines = [title, "", f"Store          Arm        {unit:>10}  Bought ≤7d   Rate   Revenue      €/{unit[:-1].lower():<9} Mails  Clicks"]
    tot = {}
    for (st, arm), g in sorted(res.items()):
        t = tot.setdefault(arm, {"checkouts": 0, "buyers": 0, "revenue": 0.0, "sent": 0, "clicks": 0})
        for k in t: t[k] += g[k]
        lines.append(f"{st:14} {arm or '-':10} {g['checkouts']:>9}  {g['buyers']:>10}  {_pct(g['buyers'], g['checkouts']):>5}  "
                     f"{_eur(g['revenue']):>11}  {_eur(g['revenue'] / g['checkouts']) if g['checkouts'] else '-':>10}  {g['sent']:>5}  {g['clicks']:>6}")
    lines.append("")
    for arm, g in sorted(tot.items(), key=lambda kv: kv[0] or ""):
        lines.append(f"{'ALL':14} {arm or '-':10} {g['checkouts']:>9}  {g['buyers']:>10}  {_pct(g['buyers'], g['checkouts']):>5}  "
                     f"{_eur(g['revenue']):>11}  {_eur(g['revenue'] / g['checkouts']) if g['checkouts'] else '-':>10}")
    lines.append("\nRevenue = order totals incl. VAT/shipping, shop currency. Compare treatment vs holdout only.")
    return "\n".join(lines)


def main():
    import db
    ap = argparse.ArgumentParser()
    ap.add_argument("--store", default="shop.example.com")
    ap.add_argument("--since", required=True, help="YYYY-MM-DD (UTC), signups from this day")
    ap.add_argument("--until", help="YYYY-MM-DD (UTC), exclusive; default now")
    ap.add_argument("--exclude", default="", help="comma-separated emails to leave out (test signups)")
    a = ap.parse_args()
    since = datetime.fromisoformat(a.since).replace(tzinfo=timezone.utc)
    until = datetime.fromisoformat(a.until).replace(tzinfo=timezone.utc) if a.until else datetime.now(timezone.utc)
    with db.connect(os.environ["MAIL_DB_DSN"]) as conn:
        print(format_report(welcome(conn, a.store, since, until, tuple(e for e in a.exclude.split(",") if e))))


if __name__ == "__main__":
    main()
