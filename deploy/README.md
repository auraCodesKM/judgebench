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
