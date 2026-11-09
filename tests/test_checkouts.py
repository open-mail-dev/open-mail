from datetime import datetime, timedelta, timezone

import checkouts
import profiles

SHOP = "example-shop.myshopify.com"
NOW = datetime(2026, 10, 12, 12, 0, tzinfo=timezone.utc)
SINCE = NOW - timedelta(hours=6)


def node(cid, email="nieuw@example.org", country="NL", host="shop.example.com", created=NOW - timedelta(hours=2), consent="NOT_SUBSCRIBED",
         orders=0, completed=None, currency="EUR"):
    return {"id": cid, "createdAt": created.isoformat().replace("+00:00", "Z"), "completedAt": completed,
            "abandonedCheckoutUrl": f"https://{host}/1234567890/checkouts/ac/{cid}",
            "customer": {"email": email, "numberOfOrders": str(orders), "emailMarketingConsent": {"marketingState": consent}},
            "shippingAddress": {"countryCodeV2": country} if country else None, "billingAddress": None,
            "totalPriceSet": {"presentmentMoney": {"amount": "319.00", "currencyCode": currency}},
            "lineItems": {"nodes": [{"title": "Desk Lamp Basic", "variantTitle": "White", "quantity": 1,
                                     "discountedTotalPriceSet": {"presentmentMoney": {"amount": "319.00", "currencyCode": currency}},
                                     "product": {"handle": "b", "featuredMedia": {"preview": {"image": {
                                         "url": "https://cdn.shopify.com/s/files/1/0123/4567/8901/files/b.jpg?v=1"}}}}}]}}


class Client:
    def __init__(self, nodes):
        self.nodes = nodes

    def gql(self, q, v):
        return {"abandonedCheckouts": {"nodes": self.nodes, "pageInfo": {"hasNextPage": False, "endCursor": None}}}


def ids(prefix, arm, n=60):
    """checkout ids that land in `arm` (the split is a hash, so pick ids rather than hard-code them)"""
    return [f"{prefix}{i}" for i in range(n) if checkouts.arm_for(f"{prefix}{i}") == arm]


def enrolments(conn):
    return {r["ref"]: (r["store"], r["arm"], r["state"]) for r in conn.execute("SELECT ref, store, arm, state FROM enrolments")}


def test_never_subscribed_new_buyers_are_split_and_holdouts_get_no_mail(conn):
    t, h = ids("gid://t", "treatment")[0], ids("gid://h", "holdout")[0]
    out = checkouts.poll(conn, Client([node(t, "a@x.nl"), node(h, "b@x.nl")]), SHOP, SINCE, now=NOW)
    assert out["enrolled"] == 1 and out["holdout"] == 1
    assert enrolments(conn) == {t: ("shop.example.com", "treatment", "active"), h: ("shop.example.com", "holdout", "completed")}
    steps = {r["ref"]: r["s"] for r in conn.execute(
        "SELECT e.ref, array_agg(fs.status || ':' || coalesce(fs.last_error,'') ORDER BY fs.step) s FROM flow_steps fs JOIN enrolments e ON e.id=fs.enrolment_id GROUP BY e.ref")}
    assert steps[t] == ["pending:", "pending:", "pending:"]
    assert steps[h] == ["skipped:skip:holdout"] * 3


def test_subscribers_and_customers_are_outside_the_test(conn):
    sub, cust = ids("gid://s", "holdout")[0], ids("gid://c", "holdout")[0]       # would be holdout if they were in the test
    checkouts.poll(conn, Client([node(sub, "s@x.nl", consent="SUBSCRIBED"), node(cust, "c@x.nl", orders=3)]), SHOP, SINCE, now=NOW)
    e = enrolments(conn)
    assert e[sub][1] == "always" and e[cust][1] == "always"


def test_markets_route_by_country_and_unknown_markets_are_ignored(conn):
    fi, de, at, ie = "gid://fi", "gid://de", "gid://at", "gid://ie"
    checkouts.poll(conn, Client([node(fi, "fi@x.fi", "FI", "shop.example.com"), node(de, "de@x.de", "DE", "shop-fi.example.com"),
                                node(at, "at@x.at", "AT", "shop-at.example.com"), node(ie, "ie@x.ie", "IE", "shop.example.com")]), SHOP, SINCE, now=NOW)
    e = enrolments(conn)
    # the shipping country wins over the domain; AT is no market of ours; IE has its own (unset) go-live
    assert e[fi][0] == "shop-fi.example.com" and e[de][0] == "shop.example.com" and at not in e and ie not in e


def test_nothing_from_before_go_live_and_no_duplicates(conn):
    old, new = "gid://old", ids("gid://n", "treatment")[0]
    c = Client([node(old, "o@x.nl", created=SINCE - timedelta(minutes=1)), node(new, "n@x.nl")])
    checkouts.poll(conn, c, SHOP, SINCE, now=NOW)
    checkouts.poll(conn, c, SHOP, SINCE, now=NOW + timedelta(minutes=5))
    assert list(enrolments(conn)) == [new]
    assert conn.execute("SELECT count(*) n FROM flow_steps").fetchone()["n"] == 3


def test_opted_out_buyers_are_never_enrolled(conn):
    p = profiles.upsert(conn, "weg@x.nl")
    profiles.set_consent(conn, p, "shop.example.com", "unsubscribed", "unsubscribe_link")
    profiles.suppress(conn, "bounce@x.nl", "*", "hard_bounce")
    checkouts.poll(conn, Client([node("gid://u", "weg@x.nl"), node("gid://b", "bounce@x.nl"), node("gid://none", None)]), SHOP, SINCE, now=NOW)
    assert enrolments(conn) == {}


def test_completed_checkout_exits_its_reminders(conn):
    t = ids("gid://t", "treatment")[0]
    checkouts.poll(conn, Client([node(t)]), SHOP, SINCE, now=NOW)
    out = checkouts.poll(conn, Client([node(t, completed=(NOW + timedelta(minutes=10)).isoformat())]), SHOP, SINCE, now=NOW + timedelta(minutes=15))
    assert out["exited"] == 1 and enrolments(conn)[t][2] == "exited"


def test_cart_snapshot_is_stored_in_the_checkout_currency_with_store_domain_images(conn):
    t = ids("gid://t", "treatment")[0]
    checkouts.poll(conn, Client([node(t, "fi@x.fi", "FI", "shop-fi.example.com", currency="SEK")]), SHOP, SINCE, now=NOW)
    p = conn.execute("SELECT payload FROM events WHERE type='checkout_started'").fetchone()["payload"]
    assert p["currency"] == "SEK" and p["url"].startswith("https://shop-fi.example.com/")
    assert p["items"] == [{"title": "Desk Lamp Basic — White", "quantity": 1, "line_price": 319.0,
                           "image": "https://shop-fi.example.com/cdn/shop/files/b.jpg?v=1&width=200",
                           "url": "https://shop-fi.example.com/products/b"}]      # original handle: a market domain 301s to its own slug
    assert p["first_name"] == ""                                            # fixture has no name


def test_split_is_close_to_half():
    arms = [checkouts.arm_for(f"gid://shopify/AbandonedCheckout/{67100000000000 + i}") for i in range(2000)]
    assert 0.45 < arms.count("treatment") / len(arms) < 0.55


def test_a_storefront_added_later_starts_at_its_own_go_live(conn):
    ie_before, ie_after = ids("gid://ib", "treatment")[0], ids("gid://ia", "treatment")[0]
    ie_since = NOW - timedelta(hours=1)
    nodes = [node(ie_before, "b@x.ie", "IE", "shop-ie.example.com", created=NOW - timedelta(hours=2)),
             node(ie_after, "a@x.ie", "IE", "shop-ie.example.com", created=NOW - timedelta(minutes=30))]
    checkouts.poll(conn, Client(nodes), SHOP, SINCE, now=NOW)                    # IE not switched on yet
    assert enrolments(conn) == {}
    checkouts.poll(conn, Client(nodes), SHOP, SINCE, now=NOW, since_by_store={"shop-ie.example.com": ie_since})
    assert enrolments(conn) == {ie_after: ("shop-ie.example.com", "treatment", "active")}   # the previous tool kept the earlier one
