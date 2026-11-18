# M0 setup checklist: things only you can do (~30 min)

**[M1]** blocks M1 verification. **[M2]** blocks only M2 real-API runs (tests use fakes).
Secrets go in `.env` (from `.env.example`) or `.secrets/`. Both are gitignored. Never paste a secret into chat or a commit.

```bash
cd judgebench && cp .env.example .env && mkdir -p .secrets && chmod 700 .secrets
uv sync --all-extras
```

Set these two shell variables first. Every check below uses them:
```bash
export P=<your-project-id>   # lowercase, 6-30 chars, globally unique, e.g. judgebench-kt-2026
export ACC=<billing-account-id>   # from step 2
```

---

### 1. gcloud CLI + login (3 min) [M1]
- **Get:** https://cloud.google.com/sdk/docs/install (macOS: `brew install --cask google-cloud-sdk`).
- **Do:** `gcloud auth login` (your own Google account).
- **.env:** nothing.
- **Don't:** create or download service-account JSON keys, at any point.
- **Check:** `gcloud auth list --filter=status:ACTIVE --format='value(account)'` prints your email.

### 2. GCP project + billing + budget alert (5 min) [M1]
- **Get:** https://console.cloud.google.com/billing to find your billing account ID (`XXXXXX-XXXXXX-XXXXXX`), then:
  ```bash
  gcloud projects create $P --name=judgebench
  gcloud billing projects link $P --billing-account=$ACC
  gcloud config set project $P
  gcloud services enable billingbudgets.googleapis.com
  gcloud billing budgets create --billing-account=$ACC --display-name=judgebench \
    --budget-amount=20USD --threshold-rule=percent=0.5 --threshold-rule=percent=0.9 \
    --filter-projects=projects/$P
  ```
- **.env:** `GOOGLE_CLOUD_PROJECT=<your-project-id>`
- **Don't:** treat the budget as a cap. It only emails you; spend isn't stopped (§12.4).
- **Check:** `gcloud billing projects describe $P --format='value(billingEnabled)'` prints `True`.

### 3. Enable APIs (2 min): Forms/Sheets/Drive [M1], rest [M2]/deploy
```bash
gcloud services enable forms.googleapis.com sheets.googleapis.com drive.googleapis.com \
  aiplatform.googleapis.com run.googleapis.com sqladmin.googleapis.com \
  secretmanager.googleapis.com cloudbuild.googleapis.com artifactregistry.googleapis.com \
  pubsub.googleapis.com cloudtrace.googleapis.com cloudscheduler.googleapis.com iam.googleapis.com
```
- **.env:** nothing.
- **Check:** `gcloud services list --enabled --format='value(config.name)' | grep -cE '^(forms|sheets|drive|aiplatform|run|sqladmin|secretmanager|cloudbuild|artifactregistry|pubsub|cloudtrace|cloudscheduler|iam)\.googleapis\.com$'` prints `13`.

### 4. OAuth client in Testing mode + yourself as test user (8 min) [M1]
- **Where:** https://console.cloud.google.com/auth/overview?project=<your-project-id>, i.e. **APIs & Services → Google Auth Platform**.
  1. **Branding:** app name `judgebench-dev`, your support email, your developer contact email. Save.
  2. **Audience:** User type **External**. Leave **Publishing status = Testing**. Under **Test users → Add users**, add your own Google account (the one that will own the Forms and Sheets).
  3. **Data access → Add or remove scopes:** add exactly these three, then **Update → Save**:
     - `https://www.googleapis.com/auth/drive.file`
     - `https://www.googleapis.com/auth/forms.body`
     - `https://www.googleapis.com/auth/forms.responses.readonly`
  4. **Clients → Create client:** type **Desktop app**, name `judgebench-cli`. **Download JSON** and save it as `.secrets/oauth_client.json`.
  5. Run `uv run --env-file .env judgebench auth-google`. A browser opens: sign in as the test user. The "unverified app" warning is expected; click **Continue**.
- **.env:** `GOOGLE_CLIENT_SECRETS_FILE=.secrets/oauth_client.json`, `GOOGLE_TOKEN_FILE=.secrets/google.token.json` (already the defaults).
- **Don't pick:**
  - **Publish app** or **In production**: that triggers Google verification.
  - Full `.../auth/drive` or `.../auth/spreadsheets`: `drive.file` covers the files the app creates.
  - Client type **Web application**.
  - **Internal**: only available for Workspace orgs.
- **Note:** Testing-mode refresh tokens **expire after 7 days**. Re-run `auth-google` on **21 Nov**, and whenever you see `invalid_grant`.
- **Check:** `uv run --env-file .env python -c "from pathlib import Path; from judgebench.google.workspace import session; print(sorted(session(Path('.secrets/google.token.json')).credentials.scopes))"` prints the three scopes.

### 5. Telegram bot + private channel + chat ID (5 min) [M1]
- **Bot:** in Telegram open **@BotFather** → `/newbot` → name + username (must end in `bot`). Copy the token. Then `/setjoingroups` → your bot → **Disable**.
- **Channel:** Telegram → **New Channel** → name → **Private Channel**. Then **Channel info → Administrators → Add Admin →** your bot. Grant **Post Messages** only.
- **Chat ID:** post any message in the channel, then:
  ```bash
  set -a; source .env; set +a
  curl -s "https://api.telegram.org/bot$TELEGRAM_BOT_TOKEN/getUpdates" | python3 -c "import sys,json;[print(u['channel_post']['chat']['id']) for u in json.load(sys.stdin)['result'] if 'channel_post' in u]"
  ```
- **.env:** `TELEGRAM_BOT_TOKEN=<token>`. The chat ID (`-100…`) goes in `events/<slug>.yaml` as `telegram_chat_id`, **not** `.env`.
- **Don't:**
  - Make the channel Public.
  - Give the bot Add Admins, Delete Messages, Invite Users or Manage Channel.
  - Share the token. If it leaks: BotFather → `/revoke`.
- **Check:** `curl -s "https://api.telegram.org/bot$TELEGRAM_BOT_TOKEN/getChat?chat_id=<chat-id>"` returns `"ok":true` and your channel title.

### 6. GitHub fine-grained token, public repos read-only (3 min): [M1] recommended, [M2] required
- **Where:** https://github.com/settings/personal-access-tokens/new, i.e. **Settings → Developer settings → Personal access tokens → Fine-grained tokens → Generate new token**.
  - Name `judgebench-worker`, expiration **30 days**, resource owner: **you**.
  - **Repository access: Public repositories.** Permissions: **leave all at "No access"**. Public read is implicit.
- **.env:** `GITHUB_TOKEN=github_pat_...`
- **Don't pick:**
  - **All repositories** or **Only select repositories**: that grants private-repo access.
  - Any repository or account permission.
  - A **classic** token.
- **Why:** without it GitHub allows 60 req/hour. CI-run reading in M2 needs the authenticated 5,000/hour.
- **Check:** `curl -s -H "Authorization: Bearer $GITHUB_TOKEN" https://api.github.com/rate_limit | python3 -c "import sys,json;print(json.load(sys.stdin)['resources']['core']['limit'])"` prints `5000`.

### 7. Vertex AI (Gemini) via Application Default Credentials (3 min) [M2]
- **Do:** `gcloud auth application-default login`, then `gcloud auth application-default set-quota-project $P`.
- **.env:**
  ```
  GOOGLE_GENAI_USE_VERTEXAI=true
  GOOGLE_CLOUD_PROJECT=<your-project-id>
  GOOGLE_CLOUD_LOCATION=global
  GEMINI_MODEL=gemini-2.5-flash
  ```
  If the check below returns 404 for the model, open **Vertex AI → Model Garden** (https://console.cloud.google.com/vertex-ai/model-garden), pick the current Gemini Flash model ID, and set `GEMINI_MODEL` to it.
- **Don't:** use an AI Studio API key (`GOOGLE_API_KEY`). We use Vertex with ADC.
- **Check:**
  ```bash
  curl -s -X POST -H "Authorization: Bearer $(gcloud auth application-default print-access-token)" -H "Content-Type: application/json" \
    "https://aiplatform.googleapis.com/v1/projects/$P/locations/global/publishers/google/models/$GEMINI_MODEL:generateContent" \
    -d '{"contents":[{"role":"user","parts":[{"text":"reply with: ok"}]}]}' | grep -o '"text": *"[^"]*"'
  ```

### 8. Event file (2 min) [M1]
`cp events/example.yaml events/<slug>.yaml`, then set `name`, `deadline` (with timezone), `telegram_chat_id` and `judges`.
- **Check:** `uv run python -c "from pathlib import Path; from judgebench.event_config import load_event_config as l; print(l(Path('events/<slug>.yaml')).slug)"`

---

## Verify M1 (after items 1-6 and 8)

```bash
docker compose up -d --wait db
uv run alembic upgrade head
uv run pytest -q                                       # all pass (+ strict xfails for later milestones)
uv run --env-file .env judgebench setup events/<slug>.yaml
#   one click: Form editor -> Responses -> Link to Sheets
uv run --env-file .env judgebench run --interval 15    # Telegram: "Event ready"
# Submit the Form with a public GitHub repo. Within ~30 s:
#   Telegram: "[Team] Submission received (v1)" then "[Team] Profiled (commit …): … -> adapters: …"
#   Leaderboard tab: team | 1 | commit | PROFILED | categories
uv run --env-file .env judgebench status <slug>
```

Cloud Run deploy steps (service accounts, secrets) live in `deploy/README.md`.
