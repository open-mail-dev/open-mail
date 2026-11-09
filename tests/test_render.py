import copy as _copy_mod
import dataclasses
import pathlib
import re
import urllib.parse
from datetime import datetime, timezone

import pytest

import render
import stores
import tokens

S = stores.get("shop.example.com")
MARKETS = ["shop.example.com", "shop-ie.example.com", "shop-fi.example.com"]

CTX = {"code": "X", "expires_at": datetime(2026, 10, 15, 21, 59, 59, tzinfo=timezone.utc)}
EXP = datetime(2026, 10, 15, 21, 59, 59, tzinfo=timezone.utc)   # 15 Oct 23:59:59 Amsterdam
NOW = datetime(2026, 10, 12, 12, tzinfo=timezone.utc)


def links():
    return render.LinkMaker(message_id=7, secret="k", base_url="https://mail.example")


def clicked_urls(html: str, secret: str = "k") -> list[str]:
    """The destination URLs behind the signed /c/<token> links in a rendered mail."""
    return [tokens.verify(tok, secret)["u"] for tok in re.findall(r"/c/([A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]+)", html)]


def P(h, title="Test Product", price=249.0, domain="shop.example.com"):
    return {"handle": h, "title": title, "url": f"https://{domain}/products/{h}", "image": "https://cdn/x.jpg", "price": price}


# --- welcome ----------------------------------------------------------------------------------------------------------
def test_welcome_step1_renders_code_everywhere():
    r = render.render("welcome", 1, S, {"code": "WELCOME-AAAA-BBBB"}, links())
    assert "WELCOME-AAAA-BBBB" in r.subject and "WELCOME-AAAA-BBBB" in r.html and "WELCOME-AAAA-BBBB" in r.text


def test_cta_goes_through_click_redirect_with_utm():
    r = render.render("welcome", 1, S, {"code": "X"}, links())
    targets = clicked_urls(r.html)
    assert targets, "no tracked links"
    assert all("utm_source=mail" in u and "utm_campaign=welcome-1" in u for u in targets)
    assert any(u.startswith("https://shop.example.com/collections/best-sellers") for u in targets)


def test_every_shop_link_is_tracked():
    r = render.render("welcome", 1, S, {"code": "X"}, links())
    assert "https://shop.example.com" not in r.html, "untracked shop link in html"


def test_code_box_shown_on_every_step():
    for step in (1, 2, 3):
        r = render.render("welcome", step, S, {**CTX, "code": "WELCOME-ZZ"}, links())
        assert "WELCOME-ZZ" in r.html and "WELCOME-ZZ" in r.text


def test_unsubscribe_link_present():
    assert "https://mail.example/u/" in render.render("welcome", 2, S, CTX, links()).html


def test_copy_is_html_escaped_for_context_values():
    r = render.render("welcome", 1, S, {"code": "<script>"}, links())
    assert "<script>" not in r.html


def test_missing_context_value_fails_loudly():
    with pytest.raises(Exception):
        render.render("welcome", 1, S, {}, links())


def test_base_shell_retains_all_jinja_placeholders():
    base_html = (pathlib.Path(__file__).parent.parent / "templates" / "base.html").read_text()
    for placeholder in (
        "{{ preheader }}", "{{ brand }}", "{{ tagline }}", "{{ usps_html }}", "{{ body_html }}", "{{ help_html }}",
        "{{ footer_links_html }}", "{{ cta_url }}", "{{ cta_label }}", "{{ footer }}", "{{ unsubscribe_url }}",
        "{{ unsubscribe_label }}",
    ):
        assert placeholder in base_html, f"MJML compilation mangled {placeholder!r}"


def test_brand_name_only_appears_as_black_link():
    # a bare brand that looks like a domain gets auto-linked in blue by Gmail
    for step in (1, 2, 3):
        html = render.render("welcome", step, S, CTX, links()).html
        assert S.brand not in re.sub(r"<a [^>]*>.*?</a>", "", html, flags=re.S)
        assert f'text-decoration:none;">{S.brand}</a>' in html


def test_cta_label_pinned_white():
    html = render.render("welcome", 1, S, {"code": "X"}, links()).html
    assert html.count('<span style="color:#ffffff;">Use my €10 discount</span>') == 2   # under the code + after the products


def test_expiry_date_and_countdown():
    r = render.render("welcome", 3, S, {"code": "X", "expires_at": EXP}, links(), now=NOW)
    assert r.subject == "3 days left: your €10 welcome discount"   # 12 → 15 Oct, calendar days
    assert "Valid until 15 October" in r.html and "until 15 October" in r.text


def test_one_day_left_is_singular():
    r = render.render("welcome", 3, S, {"code": "X", "expires_at": EXP}, links(), now=datetime(2026, 10, 15, 8, tzinfo=timezone.utc))
    assert r.subject.startswith("1 day left:")


def _with_day_units(monkeypatch, units, date_format=None):
    real = render._copy

    def fake(flow, store):
        c = _copy_mod.deepcopy(real(flow, store))
        c["day_units"] = units
        if date_format:
            c["date_format"] = date_format
        return c
    monkeypatch.setattr(render, "_copy", fake)


@pytest.mark.parametrize("now,want", [(datetime(2026, 10, 15, 8, tzinfo=timezone.utc), "1 one"), (datetime(2026, 10, 12, 12, tzinfo=timezone.utc), "3 few"),
                                      (datetime(2026, 10, 10, 12, tzinfo=timezone.utc), "5 many")])
def test_countdown_supports_three_plural_forms(monkeypatch, now, want):
    # [one, few (2-4), many], as e.g. Czech or Slovak need
    _with_day_units(monkeypatch, ["one", "few", "many"])
    r = render.render("welcome", 3, S, {"code": "X", "expires_at": EXP}, links(), now=now)
    assert r.subject.startswith(f"{want} left:"), r.subject


@pytest.mark.parametrize("now,want", [(datetime(2026, 10, 15, 8, tzinfo=timezone.utc), "1 one"), (datetime(2026, 10, 13, 12, tzinfo=timezone.utc), "2 two"),
                                      (datetime(2026, 10, 12, 12, tzinfo=timezone.utc), "3 few"), (datetime(2026, 10, 10, 12, tzinfo=timezone.utc), "5 many")])
def test_countdown_supports_four_plural_forms(monkeypatch, now, want):
    # [one, two, few (3-4), many], as e.g. Slovenian needs
    _with_day_units(monkeypatch, ["one", "two", "few", "many"])
    r = render.render("welcome", 3, S, {"code": "X", "expires_at": EXP}, links(), now=now)
    assert r.subject.startswith(f"{want} left:"), r.subject


def test_date_format_is_per_language(monkeypatch):
    _with_day_units(monkeypatch, ["day", "days"], date_format="{month} {day}.")
    r = render.render("welcome", 3, S, {"code": "X", "expires_at": EXP}, links(), now=NOW)
    assert "October 15." in r.html


def test_product_cards_heading_follows_what_was_viewed():
    for n_viewed, heading in ((0, "Popular products"), (1, "Selected for you"), (2, "You recently viewed")):
        cat = {"products": [P("a"), P("b")], "n_viewed": n_viewed}
        r = render.render("welcome", 1, S, {"code": "X"}, links(), catalog=cat)
        assert heading in r.html and heading in r.text


def test_product_card_price_is_formatted_and_link_tracked():
    r = render.render("welcome", 1, S, {"code": "X"}, links(), catalog={"products": [P("a", price=1249.5)], "n_viewed": 0})
    assert "€1,249.50" in r.html
    assert any(u.startswith("https://shop.example.com/products/a?") for u in clicked_urls(r.html))


def test_no_catalog_means_no_product_block():
    assert "Popular products" not in render.render("welcome", 1, S, {"code": "X"}, links()).html


def test_category_grid_needs_at_least_three_images():
    few = {"category_images": {"kitchen": "https://cdn/a.jpg", "lighting": "https://cdn/b.jpg"}}
    assert "Our range" not in render.render("welcome", 2, S, CTX, links(), catalog=few).html
    many = {"category_images": {h: "https://cdn/x.jpg" for h in ("kitchen", "lighting", "storage", "gifts")}}
    html = render.render("welcome", 2, S, CTX, links(), catalog=many).html
    assert "Our range" in html and "Storage" in html and "Garden" not in html


def test_wants_matches_copy():
    assert render.wants("welcome", 1, S) == ("fill", [], 4)
    mode, cats, _ = render.wants("welcome", 2, S)
    assert mode == "viewed" and cats[0] == "kitchen" and len(cats) == 6
    assert render.wants("welcome", 3, S) == ("fill", [], 6)


def test_category_tile_links_to_its_path_or_its_handle():
    cat = {"products": [], "category_images": {h: f"https://shop.example.com/cdn/shop/{h}.jpg" for h in ("kitchen", "bedding-bath", "lighting", "storage")}}
    urls = clicked_urls(render.render("welcome", 2, S, {"code": "X", "expires_at": EXP}, links(), now=NOW, catalog=cat).html)
    assert any(u.startswith("https://shop.example.com/collections/bedding-and-bath?") for u in urls)    # copy `path`
    assert any(u.startswith("https://shop.example.com/collections/kitchen?") for u in urls)            # the handle


def test_live_rating_leads_the_usp_row():
    r = render.render("welcome", 1, S, {"code": "X"}, links(), catalog={"rating": (4.4312, 3474)})
    assert "4.4/5 (3,474)" in r.html
    assert r.html.index("4.4/5 (3,474)") < r.html.index("Free delivery from €70")
    assert any(u.startswith("https://shop.example.com/pages/reviews") for u in clicked_urls(r.html))


def test_no_rating_means_no_stale_number():
    html = render.render("welcome", 1, S, {"code": "X"}, links()).html
    assert "/5 (" not in html and "Free delivery from €70" in html


def _n_cards(html):
    return html.count("display:inline-block;width:%dpx" % render.CARD_PX)


def test_grid_never_leaves_an_orphan_card():
    for n, shown, grid in ((1, 1, 2), (2, 2, 2), (3, 2, 2), (4, 4, 2), (5, 4, 2), (6, 6, 3)):
        cat = {"products": [P(str(i)) for i in range(n)], "n_viewed": 0}
        html = render.render("welcome", 1, S, {"code": "X"}, links(), catalog=cat).html
        assert _n_cards(html) == shown, n
        assert "max-width:%dpx;text-align:center" % (grid * (render.CARD_PX + render.CARD_GAP_PX)) in html, n


def test_desktop_only_usp_is_marked_for_the_phone_rule():
    html = render.render("welcome", 1, S, {"code": "X"}, links(), catalog={"rating": (4.4, 10)}).html
    assert html.count('class="om-usp om-desktop-only"') == 2
    i = html.index('class="om-usp om-desktop-only"')
    assert "14-day returns" in html[i:i + 400]
    assert re.search(r"max-width: ?480px\)\s*\{\s*\.om-desktop-only\s*\{\s*display: ?none !important", html)


def test_card_titles_drop_a_category_prefix():
    assert render._card_title("Barber Chair Model X Black", ["Barber Chair "]) == "Model X Black"
    assert render._card_title("Barber Chair ", ["Barber Chair "]) == "Barber Chair "        # never down to nothing
    assert render._card_title("Lamp " + "x" * 80, []).endswith("…")


def test_code_cta_sits_between_code_and_products():
    cat = {"products": [P("a"), P("b")], "n_viewed": 0}
    html = render.render("welcome", 1, S, {"code": "WELCOME-QQ"}, links(), catalog=cat).html
    assert html.index("WELCOME-QQ") < html.index("Use my €10 discount") < html.index("Popular products")


def test_welcome_cards_show_the_market_price_in_its_currency():
    cat = {"products": [{"handle": "x", "title": "Product X", "url": "https://shop-fi.example.com/products/x",
                         "image": "https://shop-fi.example.com/cdn/shop/x.jpg?width=400", "price": 3590.0, "currency": "SEK", "base_price": 319.0}],
           "n_viewed": 1, "category_images": {}, "rating": None}
    r = render.render("welcome", 1, S, {"code": "X"}, links(), catalog=cat)
    assert "3\u00a0590,00\u00a0kr" in r.html and "3\u00a0590,00\u00a0kr" in r.text and "€3.590" not in r.html


@pytest.mark.parametrize("key", MARKETS)
def test_every_storefront_renders_all_welcome_steps(key):
    st = stores.get(key)
    for step in (1, 2, 3):
        r = render.render("welcome", step, st, {"code": "WELCOME-AAAA-BBBB", "expires_at": EXP}, links(), now=NOW)
        assert "WELCOME-AAAA-BBBB" in r.html and "WELCOME-AAAA-BBBB" in r.text
        assert all(urllib.parse.urlparse(u).hostname == st.domain for u in clicked_urls(r.html))
        assert ".." not in re.sub(r"https?://\S+", " ", r.subject + r.text), (key, step)


def test_subfolder_market_mails_link_only_into_their_own_subfolder():
    # a market in a subfolder shares its domain with another storefront: a link without the subfolder lands there
    st = dataclasses.replace(S, key="shop.example.com/de-de", path_prefix="/de-de", copy_key="shop.example.com")
    cat = {"products": [P("x", domain="shop.example.com/de-de")], "n_viewed": 0, "rating": (4.4, 10),
           "category_images": {h: "https://shop.example.com/cdn/shop/x.jpg" for h in ("kitchen", "lighting", "storage")}}
    for step in (1, 2, 3):
        r = render.render("welcome", step, st, {"code": "X", "expires_at": EXP}, links(), catalog=cat, now=NOW)
        urls = clicked_urls(r.html)
        assert urls and all(u.startswith(st.base_url + "/") for u in urls), (step, [u for u in urls if not u.startswith(st.base_url + "/")])


def test_first_welcome_subject_does_not_put_the_code_right_after_the_word_code():
    # mail clients can hide "…code: XXXX-XXXX" subjects as one-time-password look-alikes: name the discount instead
    r = render.render("welcome", 1, S, {"code": "WELCOME-AAAA-BBBB", "expires_at": EXP}, links(), now=NOW)
    assert "WELCOME-AAAA-BBBB" in r.subject


# --- abandoned checkout -----------------------------------------------------------------------------------------------
def _cart(host, currency):
    return {"url": f"https://{host}/1234567890/checkouts/ac/abc", "currency": currency, "total": 1234.5,
            "items": [{"title": "Desk Lamp Basic — White", "quantity": 2, "line_price": 638.0, "image": f"https://{host}/cdn/shop/files/b.jpg?width=200"},
                      {"title": "Lamp Shade <Large>", "quantity": 1, "line_price": 596.5, "image": None}]}


@pytest.mark.parametrize("key,currency,money", [
    ("shop.example.com", "EUR", "€1,234.50"),
    ("shop-ie.example.com", "EUR", "€1,234.50"),
    ("shop-fi.example.com", "SEK", "1\u00a0234,50\u00a0kr"),
    ("shop-fi.example.com", "DKK", "1.234,50\u00a0kr."),
])
def test_abandoned_checkout_shows_the_cart_in_its_currency_and_links_to_the_checkout(key, currency, money):
    st = stores.get(key)
    for step in (1, 2, 3):
        r = render.render("abandoned_checkout", step, st, {"cart": _cart(key, currency)}, links())
        assert money in r.html and money in r.text
        assert "Desk Lamp Basic — White" in r.html and "Lamp Shade &lt;Large&gt;" in r.html     # escaped
        assert "Complete my order" in r.html and "discount code" not in r.html.lower()
        urls = set(clicked_urls(r.html))
        assert any(u.startswith(f"https://{key}/1234567890/checkouts/ac/abc") for u in urls)
        assert all(urllib.parse.urlparse(u).hostname == key for u in urls)     # web.click only redirects to the store's domain


def _cart2(name="Lisa", items=None):
    items = items or [{"title": "Cotton Towel 70 x 140 cm Grey", "quantity": 10, "line_price": 290.0, "image": None,
                       "url": "https://shop.example.com/products/cotton-towel"},
                      {"title": "Desk Lamp Basic", "quantity": 1, "line_price": 319.0, "image": None, "url": None}]
    return {"url": "https://shop.example.com/1/checkouts/ac/x", "currency": "EUR", "first_name": name, "items": items,
            "total": sum(i["line_price"] for i in items)}


CAT = {"rating": (4.42, 3509)}


def _ac(step, cart=None, key="shop.example.com"):
    return render.render("abandoned_checkout", step, stores.get(key), {"cart": cart or _cart2()}, links(), catalog=CAT)


def test_ac_is_personal_and_concrete():
    r = _ac(1)
    assert "Lisa, your basket is still waiting" in r.html and "Lisa, your basket is still waiting" in r.text
    assert "10 × €29.00" in r.html and "10 × €29.00" in r.text
    urls = set(clicked_urls(r.html))
    assert any(u.startswith("https://shop.example.com/products/cotton-towel") for u in urls)
    assert all(urllib.parse.urlparse(u).hostname == "shop.example.com" for u in urls)
    assert "<div" not in r.text and "<a " not in r.text
    assert "Your basket is still waiting" in _ac(1, _cart2(name="")).html


def test_ac_shipping_status_follows_the_storefront_threshold():
    assert "Free" in _ac(1).html
    small = _cart2(items=[{"title": "Towel", "quantity": 1, "line_price": 29.0, "image": None, "url": None}])
    assert "€41.00 away from free delivery" in _ac(1, small).html


def test_ac_mail_2_says_each_thing_once():
    two = _ac(2)
    assert "tel:+00123456789" in two.html and "Personal advice" in two.html
    assert "Questions? Call" not in two.html                      # the advice box is the contact block
    assert "Rather spread the cost?" in two.html and "Pay later" not in two.html   # callout, not again as a selling point
    for step in (1, 3):
        one = _ac(step)
        assert "Questions? Call" in one.html and "Personal advice" not in one.html and "Pay later" in one.html


def test_ac_review_score_never_twice():
    assert "4.4/5 from 3,509 reviews" in _ac(1).html and "★★★★☆" not in _ac(1).html
    three = _ac(3)
    assert three.html.count("4.4/5 from 3,509 reviews") == 1 and "★★★★☆" in three.html and "since 2015" in three.html


@pytest.mark.parametrize("key", MARKETS)
def test_ac_every_storefront_renders_all_steps(key):
    for step in (1, 2, 3):
        r = _ac(step, _cart2(), key)
        assert "Complete my order" in r.html and "<div" not in r.text
        assert ("Lisa" in r.html) == (step == 1)                # only mail 1's headline carries the name


# --- open pixel -------------------------------------------------------------------------------------------------------
def test_open_pixel_goes_in_just_before_body_end():
    lm = render.LinkMaker(message_id=7, secret="k", base_url="https://mail.example")
    url = lm.open()
    assert url.startswith("https://mail.example/o/") and url.endswith(".gif")
    assert tokens.verify(url.rsplit("/", 1)[1][:-4], "k") == {"m": 7, "a": "open"}
    r = render.Rendered(subject="s", preheader="p", html="<html><body><p>hi</p></body></html>", text="hi")
    out = render.with_open_pixel(r, url)
    assert out.html.endswith(f'<img src="{url}" width="1" height="1" alt="" style="display:block;border:0;width:1px;height:1px;"></body></html>')
    assert out.text == "hi" and out.subject == "s"


# --- browse / cart / post-purchase / winback / back-in-stock ----------------------------------------------------------
def _cat_market(key):
    st = stores.get(key)
    return {"products": [{"handle": "x", "title": "Product X", "url": f"{st.base_url}/products/x", "image": f"https://{st.domain}/cdn/shop/x.jpg?width=400",
                          "price": 319.0, "currency": st.currency, "base_price": 319.0, "id": "5"}],
            "n_viewed": 0, "category_images": {}, "rating": (4.42, 3555)}


@pytest.mark.parametrize("key", MARKETS)
@pytest.mark.parametrize("flow", ["browse_abandonment", "cart_reminder"])
@pytest.mark.parametrize("step", [1, 2])
def test_site_flow_copy_renders_without_false_claims(key, flow, step):
    st = stores.get(key)
    assert render.wants(flow, step, st)[0] == "viewed"
    cat = dict(_cat_market(key), n_viewed=1)
    r = render.render(flow, step, st, {"viewed": ["x"]}, links(), catalog=cat)
    assert r.subject.strip() and "Product X" in r.html
    cta = re.search(r'href="(https://mail\.example/c/[^"]+)"[^>]*>\s*<span style="color:#ffffff;">', r.html).group(1)
    target = tokens.verify(cta.rsplit("/", 1)[1], "k")["u"]
    assert urllib.parse.urlparse(target).hostname == st.domain
    assert urllib.parse.urlparse(target).path == ("/products/x" if flow == "browse_abandonment" else "/cart")
    low = (r.html + r.text).lower()
    for bad in ("limited", "free return", "{{", "}}"):
        assert bad not in low, bad


# claims no storefront page backs, plus a shipping-mail promise not every fulfilment keeps
_PP_FORBIDDEN = ("thousands", "48", "shipped", "free return")


@pytest.mark.parametrize("key", MARKETS)
@pytest.mark.parametrize("step", [1, 2])
def test_post_purchase_renders_without_invented_claims(key, step):
    st = stores.get(key)
    cat = {"products": [{**P("p5", title="Test Product Model"), "id": "5"}], "n_viewed": 0, "category_images": {}, "rating": None}
    r = render.render("post_purchase", step, st, {"order_name": "#X1", "viewed": ["1"]}, links(), catalog=cat if step == 2 else None)
    text = (r.subject + " " + r.text).lower()
    if step == 1:
        assert "#x1" in r.subject.lower() and "#x1" in r.html.lower()
    else:
        assert "test product model" in r.html.lower()
    for bad in _PP_FORBIDDEN:
        assert bad not in re.sub(r"https?://\S+", " ", text), (key, step, bad)


_WB_FORBIDDEN = ("discount", "miss you", "limited", "{{", "}}")


@pytest.mark.parametrize("key", MARKETS)
def test_winback_renders_without_invented_claims_or_discount(key):
    st = stores.get(key)
    assert st.winback
    cat = {"products": [{**P("p5", title="Test Product Model"), "id": "5"}], "n_viewed": 0, "category_images": {}, "rating": None}
    r = render.render("winback", 1, st, {"viewed": ["1"]}, links(), catalog=cat)
    assert "test product model" in r.html.lower()
    text = re.sub(r"https?://\S+", " ", (r.subject + " " + r.text).lower())
    for bad in _PP_FORBIDDEN + _WB_FORBIDDEN:
        assert bad not in text, (key, bad)


@pytest.mark.parametrize("key", MARKETS)
def test_back_in_stock_mail_shows_the_variant_title_and_link(key):
    st = stores.get(key)
    assert st.back_in_stock
    card = {**P("p1", title="Test Product Model"), "id": "1", "url": f"https://{st.domain}/products/p1?variant=11"}
    r = render.render("back_in_stock", 1, st, {"product": "Test Product Model"}, links(),
                      catalog={"products": [card], "n_viewed": 1, "category_images": {}, "rating": None})
    assert "Test Product Model" in r.subject and "is back in stock" in r.subject
    assert any(urllib.parse.urlparse(u).hostname == st.domain and "variant=11" in u for u in clicked_urls(r.html))
    text = re.sub(r"https?://\S+", " ", (r.subject + " " + r.text).lower())
    for bad in _PP_FORBIDDEN + ("limited", "hurry", "discount", "{{", "}}"):
        assert bad not in text, (key, bad)
