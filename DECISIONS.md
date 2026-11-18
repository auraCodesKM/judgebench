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

## M2: evidence and agents
- **Adapters are deterministic inspectors (rag, agentic, ml, generic). Behaviour checks (CI, live, demo) are shared, not per adapter.** One CI reader beats four copies of `check_behaviour` (§7.3 protocol simplified). The vision/infra profiles fall back to generic with an explicit "not a penalty" finding.
- **Static wiring uses a name-based call graph from `ast`; "reachable" means reachable from a request handler, `__main__`, an app file or a callback argument.** It answers "is the retriever on the request path?" without running anything. Ceiling: over-approximates (same-name functions). Upgrade to per-module resolution if the benchmark shows false positives.
- **`langchain` alone no longer triggers the RAG adapter (weight 0.3 → 0.2).** Agent-only projects were getting noisy RAG "MISSING" findings. RAG now needs a vector store or retrieval library too. Rules version bumped.
- **"CI with tests" means a workflow whose `run:` steps contain a recognised test runner.** A green run without one is `ci.green_without_tests`, never a behaviour source (Team Zeta).
- **CI logs: only a test-count summary is regex-extracted from the last 400 lines (≤2 MiB); raw logs are never stored or prompted.** Participants control log content.
- **Live check: health GET + the team's sample request, parsed deterministically (curl / JSON / `METHOD /path`), same host only, never via an LLM.** Free-text samples are reported as "not machine-readable" instead of being guessed at.
- **Health 2xx alone is not a behaviour source; a sample query that answers (and matches its `expect`) is.** "The app is up" isn't evidence that it works.
- **Probe runs in-process locally (`PROBE_URL=inprocess`), as its own role-less Cloud Run service in deploy.** Same guarded client either way. The isolation property is a deploy property.
- **Repo is re-fetched at the pinned commit per stage that needs code; no shared disk or GCS yet.** Stages are independent and crash-safe. Ceiling: one extra GitHub download per submission. Add a GCS cache if rate limits bite.
- **Evidence is append-only, grouped by `stage_attempt`; the submission points at its current attempt.** A re-run never overwrites old findings (§9.4).
- **ADK for every LLM call, with one fake `BaseLlm` in tests.** One metering/budget hook, and agent trajectories are testable without credentials.
- **Orchestrator = custom ADK `BaseAgent` whose routing comes from `session.state["adapters"]` (set by code).** Hierarchical delegation is real ADK delegation, but a prompt can't add or remove a specialist.
- **System instructions are static; all participant-derived text goes in the user turn inside `<untrusted_data>` fences, with fence tags in the text defanged.** Proven by a susceptible-fake test plus a mutation check (breaking the defang makes the test fail).
- **Investigator tools are closures bound to one snapshot; they take no IDs.** Scope can't be widened by a model.
- **Claim status rules are code (`clamp_verdict`): VERIFIED needs behavioural + code evidence; demo never verifies; CONTRADICTED needs a citation.** The model proposes, code decides (§7.5).
- **Investigator `code_refs` are validated (file exists, line range valid) and persisted as `investigation.code_ref` evidence with file hash.** Investigation findings become inspectable provenance.
- **The investigator uses JSON text output, not `output_schema`.** Gemini's tools + output-schema pairing depends on model capability. Parse failures degrade to UNVERIFIED, never crash.
- **Critic rules: a score >½ max must cite supporting evidence; model_quality >½ must cite evaluation evidence; violations are cut to floor(45% of max) and flagged.** This matches the spec's 16/20 → 9/20 example. The LLM critic may only lower or flag; code enforces it.
- **The pre-critic score is stored next to the final score.** The evaluator's recommendation stays inspectable (§6.3).
- **Budgets: per-submission token budget (default 400k) checked before every model call, plus ADK `max_llm_calls=40` and 8 investigator tool calls; budget hit → REQUIRES_HUMAN, not FAILED.** Degrade to humans instead of silently truncating.
- **Cost uses a static price table in `agents/runtime.py`, checked 2026-11-18.** Sources: the official Vertex AI pricing page (https://cloud.google.com/vertex-ai/generative-ai/pricing) gives Gemini 3.6/3.7 Flash at $0.75 / $3.75 per 1M input/output tokens (introductory to 2026-12-31, then $1.50 / $7.50) and Gemini 3.1 Pro Preview at $2.00 / $12.00 (≤200K prompt). Gemini 2.5 Flash at $0.30 / $2.50 comes only from a third-party listing (llmreference.com) and may be retired. Only a partial copy of the official page was retrievable, so re-verify before publishing RESULTS.md. Unknown models are costed at 0.
- **Two model tiers (`GEMINI_MODEL` cheap: analyst, demo, specialists; `GEMINI_MODEL_STRONG`: investigator, evaluator, critic), defaulting to the same model.** This is model routing (§12.4) without forcing a second model before it's measured.
- **Demo: Gemini reads the YouTube URL directly (first 5 min, 0.5 fps), and only bounded per-claim questions are asked.** A 400/403/404 from the model is "video unavailable" (MISSING); any other error is retried.
- **Pub/Sub messages are wake-ups (`{submission_id}`), published after the ingestion commit; publish failures are logged, not raised. The push handler just advances claimable work.** The DB is the queue, so loss = delay, duplicate = no-op. Dead-letter topic after 5 attempts; the Scheduler sweep stays as the backstop.
- **`transition()` resets `attempts` on every successful stage and can keep the lease across PROFILED → COLLECTING_EVIDENCE.** The retry budget is per stage, and a second worker can't grab work mid-handoff.

## M3
- **CLI silences `httpx`/`httpcore` INFO logs.** They print full request URLs, and Telegram's contain the bot token. `doctor` surfaced it; a test pins it.
- **`judgebench doctor` makes real calls only where a read can't prove the capability: two tiny Gemini prompts, one structured-output call, one 10-second YouTube read of a 2005 public video.** Under 1k tokens per run.
- **Sheets: a cell is written only if it is empty or still equals what we last wrote; anything else is a person's and is never overwritten.** "Last written" is kept per cell in the DB and keeps our value (not the human one), so an edited cell stays protected through every later refresh. Rows are keyed by a stable last column, so sorting or inserting rows can't misalign edits. Ceiling: read-merge-write isn't atomic (~1 s window).
- **Judge Reviews tab is read-only for us and snapshotted into `judge_reviews` (source=sheet) on every sync; machine-tab edits are logged as `sheet_override` audit events (AI value vs human value).** AI recommendation and human decision are stored separately (§6.3).
- **Retrieval: one `chunks` table (code windows 40/10, doc sections, evidence lines) with a generated `tsvector('simple')` + `vector(768)` HNSW; hybrid = OR-tsquery FTS + cosine, fused by RRF k=60, event filter inside the ranking SQL.** One DB, no second store. 'simple' keeps identifiers intact. Measured with labelled queries; numbers in tests are a lexical-embedder floor, not results.
- **Embeddings: Vertex `gemini-embedding-001` at 768 dims (EMBEDDING_MODEL); a hashing embedder only when no GCP project is configured, with a logged warning.** doctor checks the dimension. Ceiling: embedding token usage isn't costed yet.
- **Copilot replies inside the webhook response (no extra Bot API call) and only in private chats; out-of-scope teams get the same reply as nonexistent ones, logged as denials.** No existence oracle across events.
- **The telegram service is public (Telegram can't send ID tokens): the webhook checks Telegram's secret header, `/internal/*` verifies the Scheduler's OIDC token in-app and fails closed on Cloud Run if unconfigured.** The other services stay `--no-allow-unauthenticated`.
- **Tracing: the trace context is persisted on the submission at ingestion; every stage span uses it as parent.** One Cloud Trace trace per submission across processes, retries and crashes. ADK's own `invoke_agent` / `generate_content` / `execute_tool` spans nest under ours. Attributes are an allow-list of IDs and metrics.
- **`usage_records` gains stage, agent, tokens/sec; `judgebench usage <slug>` reports per submission and per agent (calls, tokens, USD, p50/p95, tokens/sec, end-to-end).** These are the §16.1 numbers RESULTS.md needs.
- **Profiler rules 2026-11-20 (found on the real demo cast): hand-rolled retrieval (>=2 code identifiers like `BM25`, `Retriever`, `load_chunks`) now signals RAG; LLM SDK imports (openai, gemini...) no longer count toward agentic.** Alpha was `generic`, Beta was `agentic`. Code identifiers count; README prose still never does. Known limitation: lexical RAG (BM25) still gets "no embedding / vector store" MISSING findings, which are true but not defects; the RAG specialist prompt should treat them as a design choice.
- **`LLM_MODE=offline` is the default everywhere (local, `.env.example`, `cloudbuild.yaml` `_LLM_MODE`); `vertex` is opt-in.** Zero-spend rule: no code path calls a billed model unless someone sets it. Offline runs the full pipeline with deterministic mock agents (`agents/offline.py`) and the lexical hashing embedder. Mock agents are conservative: never watch the video, never mark a claim beyond UNVERIFIED, score = max x share of OBSERVED/VERIFIED evidence. Every surface says MOCK: Telegram "MOCK evaluation" + "$0.00, offline mock", Sheets Leaderboard "Evaluated by" column, `[offline mock]` rationales and Copilot answers. `doctor` skips Vertex checks offline and names billing as the fix on a billing 403. Free-tier alternatives (AI Studio key, local Ollama) are possible later but are a product decision: AI Studio free tier may use inputs for training.
