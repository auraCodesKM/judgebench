# Event runbook: Campus AI Hackathon (21–23 Nov 2026)

One laptop runs everything locally: Form polling, the evaluation pipeline, Telegram and the judging
workbook. **Mode: `LLM_MODE=offline`** (the default). Evidence is real (GitHub CI, live checks of
deployed apps, code wiring, claims); AI scores are placeholders labelled **MOCK** everywhere. No
participant data goes to any AI model and nothing costs money. Judges score in **Judge Reviews**.

Event slug: `campus-ai-2026` · Deadline: **23 Nov 12:00 IST** (later submissions are marked "after the deadline").

## T-1 (20 Nov): checks, 15 min

```bash
cd ~/Desktop/judgeBench
docker compose up -d --wait db
uv run --env-file .env judgebench doctor          # expect 0 failing (vertex SKIP, pubsub SKIP are fine)
uv run --env-file .env judgebench status campus-ai-2026
```

1. **Remove test teams** so the Leaderboard starts empty. Each asks you to type the team name:
   ```bash
   uv run --env-file .env judgebench purge campus-ai-2026 --team "Team Alpha"
   ```
   This deletes the team, its rows and the bot's Telegram posts about it. The Sheet rows disappear on the next sync.
2. **Judges** (optional): add each judge's `email` (workbook access) and `telegram_user_id` (Copilot) to
   `events/campus-ai-2026.yaml`, then re-run `judgebench setup events/campus-ai-2026.yaml`. Google
   emails each judge a sharing notice. Removing a judge and re-running setup revokes Copilot access.
3. **Google token**: issued 18 Nov 12:07, expires about **25 Nov 12:00** (Testing-mode OAuth, 7 days). It
   covers the event. If `doctor` ever shows `invalid_grant`: `uv run --env-file .env judgebench auth-google`.
4. **Laptop**: on power, sleep disabled, stable network (a phone hotspot as backup).

## T-0: open submissions

```bash
caffeinate -i uv run --env-file .env judgebench run --interval 15
```

- `run` refuses `LLM_MODE=gemini_free` (it would fail every real submission) and billed `vertex`.
- Share the **participant Form link** only with registered teams:
  <Form link: docs/private/event-links.md, not committed>
- The Form does not collect emails: anyone with the link can submit under any team name, and a second
  submission with the same team name becomes that team's next version (earlier ones are kept). Keep the
  link inside the registered-teams channel.
- Ask finalists for a **deployed URL** and a **sample request** (for example `POST /ask {"q": "..."}`
  plus the text a correct answer contains), and a YouTube demo link.

## During the event: what to watch

| Where | What you should see |
|---|---|
| Telegram channel *JudgeBench Live* | Per team: received → profiled → CI → live check → demo → claims → critic → MOCK evaluation → cost, within ~1 min |
| Workbook → **Leaderboard** | One row per team, ranked; links to repo / demo / app; "Evaluated by: MOCK (offline, no model)" |
| `judgebench status campus-ai-2026` | Every team ends in `READY_FOR_REVIEW` |

Workbook: <workbook link: docs/private/event-links.md, not committed>

### If something goes wrong

| Symptom | Cause | Do |
|---|---|---|
| A team stays in `FAILED` | see `status` (error category) | `repo_not_found` (private, deleted or misspelt) / `invalid_repo_url` / `repo_empty`: ask the team to fix it and **resubmit the Form**. `no_commit_before_submission`: they pushed only after submitting; resubmit. `github_unavailable`, `internal_error`: `judgebench reevaluate campus-ai-2026 "<team>"`; if it fails again, judges assess manually |
| Live check "unreachable" | the app is down, or a free host slept for over 20 s twice | Not a penalty (NOT_VERIFIABLE). Ask the team to open their app, then re-evaluate |
| No Telegram messages | token, network, or the loop stopped | Check the terminal; `doctor`; restart `run` (state is in Postgres, nothing is lost or repeated) |
| `invalid_grant` in the log | Google token expired | `judgebench auth-google`, then restart `run` |
| GitHub `rate limit` | 5,000 requests/hour per token | Wait; the pipeline retries |
| A judge edited a generated cell | intended | The edit is kept forever and logged in the Audit Log with the AI value next to it |
| Wrong team / test submission | — | `judgebench purge campus-ai-2026 --team "<team>"` |
| Laptop crashed mid-run | — | Restart `run`; interrupted stages resume, no duplicate messages or rows |

## Judges (about 5 minutes per team)

1. **Leaderboard**: score, confidence, flags, *Biggest concern*, *Question to ask*, and links to repo, demo and app.
2. **Team Detail**: per criterion, the reason, evidence (clickable GitHub file#line links) and any critic change ("15 → 9: why").
3. **Evidence**: what was checked, coloured by status. Green VERIFIED = behaviour (CI tests or a live call) plus matching code.
4. **Judge Reviews**: the only tab judges type in. Pick Team, Judge, Criterion from the dropdowns, enter a score
   (0 to that criterion's max; the Sheet rejects anything else) and a comment. Score each criterion
   independently, before discussing with other judges.
5. Telegram Copilot (judges listed with `telegram_user_id` only): run `judgebench copilot-poll` in a
   second terminal; judges message the bot privately: `/teams`, `/team <name>`, `/evidence <name>`, `/ask <name> <question>`.

MOCK scores are placeholders from evidence counts, not judgements. Decide from the evidence and the demo.

## After the deadline

```bash
uv run --env-file .env judgebench status campus-ai-2026      # everything READY_FOR_REVIEW?
uv run --env-file .env judgebench explain campus-ai-2026 > results/explain.txt
uv run --env-file .env judgebench agreement campus-ai-2026   # human scores vs (MOCK) AI: a baseline only
```

- Stop `run` with Ctrl-C once judging is over. Keep the database (`docker compose stop`, never `down -v`).
- Results are anonymised before any public use (RESULTS.md, demo).
- Later, with real model access, the same pinned commits can be re-scored (`judgebench reevaluate`)
  and compared with the judges' scores. Check the consent text first (see SECURITY.md / DECISIONS.md).

## Dry run (done 19 Nov)

Three fake submissions (DRY RUN Gamma / Zeta / Beta) went through the real ingestion path, GitHub, the live
probe, Telegram and the workbook, and were then purged. Found and fixed in the process: Telegram lagged until
the whole queue finished; a sleeping Vercel app timed out the live check (now retried once).
