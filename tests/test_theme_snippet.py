import json
import pathlib
import re
import shutil
import subprocess

import pytest

SNIPPET = (pathlib.Path(__file__).parent.parent / "theme" / "om-signup.liquid").read_text()


def test_snippet_posts_the_fields_signup_expects():
    for f in ("store: {{ om_store | json }}", "source: source", 'source = src || "popup"', "website:", "email:"):
        assert f in SNIPPET


def test_footer_newsletter_form_signs_up_through_us():
    # otherwise footer signups reach another tool's newsletter welcome via a Shopify sync and get a second code
    assert 'form[action^="/contact"]' in SNIPPET and 'indexOf("newsletter")' in SNIPPET
    assert 'open("footer")' in SNIPPET and "e.preventDefault()" in SNIPPET


def test_snippet_storage_is_guarded():
    assert SNIPPET.count("try {") >= 2


def test_snippet_never_shows_the_code():
    # the code only travels by email (keeps made-up addresses off the list)
    assert "x.b.code" not in SNIPPET.replace("x.b.code_valid", "")
    assert "check your inbox" in SNIPPET.lower() and "spam" in SNIPPET


def _popup():
    return SNIPPET[SNIPPET.index('<div id="om-signup"'):SNIPPET.index('<script type="application/json" id="om-t">')]


def test_popup_signs_up_in_one_step_then_confirms():
    p = _popup()
    form = p[p.index('<form data-step="1"'):p.index("</form>")]
    for s in ('type="email"', 'type="submit"', 'id="om-no"', 'name="website"', 'class="om-row"'):
        assert s in form, s                                    # email + submit side by side, "No thanks", honeypot
    assert 'data-step="2"' in p and 'data-step="3"' not in p   # step 2 = "check your inbox"
    assert 'id="om-signup-close"' in p and 'data-t-aria="close"' in p
    assert "open(\"footer\")" in SNIPPET and "show(2)" in SNIPPET


def test_popup_has_no_side_teaser():
    assert "teaser" not in SNIPPET.lower() and "writing-mode" not in SNIPPET


def test_popup_is_themable_with_css_variables():
    css = SNIPPET[SNIPPET.index("<style>"):SNIPPET.index("</style>")]
    for var in ("--om-accent:", "--om-radius:", "--om-text:", "--om-overlay:"):
        assert var in css, var
    assert "var(--om-accent)" in css and "var(--om-radius)" in css
    assert "text-transform: uppercase" not in css
    assert re.search(r"@media \(max-width: 480px\)[^@]*\.om-row \{ flex-direction: column; \}", css)   # stacked on mobile


def test_whole_snippet_is_inside_the_known_host_guard():
    # unknown host -> om_store stays blank -> nothing at all is rendered
    guard = SNIPPET.index("{%- if om_store != blank -%}")
    assert guard < SNIPPET.index("<style>") and guard < SNIPPET.index("<script")
    assert SNIPPET.index("{%- assign om_store = '' -%}") < guard
    assert SNIPPET.rstrip().endswith("{%- endif -%}")


def test_snippet_suggests_fix_for_common_domain_typos():
    assert '"gmial.com": "gmail.com"' in SNIPPET and "Did you mean" in SNIPPET


def test_snippet_explains_undeliverable_domain():
    assert 'x.b.error === "undeliverable"' in SNIPPET


def test_snippet_handles_400_429_502_distinctly():
    assert "x.s === 429" in SNIPPET and "T.err_many" in SNIPPET
    assert "x.s === 400" in SNIPPET and "T.err_check" in SNIPPET
    assert "T.err_fail" in SNIPPET


def test_snippet_moves_focus_on_open_and_restores_on_close():
    assert "activeElement" in SNIPPET
    assert ".focus()" in SNIPPET


def test_snippet_escape_only_closes_when_open():
    keydown = re.search(r'addEventListener\("keydown".*?\);', SNIPPET, re.S)
    assert keydown, "no keydown handler found"
    assert "Escape" in keydown.group(0)
    assert "hidden" in keydown.group(0)


def test_snippet_default_display_is_none_not_flex():
    # The popup carries the `hidden` attribute, but an inline `display:flex`
    # would out-rank the UA stylesheet's `[hidden]{display:none}` and the
    # popup would render open despite `hidden` being set. The inline style
    # must default to display:none so `hidden` actually hides it.
    div_open = SNIPPET.index('id="om-signup"')
    style_start = SNIPPET.index('style="', div_open)
    style_end = SNIPPET.index('"', style_start + len('style="'))
    style = SNIPPET[style_start:style_end]
    assert "display:none" in style
    assert "display:flex" not in style


def test_snippet_renders_without_js_hidden_by_default():
    assert re.search(r'<div id="om-signup"[^>]*\bhidden\b', SNIPPET)


@pytest.mark.skipif(shutil.which("node") is None, reason="node not on PATH")
def test_script_is_valid_javascript():
    blocks = re.findall(r"<script>(.*?)</script>", SNIPPET, re.S)
    assert len(blocks) >= 2, "expected the signup and the back-in-stock <script> blocks"
    for block in blocks:                                    # each block on its own: they are separate scripts on the page
        # strip Liquid so node sees what a product page would serve
        script = re.sub(r"\{%-?.*?-?%\}", "", block)
        script = re.sub(r"\{\{.*?\}\}", '"a-handle"', script)
        result = subprocess.run(
            ["node", "--check"],
            input=script,
            text=True,
            capture_output=True,
        )
        assert result.returncode == 0, result.stderr


def test_viewed_products_only_recorded_with_marketing_consent():
    rec = SNIPPET[SNIPPET.index('{%- if template.name == "product"'):SNIPPET.index("{%- endif %}")]
    assert "marketingAllowed()" in rec
    assert rec.index("marketingAllowed()") < rec.index("localStorage.setItem(VIEWED")


def test_signup_posts_viewed_products():
    assert "viewed: viewed()" in SNIPPET


def _strings():
    m = re.search(r'<script type="application/json" id="om-t">(.*?)</script>', SNIPPET, re.S)
    assert m, "no string table"
    return json.loads(m.group(1))


KEYS = {"close", "title", "text", "no", "placeholder", "submit", "fine", "done_title", "done_btn",
        "err_check", "typo", "err_many", "err_undeliverable", "err_fail", "done_new", "done_repeat", "done_used"}


def test_every_language_has_every_text():
    t = _strings()
    assert "en" in t
    for lang, texts in t.items():
        assert set(texts) == KEYS, lang
        for k in ("typo", "done_new", "done_repeat", "done_used"):
            assert ("{fix}" if k == "typo" else "{addr}") in texts[k], (lang, k)
        assert "free return" not in " ".join(texts.values()).lower()


def test_host_picks_store_key_and_language():
    head = SNIPPET[:SNIPPET.index("<style>")]
    for host in ("shop-ie.example.com", "shop-fi.example.com"):
        branch = re.search(r"om_host == '" + re.escape(host) + r"' and om_live contains '" + re.escape(host) + r"' -%\}(.*?)\{%- (?:elsif|endif)", head, re.S)
        assert branch, host
        assert f"assign om_store = '{host}'" in branch.group(1) and "assign om_lang = 'en'" in branch.group(1)
    # www. is stripped first, and the shop's own primary domain gets its own key
    assert "remove_first: 'www.'" in head and "om_host == shop.domain" in head
    assert "assign om_store = shop.domain" in head


def test_every_snippet_store_key_is_a_registered_store():
    import stores
    head = SNIPPET[:SNIPPET.index("<style>")]
    for listing in ("om_live", "om_track", "om_bis"):
        keys = re.search(r"assign " + listing + r" = '([^']*)' \| split: ','", head).group(1).split(",")
        for k in keys:
            stores.get(k)


def test_snippet_renders_only_on_our_hosts():
    assert "{%- if om_store != blank -%}" in SNIPPET
    assert SNIPPET.rstrip().endswith("{%- endif -%}")
    assert "var T = JSON.parse(document.getElementById(\"om-t\").textContent)[{{ om_lang | json }}]" in SNIPPET


def test_markets_only_render_when_listed_live():
    """One theme can serve several domains: a market switches on only by being in om_live."""
    head = SNIPPET[:SNIPPET.index("<style>")]
    m = re.search(r"assign om_live = '([^']*)' \| split: ','", head)
    assert m, "no live-market list"
    for host in ("shop-ie.example.com", "shop-fi.example.com"):
        assert f"om_host == '{host}' and om_live contains '{host}'" in head, host
    assert "om_host == shop.domain" in head     # the shop's own domain does not depend on the list


def test_endpoints_come_from_one_setting():
    assert "assign om_api = '" in SNIPPET
    for path in ("/signup", "/track", "/ident", "/notify"):
        assert '{{ om_api | json }} + "' + path + '"' in SNIPPET, path
    assert "cloudfunctions.net/mail-ingest/" not in SNIPPET


def test_viewed_products_are_recorded_by_id():
    # a product id is the same on every domain; a handle can be translated on a market domain
    assert "var h = String({{ product.id | json }})" in SNIPPET


def _track_block():
    return SNIPPET[SNIPPET.index("// Browse/cart reminders"):SNIPPET.index("})();")]


def test_identity_from_mail_click_fragment_and_signup():
    t = _track_block()
    assert re.search(r"om=\(\[A-Za-z0-9_\.-\]\+\)", t) and "location.hash" in t
    assert "history.replaceState" in t                        # the token leaves the address bar at once
    assert "x.b.vid" in SNIPPET and "pendingId = x.b.vid" in SNIPPET


def test_tracking_waits_for_marketing_consent():
    t = _track_block()
    assert "marketingAllowed()" in t and "visitorConsentCollected" in t
    assert t.index("if (!mktOk()) return;") < t.index("set(ID, pendingId)")   # not even the id is stored before consent


def test_tracking_posts_ids_not_handles_and_only_new_cart_lines():
    t = _track_block()
    assert 'product_id: String({{ product.id | json }})' in t and "product.handle" not in t
    assert '"/cart.js"' in t and "CART_SEEN" in t and "seen.indexOf(v) < 0" in t
    assert "keepalive: true" in t and '"visibilitychange"' in t
    assert '"/track"' in t


def test_tracking_is_switched_on_per_market():
    head = SNIPPET[:SNIPPET.index("<style>")]
    assert re.search(r"assign om_track = '[^']*' \| split: ','", head)
    t = _track_block()
    assert "{%- if om_track contains om_store -%}" in t      # events only on listed markets; identity is kept everywhere


def test_tracking_storage_goes_through_the_guarded_helpers():
    t = _track_block()
    assert "localStorage" not in t and "get(ID)" in t and "set(CART_SEEN" in t


def test_track_post_is_a_simple_cors_request_and_retries_for_late_consent_api():
    t = _track_block()
    assert '"Content-Type": "text/plain' in t and "application/json" not in t
    assert "setTimeout(track," in t                            # customerPrivacy can load after window.load


def test_logged_in_customer_is_identified_by_a_signed_email():
    """The theme signs "email|store" with a key kept in a shop metafield (never in this repo); the server answers with
    the visitor id. Only after marketing consent, only on markets with tracking on, once per customer per browser."""
    head = SNIPPET[:SNIPPET.index("<style>")]
    assert "customer.email | strip | downcase" in head
    assert "append: '|' | append: om_store | hmac_sha256: shop.metafields.om.ident_key.value" in head
    t = _track_block()
    assert '"/ident"' in t and '"Content-Type": "text/plain' in t
    assert t.index("if (!mktOk()) return;") < t.index("if (ident()) return;")
    assert "{%- if om_track contains om_store and om_ident_sig != blank" in t
    assert "get(CUST) === String({{ customer.id | json }})) return false" in t
    assert '"vid" in b' in t and "rm(ID)" in t            # not subscribed: drop an id another person left here


# --- back-in-stock button ---------------------------------------------------------------------------------------------
def _bis():
    return SNIPPET[SNIPPET.index("{%- if template.name == \"product\" and product and om_bis"):]


def test_bis_button_is_gated_by_market_and_never_available_again():
    assert re.search(r"assign om_bis = '[^']*' \| split: ','", SNIPPET)       # market hosts with our button
    assert "om_bis contains om_store" in SNIPPET and "'never available again'" in SNIPPET
    assert 'template.name == "product"' in _bis()


def test_bis_posts_notify_fields():
    b = _bis()
    for f in ('"/notify"', "variant_id:", "product_id:", "website:", "store: {{ om_store | json }}"):
        assert f in b, f


def test_bis_handles_available_and_errors():
    b = _bis()
    assert "r.status === 409" in b and "T.back" in b and "T.fail" in b and "T.ok" in b


def test_bis_prefills_logged_in_email_and_uses_the_visitor_id():
    b = _bis()
    assert "customer.email" in b and 'get("om_id")' in b.replace("get(ID)", 'get("om_id")')


def test_bis_form_does_not_inherit_the_theme_layout():
    # the theme makes every form a flex COLUMN (the email field's flex-basis became its height) and its .btn has no
    # background or border, so the block sets its own row layout and button looks
    css = SNIPPET[SNIPPET.index("#om-bis {"):SNIPPET.index('<div id="om-bis"')]
    assert re.search(r"#om-bis form \{[^}]*flex-direction: row", css)
    assert re.search(r"#om-bis input\[type=email\] \{[^}]*height: 44px", css)
    assert re.search(r"#om-bis \.om-bis-btn \{[^}]*border: 1px solid", css)
    assert re.search(r"#om-bis \.om-bis-submit \{[^}]*background: #1a1a1a", css)
    assert 'class="btn om-bis-submit"' in SNIPPET
    assert re.search(r"#om-bis \[hidden\] \{ display: none !important; \}", css)   # theme .btn display beats [hidden]


def _bis_strings():
    m = re.search(r'<script type="application/json" id="om-bis-t">(.*?)</script>', SNIPPET, re.S)
    assert m, "no back-in-stock string table"
    return json.loads(m.group(1))


def test_bis_texts_come_from_the_table_in_every_market_language():
    t = _bis_strings()
    assert "en" in t
    for lang, s in t.items():
        assert set(s) == set(t["en"]), lang
        assert "{title}" in s["intro"] and "{title}" in s["ok"], lang
    b = SNIPPET[SNIPPET.index('<div id="om-bis"'):]
    code = b[:b.index('<script type="application/json" id="om-bis-t">')] + b[b.index("</script>", b.index('id="om-bis-t"')):]
    for text in t["en"].values():
        assert text not in code, text                                     # no hard-coded text outside the table
    assert "[{{ om_lang | json }}]" in b[b.index('id="om-bis-t"'):]
