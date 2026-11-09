import base64
import hashlib
import hmac
import time
from datetime import datetime, timezone

import requests

import config


class ShopifyError(RuntimeError):
    pass


def verify_webhook(body: bytes, hmac_header: str, secret: str) -> bool:
    if not hmac_header or not secret:
        return False
    want = base64.b64encode(hmac.new(secret.encode(), body, hashlib.sha256).digest()).decode()
    return hmac.compare_digest(want, hmac_header)


def _user_errors(node):
    if isinstance(node, dict):
        for k, v in node.items():
            if k == "userErrors" and v:
                return v
            found = _user_errors(v)
            if found:
                return found
    return None


_TOKENS: dict[str, tuple[str, float]] = {}   # store key → (token, monotonic expiry); lives as long as the instance
_REFRESH_MARGIN_S = 300


def access_token(store, client_id: str, client_secret: str) -> str:
    """Admin API token via the client-credentials grant (Dev Dashboard apps in the shop's own organisation).
    Shopify's tokens live 24h; cached per instance and renewed 5 minutes before they lapse."""
    cached = _TOKENS.get(store.key)
    if cached and cached[1] - _REFRESH_MARGIN_S > time.monotonic():
        return cached[0]
    r = requests.post(
        f"https://{store.shop}/admin/oauth/access_token",
        data={"grant_type": "client_credentials", "client_id": client_id, "client_secret": client_secret},
        timeout=20,
    )
    r.raise_for_status()
    body = r.json()
    token = body["access_token"]
    _TOKENS[store.key] = (token, time.monotonic() + float(body.get("expires_in", 86399)))
    return token


class Client:
    def __init__(self, store, token: str):
        self.store, self.token = store, token

    def _post(self, query: str, variables: dict) -> dict:
        r = requests.post(
            f"https://{self.store.shop}/admin/api/{config.SHOPIFY_API_VERSION}/graphql.json",
            json={"query": query, "variables": variables},
            headers={"X-Shopify-Access-Token": self.token},
            timeout=20,
        )
        r.raise_for_status()
        return r.json()

    def gql(self, query: str, variables: dict) -> dict:
        res = self._post(query, variables)
        if res.get("errors"):
            raise ShopifyError(str(res["errors"]))
        ue = _user_errors(res.get("data"))
        if ue:
            raise ShopifyError("; ".join(e.get("message", "") for e in ue))
        return res["data"]

    def _customer_id(self, email: str):
        # Exact lookup, not customers(query:): the search index lags, so a customer the storefront form created
        # seconds ago is not found and customerCreate then fails with "Email has already been taken".
        node = self.gql("query($e:String!){customerByIdentifier(identifier:{emailAddress:$e}){id}}", {"e": email})
        return (node.get("customerByIdentifier") or {}).get("id")

    def set_marketing_consent(self, email: str, subscribed: bool) -> None:
        state = "SUBSCRIBED" if subscribed else "UNSUBSCRIBED"
        # email is pre-validated by profiles.normalise
        consent = {"marketingState": state, "marketingOptInLevel": "SINGLE_OPT_IN"}
        customer_id = self._customer_id(email)
        if not customer_id and subscribed:
            try:
                self.gql(
                    """mutation($input:CustomerInput!){customerCreate(input:$input){userErrors{field message}}}""",
                    {"input": {"email": email, "emailMarketingConsent": consent}},
                )
                return
            except ShopifyError as e:
                if "already been taken" not in str(e):
                    raise
                customer_id = self._customer_id(email)   # created by someone else in between: update it instead
        if customer_id:
            self.gql(
                """mutation($input:CustomerEmailMarketingConsentUpdateInput!){
                     customerEmailMarketingConsentUpdate(input:$input){userErrors{field message}}}""",
                {"input": {"customerId": customer_id, "emailMarketingConsent": consent}},
            )

    def create_discount_code(self, code: str, amount: int, min_subtotal: int, title: str, ends_at: datetime) -> None:
        self.gql(
            """mutation($d:DiscountCodeBasicInput!){discountCodeBasicCreate(basicCodeDiscount:$d){userErrors{field message}}}""",
            {"d": {
                "title": title,
                "code": code,
                "startsAt": datetime.now(timezone.utc).isoformat(),
                "endsAt": ends_at.isoformat(),
                "customerSelection": {"all": True},
                "customerGets": {"value": {"discountAmount": {"amount": str(amount), "appliesOnEachItem": False}},
                                 "items": {"all": True}},
                "minimumRequirement": {"subtotal": {"greaterThanOrEqualToSubtotal": str(min_subtotal)}},
                "usageLimit": 1,
                "appliesOncePerCustomer": True,
            }},
        )
