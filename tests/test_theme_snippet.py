import json
import pathlib
import re
import shutil
import subprocess

import pytest

SNIPPET = (pathlib.Path(__file__).parent.parent / "theme" / "lsp-signup.liquid").read_text()


def test_snippet_posts_the_fields_signup_expects():
    for f in ("store: {{ lsp_store | json }}", "source: source", 'source = src || "popup"', "website:", "email:"):
        assert f in SNIPPET


def test_footer_newsletter_form_signs_up_through_us():
    # otherwise footer signups reach another tool's newsletter welcome via a Shopify sync and get a second code
    assert 'form[action^="/contact"]' in SNIPPET and 'indexOf("newsletter")' in SNIPPET
    assert 'open(2, "footer")' in SNIPPET and "e.preventDefault()" in SNIPPET


def test_snippet_storage_is_guarded():
    assert SNIPPET.count("try {") >= 2


def test_snippet_never_shows_the_code():
    # the code only travels by email (keeps made-up addresses off the list)
    assert "x.b.code" not in SNIPPET.replace("x.b.code_valid", "")
    assert "check your inbox" in SNIPPET.lower() and "spam" in SNIPPET


def test_snippet_has_the_two_step_form_and_teaser():
    for s in ('data-step="1"', 'data-step="2"', 'data-step="3"', 'id="lsp-yes"', 'id="lsp-no"', 'id="lsp-teaser"'):
        assert s in SNIPPET, s


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
    div_open = SNIPPET.index('id="lsp-signup"')
    style_start = SNIPPET.index('style="', div_open)
    style_end = SNIPPET.index('"', style_start + len('style="'))
    style = SNIPPET[style_start:style_end]
    assert "display:none" in style
    assert "display:flex" not in style


def test_snippet_renders_without_js_hidden_by_default():
    assert re.search(r'<div id="lsp-signup"[^>]*\bhidden\b', SNIPPET)


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
    m = re.search(r'<script type="application/json" id="lsp-t">(.*?)</script>', SNIPPET, re.S)
    assert m, "no string table"
    return json.loads(m.group(1))


KEYS = {"close", "title", "yes", "no", "h2", "placeholder", "submit", "fine", "done_title", "done_btn", "teaser", "teaser_hide",
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
        branch = re.search(r"lsp_host == '" + re.escape(host) + r"' and lsp_live contains '" + re.escape(host) + r"' -%\}(.*?)\{%- (?:elsif|endif)", head, re.S)
        assert branch, host
        assert f"assign lsp_store = '{host}'" in branch.group(1) and "assign lsp_lang = 'en'" in branch.group(1)
    # www. is stripped first, and the shop's own primary domain gets its own key
    assert "remove_first: 'www.'" in head and "lsp_host == shop.domain" in head
    assert "assign lsp_store = shop.domain" in head


def test_every_snippet_store_key_is_a_registered_store():
    import stores
    head = SNIPPET[:SNIPPET.index("<style>")]
    for listing in ("lsp_live", "lsp_track", "lsp_bis"):
        keys = re.search(r"assign " + listing + r" = '([^']*)' \| split: ','", head).group(1).split(",")
        for k in keys:
            stores.get(k)


def test_snippet_renders_only_on_our_hosts():
    assert "{%- if lsp_store != blank -%}" in SNIPPET
    assert SNIPPET.rstrip().endswith("{%- endif -%}")
    assert "var T = JSON.parse(document.getElementById(\"lsp-t\").textContent)[{{ lsp_lang | json }}]" in SNIPPET


def test_markets_only_render_when_listed_live():
    """One theme can serve several domains: a market switches on only by being in lsp_live."""
    head = SNIPPET[:SNIPPET.index("<style>")]
    m = re.search(r"assign lsp_live = '([^']*)' \| split: ','", head)
    assert m, "no live-market list"
    for host in ("shop-ie.example.com", "shop-fi.example.com"):
        assert f"lsp_host == '{host}' and lsp_live contains '{host}'" in head, host
    assert "lsp_host == shop.domain" in head     # the shop's own domain does not depend on the list


def test_endpoints_come_from_one_setting():
    assert "assign lsp_api = '" in SNIPPET
    for path in ("/signup", "/track", "/ident", "/notify"):
        assert '{{ lsp_api | json }} + "' + path + '"' in SNIPPET, path
    assert "cloudfunctions.net/mail-ingest/" not in SNIPPET


def test_viewed_products_are_recorded_by_id():
    # a product id is the same on every domain; a handle can be translated on a market domain
    assert "var h = String({{ product.id | json }})" in SNIPPET


def _track_block():
    return SNIPPET[SNIPPET.index("// Browse/cart reminders"):SNIPPET.index("})();")]


def test_identity_from_mail_click_fragment_and_signup():
    t = _track_block()
    assert re.search(r"lsp=\(\[A-Za-z0-9_\.-\]\+\)", t) and "location.hash" in t
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
    assert re.search(r"assign lsp_track = '[^']*' \| split: ','", head)
    t = _track_block()
    assert "{%- if lsp_track contains lsp_store -%}" in t      # events only on listed markets; identity is kept everywhere


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
    assert "append: '|' | append: lsp_store | hmac_sha256: shop.metafields.lsp.ident_key.value" in head
    t = _track_block()
    assert '"/ident"' in t and '"Content-Type": "text/plain' in t
    assert t.index("if (!mktOk()) return;") < t.index("if (ident()) return;")
    assert "{%- if lsp_track contains lsp_store and lsp_ident_sig != blank" in t
    assert "get(CUST) === String({{ customer.id | json }})) return false" in t
    assert '"vid" in b' in t and "rm(ID)" in t            # not subscribed: drop an id another person left here


# --- back-in-stock button ---------------------------------------------------------------------------------------------
def _bis():
    return SNIPPET[SNIPPET.index("{%- if template.name == \"product\" and product and lsp_bis"):]


def test_bis_button_is_gated_by_market_and_never_available_again():
    assert re.search(r"assign lsp_bis = '[^']*' \| split: ','", SNIPPET)       # market hosts with our button
    assert "lsp_bis contains lsp_store" in SNIPPET and "'never available again'" in SNIPPET
    assert 'template.name == "product"' in _bis()


def test_bis_posts_notify_fields():
    b = _bis()
    for f in ('"/notify"', "variant_id:", "product_id:", "website:", "store: {{ lsp_store | json }}"):
        assert f in b, f


def test_bis_handles_available_and_errors():
    b = _bis()
    assert "r.status === 409" in b and "T.back" in b and "T.fail" in b and "T.ok" in b


def test_bis_prefills_logged_in_email_and_uses_the_visitor_id():
    b = _bis()
    assert "customer.email" in b and 'get("lsp_id")' in b.replace("get(ID)", 'get("lsp_id")')


def test_bis_form_does_not_inherit_the_theme_layout():
    # the theme makes every form a flex COLUMN (the email field's flex-basis became its height) and its .btn has no
    # background or border, so the block sets its own row layout and button looks
    css = SNIPPET[SNIPPET.index("#lsp-bis {"):SNIPPET.index('<div id="lsp-bis"')]
    assert re.search(r"#lsp-bis form \{[^}]*flex-direction: row", css)
    assert re.search(r"#lsp-bis input\[type=email\] \{[^}]*height: 44px", css)
    assert re.search(r"#lsp-bis \.lsp-bis-btn \{[^}]*border: 1px solid", css)
    assert re.search(r"#lsp-bis \.lsp-bis-submit \{[^}]*background: #1a1a1a", css)
    assert 'class="btn lsp-bis-submit"' in SNIPPET
    assert re.search(r"#lsp-bis \[hidden\] \{ display: none !important; \}", css)   # theme .btn display beats [hidden]


def _bis_strings():
    m = re.search(r'<script type="application/json" id="lsp-bis-t">(.*?)</script>', SNIPPET, re.S)
    assert m, "no back-in-stock string table"
    return json.loads(m.group(1))


def test_bis_texts_come_from_the_table_in_every_market_language():
    t = _bis_strings()
    assert "en" in t
    for lang, s in t.items():
        assert set(s) == set(t["en"]), lang
        assert "{title}" in s["intro"] and "{title}" in s["ok"], lang
    b = SNIPPET[SNIPPET.index('<div id="lsp-bis"'):]
    code = b[:b.index('<script type="application/json" id="lsp-bis-t">')] + b[b.index("</script>", b.index('id="lsp-bis-t"')):]
    for text in t["en"].values():
        assert text not in code, text                                     # no hard-coded text outside the table
    assert "[{{ lsp_lang | json }}]" in b[b.index('id="lsp-bis-t"'):]
