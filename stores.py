"""Store registry: one entry per storefront that sends mail through this system. Edit _STORES for your own shops."""
from dataclasses import dataclass


@dataclass(frozen=True)
class Store:
    key: str
    shop: str
    domain: str
    brand: str
    locale: str
    currency: str
    from_email: str
    from_name: str
    reply_to: str
    sending_domain: str
    welcome_pool: str
    welcome_amount: int
    welcome_min_subtotal: int
    # Dev Dashboard app credentials (env vars); since 2026 Shopify issues no static Admin API token for new
    # apps — shopify.access_token() trades these for a 24-hour token (client-credentials grant)
    client_id_env: str
    client_secret_env: str   # also the key Shopify signs this app's webhooks with
    # SES configuration set (bounce/complaint events). Explicit per storefront, so several storefronts of one
    # Shopify store can each keep their own sending reputation.
    configuration_set: str
    bestseller_collection: str = ""   # collection whose best-sellers fill a mail's product row
    # several storefronts can share ONE Shopify store (shop.example.com, shop-ie.example.com and shop-fi.example.com): orders and checkouts belong to the
    # storefront of their shipping country, else of their domain
    countries: tuple = ()
    welcome: bool = True              # runs the popup welcome on this system (False: signups for it are refused)
    abandoned_checkout: bool = False  # our poller sends its abandoned-checkout reminders
    # True: only from this storefront's own go-live moment (config.AC_SINCE_BY_STORE), not the global MAIL_AC_SINCE —
    # for a storefront added later, whose checkouts another tool was still reminding in between
    ac_own_start: bool = False
    welcome_valid_days: int = 14      # how long a welcome code stays valid
    code_prefix: str = "WELCOME"      # welcome codes read WELCOME-XXXX-XXXX
    site_flows: bool = False          # accepts /track (product views, cart adds) → browse_abandonment + cart_reminder
    post_purchase: bool = False       # orders → post_purchase series (switched on per market by MAIL_PP_LIVE)
    back_in_stock: bool = False       # "Notify me" requests + mail (switched on per market by MAIL_BIS_LIVE)
    winback: bool = False             # last order 120 days old → winback mail (switched on per market by MAIL_WB_LIVE)
    # a market served from a subfolder of another storefront's domain (shop.example.com/de-de): every link in its mails
    # starts with it, while CORS origins, image URLs and the click-redirect host check keep using the bare domain
    path_prefix: str = ""
    # the copy/<dir>/<locale>/ folder this storefront's mails are written in; "" = its own key. Lets storefronts that
    # speak the same language share one copy set.
    copy_key: str = ""

    @property
    def base_url(self) -> str:
        return f"https://{self.domain}{self.path_prefix}"


def _shop_store(key: str, countries: tuple, **kw) -> Store:
    """A storefront of the example multi-market Shopify store (one Shopify store, several domains)."""
    return Store(
        key=key,
        shop="example-shop.myshopify.com",
        domain=key,
        brand="Example Shop",
        locale="en",
        currency="EUR",
        from_email=f"hello@mail.{key}",
        from_name="Example Shop",
        reply_to=f"hello@{key}",
        sending_domain=f"mail.{key}",
        welcome_pool="welcome10",
        welcome_amount=10,
        welcome_min_subtotal=100,
        client_id_env="SHOPIFY_CLIENT_ID_EXAMPLE_SHOP",
        client_secret_env="SHOPIFY_CLIENT_SECRET_EXAMPLE_SHOP",
        configuration_set="mail-" + key.replace(".", "-"),
        bestseller_collection="best-sellers",
        countries=countries,
        copy_key="shop.example.com",
        **kw,
    )


_STORES = {
    # A separate Shopify store with only the welcome popup on this system.
    "outlet.example.com": Store(
        key="outlet.example.com",
        shop="example-outlet.myshopify.com",
        domain="outlet.example.com",
        brand="Example Outlet",
        locale="en",
        currency="EUR",
        from_email="hello@mail.outlet.example.com",
        from_name="Example Outlet",
        reply_to="hello@outlet.example.com",
        sending_domain="mail.outlet.example.com",
        welcome_pool="welcome10",
        welcome_amount=10,
        welcome_min_subtotal=100,
        client_id_env="SHOPIFY_CLIENT_ID_EXAMPLE_OUTLET",
        client_secret_env="SHOPIFY_CLIENT_SECRET_EXAMPLE_OUTLET",
        configuration_set="mail-example-outlet",
        bestseller_collection="best-sellers",
        copy_key="shop.example.com",
    ),
    # One Shopify store behind shop.example.com, shop-ie.example.com and shop-fi.example.com — each domain is its own entry (orders/checkouts by shipping country).
    "shop.example.com": _shop_store("shop.example.com", ("NL", "BE", "DE"), site_flows=True, post_purchase=True,
                                    back_in_stock=True, winback=True, abandoned_checkout=True),
    # Market storefronts. .ie was "added later": its abandoned-checkout reminders start at its own go-live (ac_own_start).
    "shop-ie.example.com": _shop_store("shop-ie.example.com", ("IE",), site_flows=True, post_purchase=True, back_in_stock=True,
                                   winback=True, abandoned_checkout=True, ac_own_start=True),
    "shop-fi.example.com": _shop_store("shop-fi.example.com", ("FI",), site_flows=True, post_purchase=True, back_in_stock=True,
                                   winback=True, abandoned_checkout=True),
}


def get(store: str) -> Store:
    return _STORES[store]


def all_stores() -> list[Store]:
    return list(_STORES.values())


def for_market(shop_domain: str, country: str | None = None, host: str | None = None) -> Store | None:
    """The storefront an order/checkout of this Shopify store belongs to; None if none of ours."""
    cands = [s for s in _STORES.values() if s.shop == shop_domain]
    if len(cands) <= 1:
        return cands[0] if cands else None
    host = (host or "").lower().removeprefix("www.")
    return (next((s for s in cands if country and country.upper() in s.countries), None)
            or next((s for s in cands if host and host == s.domain), None))


def same_shop(store: Store) -> list[str]:
    """Keys of every storefront served by this store's Shopify store (a welcome code works on all of them)."""
    return [s.key for s in _STORES.values() if s.shop == store.shop]


def by_shop(shop_domain: str) -> Store:
    for s in _STORES.values():
        if s.shop == shop_domain:
            return s
    raise KeyError(shop_domain)
