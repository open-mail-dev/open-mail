import dataclasses
import html as _html
import pathlib
import re
import urllib.parse
from dataclasses import dataclass
from datetime import datetime, timezone

import jinja2
import yaml

import coupons
import tokens

ROOT = pathlib.Path(__file__).parent
_env = jinja2.Environment(undefined=jinja2.StrictUndefined, autoescape=False)
_SHELL = _env.from_string((ROOT / "templates" / "base.html").read_text())


@dataclass
class Rendered:
    subject: str
    preheader: str
    html: str
    text: str


class LinkMaker:
    def __init__(self, message_id: int, secret: str, base_url: str):
        self.m, self.k, self.base = message_id, secret, base_url

    def click(self, url: str) -> str:
        return f"{self.base}/c/{tokens.sign({'m': self.m, 'u': url}, self.k)}"

    def unsubscribe(self) -> str:
        return f"{self.base}/u/{tokens.sign({'m': self.m, 'a': 'unsub'}, self.k)}"

    def open(self) -> str:
        return f"{self.base}/o/{tokens.sign({'m': self.m, 'a': 'open'}, self.k)}.gif"


def with_open_pixel(r: "Rendered", url: str) -> "Rendered":
    """The same mail with a 1×1 open pixel as the last thing in <body> (HTML part only)."""
    img = f'<img src="{url}" width="1" height="1" alt="" style="display:block;border:0;width:1px;height:1px;">'
    i = r.html.rfind("</body>")
    html = r.html[:i] + img + r.html[i:] if i >= 0 else r.html + img
    return dataclasses.replace(r, html=html)


def _utm(url: str, campaign: str) -> str:
    sep = "&" if "?" in url else "?"
    return f"{url}{sep}" + urllib.parse.urlencode({"utm_source": "mail", "utm_medium": "email", "utm_campaign": campaign})


def _link_row(items: list, href, sep: str, style: str) -> str:
    return sep.join(f'<a href="{href(i["path"])}" style="{style}">{_html.escape(i["label"])}</a>' for i in items)


def _code_box(box: dict, code: str, note: str) -> str:
    return (
        '<div style="text-align:center;padding:16px 8px;border:2px dashed #1a1a1a;border-radius:8px;'
        'margin:24px 0 4px;background:#faf2f4;">'
        f'<div style="font-size:14px;color:#555555;margin-bottom:8px;">{_html.escape(box["label"])}</div>'
        # 22px/1px spacing keeps "WELCOME-XXXX-XXXX" on one line in a ~260px phone box; nowrap guarantees it
        f'<div style="font-size:22px;font-weight:700;color:#1a1a1a;letter-spacing:1px;white-space:nowrap;">{code}</div>'
        f'<div style="font-size:13px;color:#666666;margin-top:8px;">{note}</div></div>'
    )


def _button(url: str, label: str) -> str:
    """The primary button, styled like the shell's mj-button, for use inside body_html (no MJML there)."""
    return (f'<div style="text-align:center;margin:20px 0 0;"><a href="{url}" target="_blank" '
            'style="display:inline-block;background:#1a1a1a;color:#ffffff;font-size:16px;font-weight:600;line-height:120%;'
            f'text-decoration:none;padding:16px 48px;border-radius:4px;"><span style="color:#ffffff;">{_html.escape(label)}</span></a></div>')


def _eur(amount: float, locale: str = "") -> str:
    if locale.split("-")[0] == "en":
        return f"€{amount:,.2f}"
    whole, cents = f"{amount:,.2f}".split(".")
    return f"€{whole.replace(',', '.')},{cents}"


def _number(x: float, locale: str, decimals: int = 0) -> str:
    """A rating or count: "4.4" / "3,474" for English stores, "4,4" / "3.474" otherwise."""
    s = f"{x:,.{decimals}f}"
    return s if locale.split("-")[0] == "en" else s.translate(str.maketrans(",.", ".,"))


def _money(amount: float, currency: str, locale: str = "") -> str:
    """The checkout's own currency, written the way that market writes it (euros: the way the store's language writes them)."""
    whole, cents = f"{amount:,.2f}".split(".")
    if currency == "SEK":
        return f"{whole.replace(',', '\u00a0')},{cents}\u00a0kr"
    if currency == "DKK":
        return f"{whole.replace(',', '.')},{cents}\u00a0kr."
    if currency == "CZK":
        # Czech crowns: whole crowns when integral ("12 990 Kč"), haléře only when present.
        grouped = whole.replace(',', '\u00a0')
        return f"{grouped}\u00a0Kč" if cents == "00" else f"{grouped},{cents}\u00a0Kč"
    if currency == "RON":
        # Romanian lei: always with bani, "." thousands ("1.299,00 lei").
        return f"{whole.replace(',', '.')},{cents}\u00a0lei"
    if currency == "HUF":
        # Hungarian forints: whole forints ("12 990 Ft"), fillér only when present.
        grouped = whole.replace(',', '\u00a0')
        return f"{grouped}\u00a0Ft" if cents == "00" else f"{grouped},{cents}\u00a0Ft"
    return _eur(amount, locale)


def _cart(cart: dict, labels: dict, link, locale: str = "") -> tuple[str, str]:
    """The buyer's own cart as they left it: image, title, quantity, line price; then the total. (html, text)"""
    rows, text = [], []
    for it in cart["items"]:
        url = link(it["url"]) if it.get("url") else None
        a = (lambda inner: f'<a href="{url}" style="color:#1a1a1a;text-decoration:none;">{inner}</a>') if url else (lambda inner: inner)
        img = (a(f'<img src="{_html.escape(it["image"])}" width="72" alt="" style="display:block;width:72px;height:auto;'
                 'border:1px solid #eeeeee;border-radius:6px;">') if it.get("image") else "")
        price = _money(it["line_price"], cart["currency"], locale)
        qty = int(it["quantity"])
        # "10 × €29,00": a big quantity with only a line total reads like a wrong price
        each = f" × {_money(it['line_price'] / qty, cart['currency'], locale)}" if qty > 1 else ""
        rows.append(
            '<tr><td style="width:72px;padding:8px 12px 8px 0;vertical-align:top;">' + img + '</td>'
            f'<td style="padding:8px 0;vertical-align:top;text-align:left;font-size:14px;line-height:20px;color:#1a1a1a;">{a(_html.escape(it["title"]))}'
            f'<div style="color:#777777;font-size:13px;">{_html.escape(labels["quantity"])}: {qty}{each}</div></td>'
            f'<td style="padding:8px 0 8px 12px;vertical-align:top;text-align:right;white-space:nowrap;font-size:14px;color:#1a1a1a;">{price}</td></tr>')
        text.append(f"- {it['title']} ({labels['quantity']}: {qty}{each}) {price}" + (f": {url}" if url else ""))
    total = _money(cart["total"], cart["currency"], locale)
    rows.append(f'<tr><td></td><td style="padding:12px 0 0;text-align:left;font-size:15px;font-weight:700;color:#1a1a1a;border-top:1px solid #eeeeee;">'
                f'{_html.escape(labels["total"])}</td><td style="padding:12px 0 0 12px;text-align:right;white-space:nowrap;font-size:15px;'
                f'font-weight:700;color:#1a1a1a;border-top:1px solid #eeeeee;">{total}</td></tr>')
    html_ = ('<table role="presentation" cellpadding="0" cellspacing="0" border="0" style="width:100%;max-width:480px;margin:24px auto 0;'
             'border-collapse:collapse;">' + "".join(rows) + "</table>")
    return html_, "\n".join(text) + f"\n{labels['total']}: {total}"


# Fixed-width inline-block cards, no media queries (the Gmail app ignores them for non-Gmail accounts):
# 128px + 2x8px margin = 144px, so the narrowest phone column (~297px, Gmail app on a 375pt iPhone) fits
# 2 per row with a 16px gap, and the 552px desktop column fits 3 (4 would need 576px).
# Percent widths collapse to 1 per row on phones once min-width kicks in.
CARD_PX, CARD_GAP_PX = 128, 16


def _cards(heading: str, cards: list) -> str:
    """A grid of image cards with no orphan: a phone shows 2 per row, so an odd count (3, 5) would leave one card
    alone on the last row — drop the last one instead. 6 cards → 3x2 on desktop / 2x3 on a phone; 2 or 4 cards →
    the grid is capped at 2 columns so desktop shows the same tidy 2x2. cards: (url, image, title, sub)."""
    if len(cards) > 1 and len(cards) % 2:
        cards = cards[:-1]
    cols = 3 if len(cards) % 3 == 0 else 2
    grid_px = cols * (CARD_PX + CARD_GAP_PX)
    out = [f'<div style="margin:32px auto 0;max-width:{grid_px}px;text-align:center;font-size:0;">'
           f'<div style="font-size:18px;font-weight:700;color:#1a1a1a;margin-bottom:16px;">{_html.escape(heading)}</div>']
    for url, image, title, sub in cards:
        out.append(
            f'<div style="display:inline-block;width:{CARD_PX}px;vertical-align:top;margin:0 {CARD_GAP_PX // 2}px 28px;font-size:13px;">'
            f'<a href="{url}" style="text-decoration:none;color:#1a1a1a;">'
            f'<img src="{_html.escape(image)}" width="{CARD_PX}" alt="{_html.escape(title)}" '
            f'style="display:block;width:{CARD_PX}px;max-width:100%;height:auto;margin:0 auto 8px;border:1px solid #eeeeee;border-radius:6px;">'
            f'<div style="font-size:14px;line-height:19px;color:#1a1a1a;">{_html.escape(title)}</div>'
            + (f'<div style="font-size:14px;font-weight:700;color:#1a1a1a;margin-top:4px;">{sub}</div>' if sub else "")
            + '</a></div>'
        )
    out.append("</div>")
    return "".join(out)


def _copy(flow: str, store) -> dict:
    return yaml.safe_load((ROOT / "copy" / (store.copy_key or store.key) / store.locale / f"{flow}.yaml").read_text())


def wants(flow: str, step: int, store) -> tuple[str | None, list, int]:
    """What live catalogue data this step needs: (product mode None/"viewed"/"fill", category collection handles,
    how many product cards)."""
    s = _copy(flow, store)["steps"][step]
    return s.get("products"), [c["handle"] for c in s.get("categories", [])], s.get("products_count", 3)


def _cat_path(c: dict) -> str:
    """A category tile's link: the market's own collection URL (copy `path`, e.g. a translated
    /collections/<slug> on a market domain) or, on the shop's own domain, /collections/<handle>. `handle` stays the base handle the image is
    fetched by."""
    return c.get("path") or f"/collections/{c['handle']}"


def _card_title(title: str, strip: list) -> str:
    """Drop a redundant category prefix ("Barber Chair Model X" → "Model X"): the block heading and the CTA
    already say what the products are, and a 128px card wraps long titles to 3 lines."""
    for prefix in strip:
        if title.startswith(prefix) and len(title) > len(prefix):
            title = title[len(prefix):]
            break
    return _short(title)


def _short(title: str, limit: int = 60) -> str:
    return title if len(title) <= limit else title[: limit - 1].rsplit(" ", 1)[0] + "…"


def render(flow: str, step: int, store, ctx: dict, links: LinkMaker, catalog: dict | None = None, now=None) -> Rendered:
    copy = _copy(flow, store)
    if flow == "abandoned_checkout":
        import cart_mail   # v3 design has its own layout (cart card, no USP bar); imported here: it builds on this module
        return cart_mail.render_mail(step, store, copy, ctx["cart"], links, catalog)
    s = copy["steps"][step]
    ctx = dict(ctx)
    cart = ctx.pop("cart", None)
    if cart:
        ph = copy["cart"]["products_phrase"]
        n_more = len(cart["items"]) - 1
        first = _short(cart["items"][0]["title"], 70) if cart["items"] else ""
        ctx["products"] = (ph["one"] if n_more == 0 else ph["two"] if n_more == 1 else ph["many"]).format(product=first, n=n_more)
    expires_at, viewed = ctx.pop("expires_at", None), ctx.pop("viewed", None)  # noqa: F841 (viewed only feeds the catalog)
    if expires_at is not None:
        ams = expires_at.astimezone(coupons.AMS)
        ctx["expires"] = copy.get("date_format", "{day} {month}").format(day=ams.day, month=copy["months"][ams.month - 1])
        # calendar days, so it agrees with "valid until <date>" (on 11 Oct, "until 14 October" reads as 3 days, not 3.5 → 4)
        today = (now or datetime.now(timezone.utc)).astimezone(coupons.AMS).date()
        days = max(1, (ams.date() - today).days)
        # [one, other]; Czech [one, few (2-4), many]; Slovenian [one, two, few (3-4), many]
        u = copy["day_units"]
        if days == 1 or len(u) == 2:
            unit = u[0] if days == 1 else u[1]
        elif len(u) == 3:
            unit = u[1] if 2 <= days <= 4 else u[2]
        else:
            unit = u[1] if days == 2 else u[2] if 3 <= days <= 4 else u[3]
        ctx["days_left"] = f"{days} {unit}"
    safe = {k: _html.escape(str(v)) for k, v in ctx.items()}
    fill = lambda t: _env.from_string(t).render(**safe)  # noqa: E731
    campaign = f"{flow}-{step}"
    link = lambda url: links.click(_utm(url, campaign))  # noqa: E731
    href = lambda path: link(f"{store.base_url}{path}")  # noqa: E731
    # the brand name is a domain, which Gmail/Apple Mail auto-link in blue: link it ourselves, in black
    brand_a = f'<a href="{href("/")}" style="color:#1a1a1a;text-decoration:none;">{store.brand}</a>'
    brand_link = lambda h: h.replace(store.brand, brand_a)  # noqa: E731
    subject, pre, body = fill(s["subject"]), fill(s["preheader"]), fill(s["body_html"])
    if cart and cart.get("first_name") and copy.get("greeting"):
        body = _html.escape(copy["greeting"].format(name=cart["first_name"])) + "<br/>" + body
    headline = f'<div style="text-align:center;font-size:22px;font-weight:700;line-height:30px;color:#1a1a1a;margin-bottom:12px;">{brand_link(fill(s["headline"]))}</div>'
    # optional blocks are assembled here, inside body_html: MJML drops/mangles Jinja control
    # blocks placed between components, so the shell cannot carry {% if %}
    box = copy.get("code_box") or {}
    note = (fill(box["note_until"]) if "expires" in ctx else _html.escape(box["note"])) if "code" in ctx else ""
    code_box = _code_box(box, safe["code"], note) if "code" in ctx else ""
    if code_box and box.get("cta_label"):
        code_box += _button(href(s["cta_path"]), box["cta_label"])

    blocks, block_text = "", ""
    if cart:
        cart_html, cart_text = _cart(cart, copy["cart"], link, store.locale)
        blocks += cart_html
        block_text += "\n\n" + cart_text
    if s.get("contact") and copy.get("contact"):
        c = copy["contact"]
        blocks += ('<div style="margin:28px auto 0;max-width:480px;padding:16px 20px;background:#faf7f8;border-radius:8px;text-align:left;'
                   'font-size:14px;line-height:22px;color:#1a1a1a;">'
                   f'<div style="font-weight:700;margin-bottom:6px;">{_html.escape(c["heading"])}</div>'
                   f'☎ <a href="tel:{c["tel"]}" style="color:#1a1a1a;">{_html.escape(c["phone"])}</a> '
                   f'<span style="color:#777777;">({_html.escape(c["hours"])})</span><br/>'
                   f'💬 <a href="{href(c["chat_path"])}" style="color:#1a1a1a;">{_html.escape(c["chat_label"])}</a><br/>'
                   f'✉ <a href="{href(copy["help_path"])}" style="color:#1a1a1a;">{_html.escape(c["service_label"])}</a></div>')
        block_text += (f"\n\n{c['heading']}\n{c['phone']} ({c['hours']})\n{c['chat_label']}: {href(c['chat_path'])}"
                       f"\n{c['service_label']}: {href(copy['help_path'])}")
    products = (catalog or {}).get("products") or []
    if products:
        n_viewed = catalog.get("n_viewed", 0)
        heading = copy["products_heading"]["viewed" if n_viewed == len(products) else "mixed" if n_viewed else "popular"]
        strip = copy.get("card_title_strip", [])
        price = lambda p: _money(p["price"], p.get("currency", "EUR"), store.locale)  # noqa: E731 — the storefront's own price
        blocks += _cards(heading, [(link(p["url"]), p["image"], _card_title(p["title"], strip), price(p)) for p in products])
        block_text += f"\n\n{heading}:\n" + "\n".join(f"- {p['title']} ({price(p)}): {link(p['url'])}" for p in products)
    if s.get("categories"):
        images = (catalog or {}).get("category_images") or {}
        tiles = [c for c in s["categories"] if c["handle"] in images]
        if len(tiles) >= 3:   # a grid with holes looks broken; without images, the CTA carries the mail
            blocks += _cards(s["categories_heading"], [(href(_cat_path(c)), images[c["handle"]], c["label"], "") for c in tiles])
        block_text += f"\n\n{s['categories_heading']}:\n" + "\n".join(f"- {c['label']}: {href(_cat_path(c))}" for c in s["categories"])

    # every step MUST have cta_path/cta_label: the compiled MJML shell always renders the button
    # "@checkout": the buyer's own checkout (abandonedCheckoutUrl, on the market's domain)
    # "@product": the first product card (browse_abandonment), falling back to the bestsellers if there is none
    if s["cta_path"] == "@product":
        cta = link(products[0]["url"]) if products else href(copy.get("fallback_path", "/"))
    else:
        cta = link(cart["url"]) if s["cta_path"] == "@checkout" else href(s["cta_path"])
    unsub = links.unsubscribe()
    help_html = _env.from_string(brand_link(copy["help_html"])).render(help_url=href(copy["help_path"]))
    assurance_text, help_for_text = "", help_html
    if cart and copy.get("assurances"):
        # what makes a buyer hesitate over a big order, said in words (the header bar is tiny)
        # spaced nowrap items, no "·" separators: a phone wraps between items and a separator would dangle at a line end
        items_ = " ".join(f'<a href="{href(x["path"])}" style="color:#1a1a1a;text-decoration:none;white-space:nowrap;margin:0 8px;">✓ {_html.escape(x["label"])}</a>'
                          for x in copy["assurances"])
        help_html = (f'<div style="text-align:center;font-size:14px;line-height:24px;color:#1a1a1a;margin:0 0 24px;">{items_}</div>' + help_html)
        assurance_text = "\n" + " · ".join(x["label"] for x in copy["assurances"])
    # One row: live review score + USPs, glued into nowrap PAIRS. Desktop fits all four on one line; a phone can
    # only break between pairs, so it always shows a balanced 2 + 2 (per-item nowrap gave 3 + 1 at 360px).
    item_style = "color:#555555;text-decoration:none;white-space:nowrap;margin:0 6px;"   # 4 items = 539px ≤ 552px in Helvetica
    # classes drive the phone-only rule in base.mjml (tighter gaps, `mobile: false` items hidden); clients that ignore
    # <style> media queries keep every item and fall back to the 2 + 2 pairs below
    items = [f'<a class="om-usp{"" if u.get("mobile", True) else " om-desktop-only"}" href="{href(u["path"])}" '
             f'style="{item_style}">{_html.escape(u["label"])}</a>' for u in copy["usps"]]
    rating = (catalog or {}).get("rating")
    if rating:
        avg, count = rating
        label = copy["rating"]["format"].format(rating=_number(avg, store.locale, 1), count=_number(count, store.locale))
        items.insert(0, f'<a class="om-usp" href="{href(copy["rating"]["path"])}" style="{item_style}color:#1a1a1a;">'
                        f'<span style="color:#f0a93b;">★</span> {_html.escape(label)}</a>')
    usps_html = " ".join(f'<span style="white-space:nowrap;">{"".join(items[i:i + 2])}</span>' for i in range(0, len(items), 2))
    html_out = _SHELL.render(
        brand=brand_a, tagline=copy["tagline"], preheader=pre, usps_html=usps_html,
        body_html=headline + f'<div style="text-align:center;color:#555555;">{brand_link(body)}</div>' + code_box + blocks,
        # some clients repaint link text in their own link colour; the inner span pins it to white
        cta_url=cta, cta_label=f'<span style="color:#ffffff;">{s["cta_label"]}</span>', help_html=help_html,
        footer_links_html=_link_row(copy["footer_links"], href, "&nbsp;&nbsp;·&nbsp;&nbsp;", "color:#1a1a1a;text-decoration:underline;"),
        footer=copy["footer"], unsubscribe_url=unsub, unsubscribe_label=copy["unsubscribe_label"],
    )
    code_text = f"\n\n{box['label']} {safe['code']}\n{re.sub(r'<[^>]+>', '', note)}" if code_box else ""
    if code_box and box.get("cta_label"):
        code_text += f"\n{box['cta_label']}: {href(s['cta_path'])}"
    text = re.sub(r"<[^>]+>", "", fill(s["headline"])) + "\n\n" + re.sub(r"<[^>]+>", "", body) + code_text + block_text + \
        f"\n\n{s['cta_label']}: {cta}" + assurance_text + "\n\n" + re.sub(r"<br/>", "\n", re.sub(r"<a [^>]+>|</a>", "", help_for_text)) + \
        f"\n\n{copy['footer']}\n{copy['unsubscribe_label']}: {unsub}"
    return Rendered(subject=_html.unescape(re.sub(r"<[^>]+>", "", subject)), preheader=pre, html=html_out, text=_html.unescape(text))
