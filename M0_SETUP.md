# M0 setup

Everything M1 needs that can't be created from code. Do Part A now; Part B is for the Cloud Run
deploy that follows M1. Keep every secret in `.secrets/` or `.env`; both are gitignored.

```bash
mkdir -p .secrets && chmod 700 .secrets
cp .env.example .env
```

---

## Part A: needed for M1 (local)

### 1. GCP project with budget alert

```bash
gcloud auth login
gcloud projects create <PROJECT_ID> --name="judgebench"
gcloud billing accounts list                       # note ACCOUNT_ID
gcloud billing projects link <PROJECT_ID> --billing-account=<ACCOUNT_ID>
gcloud config set project <PROJECT_ID>

# M1 needs only Forms/Sheets/Drive. The rest are enabled now so deploy isn't blocked later.
gcloud services enable forms.googleapis.com sheets.googleapis.com drive.googleapis.com \
  run.googleapis.com sqladmin.googleapis.com secretmanager.googleapis.com \
  artifactregistry.googleapis.com cloudbuild.googleapis.com cloudscheduler.googleapis.com \
  pubsub.googleapis.com aiplatform.googleapis.com iam.googleapis.com billingbudgets.googleapis.com

gcloud billing budgets create --billing-account=<ACCOUNT_ID> --display-name="judgebench" \
  --budget-amount=20USD --threshold-rule=percent=0.5 --threshold-rule=percent=0.9 \
  --filter-projects=projects/<PROJECT_ID>
```

A budget alert emails you. It does **not** cap spend (§12.4).

### 2. OAuth client (Testing mode)

In Cloud Console (project `<PROJECT_ID>`): **APIs & Services → Google Auth Platform**.

1. **Branding**: set the app name (use the working name, not your real product name), your support email and the developer contact email. Save.
2. **Audience**:
   - User type: **External**. Publishing status: **Testing**. Do not publish.
   - **Test users → Add users**: add the Google account that will own the Forms and Sheets.
3. **Data access → Add or remove scopes**: paste these three and save:
   - `https://www.googleapis.com/auth/drive.file`
   - `https://www.googleapis.com/auth/forms.body`
   - `https://www.googleapis.com/auth/forms.responses.readonly`
4. **Clients → Create client**: type **Desktop app**, name `judgebench-cli`. Click **Download JSON** and save it as `.secrets/oauth_client.json`.
5. Authorise once. A browser opens; sign in as the test user and accept:
   ```bash
   uv sync --all-extras
   uv run --env-file .env judgebench auth-google      # writes .secrets/google.token.json
   ```

> **Testing-mode refresh tokens expire after 7 days.** Re-run `judgebench auth-google` the day
> before the event (21 Nov) and whenever you see `invalid_grant`.

### 3. Telegram bot and private channel

1. In Telegram, open **@BotFather** → `/newbot` → pick a name and username. Copy the token into `.env` as `TELEGRAM_BOT_TOKEN=...`.
2. In BotFather: `/setjoingroups` → your bot → **Disable**. Strangers can't add it to groups.
3. Create a **New Channel** → **Private**. Then **Manage channel → Administrators → Add admin →** your bot, with **Post messages** only.
4. Post any message in the channel, then read the channel ID (starts with `-100`):
   ```bash
   set -a; source .env; set +a
   curl -s "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/getUpdates" \
     | python3 -c "import sys,json; [print(u['channel_post']['chat']['id'], u['channel_post']['chat']['title']) for u in json.load(sys.stdin)['result'] if 'channel_post' in u]"
   ```
   Put the ID in your event file as `telegram_chat_id`.
5. Judges' Telegram user IDs (optional for M1): each judge sends `/start` to the bot. Run the `getUpdates` call above and read `message.from.id`. Put it in the event file as `telegram_user_id`.

### 4. GitHub token (public repositories, read-only)

GitHub → **Settings → Developer settings → Personal access tokens → Fine-grained tokens → Generate new token**.

- Name `judgebench-worker`, expiration 30 days.
- Resource owner: you. Repository access: **Public repositories**. Permissions: none.

Put it in `.env` as `GITHUB_TOKEN=...`. Without it, GitHub allows 60 requests/hour, which is enough to try things but not for the event.

### 5. Event file

```bash
cp events/example.yaml events/<slug>.yaml   # set name, deadline, telegram_chat_id, judges
```

---

## Verify M1

```bash
docker compose up -d --wait db
uv sync --all-extras
uv run alembic upgrade head

# 1. Security suite and everything else green (needs the DB container)
uv run pytest -q tests/security          # expect: all pass + 2 xfailed (M2, M3)
uv run pytest -q                         # expect: all pass + 2 xfailed
uv run ruff check src tests && uv run mypy

# 2. Create the event. Prints the Form link, Form editor and judging workbook URLs.
uv run --env-file .env judgebench setup events/<slug>.yaml
#    One manual click: Form editor -> Responses -> Link to Sheets (fallback, §6.2)

# 3. Start the loop. Telegram should show "Event ready".
uv run --env-file .env judgebench run --interval 15

# 4. Open the participant Form link and submit a public repo,
#    e.g. https://github.com/<you>/<any-public-python-repo>
#    Within ~30 s Telegram shows:
#      [<Team>] Submission received (v1)
#      [<Team>] Profiled (commit abc1234): Python, ... -> adapters: ...
#    and the judging workbook's Leaderboard tab gets a row: team, 1, commit, PROFILED, categories.

# 5. State in the DB
uv run --env-file .env judgebench status <slug>
```

Idempotency check: stop and restart `judgebench run`. No duplicate submissions, Telegram
messages or Leaderboard writes.

---

## Part B: after M1 passes locally (Cloud Run deploy)

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
