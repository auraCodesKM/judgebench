# Cloud Run deploy (after M1 passes locally)

One service account per component. **Never use the default compute service account.**

```bash
P=<PROJECT_ID>
for sa in jb-api jb-worker jb-telegram jb-probe jb-build jb-scheduler; do
  gcloud iam service-accounts create $sa --project=$P
done
# api, worker, telegram: Cloud SQL client. jb-probe gets NO roles at all.
for sa in jb-api jb-worker jb-telegram; do
  gcloud projects add-iam-policy-binding $P --role=roles/cloudsql.client \
    --member=serviceAccount:$sa@$P.iam.gserviceaccount.com
done
# build SA: deploy, push images, act as the runtime SAs, write logs
for role in roles/run.admin roles/artifactregistry.writer roles/logging.logWriter; do
  gcloud projects add-iam-policy-binding $P --role=$role \
    --member=serviceAccount:jb-build@$P.iam.gserviceaccount.com
done
for sa in jb-api jb-worker jb-telegram jb-probe; do
  gcloud iam service-accounts add-iam-policy-binding $sa@$P.iam.gserviceaccount.com \
    --role=roles/iam.serviceAccountUser --member=serviceAccount:jb-build@$P.iam.gserviceaccount.com
done
```

Secret access is granted per secret, not per project: `google-oauth-token` → jb-api only;
`github-token` → jb-worker only; `telegram-bot-token` → jb-telegram only; `database-url` →
api, worker and telegram. Invoker rights: jb-scheduler → api, worker and telegram;
jb-worker → probe. The full deploy runbook (Artifact Registry, Cloud SQL, secrets, Scheduler
jobs) comes with the deploy step.

## M2 additions: Vertex AI and Pub/Sub

```bash
# Worker calls Gemini on Vertex AI. It is the only SA with this role.
gcloud projects add-iam-policy-binding $P --role=roles/aiplatform.user \
  --member=serviceAccount:jb-worker@$P.iam.gserviceaccount.com

# Topic + dead-letter topic. Only jb-api may publish.
gcloud pubsub topics create evaluate
gcloud pubsub topics create evaluate-dlq
gcloud pubsub topics add-iam-policy-binding evaluate --role=roles/pubsub.publisher \
  --member=serviceAccount:jb-api@$P.iam.gserviceaccount.com

# Push subscription to the worker, authenticated with its own SA (OIDC), 5 attempts then DLQ.
gcloud iam service-accounts create jb-pubsub-push --project=$P
gcloud run services add-iam-policy-binding jb-worker --region=asia-south1 --role=roles/run.invoker \
  --member=serviceAccount:jb-pubsub-push@$P.iam.gserviceaccount.com
WORKER_URL=$(gcloud run services describe jb-worker --region=asia-south1 --format='value(status.url)')
gcloud pubsub subscriptions create evaluate-push --topic=evaluate \
  --push-endpoint=$WORKER_URL/internal/pubsub --push-auth-service-account=jb-pubsub-push@$P.iam.gserviceaccount.com \
  --ack-deadline=600 --dead-letter-topic=evaluate-dlq --max-delivery-attempts=5 \
  --min-retry-delay=30s --max-retry-delay=600s
# Pub/Sub's service agent must be able to publish to the DLQ and ack the source subscription:
PN=$(gcloud projects describe $P --format='value(projectNumber)')
gcloud pubsub topics add-iam-policy-binding evaluate-dlq --role=roles/pubsub.publisher \
  --member=serviceAccount:service-$PN@gcp-sa-pubsub.iam.gserviceaccount.com
gcloud pubsub subscriptions add-iam-policy-binding evaluate-push --role=roles/pubsub.subscriber \
  --member=serviceAccount:service-$PN@gcp-sa-pubsub.iam.gserviceaccount.com

# Worker may invoke the probe; nothing else may.
gcloud run services add-iam-policy-binding jb-probe --region=asia-south1 --role=roles/run.invoker \
  --member=serviceAccount:jb-worker@$P.iam.gserviceaccount.com
```

Scheduler still calls `jb-worker /internal/work` every few minutes as the safety sweep, for lost
messages and retries.

## M3 additions: tracing, Copilot

```bash
# Every runtime SA writes spans to Cloud Trace (set OTEL_TRACES_EXPORTER=gcp on each service).
for sa in jb-api jb-worker jb-telegram; do
  gcloud projects add-iam-policy-binding $P --role=roles/cloudtrace.agent \
    --member=serviceAccount:$sa@$P.iam.gserviceaccount.com
done
# The Copilot answers with Gemini and embeds queries: Vertex for jb-telegram too.
gcloud projects add-iam-policy-binding $P --role=roles/aiplatform.user \
  --member=serviceAccount:jb-telegram@$P.iam.gserviceaccount.com
# Webhook: Telegram must send our secret header. The telegram service must allow unauthenticated
# invocations (Telegram can't send Google ID tokens), so the secret header IS the auth.
SECRET=$(python3 -c "import secrets; print(secrets.token_urlsafe(32))")
printf %s "$SECRET" | gcloud secrets create telegram-webhook-secret --data-file=-
curl -s "https://api.telegram.org/bot$TELEGRAM_BOT_TOKEN/setWebhook" \
  -d url="$TELEGRAM_URL/integrations/telegram/webhook" -d secret_token="$SECRET" -d allowed_updates='["message"]'
```
