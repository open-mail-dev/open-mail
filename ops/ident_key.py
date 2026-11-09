"""The key the theme signs a logged-in customer's email with (snippet → mail-ingest/ident, sitetrack.ident): shop
metafield om.ident_key = mail-ingest's MAIL_IDENT_SECRET. One Shopify store can serve several storefronts, so one
write covers them all.

    MAIL_IDENT_SECRET=... python ops/ident_key.py shop.example.com            # dry run: does the metafield match?
    MAIL_IDENT_SECRET=... python ops/ident_key.py shop.example.com --apply    # write it
    python ops/ident_key.py shop.example.com --remove                         # delete it: the theme then signs nothing

No metafield definition is created on purpose: without one the value is not exposed to the Storefront API, only to
Liquid. Needs the store's app credentials in the env (stores.py client_id_env / client_secret_env).
"""
import os
import pathlib
import sys

import requests

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
import config  # noqa: E402
import shopify  # noqa: E402
import stores  # noqa: E402

NS, KEY = "om", "ident_key"


def _gql(store):
    """A GraphQL caller for the store's Admin API, with the same app credentials as the functions (env vars named in
    stores.py: client_id_env / client_secret_env)."""
    token = shopify.access_token(store, os.environ[store.client_id_env].strip(), os.environ[store.client_secret_env].strip())
    url = f"https://{store.shop}/admin/api/{config.SHOPIFY_API_VERSION}/graphql.json"
    h = {"X-Shopify-Access-Token": token, "Content-Type": "application/json"}

    def gql(q, v=None):
        j = requests.post(url, headers=h, json={"query": q, "variables": v or {}}, timeout=60).json()
        if j.get("errors"):
            sys.exit(f"GraphQL errors: {j['errors']}")
        return j["data"]
    return gql


def main(argv):
    store = stores.get(argv[0])
    mode = (argv[1:] or ["--dry"])[0]
    gql = _gql(store)

    secret = config.IDENT_SECRET          # MAIL_IDENT_SECRET, the same value mail-ingest runs with
    if not secret and mode != "--remove":
        sys.exit("set MAIL_IDENT_SECRET")
    shop = gql(f'{{shop{{id metafield(namespace:"{NS}",key:"{KEY}"){{id value}}}}}}')["shop"]
    have = shop["metafield"]
    print("metafield present:", bool(have), "| matches secret:", bool(have) and have["value"] == secret)
    if mode == "--apply":
        r = gql("mutation($m:[MetafieldsSetInput!]!){metafieldsSet(metafields:$m){metafields{id} userErrors{field message}}}",
                {"m": [{"ownerId": shop["id"], "namespace": NS, "key": KEY, "type": "single_line_text_field", "value": secret}]})
        print("written:", r["metafieldsSet"])
    elif mode == "--remove" and have:
        r = gql("mutation($m:[MetafieldIdentifierInput!]!){metafieldsDelete(metafields:$m){deletedMetafields{key} userErrors{message}}}",
                {"m": [{"ownerId": shop["id"], "namespace": NS, "key": KEY}]})
        print("removed:", r["metafieldsDelete"])


if __name__ == "__main__":
    main(sys.argv[1:])
