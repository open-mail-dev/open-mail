"""Live shop data for mails, fetched at send time so a price, photo, sold-out state or review score is never stale.

Callers treat any failure here as "no live blocks": a Shopify hiccup must never hold up a mail."""
import re

HANDLE = re.compile(r"^[a-z0-9][a-z0-9-]{0,254}$")
MAX_VIEWED = 6

_PRODUCT = (
    "legacyResourceId handle title status onlineStoreUrl featuredMedia { preview { image { url } } } "
    "variants(first: 20) { nodes { availableForSale price } }"
)
# A market storefront (a storefront in another language than the shop's own, SHOP_LOCALE): that market's price and
# currency, and the title + URL handle in its language. Storefronts in the shop's own language keep the plain query.
_PRODUCT_MARKET = (
    "legacyResourceId handle title status onlineStoreUrl featuredMedia { preview { image { url } } } translations(locale: $loc) { key value } "
    "variants(first: 20) { nodes { availableForSale price contextualPricing(context: { country: $cc }) { price { amount currencyCode } } } }"
)
SHOP_CURRENCY = "EUR"   # the Shopify store's own currency and language
SHOP_LOCALE = "en"


def _market(store) -> tuple[str, str] | None:
    """(locale, country) when cards must be localised, else None."""
    return (store.locale, store.countries[0]) if store.countries and store.locale != SHOP_LOCALE else None
_COLLECTION_IMAGE = "image { url } products(first: 1, sortKey: BEST_SELLING) { nodes { featuredMedia { preview { image { url } } } } }"


def _query(n_categories: int, market: bool = False, fbt: bool = False) -> str:
    # 80 best-sellers: a top 30 can hold only a few buyable items above the code's minimum (accessories rank high);
    # query cost stays ~60 points (measured 2026-10-08)
    params = "".join(f", $c{i}: String!" for i in range(n_categories)) + (", $loc: String!, $cc: CountryCode!" if market else "")
    product = _PRODUCT_MARKET if market else _PRODUCT
    cats = "".join(
        f" c{i}: collectionByIdentifier(identifier: {{handle: $c{i}}}) {{ {_COLLECTION_IMAGE} }}"
        for i in range(n_categories)
    )
    # fbt: each ordered product's "frequently bought together" list (product metafield custom.frequently_bought_together, a list of product references filled by any job you like)
    lists = (f' fbt: metafield(namespace: "custom", key: "frequently_bought_together")'
             f" {{ references(first: 10) {{ nodes {{ ... on Product {{ {product} }} }} }} }}") if fbt else ""
    return (
        f"query($q: String!, $top: String!{params}) {{"
        f" viewed: products(first: {MAX_VIEWED}, query: $q) {{ nodes {{ {product}{lists} }} }}"
        f" top: collectionByIdentifier(identifier: {{handle: $top}}) {{"
        f" products(first: 80, sortKey: BEST_SELLING) {{ nodes {{ {product} }} }} }}"
        f"{cats}"
        # Judge.me keeps the shop-wide score in these metafields; the storefront header renders the same two
        ' shop { rating: metafield(namespace: "judgeme", key: "all_reviews_rating") { value }'
        ' reviews: metafield(namespace: "judgeme", key: "all_reviews_count") { value } } }'
    )


def _image(node) -> str | None:
    return ((((node or {}).get("featuredMedia") or {}).get("preview") or {}).get("image") or {}).get("url")


_SHOP_CDN = re.compile(r"^https://cdn\.shopify\.com/s/files/\d+/\d+/\d+/\d+/")


def _sized(url: str, width: int, domain: str) -> str:
    # The storefront serves the same file at /cdn/shop/…: SpamAssassin scores a cdn.shopify.com image in mail
    # not sent by Shopify (SHOPIFY_IMG_NOT_RCVD_SFY, +1.0), and our own domain also matches the sender.
    url = _SHOP_CDN.sub(f"https://{domain}/cdn/shop/", url)
    return f"{url}{'&' if '?' in url else '?'}width={width}"


# Colour variants are often separate products ("… Classic 210 White" / "… Classic 210 Black"): one per model in a row.
_COLOURS = {
    "wit", "zwart", "grijs", "antraciet", "roze", "rood", "blauw", "groen", "geel", "beige", "bruin", "creme", "crème",
    "zilver", "goud", "paars", "cappuccino", "taupe", "oranje", "turquoise", "mint", "lila",
    "white", "black", "grey", "gray", "pink", "red", "blue", "green", "silver", "gold",
    "weiß", "weiss", "schwarz", "grau", "rosa", "rot", "blau", "grün", "braun", "silber",
    "blanc", "noir", "gris", "rose", "bleu", "vert", "blanco", "negro", "rosa", "bianco", "nero", "grigio",
    "vit", "svart", "grå", "hvid", "sort", "valkoinen", "musta", "harmaa",
}


def _family(title: str) -> str:
    words = title.lower().split()
    while len(words) > 1 and words[-1].strip(",-/") in _COLOURS:
        words.pop()
    return " ".join(words)


def _product(node, domain: str, market: bool = False, prefix: str = "") -> dict | None:
    """None unless the product is live on the storefront, has a photo and a buyable variant — and, on a market
    storefront, a title in that market's language (never the shop-language title in a translated mail).
    price/currency = what that storefront shows (cheapest buyable variant); base_price = the same variant in shop
    currency, which the welcome code's minimum is measured in."""
    if not node or node.get("status") != "ACTIVE" or not node.get("onlineStoreUrl"):
        return None
    img = _image(node)
    if market:
        tr = {t["key"]: t["value"] for t in node.get("translations") or []}
        title, slug = tr.get("title"), tr.get("handle") or node["handle"]
        offers = [(float(v["contextualPricing"]["price"]["amount"]), v["contextualPricing"]["price"]["currencyCode"], float(v["price"]))
                  for v in node["variants"]["nodes"] if v.get("availableForSale") and v.get("contextualPricing")]
    else:
        title, slug = node["title"], node["handle"]
        offers = [(float(v["price"]), SHOP_CURRENCY, float(v["price"])) for v in node["variants"]["nodes"] if v.get("availableForSale")]
    if not offers or not img or not title:
        return None
    price, currency, base = min(offers)
    return {"id": node.get("legacyResourceId"), "handle": node["handle"], "title": title, "url": f"https://{domain}{prefix}/products/{slug}",
            "image": _sized(img, 400, domain), "price": price, "currency": currency, "base_price": base}


def fetch(client, store, viewed: list, categories: list, want_products: str | None, n: int = 3) -> dict:
    """want_products: None (no product row), "viewed" (only what they looked at) or
    "fill" (what they looked at, topped up with bestsellers >= the code's minimum order)."""
    # entries are product ids (popup since 2026-11: same on every domain) or handles (older browsers)
    every = [h for h in viewed if isinstance(h, str) and HANDLE.match(h)]    # fbt: all ordered products stay excluded
    viewed = every[:MAX_VIEWED]
    q = " OR ".join(f"id:{h}" if h.isdigit() else f"handle:{h}" for h in viewed) or "handle:__none__"
    variables = {"q": q, "top": store.bestseller_collection}
    variables.update({f"c{i}": h for i, h in enumerate(categories)})
    market = _market(store)
    if market:
        variables.update({"loc": market[0], "cc": market[1]})
    data = client.gql(_query(len(categories), bool(market), want_products == "fbt"), variables)

    products = []
    if want_products == "fbt":
        products = _fbt(data, store, every, n, bool(market))
    elif want_products:
        live = {}
        for p in (_product(x, store.domain, bool(market), store.path_prefix) for x in data["viewed"]["nodes"]):
            if p:
                live[p["handle"]] = p
                if p.get("id"):
                    live[p["id"]] = p
        products = [live[h] for h in viewed if h in live][:n]
        if want_products == "fill":
            seen = {p["handle"] for p in products}
            families = {_family(p["title"]) for p in products}
            for node in ((data.get("top") or {}).get("products") or {}).get("nodes", []):
                if len(products) >= n:
                    break
                p = _product(node, store.domain, bool(market), store.path_prefix)
                if p and p["handle"] not in seen and _family(p["title"]) not in families and p["base_price"] >= store.welcome_min_subtotal:
                    products.append(p)
                    seen.add(p["handle"])
                    families.add(_family(p["title"]))
    n_viewed = sum(1 for p in products if p["handle"] in viewed or p.get("id") in viewed)

    images = {}
    for i, handle in enumerate(categories):
        c = data.get(f"c{i}") or {}
        first = ((c.get("products") or {}).get("nodes") or [None])[0]
        url = (c.get("image") or {}).get("url") or _image(first)
        if url:
            images[handle] = _sized(url, 400, store.domain)
    return {"products": products, "n_viewed": n_viewed, "category_images": images, "rating": _rating(data.get("shop"))}


def _fbt(data: dict, store, ordered: list, n: int, market: bool) -> list:
    """What is often bought with the ordered products: their lists round-robin (first of each, then second of each…),
    never an ordered product, one per model, topped up with bestsellers. An ordered product no longer for sale still
    lends its list."""
    nodes = {x.get("legacyResourceId"): x for x in data["viewed"]["nodes"]}
    lists = [[r for r in (((nodes.get(i) or {}).get("fbt") or {}).get("references") or {}).get("nodes") or []
              if r and r.get("legacyResourceId") not in ordered] for i in ordered]
    candidates = [lst[k] for k in range(max(map(len, lists), default=0)) for lst in lists if k < len(lst)]
    candidates += ((data.get("top") or {}).get("products") or {}).get("nodes", [])
    bought = set(ordered) | {x["handle"] for x in nodes.values() if x}
    # one per model, counting what they bought: no "Chair X Black" after "Chair X White" (shop and market titles)
    families = {_family(t) for x in nodes.values() if x
                for t in (x.get("title"), (_product(x, store.domain, market, store.path_prefix) or {}).get("title")) if t}
    out = []
    for node in candidates:
        if len(out) >= n:
            break
        p = _product(node, store.domain, market, store.path_prefix)
        if p and p["id"] not in bought and p["handle"] not in bought and _family(p["title"]) not in families:
            out.append(p)
            bought.add(p["handle"])
            families.add(_family(p["title"]))
    return out


# --- one variant per back-in-stock request ---------------------------------------------------------------------------
NEVER_AGAIN = "never available again"
_VARIANT = (
    "legacyResourceId title availableForSale price image { url }%CTX% product { legacyResourceId handle title status "
    "onlineStoreUrl productType featuredMedia { preview { image { url } } }%TR% }"
)


def _variant_query(market: bool) -> str:
    fields = (_VARIANT.replace("%CTX%", " contextualPricing(context: { country: $cc }) { price { amount currencyCode } }" if market else "")
              .replace("%TR%", " translations(locale: $loc) { key value }" if market else ""))
    params = ", $loc: String!, $cc: CountryCode!" if market else ""
    return f"query($ids: [ID!]!{params}) {{ nodes(ids: $ids) {{ ... on ProductVariant {{ {fields} }} }} }}"


def variants(client, store, variant_ids: list[str]) -> dict:
    """Back-in-stock: per variant, a card when it is buyable on this storefront right now (else None), and the
    variants that will not come back (gone: missing, product not live, or NEVER_AGAIN). The trigger and the mail
    both use this, so they always agree on "buyable". Shopify errors propagate."""
    market = _market(store)
    cards, gone = {v: None for v in variant_ids}, set()
    found = {}
    for i in range(0, len(variant_ids), 100):
        variables = {"ids": [f"gid://shopify/ProductVariant/{v}" for v in variant_ids[i:i + 100]]}
        if market:
            variables.update({"loc": market[0], "cc": market[1]})
        for n in client.gql(_variant_query(bool(market)), variables)["nodes"]:
            if n and n.get("legacyResourceId"):
                found[n["legacyResourceId"]] = n
    for vid in variant_ids:
        n = found.get(vid)
        p = (n or {}).get("product") or {}
        if not n or p.get("status") != "ACTIVE" or not p.get("onlineStoreUrl") or p.get("productType") == NEVER_AGAIN:
            gone.add(vid)
            continue
        if not n.get("availableForSale"):
            continue
        img = ((n.get("image") or {}).get("url")) or _image(p)
        if market:
            tr = {t["key"]: t["value"] for t in p.get("translations") or []}
            title, slug = tr.get("title"), tr.get("handle") or p["handle"]
            money = (n.get("contextualPricing") or {}).get("price") or {}
            price, currency = (float(money["amount"]), money["currencyCode"]) if money.get("amount") else (None, None)
        else:
            title, slug, price, currency = p.get("title"), p["handle"], float(n["price"]), SHOP_CURRENCY
        if not img or not title or price is None:
            continue
        cards[vid] = {"id": p.get("legacyResourceId"), "handle": p["handle"], "title": title, "image": _sized(img, 400, store.domain),
                      "url": f"{store.base_url}/products/{slug}?variant={vid}", "price": price, "currency": currency,
                      "base_price": float(n["price"]), "variant_id": vid, "variant_title": n.get("title") or ""}
    return {"cards": cards, "gone": gone}


def _rating(shop) -> tuple[float, int] | None:
    """(average, count), or None when the metafields are missing or garbled — the mail then just leaves it out."""
    try:
        avg, count = float(shop["rating"]["value"]), int(shop["reviews"]["value"])
    except (KeyError, TypeError, ValueError):
        return None
    return (avg, count) if 0 < avg <= 5 and count > 0 else None
