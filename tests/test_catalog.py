import dataclasses
from datetime import datetime, timezone

import catalog
import coupons
import stores

S = stores.get("outlet.example.com")


def node(h, price="249.00", status="ACTIVE", available=True, image=True, url="https://x"):
    return {"handle": h, "title": h.title(), "status": status, "onlineStoreUrl": url,
            "featuredMedia": {"preview": {"image": {"url": f"https://cdn.shopify.com/{h}.jpg?v=1"}}} if image else None,
            "variants": {"nodes": [{"availableForSale": available, "price": price}]}}


class Client:
    def __init__(self, viewed=(), top=(), cats=None):
        self.data = {"viewed": {"nodes": list(viewed)}, "top": {"products": {"nodes": list(top)}}, **(cats or {})}
        self.calls = []

    def gql(self, query, variables):
        self.calls.append((query, variables))
        return self.data


def test_viewed_first_in_viewed_order_then_bestsellers_over_minimum():
    c = Client(viewed=[node("b"), node("a", price="20.00")],
               top=[node("cheap", price="45.00"), node("a"), node("top1"), node("top2")])
    out = catalog.fetch(c, S, ["a", "b"], [], "fill")
    # viewed products show whatever their price; fillers must reach the code's €100 minimum
    assert [p["handle"] for p in out["products"]] == ["a", "b", "top1"]
    assert out["n_viewed"] == 2


def test_unbuyable_products_are_never_shown():
    top = [node("draft", status="DRAFT"), node("gone", available=False), node("noimg", image=False),
           node("hidden", url=None), node("ok")]
    out = catalog.fetch(Client(top=top), S, [], [], "fill")
    assert [p["handle"] for p in out["products"]] == ["ok"]


def test_viewed_mode_does_not_fill():
    out = catalog.fetch(Client(viewed=[node("a")], top=[node("t")]), S, ["a"], [], "viewed")
    assert [p["handle"] for p in out["products"]] == ["a"]


def test_bad_handles_never_reach_the_query():
    c = Client()
    catalog.fetch(c, S, ["ok-1", "x) OR (status:draft", 7], [], "fill")
    assert c.calls[0][1]["q"] == "handle:ok-1"
    catalog.fetch(c, S, [], [], "fill")
    assert c.calls[1][1]["q"] == "handle:__none__"


def test_category_image_falls_back_to_bestseller_photo():
    cats = {"c0": {"image": {"url": "https://cdn/c0.jpg"}, "products": {"nodes": []}},
            "c1": {"image": None, "products": {"nodes": [node("p")]}},
            "c2": None}
    c = Client(cats=cats)
    out = catalog.fetch(c, S, [], ["kitchen", "lighting", "gone"], None)
    assert out["category_images"] == {"kitchen": "https://cdn/c0.jpg?width=400",
                                      "lighting": "https://cdn.shopify.com/p.jpg?v=1&width=400"}
    assert c.calls[0][1] == {"q": "handle:__none__", "top": "best-sellers", "c0": "kitchen", "c1": "lighting", "c2": "gone"}
    assert out["products"] == []


def test_expiry_is_end_of_amsterdam_day_across_dst():
    # 25 Oct 2026 is the CEST→CET switch: 14 days from 20 Oct lands on 3 Nov, in winter time
    e = coupons.expiry(datetime(2026, 10, 20, 22, 30, tzinfo=timezone.utc), 14)   # 21 Oct 00:30 in Amsterdam
    assert e.astimezone(coupons.AMS).isoformat() == "2026-11-04T23:59:59+01:00"


def test_six_cards_fill_from_the_bestseller_pool():
    top = [node(f"t{i}") for i in range(10)]
    out = catalog.fetch(Client(viewed=[node("v")], top=top), S, ["v"], [], "fill", n=6)
    assert [p["handle"] for p in out["products"]] == ["v", "t0", "t1", "t2", "t3", "t4"]


def test_rating_parsed_from_judgeme_metafields():
    c = Client(cats={"shop": {"rating": {"value": "4.43"}, "reviews": {"value": "3474"}}})
    assert catalog.fetch(c, S, [], [], None)["rating"] == (4.43, 3474)
    assert 'namespace: "judgeme"' in c.calls[0][0]


def test_missing_or_garbled_rating_is_none():
    for shop in (None, {"rating": None, "reviews": {"value": "3"}}, {"rating": {"value": "x"}, "reviews": {"value": "3"}},
                 {"rating": {"value": "4.4"}, "reviews": {"value": "0"}}):
        assert catalog.fetch(Client(cats={"shop": shop}), S, [], [], None)["rating"] is None


def test_shopify_cdn_images_served_from_the_store_domain():
    real = "https://cdn.shopify.com/s/files/1/0123/4567/8901/files/product_01.jpg?v=1700000000"
    assert catalog._sized(real, 400, "outlet.example.com") == \
        "https://outlet.example.com/cdn/shop/files/product_01.jpg?v=1700000000&width=400"
    assert catalog._sized("https://other.example/x.jpg", 400, "outlet.example.com") == "https://other.example/x.jpg?width=400"


def test_fill_skips_colour_twins_of_a_product_already_shown():
    def titled(h, title):
        n = node(h); n["title"] = title; return n
    top = [titled("w", "Desk Lamp Classic 210 White"), titled("z", "Desk Lamp Classic 210 Black"),
           titled("l", "Electric Standing Desk Pro 3 Motors White"), titled("k", "Bar Stool S-80 Black")]
    viewed = [titled("kw", "Bar Stool S-80 White")]
    out = catalog.fetch(Client(viewed=viewed, top=top), S, ["kw"], [], "fill", n=4)
    assert [p["handle"] for p in out["products"]] == ["kw", "w", "l"]
    assert catalog._family("Floor Lamp Arc 60 Black") == catalog._family("Floor Lamp Arc 60 White")
    assert catalog._family("White") == "white"     # never strips a title down to nothing


# a market storefront in another language than the shop's own (catalog.SHOP_LOCALE): translated titles, market prices
SE = dataclasses.replace(stores.get("shop-fi.example.com"), key="shop-se.example.com", domain="shop-se.example.com", locale="sv",
                         currency="SEK", countries=("SE",))


def mnode(h, base="319.00", market=None, tr=None, **kw):
    """A product as the market query returns it: base price + contextual (market) price + translations."""
    n = node(h, price=base, **kw)
    if market:
        n["variants"]["nodes"][0]["contextualPricing"] = {"price": {"amount": market[0], "currencyCode": market[1]}}
    n["translations"] = [{"key": k, "value": v} for k, v in (tr or {}).items()]
    return n


def test_market_price_currency_and_swedish_title():
    c = Client(viewed=[mnode("product-x", market=("3590.00", "SEK"),
                             tr={"title": "Produkt X", "handle": "produkt-x"})])
    p = catalog.fetch(c, SE, ["product-x"], [], "viewed")["products"][0]
    assert (p["handle"], p["title"], p["price"], p["currency"], p["base_price"]) == ("product-x", "Produkt X", 3590.0, "SEK", 319.0)
    assert p["url"] == "https://shop-se.example.com/products/produkt-x"
    assert c.calls[0][1]["loc"] == "sv" and c.calls[0][1]["cc"] == "SE"


def test_product_without_translation_is_left_out_in_a_foreign_market():
    c = Client(viewed=[mnode("h", base="150.00", market=("1690.00", "SEK"), tr={})])
    assert catalog.fetch(c, SE, ["h"], [], "viewed")["products"] == []


def test_filler_threshold_uses_the_base_price():   # the €100 minimum is in shop currency
    cheap = mnode("a", base="99.00", market=("1150.00", "SEK"), tr={"title": "A sv", "handle": "a-sv"})
    ok = mnode("b", base="100.00", market=("1100.00", "SEK"), tr={"title": "B sv", "handle": "b-sv"})
    out = catalog.fetch(Client(top=[cheap, ok]), SE, [], [], "fill")
    assert [p["handle"] for p in out["products"]] == ["b"]


def test_cheapest_buyable_variant_sets_the_market_price():
    n = mnode("v", base="200.00", market=("2300.00", "SEK"), tr={"title": "V", "handle": "v"})
    n["variants"]["nodes"] += [{"availableForSale": True, "price": "150.00", "contextualPricing": {"price": {"amount": "1700.00", "currencyCode": "SEK"}}},
                               {"availableForSale": False, "price": "120.00", "contextualPricing": {"price": {"amount": "1400.00", "currencyCode": "SEK"}}}]
    p = catalog.fetch(Client(viewed=[n]), SE, ["v"], [], "viewed")["products"][0]
    assert (p["price"], p["base_price"]) == (1700.0, 150.0)


def test_shop_language_storefronts_query_and_cards_unchanged():
    nl = stores.get("shop.example.com")
    c = Client(viewed=[node("h", price="319.00")])
    p = catalog.fetch(c, nl, ["h"], [], "viewed")["products"][0]
    assert (p["title"], p["price"], p["currency"], p["url"]) == ("H", 319.0, "EUR", "https://shop.example.com/products/h")
    assert "translations" not in c.calls[0][0] and "contextualPricing" not in c.calls[0][0]
    p = catalog.fetch(Client(viewed=[node("k")]), S, ["k"], [], "viewed")["products"][0]
    assert (p["currency"], p["price"]) == ("EUR", 249.0)


def test_viewed_products_are_found_by_id_on_a_translated_storefront():
    """On shop-se.example.com the visitor saw /products/produkt-x; the popup stores the product id, which works in
    every language (the Admin search only knows the base handle)."""
    n = mnode("product-x", market=("3590.00", "SEK"), tr={"title": "Produkt X", "handle": "produkt-x"})
    n["legacyResourceId"] = "8123"
    c = Client(viewed=[n])
    out = catalog.fetch(c, SE, ["8123"], [], "viewed")
    assert c.calls[0][1]["q"] == "id:8123"
    assert [p["title"] for p in out["products"]] == ["Produkt X"] and out["n_viewed"] == 1


def test_ids_and_legacy_handles_mix_in_viewed_order():
    a, b = node("old-handle"), node("new")
    b["legacyResourceId"] = "77"
    c = Client(viewed=[a, b])
    out = catalog.fetch(c, S, ["77", "old-handle"], [], "viewed")
    assert c.calls[0][1]["q"] == "id:77 OR handle:old-handle"
    assert [p["handle"] for p in out["products"]] == ["new", "old-handle"] and out["n_viewed"] == 2


# --- "fbt": mail 2 of post_purchase — what is often bought with the ordered products ---
SP = stores.get("shop.example.com")


def pnode(i, fbt=(), **kw):
    n = node(f"p{i}", **kw)
    n["legacyResourceId"] = str(i)
    n["fbt"] = {"references": {"nodes": [pnode(j) for j in fbt]}} if fbt else None
    return n


def test_fbt_mode_uses_the_ordered_products_lists_and_skips_what_they_bought():
    # ordered 1 (list 1, 5, 6) and 2 (list 7): round-robin across the lists, never an ordered product, topped up
    c = Client(viewed=[pnode(1, fbt=(1, 5, 6)), pnode(2, fbt=(7,))], top=[pnode(2), pnode(5), pnode(9, price="20.00")])
    out = catalog.fetch(c, SP, ["1", "2"], [], "fbt", n=4)
    assert [p["handle"] for p in out["products"]] == ["p5", "p7", "p6", "p9"]
    assert "frequently_bought_together" in c.calls[0][0]


def test_fbt_mode_without_lists_is_all_bestsellers_minus_the_order():
    c = Client(viewed=[pnode(1)], top=[pnode(1), pnode(3), pnode(4)])
    assert [p["handle"] for p in catalog.fetch(c, SP, ["1"], [], "fbt", n=4)["products"]] == ["p3", "p4"]


def test_fbt_list_of_an_ordered_product_that_is_no_longer_sold_still_counts():
    c = Client(viewed=[pnode(1, fbt=(5,), status="ARCHIVED")], top=[])
    assert [p["handle"] for p in catalog.fetch(c, SP, ["1"], [], "fbt", n=4)["products"]] == ["p5"]


def test_fbt_never_shows_any_ordered_product_even_past_the_query_cap():
    # 8 ordered products: only 6 are queried, but none of the 8 may come back via a list or the bestsellers
    ordered = [str(i) for i in range(1, 9)]
    c = Client(viewed=[pnode(1, fbt=(8, 20))], top=[pnode(7), pnode(21)])
    out = catalog.fetch(c, SP, ordered, [], "fbt", n=4)
    assert [p["handle"] for p in out["products"]] == ["p20", "p21"]


def test_fbt_skips_colour_twins_of_what_they_bought():
    def titled(i, title, **kw):
        n = pnode(i, **kw); n["title"] = title; return n
    c = Client(viewed=[titled(1, "Desk Lamp Basic 210 White", fbt=())], top=[titled(2, "Desk Lamp Basic 210 Black"), titled(3, "Stool 6001")])
    c.data["viewed"]["nodes"][0]["fbt"] = {"references": {"nodes": []}}
    assert [p["title"] for p in catalog.fetch(c, SP, ["1"], [], "fbt", n=4)["products"]] == ["Stool 6001"]


# --- variants (back-in-stock) -----------------------------------------------------------------------------------------
NL = stores.get("shop.example.com")


def vnode(vid, pid="1", available=True, price="99.00", status="ACTIVE", ptype="furniture", url="https://x", image=True,
          market_price=None, tr=None):
    product = {"legacyResourceId": pid, "handle": f"p{pid}", "title": f"Chair {pid}", "status": status, "onlineStoreUrl": url,
               "productType": ptype, "translations": [{"key": k, "value": v} for k, v in (tr or {}).items()],
               "featuredMedia": {"preview": {"image": {"url": f"https://cdn.shopify.com/s/files/1/0123/4567/8901/files/p{pid}.jpg"}}} if image else None}
    v = {"legacyResourceId": vid, "title": "White", "availableForSale": available, "price": price, "image": None, "product": product}
    if market_price:
        v["contextualPricing"] = {"price": {"amount": market_price, "currencyCode": "SEK"}}
    return v


class VClient:
    def __init__(self, nodes):
        self.nodes, self.calls = nodes, []

    def gql(self, query, variables):
        self.calls.append((query, variables))
        want = {i.rsplit("/", 1)[-1] for i in variables["ids"]}
        return {"nodes": [n if n and n["legacyResourceId"] in want else None for n in self.nodes]}


def test_variants_card_for_a_buyable_variant():
    out = catalog.variants(VClient([vnode("11", price="129.00")]), NL, ["11"])
    card = out["cards"]["11"]
    assert card["url"] == "https://shop.example.com/products/p1?variant=11"
    assert card["price"] == 129.0 and card["variant_id"] == "11" and card["title"] == "Chair 1"
    assert out["gone"] == set()


def test_sold_out_variant_is_none_even_if_a_sibling_is_buyable():
    out = catalog.variants(VClient([vnode("11", available=False), vnode("12")]), NL, ["11", "12"])
    assert out["cards"]["11"] is None and out["cards"]["12"] is not None and out["gone"] == set()


def test_never_available_again_and_archived_are_gone():
    nodes = [vnode("11", ptype="never available again", available=False), vnode("12", pid="2", status="ARCHIVED"),
             vnode("13", pid="3", url=None)]
    out = catalog.variants(VClient(nodes), NL, ["11", "12", "13", "14"])
    assert out["gone"] == {"11", "12", "13", "14"}                    # 14: the variant no longer exists
    assert all(out["cards"][v] is None for v in ("11", "12", "13", "14"))


def test_market_store_needs_translated_title_and_market_price():
    se = SE
    out = catalog.variants(VClient([vnode("11", market_price="1495.00"),
                                    vnode("12", pid="2", market_price="999.00", tr={"title": "Stol 2", "handle": "stol-2"})]), se, ["11", "12"])
    assert out["cards"]["11"] is None
    assert out["cards"]["12"]["url"] == "https://shop-se.example.com/products/stol-2?variant=12" and out["cards"]["12"]["currency"] == "SEK"


def test_variants_query_batches_ids():
    c = VClient([])
    catalog.variants(c, NL, [str(i) for i in range(150)])
    assert [len(v["ids"]) for _, v in c.calls] == [100, 50]
    assert c.calls[0][1]["ids"][0] == "gid://shopify/ProductVariant/0"


def test_subfolder_market_cards_link_into_the_subfolder_and_ask_in_its_locale():
    pt = dataclasses.replace(stores.get("shop.example.com"), key="shop.example.com/pt-pt", path_prefix="/pt-pt",
                             locale="pt-PT", countries=("PT",))
    c = Client(viewed=[mnode("product-x", market=("339.00", "EUR"),
                             tr={"title": "Marquesa X", "handle": "marquesa-x"})])
    p = catalog.fetch(c, pt, ["product-x"], [], "viewed")["products"][0]
    assert p["url"] == "https://shop.example.com/pt-pt/products/marquesa-x"
    assert "/pt-pt" not in p["image"]   # images stay on the bare domain
    assert c.calls[0][1]["loc"] == "pt-PT" and c.calls[0][1]["cc"] == "PT"
