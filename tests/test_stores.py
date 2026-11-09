import pytest

import stores


def test_example_store_is_registered():
    s = stores.get("outlet.example.com")
    assert s.shop == "example-outlet.myshopify.com"
    assert s.locale == "en" and s.currency == "EUR"
    assert (s.welcome_amount, s.welcome_min_subtotal) == (10, 100)


def test_lookup_by_shop_domain():
    assert stores.by_shop("example-outlet.myshopify.com").key == "outlet.example.com"


def test_unknown_store_raises():
    with pytest.raises(KeyError):
        stores.get("evil.example")


def test_storefronts_of_one_shopify_store_route_by_country_then_host():
    shop = "example-shop.myshopify.com"
    assert stores.for_market(shop, "FI").key == "shop-fi.example.com"
    assert stores.for_market(shop, "NL").key == "shop.example.com"
    assert stores.for_market(shop, None, "www.shop-ie.example.com").key == "shop-ie.example.com"
    assert stores.for_market(shop, "AT") is None
    assert set(stores.same_shop(stores.get("shop-ie.example.com"))) == {"shop.example.com", "shop-ie.example.com", "shop-fi.example.com"}
