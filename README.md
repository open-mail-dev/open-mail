# open-mail

A small, self-hosted email-marketing system for Shopify stores: a signup popup with a welcome discount, and the
usual automated flows (welcome series, abandoned checkout, browse abandonment, cart reminder, post-purchase,
win-back, back-in-stock). It is an alternative to hosted tools such as Klaviyo for shops that want to own their
subscriber data and pay per email sent instead of per contact.

It runs as four Google Cloud Functions (Python 3.12) on a Cloud SQL Postgres database and sends through Amazon SES.

> **Not affiliated with, endorsed by or connected to Klaviyo or Shopify.** Product names are mentioned only to
> describe compatibility.
>
> **No warranty, no support.** This is published as-is (see [LICENSE](LICENSE)). It was extracted from a working
> production setup, but you are responsible for your own deployment, your sending reputation and compliance with
> e-mail and privacy law (GDPR, ePrivacy, CAN-SPAM, …) in your markets. Issues and pull requests may not be answered.

## What it does

| Flow | Trigger | Mails | Marketing? |
|---|---|---|---|
| `welcome` | popup / footer signup | 3 (code, range, expiry reminder) | yes, opt-in only |
| `abandoned_checkout` | Shopify abandoned checkout (polled) | 3 | no (holdout-tested) |
| `browse_abandonment` | product views on the storefront | 2 | yes |
| `cart_reminder` | cart adds on the storefront | 2 | yes |
| `post_purchase` | `orders/create` webhook | 2 (thank-you, cross-sell) | yes |
| `winback` | last order turned 120 days old (polled) | 1 | yes |
| `back_in_stock` | "Notify me" request + variant buyable again | 1 | no (requested) |

Flow timings live in `flows/*.py`; the words live in `copy/<store>/<locale>/<flow>.yaml`.

Other features:

- One welcome code per address per Shopify store, created in Shopify as a single-use discount code with an expiry.
- A frequency cap for marketing mails, exit rules (an order ends the reminders), 50/50 holdout groups for
  abandoned checkout, post-purchase and win-back, so you can measure whether a flow earns its keep (`report.py`).
- Click tracking through signed redirect links, an optional open pixel, RFC 8058 one-click unsubscribe headers,
  and SES bounce/complaint handling through SNS (bounces and complaints suppress the address everywhere).
- Live product cards (price, photo, stock, translated title and market price) fetched from Shopify at send time.
- Several storefront domains on one Shopify store ("markets"): orders and checkouts are filed under the storefront
  of their shipping country.
- Consent is written back to Shopify's customer marketing consent.

## Architecture

```
 Shopify storefront ──(theme snippet: signup / track / ident / notify)──▶ mail-ingest ──▶ Postgres
 Shopify webhooks (orders/create) ──────────────────────────────────────▶ mail-ingest      ▲
 Amazon SNS (SES bounces / complaints) ─────────────────────────────────▶ mail-ingest      │
 Cloud Scheduler (every few minutes) ───────────────────────────────────▶ mail-tick ───────┤──▶ Amazon SES ──▶ inbox
 Mail recipient (click / open / unsubscribe) ───────────────────────────▶ mail-web ────────┤
 Customer-service tooling (private) ────────────────────────────────────▶ mail-lookup ─────┘
```

| Function | Entry point (`main.py`) | Access | What it does |
|---|---|---|---|
| `mail-ingest` | `ingest` | public | `POST /signup` (popup), `/track` + `/ident` (browse/cart events), `/notify` (back-in-stock request), `/shopify` (HMAC-verified webhooks), `/ses` (SNS-signed SES events). CORS limited to the registered storefront domains. |
| `mail-web` | `web_entry` | public | `/c/<token>` click redirect (only to the store's own domain), `/o/<token>.gif` open pixel, `/u/<token>` unsubscribe page. Tokens are HMAC-signed (`tokens.py`). |
| `mail-tick` | `tick_entry` | private | Polls abandoned checkouts, win-back candidates and stock; renders and sends every due flow step; alerts (optional Slack webhook) when sending stalls. |
| `mail-lookup` | `lookup_entry` | private | `POST /welcome-code` read-only lookup of a customer's welcome code, for support tooling. |

Main modules: `engine.py` (enrolment, steps, frequency cap, exits), `render.py` + `cart_mail.py` (email HTML from
`templates/base.mjml` and the copy YAML), `catalog.py` (Shopify product data), `sender.py` (SES), `signup.py`,
`sitetrack.py`, `checkouts.py`, `orders.py`, `winback.py`, `stock.py`/`notify.py`, `ses_events.py`, `report.py`.

### Database

Plain Postgres; `python migrate.py <dsn>` applies `migrations/*.sql` once each, in order.

| Table | Holds |
|---|---|
| `profiles` | one row per normalised email address |
| `consent`, `consent_log` | subscribed / unsubscribed per profile per store, with source, IP and time (and its history) |
| `suppressions` | addresses never to mail: per store (unsubscribed) or everywhere (`*`: bounce, complaint) |
| `coupons` | welcome codes per profile, store and pool |
| `events` | signups, product views, cart adds, checkouts, orders |
| `enrolments`, `flow_steps` | a profile in a flow (with holdout arm) and its scheduled steps |
| `messages`, `clicks`, `opens` | every sent mail, and the clicks and opens on it |
| `stock_requests` | back-in-stock requests and their state |
| `signup_rate` | per-IP rate limiting of the signup endpoint |

### Storefront snippet

`theme/lsp-signup.liquid` is one Liquid snippet for the Shopify theme. It renders the signup popup (and takes over
the theme's footer newsletter form), records product views / cart adds after the visitor allowed marketing in
Shopify's consent banner, identifies logged-in customers with an HMAC signature (key in shop metafield
`lsp.ident_key`, never in the theme), and adds a "Notify me" button to sold-out variants. The welcome code is
never shown on the page: it only goes out by email, so made-up addresses get nothing.

## Setup

1. **Stores.** Edit `stores.py`: one `Store` per storefront (Shopify shop domain, storefront domain, sender
   addresses, SES configuration set, welcome discount, which flows are on). The repo ships with an example
   multi-market store (`shop.example.com` / `.ie` / `.fi`) and a separate single-storefront store
   (`outlet.example.com`).
2. **Copy.** Write `copy/<store key>/<locale>/<flow>.yaml` for every flow a storefront uses (or point several
   storefronts at one copy set with `Store.copy_key`). `copy/shop.example.com/en/` is a complete English example.
   Every claim in a mail (delivery, returns, payment, phone number) should match your storefront.
3. **Email template.** `templates/base.mjml` is compiled to `templates/base.html` by `build_templates.sh`
   (needs `npx`/Node); `deploy.sh` runs it.
4. **Shopify.** Create an app per Shopify store (client-credentials grant) with at least `read_products`,
   `read_orders` (+ `read_all_orders` for win-back), `read_checkouts`, `read_customers`, `write_customers`,
   `write_discounts`, and for `ops/` scripts `read_themes`, `write_themes`, `write_metafields`. Register the
   `orders/create` webhook to `<mail-ingest URL>/shopify`. Optionally, a `custom.frequently_bought_together`
   product metafield (list of product references) feeds the post-purchase and win-back product cards, and the shop
   metafields `judgeme.all_reviews_rating` / `judgeme.all_reviews_count` (as a reviews app writes them) feed the
   rating line; without them the line is left out (see `catalog.py`).
5. **AWS SES.** Verify each sending domain (DKIM, SPF, DMARC), create a configuration set per storefront that
   publishes bounces and complaints to an SNS topic, and subscribe `<mail-ingest URL>/ses` to that topic.
6. **Google Cloud.** Create a Cloud SQL Postgres instance and run the migrations, put the secrets in Secret
   Manager, then `GCP_PROJECT=… CLOUDSQL_INSTANCE=… MAIL_SNS_TOPIC_ARNS=… bash deploy.sh`. Add a Cloud Scheduler
   job that calls `mail-tick` every few minutes. See the checklist at the top of `deploy.sh`.
7. **Theme.** Set `lsp_api` in `theme/lsp-signup.liquid` to your `mail-ingest` URL, adjust the host and language
   tables, try it on an unpublished theme copy, then `python ops/theme_signup.py <store> --apply`. For logged-in
   customer identification: `MAIL_IDENT_SECRET=… python ops/ident_key.py <store> --apply`.
8. **Switch flows on.** Flows other than welcome are switched on per storefront through env vars on the functions
   (`MAIL_AC_SINCE`, `MAIL_SITE_LIVE`, `MAIL_PP_LIVE`, `MAIL_WB_LIVE`, `MAIL_BIS_LIVE`; see `.env.example` and
   `config.py`), so you can switch one off again without a deploy.

Migrating from another tool: `import_klaviyo.py` imports consent and suppressions from a Klaviyo account once, so
nobody who unsubscribed or bounced there gets mail here. Switch the other tool's flows off before (or at the
moment) you switch the same flow on here, or people get two mails.

## Configuration

See [`.env.example`](.env.example). Secrets (database DSN, token/ident HMAC keys, Shopify app credentials, AWS keys,
Slack webhook) belong in a secret manager, never in the repository.

## Development

```
python3.12 -m venv .venv && . .venv/bin/activate
pip install -r requirements-dev.txt
pytest -q          # needs a local PostgreSQL (pg_ctl on PATH) for pytest-postgresql; node for the snippet JS check
```

Reports: `MAIL_DB_DSN=… python report.py --store shop.example.com --since 2026-10-01` (welcome funnel); the
`report.site(...)` and `report.back_in_stock(...)` helpers give holdout readouts for the other flows.

## License

MIT, see [LICENSE](LICENSE).
