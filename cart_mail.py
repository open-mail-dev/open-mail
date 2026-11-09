"""Abandoned-checkout mail, design v3: one cart card with subtotal / shipping / total and the button,
selling points once (✓ circles), mail 2 = help (advice box + pay-later callout), mail 3 = trust (stars). Hand-written email HTML:
600px tables, inline styles; fixed-width inline-block cells stack on phones without media queries.

Copy: copy/<store>/<locale>/abandoned_checkout.yaml. Every claim there is taken from the storefront itself (header,
payment page, about page, customer-service page) — re-check the page before changing one."""
import html as H
import re

import render

INK, GREY, LINE, PINK = "#1a1a1a", "#6b6b6b", "#ececec", "#faf3f5"
FONT = "Cabin,'Helvetica Neue',Helvetica,Arial,sans-serif"


def _button(url, label):
    return (f'<a href="{url}" style="display:block;width:100%;box-sizing:border-box;background:{INK};color:#ffffff;font-family:{FONT};'
            f'font-size:16px;font-weight:700;text-decoration:none;text-align:center;padding:15px 24px;border-radius:6px;">'
            f'<span style="color:#ffffff;">{H.escape(label)}</span></a>')


def _circle(glyph, size=26):
    return (f'<div style="display:inline-block;width:{size}px;height:{size}px;line-height:{size}px;border-radius:{size // 2}px;'
            f'background:{INK};color:#ffffff;font-size:13px;font-weight:700;text-align:center;">{glyph}</div>')


def render_mail(step: int, store, copy: dict, cart: dict, links, catalog: dict | None) -> render.Rendered:
    s = copy["steps"][step]
    cur = cart["currency"]
    money = lambda x: render._money(x, cur, store.locale)  # noqa: E731
    href = lambda p: links.click(render._utm(f"{store.base_url}{p}", f"abandoned_checkout-{step}"))  # noqa: E731
    link = lambda u: links.click(render._utm(u, f"abandoned_checkout-{step}"))  # noqa: E731

    name = (cart.get("first_name") or "").strip()
    head = s["headline"].replace("{name}", f"{name}, " if name else "")
    head = head[:1].upper() + head[1:]
    text = [head, s["sub"], ""]

    # cart card
    subtotal = sum(i["line_price"] for i in cart["items"])
    left = float(copy["free_shipping_from"]) - subtotal
    rows = ""
    for it in cart["items"]:
        url = link(it["url"]) if it.get("url") else link(cart["url"])
        q = int(it["quantity"])
        meta = f'{copy["labels"]["quantity"]}: {q}' + (f' × {money(it["line_price"] / q)}' if q > 1 else "")
        img = (f'<img src="{H.escape(it["image"])}" width="72" height="72" alt="" style="display:block;width:72px;height:72px;'
               f'object-fit:contain;background:#ffffff;border:1px solid {LINE};border-radius:8px;">' if it.get("image") else "")
        rows += (f'<tr><td width="84" style="padding:14px 12px 14px 0;vertical-align:top;"><a href="{url}">{img}</a></td>'
                 f'<td style="padding:14px 0;vertical-align:top;font-family:{FONT};">'
                 f'<a href="{url}" style="color:{INK};text-decoration:none;font-size:15px;line-height:21px;font-weight:700;">{H.escape(it["title"])}</a>'
                 f'<div style="color:{GREY};font-size:13px;line-height:20px;margin-top:4px;">{meta}</div></td>'
                 f'<td style="padding:14px 0 14px 12px;vertical-align:top;text-align:right;white-space:nowrap;font-family:{FONT};'
                 f'font-size:15px;color:{INK};">{money(it["line_price"])}</td></tr>'
                 f'<tr><td colspan="3" style="border-bottom:1px solid {LINE};font-size:0;line-height:0;">&nbsp;</td></tr>')
        text.append(f"- {it['title']} ({meta}) {money(it['line_price'])}" + (f": {link(it['url'])}" if it.get("url") else ""))
    lab = copy["labels"]
    ship = f'<span style="color:{INK};font-weight:700;">{H.escape(lab["shipping_free"])}</span>' if left <= 0 else ""
    hint = ("" if left <= 0 else
            f'<tr><td></td><td colspan="2" style="padding:0 0 6px;font-family:{FONT};font-size:13px;color:{GREY};">'
            f'{H.escape(lab["shipping_left"].format(x=money(left)))}</td></tr>')
    cell = lambda t, style: f'<td style="font-family:{FONT};{style}">{t}</td>'  # noqa: E731
    summary = (f'<tr><td></td>{cell(H.escape(lab["subtotal"]), f"padding:14px 0 4px;font-size:14px;color:{GREY};")}'
               f'{cell(money(subtotal), f"padding:14px 0 4px;text-align:right;font-size:14px;color:{GREY};white-space:nowrap;")}</tr>'
               f'<tr><td></td>{cell(H.escape(lab["shipping"]), f"padding:4px 0;font-size:14px;color:{GREY};")}'
               f'{cell(ship, "padding:4px 0;text-align:right;font-size:14px;white-space:nowrap;")}</tr>{hint}'
               f'<tr><td></td>{cell(H.escape(lab["total"]), f"padding:8px 0 0;font-size:17px;font-weight:700;color:{INK};")}'
               f'{cell(money(cart["total"]), f"padding:8px 0 0;text-align:right;font-size:17px;font-weight:700;color:{INK};white-space:nowrap;")}</tr>')
    cta = link(cart["url"])
    card = (f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="border:1px solid {LINE};border-radius:12px;'
            f'border-collapse:separate;"><tr><td style="padding:4px 14px 16px;"><table role="presentation" width="100%" cellpadding="0" '
            f'cellspacing="0">{rows}{summary}</table><div style="margin-top:20px;">{_button(cta, copy["cta"])}</div></td></tr></table>')
    text += ["", f"{lab['subtotal']}: {money(subtotal)}",
             f"{lab['shipping']}: {lab['shipping_free']}" if left <= 0 else lab["shipping_left"].format(x=money(left)),
             f"{lab['total']}: {money(cart['total'])}", "", f"{copy['cta']}: {cta}"]

    # mail-specific block
    extra = ""
    if s.get("help"):
        hp = s["help"]
        rows_ = ""
        for r in hp["rows"]:
            u = r["url"] if r["url"].startswith(("tel:", "mailto:")) else href(r["url"])
            rows_ += (f'<tr><td width="40" style="padding:10px 0;vertical-align:top;">{_circle(r["icon"])}</td>'
                      f'<td style="padding:10px 0;font-family:{FONT};"><a href="{u}" style="color:{INK};text-decoration:none;font-size:15px;">{r["label"]}</a>'
                      f'<div style="color:{GREY};font-size:13px;">{H.escape(r["note"])}</div></td></tr>')
            text.append(f"{re.sub('<[^>]+>', '', r['label'])} ({r['note']}): {u}")
        extra += (f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:{PINK};border-radius:12px;margin-top:24px;">'
                  f'<tr><td style="padding:18px 22px;"><div style="font-family:{FONT};font-size:17px;font-weight:700;color:{INK};margin-bottom:4px;">'
                  f'{H.escape(hp["title"])}</div><table role="presentation" cellpadding="0" cellspacing="0">{rows_}</table></td></tr></table>')
    if s.get("pay_later"):
        extra += (f'<div style="font-family:{FONT};font-size:14px;line-height:22px;color:{INK};margin-top:20px;padding:14px 18px;'
                  f'border-left:3px solid {INK};">{s["pay_later"]}</div>')
        text.append(re.sub("<[^>]+>", "", s["pay_later"]))
    rating = (catalog or {}).get("rating")
    stars = head_rating = ""
    if rating:
        avg, n = rating
        lbl = copy["rating"]["format"].format(rating=render._number(avg, store.locale, 1), count=render._number(n, store.locale))
        if s.get("stars"):
            full = int(round(avg))
            stars = (f'<div style="text-align:center;margin:28px 0 4px;font-family:{FONT};"><a href="{href(copy["rating"]["path"])}" '
                     f'style="text-decoration:none;color:{INK};"><div style="font-size:26px;letter-spacing:3px;color:#f0a93b;">'
                     f'{"★" * full}{"☆" * (5 - full)}</div><div style="font-size:15px;font-weight:700;margin-top:4px;">{H.escape(lbl)}</div>'
                     f'<div style="font-size:13px;color:{GREY};margin-top:2px;">{H.escape(copy["since"])}</div></a></div>')
            text += ["", f"★ {lbl} · {copy['since']}"]
        else:   # mail 3 shows the big stars instead: never the score twice
            head_rating = (f'<a href="{href(copy["rating"]["path"])}" style="text-decoration:none;color:{INK};font-family:{FONT};'
                           f'font-size:13px;white-space:nowrap;"><span style="color:#f0a93b;">★</span> {H.escape(lbl)}</a>')

    # selling points once; the pay-later item drops out where the pay-later callout already says it
    trust_items = [t for t in copy["trust"] if not (s.get("pay_later") and t.get("pay_later"))]
    trust = "".join(
        f'<div style="display:inline-block;width:146px;vertical-align:top;margin:0 3px 16px;text-align:center;font-family:{FONT};">'
        f'<a href="{href(t["path"])}" style="text-decoration:none;color:{INK};">{_circle("✓")}'
        f'<div style="font-size:14px;font-weight:700;margin-top:6px;">{H.escape(t["label"])}</div>'
        f'<div style="font-size:13px;color:{GREY};margin-top:2px;">{H.escape(t["note"])}</div></a></div>' for t in trust_items)
    text += [""] + [f"✓ {t['label']} ({t['note']})" for t in trust_items]

    # one contact line — not in mail 2, whose advice box is the contact block
    contact = ""
    if not s.get("help"):
        ct = copy["contact"]
        # phone and email are optional: markets without a phone line in their language get chat + email only
        contact = ct["html"].format(phone=f'<a href="tel:{ct.get("tel", "")}" style="color:{INK};">{H.escape(ct.get("phone", ""))}</a>',
                                    email=f'<a href="mailto:{ct.get("email", "")}" style="color:{INK};">{H.escape(ct.get("email", ""))}</a>',
                                    chat=f'<a href="{href(ct["chat_path"])}" style="color:{INK};">{H.escape(ct["chat_label"])}</a>') + "<br><br>"
        text += ["", re.sub("<[^>]+>", "", ct["html"].format(phone=ct.get("phone", ""), email=ct.get("email", ""), chat=ct["chat_label"]))
                 + f" {href(ct['chat_path'])}"]

    unsub = links.unsubscribe()
    foot = " &nbsp;·&nbsp; ".join(f'<a href="{href(p["path"])}" style="color:{GREY};">{H.escape(p["label"])}</a>' for p in copy["footer_links"])
    html_ = f"""<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="x-apple-disable-message-reformatting"><meta name="format-detection" content="telephone=no,address=no,email=no">
<link href="https://fonts.googleapis.com/css2?family=Cabin:wght@400;700&display=swap" rel="stylesheet"><title>{H.escape(s["subject"])}</title></head>
<body style="margin:0;padding:0;background:#f4f4f4;">
<div style="display:none;max-height:0;overflow:hidden;">{H.escape(s["preheader"])}</div>
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:#f4f4f4;"><tr><td align="center" style="padding:16px 6px;">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="max-width:600px;background:#ffffff;border-radius:14px;overflow:hidden;">
<tr><td style="background:{PINK};padding:16px 20px;"><table role="presentation" width="100%" cellpadding="0" cellspacing="0"><tr>
 <td style="font-family:{FONT};font-size:22px;font-weight:700;"><a href="{href('/')}" style="color:{INK};text-decoration:none;">{H.escape(store.brand)}</a></td>
 <td align="right">{head_rating}</td></tr></table></td></tr>
<tr><td style="padding:28px 20px 8px;font-family:{FONT};">
 <div style="font-size:26px;line-height:32px;font-weight:700;color:{INK};">{H.escape(head)}</div>
 <div style="font-size:16px;line-height:24px;color:{GREY};margin:10px 0 24px;">{H.escape(s["sub"])}</div>
 {card}{extra}{stars}
</td></tr>
<tr><td style="padding:28px 14px 4px;text-align:center;font-size:0;">{trust}</td></tr>
<tr><td style="padding:0 20px 28px;font-family:{FONT};font-size:14px;line-height:22px;color:{INK};">{contact}{copy["sign"]}</td></tr>
<tr><td style="background:#fafafa;padding:20px;text-align:center;font-family:{FONT};font-size:12px;line-height:20px;color:{GREY};">
 {foot}<br><a href="{unsub}" style="color:{GREY};">{H.escape(copy["unsubscribe_label"])}</a><br>{copy["footer"]}</td></tr>
</table></td></tr></table></body></html>"""
    text += ["", re.sub("<br>", "\n", copy["sign"]), "", re.sub("&zwnj;", "", copy["footer"]), f"{copy['unsubscribe_label']}: {unsub}"]
    return render.Rendered(subject=s["subject"], preheader=s["preheader"], html=html_, text=H.unescape("\n".join(text)))
