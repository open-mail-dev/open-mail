#!/usr/bin/env bash
# Deploy the four Cloud Functions (gen2). A template: set the variables below (or export them) before running.
#
# Pre-deploy infra checklist (not automated here):
#   1. A Cloud SQL Postgres instance and database; run `python migrate.py <dsn>` against it once (and after every
#      new file in migrations/).
#   2. Secrets in Secret Manager (names below), one per value. Never put secret values in this file.
#   3. An SES configuration set per storefront (stores.py Store.configuration_set) with bounce/complaint events
#      published to an SNS topic, and that topic's ARN in MAIL_SNS_TOPIC_ARNS. Set SignatureVersion=2 on the topic:
#        aws sns set-topic-attributes --topic-arn <topic-arn> --attribute-name SignatureVersion --attribute-value 2
#   4. After subscribing the /ses URL (mail-ingest's /ses path) to that topic, confirm the subscription is no longer
#      pending:  aws sns list-subscriptions-by-topic --topic-arn <topic-arn>
#   5. A Cloud Scheduler job that calls mail-tick every few minutes (OIDC token of a service account with
#      roles/run.invoker on mail-tick).
set -euo pipefail

: "${GCP_PROJECT:?set GCP_PROJECT to your Google Cloud project id}"
REGION="${REGION:-europe-west1}"
: "${CLOUDSQL_INSTANCE:?set CLOUDSQL_INSTANCE to <project>:<region>:<instance>}"
: "${MAIL_SNS_TOPIC_ARNS:?set MAIL_SNS_TOPIC_ARNS to the SES events SNS topic ARN(s), comma-separated}"
MAIL_SES_REGION="${MAIL_SES_REGION:-eu-west-1}"
GCLOUD="${GCLOUD:-gcloud}"
# Public base URL of mail-web (click / unsubscribe / open-pixel links in every mail).
MAIL_WEB_BASE_URL="${MAIL_WEB_BASE_URL:-https://$REGION-$GCP_PROJECT.cloudfunctions.net/mail-web}"

COMMON=(--project="$GCP_PROJECT" --gen2 --runtime=python312 --region="$REGION" --source=. --trigger-http)
# One Shopify app (client id/secret) per Shopify store; the env var names come from stores.py (client_id_env /
# client_secret_env). Webhook HMAC = the sending store's client secret (main._webhook_secret).
SECRETS="${SECRETS:-MAIL_DB_DSN=mail-db-dsn:latest,MAIL_TOKEN_SECRET=mail-token-secret:latest,MAIL_IDENT_SECRET=mail-ident-secret:latest,SHOPIFY_CLIENT_ID_EXAMPLE_SHOP=shopify-client-id-example-shop:latest,SHOPIFY_CLIENT_SECRET_EXAMPLE_SHOP=shopify-client-secret-example-shop:latest,SHOPIFY_CLIENT_ID_EXAMPLE_OUTLET=shopify-client-id-example-outlet:latest,SHOPIFY_CLIENT_SECRET_EXAMPLE_OUTLET=shopify-client-secret-example-outlet:latest,SLACK_WEBHOOK_URL=slack-alerts-webhook-url:latest,AWS_ACCESS_KEY_ID=mail-ses-key-id:latest,AWS_SECRET_ACCESS_KEY=mail-ses-secret:latest}"
ENV="MAIL_WEB_BASE_URL=$MAIL_WEB_BASE_URL,MAIL_SES_REGION=$MAIL_SES_REGION,MAIL_SNS_TOPIC_ARNS=$MAIL_SNS_TOPIC_ARNS"

cd "$(dirname "$0")"
bash build_templates.sh
# --add-cloudsql-instances is not a `gcloud functions deploy` flag — gen2 functions are Cloud Run services under the
# hood, so the Cloud SQL attachment is applied with `gcloud run services update` after each function deploy.
"$GCLOUD" functions deploy mail-ingest "${COMMON[@]}" --entry-point=ingest --allow-unauthenticated --memory=256MB --timeout=30s --update-secrets="$SECRETS" --update-env-vars="$ENV"
"$GCLOUD" run services update mail-ingest --project="$GCP_PROJECT" --region="$REGION" --add-cloudsql-instances="$CLOUDSQL_INSTANCE"
"$GCLOUD" functions deploy mail-web    "${COMMON[@]}" --entry-point=web_entry --allow-unauthenticated --memory=256MB --timeout=15s --update-secrets="$SECRETS" --update-env-vars="$ENV"
"$GCLOUD" run services update mail-web --project="$GCP_PROJECT" --region="$REGION" --add-cloudsql-instances="$CLOUDSQL_INSTANCE"
"$GCLOUD" functions deploy mail-tick   "${COMMON[@]}" --entry-point=tick_entry --no-allow-unauthenticated --memory=512MB --timeout=300s --update-secrets="$SECRETS" --update-env-vars="$ENV"
"$GCLOUD" run services update mail-tick --project="$GCP_PROJECT" --region="$REGION" --add-cloudsql-instances="$CLOUDSQL_INSTANCE"
# mail-lookup: private (--no-allow-unauthenticated) read of a welcome code for customer-service tooling. Only the DB
# DSN: it reads, never sends or touches Shopify. Grant the caller's service account roles/run.invoker on it:
#   gcloud run services add-iam-policy-binding mail-lookup --region=$REGION --project=$GCP_PROJECT \
#     --member=serviceAccount:<caller-sa>@$GCP_PROJECT.iam.gserviceaccount.com --role=roles/run.invoker
"$GCLOUD" functions deploy mail-lookup "${COMMON[@]}" --entry-point=lookup_entry --no-allow-unauthenticated --memory=256MB --timeout=15s --update-secrets="MAIL_DB_DSN=mail-db-dsn:latest"
"$GCLOUD" run services update mail-lookup --project="$GCP_PROJECT" --region="$REGION" --add-cloudsql-instances="$CLOUDSQL_INSTANCE"
# Verify afterwards for each function (mail-ingest / mail-web / mail-tick / mail-lookup):
#   gcloud run services describe <fn> --region=$REGION --format='value(spec.template.metadata.annotations)'
# and confirm run.googleapis.com/cloudsql-instances is set to "$CLOUDSQL_INSTANCE".
