# Decisions

One line per significant choice, and why. Newest last.

## Repo and tooling
- **src layout, one package `judgebench`; product name only in `config.APP_NAME`.** Rename (§0.2) = package rename + one constant; user-facing strings read `settings().app_name`.
- **`google/` and `telegram/` live under `judgebench/`, not top level.** A top-level `google` package would shadow the `google.auth` namespace package.
- **Two dependency tiers: base (FastAPI, Pydantic) and the `[app]` extra (DB, Google, GitHub, Telegram).** The probe image installs base only, so it has no DB driver or credential-handling libraries.
- **ruff formatter owns line length (E501 ignored); mypy strict.** One formatter and no style debates.
- **DB tests fail rather than skip when Postgres is down.** CI can never go green by silently skipping the security suite.

## Data and state
- **Sync SQLAlchemy 2 + psycopg 3; FastAPI sync endpoints run in its threadpool.** Simpler than async DB at hackathon scale (tens to hundreds of submissions).
- **UUID PKs; `tenant_id` + `event_id` on every event-owned table, with composite FKs to `events(tenant_id,id)`, `teams(event_id,id)`, `submissions(event_id,id)`.** The DB itself rejects a row pointing across events (§13.2).
- **`audit_events` is not event-FK-composite.** An access denial may reference an event the actor can't see.
- **Postgres is the work queue in M1 (lease + `FOR UPDATE SKIP LOCKED`); Pub/Sub only triggers it from M2.** Pub/Sub isn't in the M1 list. State lives in the DB, so a Pub/Sub push later just calls the same handler.
- **Compare-and-set transitions (`UPDATE … WHERE state = expected`).** Duplicate deliveries and concurrent workers can't apply a step twice.
- **Transactional outbox (`notifications`) for Telegram.** It's written in the same transaction as the state change, so "if the DB didn't record it, the bot doesn't announce it" (§6.4).
- **`transition()` keeps the ORM session in sync after its bulk UPDATE.** The leaderboard test caught stale in-memory state with `synchronize_session=False`.
- **`FAILED` is resumable, but only to `last_successful_state`.** This is resume-after-crash without repeating finished stages (§16.3).
- **Retryable errors stay in state until `MAX_ATTEMPTS=3`; leases expire after 10 min; `sweep_exhausted` fails submissions whose worker crashed on every attempt.** Without the sweep they would sit in RECEIVED forever.
- **pgvector is enabled in migration 0001, but no vector columns exist until M3.** This proves the image or Cloud SQL has the extension without adding speculative schema.

## Ingestion
- **Idempotency comes from `UNIQUE(event_id, form_response_id)` + `ON CONFLICT DO NOTHING`; the cursor is only an optimisation (`timestamp >=`, overlap is harmless).** Delivery is at-least-once (§6.2).
- **Resubmission (new response, same casefolded team name) = new version; edits to an existing response are ignored.** Forms disables response editing by default, and versions stay immutable.
- **Invalid repo URLs and late submissions are recorded and announced, not dropped.** The FAILED or late flag is visible, and judges decide.
- **Answers to questions not created by setup are ignored.** Hand-added questions can't inject fields.
- **Form → response-Sheet linking uses the manual fallback (one click), not Apps Script.** Cut-order item 3. Ingestion reads the Forms API either way.
- **The form is published via the Forms API `setPublishSettings`; if that fails, setup tells the operator to click Publish.** API-created forms start unpublished.

## Repository fetch and extraction
- **Commit pinning: the worker resolves the latest commit at or before the Form submission timestamp (`GET /commits?until=<submitted_at>&per_page=1`, on the submitted branch if the URL names one), then commits the SHA before downloading; retries reuse it.** This closes the push-in-the-gap problem, and the API still needs no GitHub token. Ceiling: `until` filters on commit date, which a committer can backdate. Upgrade: cross-check push time via the repo's Events API if a dispute ever needs it.
- **GitHub tarball fetch goes worker → api.github.com/codeload.github.com directly, not via the probe.** The participant controls only a regex-validated owner/repo, not the URL. This keeps the GitHub token out of the probe. Redirects are allowed only to codeload.
- **No `git clone`.** Tarball of a pinned commit only: no hooks, submodules or LFS (§14.1).
- **Every symlink and hard link is skipped rather than resolved.** Code reading never needs them, and skipping is strictly safer than "links inside the workspace are fine".
- **Files over 5 MiB (weights, datasets) are skipped with a limitation; global caps (1 GiB decompressed stream, 300 MiB written, 20k files, depth 25, 120 s) stop extraction with a limitation.** Huge repos still get profiled (§14.2 "processing completes").
- **Files are written 0644 with `O_EXCL|O_NOFOLLOW`.** Nothing is executable, nothing is overwritten, nothing is written through a link.
- **Tarballs are not stored in GCS in M1; sha256 + pinned commit are recorded.** That's reproducible by re-fetching. Add GCS when M2 adapters need re-reads.
- **The profiler ignores README/doc prose for classification; only manifests, imports (`ast`) and layout count.** Prompt-injection text can't steer adapter selection.

## SSRF guard / probe
- **Stdlib `http.client` with a pinned-IP connection (TLS SNI + cert check still use the hostname).** This gives exact control over "resolve once, connect to that IP" and adds no HTTP library to the probe image.
- **A host is blocked if *any* DNS answer is non-public.** Otherwise a mixed answer is a rebinding vector.
- **`ipaddress.is_global` + explicit multicast check; IPv4-mapped IPv6 unwrapped; 6to4/Teredo blocked.** `is_global` alone lets multicast through.
- **Request caps are per `GuardedClient` instance = per probe batch = per submission.** Ponytail ceiling: no cross-call per-host budget; add a DB counter if one submission needs multiple batches.

## Google, Telegram, Sheets
- **Google OAuth scopes are `drive.file`, `forms.body` and `forms.responses.readonly`.** `drive.file` also covers Sheets and sharing on files the app created, so there's no `spreadsheets` or full Drive scope.
- **OAuth app stays in Testing, so refresh tokens expire after 7 days.** The operator re-runs `auth-google` the day before an event. Accepted for MVP (§19.3); see M0_SETUP.
- **Sheets writes use `valueInputOption=RAW`.** A team named `=IMPORTXML(...)` stays text, not a formula (formula injection).
- **The Leaderboard tab is fully rewritten from the DB, skipped when its hash is unchanged.** It's machine-only; judge input lives in Judge Reviews, which is never written. The hash skip saves Sheets write quota.
- **Telegram posts a new message per stage (edit-in-place deferred).** Cut-order item 4.
- **Telegram sends plain text (no `parse_mode`), and the bot token is scrubbed from every stored error.** Team names can't inject markup, and the token sits in the URL path.
- **Each component owns one secret: API = Google token, worker = GitHub token, telegram = bot token, probe = none.** Least privilege (§14.1 rule 4).
- **M1 service auth = Cloud Run IAM (`--no-allow-unauthenticated`) + Scheduler OIDC; no app-level tokens.** One mechanism, enforced before the code runs.
