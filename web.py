import html
import logging
import re
import urllib.parse

import profiles
import slack
import stores
import tokens

_HOSTNAME_RE = re.compile(r"^[a-z0-9.-]+$")


def _msg(conn, token, secret):
    try:
        data = tokens.verify(token, secret)
    except ValueError:
        return None, None
    row = conn.execute("SELECT id, profile_id, store FROM messages WHERE id=%s", (data.get("m"),)).fetchone()
    return data, row


def click(conn, token: str, secret: str):
    data, row = _msg(conn, token, secret)
    if not row or "u" not in data:
        return 404, {}
    u = urllib.parse.urlparse(data["u"])
    hostname = u.hostname or ""
    if u.scheme != "https" or not _HOSTNAME_RE.match(hostname):
        return 404, {}
    if hostname != stores.get(row["store"]).domain:
        return 404, {}
    conn.execute("INSERT INTO clicks (message_id, url) VALUES (%s,%s)", (row["id"], data["u"]))
    conn.commit()
    # identify this browser to the storefront snippet; a fragment never reaches a server or analytics
    loc = data["u"] if u.fragment else f"{data['u']}#lsp={tokens.visitor(row['profile_id'], secret)}"
    return 302, {"Location": loc}


# 1×1 transparent GIF
_GIF = bytes.fromhex("47494638396101000100800000000000ffffff21f90401000000002c00000000010001000002024401003b")
_GIF_HEADERS = {"Content-Type": "image/gif", "Cache-Control": "no-store, no-cache, must-revalidate, private", "Pragma": "no-cache"}


def open_pixel(conn, token: str, secret: str, user_agent: str | None):
    """Record an open of a known message; always answer with the image so a mail never shows a broken one."""
    data, row = _msg(conn, token, secret)
    if row and data.get("a") == "open":
        conn.execute("INSERT INTO opens (message_id, user_agent) VALUES (%s,%s)", (row["id"], (user_agent or "")[:300]))
        conn.commit()
    return 200, dict(_GIF_HEADERS), _GIF


_PAGE = """<!doctype html><meta name=viewport content="width=device-width"><title>{t}</title>
<body style="font-family:sans-serif;max-width:32rem;margin:3rem auto;padding:0 1rem">{b}</body>"""


def unsubscribe(conn, token: str, secret: str, method: str, shopify_client_factory, ip: str | None = None):
    data, row = _msg(conn, token, secret)
    if not row or data.get("a") != "unsub":
        return 404, _PAGE.format(t="Not found", b="<p>This link is not valid.</p>")
    store = stores.get(row["store"])
    if method != "POST":
        return 200, _PAGE.format(t="Unsubscribe", b=f'<p>Unsubscribe from emails from {html.escape(store.brand)}?</p>'
                                 '<form method="post"><button type="submit">Unsubscribe</button></form>')
    email = profiles.email_of(conn, row["profile_id"])
    profiles.suppress(conn, email, store.key, "unsubscribed")
    profiles.set_consent(conn, row["profile_id"], store.key, "unsubscribed", "unsubscribe_link", ip)
    conn.commit()
    try:
        shopify_client_factory(store).set_marketing_consent(email, False)
    except Exception:
        logging.exception("shopify consent write-back failed for %s", store.key)   # suppression already committed
        domain = email.split("@", 1)[-1]
        slack.alert(f"unsubscribe: Shopify consent write-back failed for {store.key} (…@{domain}) — suppression already recorded")
    return 200, _PAGE.format(t="Unsubscribed", b=f"<p>You have been unsubscribed from emails from {html.escape(store.brand)}.</p>")
