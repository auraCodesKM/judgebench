# Demo shot list: live take (Team Beta, gemini-3.6-flash)

You submit Team Beta's Google Form on camera; the agent works live in Telegram and the Sheet.
Everything on screen is real and live. Waiting time may be cut or sped up in editing (marked ⏩); say
"sped up" on screen when you do (§20.4).

Event: **AI Builders Hackathon – Demo** (`demo-2026`), its own Form, workbook and the *JudgeBench Demo*
Telegram chat. The live event is never touched.

## Screen layout (1920×1080)

```
┌────────────────────────────┬─────────────────────────────────────────────────┐
│ Chrome: demo Google Form   │ Telegram Desktop: "JudgeBench Demo"   (top half) │
│ (participant view)         │ font 115%                                         │
│                            ├─────────────────────────────────────────────────┤
│ ~ 800 px                   │ Chrome: demo workbook, Leaderboard tab (bottom)  │
│                            │ zoom 100%, bookmarks bar hidden                   │
└────────────────────────────┴─────────────────────────────────────────────────┘
```
Terminal (run loop) off-screen. macOS Focus on. No `.env`, tokens or emails visible.
Files to have open: `DEMO_FORM_ANSWERS.md` (off-screen, to copy from), this shot list.

## Before the take (5 minutes)

```bash
cd ~/Desktop/judgeBench
docker compose up -d --wait db
uv run --env-file .env judgebench reset-demo demo-2026 --yes          # only if a previous take exists
LLM_MODE=gemini_free GEMINI_FREE_MODEL=gemini-3.6-flash INVESTIGATOR_TOOL_BUDGET=6 TELEGRAM_MODE=compact \
  uv run --env-file .env judgebench demo-preflight demo-2026
```
Preflight must print **READY**. Then, in the off-screen terminal, start the agent **just before** you hit Submit:

```bash
LLM_MODE=gemini_free GEMINI_FREE_MODEL=gemini-3.6-flash INVESTIGATOR_TOOL_BUDGET=6 TELEGRAM_MODE=compact \
  caffeinate -i uv run --env-file .env judgebench run --event demo-2026 --interval 10
```
(Majority-of-3 claim voting is automatic with a model; `--event` keeps the loop on the demo event only.)

Pre-fill the Form with Team Beta's answers from `DEMO_FORM_ANSWERS.md` before recording, except the
last field, so the take starts on a nearly finished Form.

## Shots

| # | Time | You do / say | What appears | Point at |
|---|---|---|---|---|
| 1 | 0:00 | "Judges get five minutes per team. They can't read every repository." Scroll the Form once (team, repo, live app URL, claims: *98% accuracy, real-time answers, fully tested*). | Form, pre-filled | The claims field |
| 2 | 0:10 | Tick consent, click **Submit**. "Team Beta just submitted." | Form confirmation | — |
| 3 | ~0:15 (≤10 s after Submit) | "The agent picks it up." | Telegram: **Team Beta** progress message: `received ✓ · profiled … · CI … · live … · …` | The single message |
| 4 | +5–40 s ⏩ | "It reads the code, checks CI, and calls their live app." | The same message ticks: `profiled ✓ · CI ✓ · live ✓ · demo ✓` (edited in place, no spam) | The ticks filling in |
| 5 | +1–3 min ⏩ | "Now it checks each claim against what it found. Three independent votes per claim." | Alert: **Claim contradicted: "answers in real time". The live check measured 8.5 s for POST /ask, not real-time.** Possibly also "Fully tested…" contradicted | **The 8.5 s**: measured by calling their app, not guessed |
| 6 | next | "A second check rejects any score without evidence." | Alert: **Critic: "…" … revised to N/20, flagged for judges** (if the critic revises; not every run does) | "revised to" |
| 7 | next | "And the judge gets a summary." | **Summary card**: score /100, confidence, *Top concern*, *Ask:* question, Sheet link | Top concern + the question |
| 8 | +5 s | Switch focus to the Sheet. "The judging sheet fills in." | Leaderboard row: rank, score, confidence (coloured), flags, biggest concern, question, Repo / Live app links, "Evaluated by: Gemini API free tier: gemini-3.6-flash" | Biggest concern; **Evaluated by** label |
| 9 | +10 s | Click **Team Detail**. "One sentence per criterion, every claim linked to the file or check behind it." | Team row with **3 evidence-backed questions**; criteria with reasons, clickable evidence (`retriever.py#L…`, `live check POST /ask`), critic note | Click one evidence link → GitHub file at the pinned commit |
| 10 | +20 s | Open the bot's private chat. Type: `/ask Team Beta Is the retriever actually called when a question comes in?` | Answer citing `retriever.py` / `app.py` with sources | The cited file paths |
| 11 | end | "Humans make the final call. The agent hands them evidence and better questions." | Leaderboard | — |

⏩ = cut or speed up in editing (shots 4–7 are where the waiting is; real run ≈ 2–4 min on the free tier).

### Judge Q&A (shot 10)

Start the Copilot listener in a second off-screen terminal before the take:
```bash
LLM_MODE=gemini_free GEMINI_FREE_MODEL=gemini-3.5-flash-lite uv run --env-file .env judgebench copilot-poll
```
Flash-Lite keeps the 3.6 Flash quota for the evaluation. Other good questions:
`/ask Team Beta Where does the 98% accuracy come from?`, `/evidence Team Beta`.

## If something goes wrong mid-take

- Stop the loop (Ctrl-C), then `judgebench reset-demo demo-2026 --yes`, run preflight again, and redo the take.
  Each Beta take uses ~15 of the 20 daily `gemini-3.6-flash` requests (17 at the default tool budget; retries
  count too), so plan **one** take per day;
  rehearse with `GEMINI_FREE_MODEL=gemini-3.5-flash-lite` (500/day).
- If the live check says "cold start: retried once", it still counts; preflight warms the apps beforehand.

## After the take

`judgebench explain demo-2026` prints the full record of the run (claims, votes, checks, critic changes,
tool calls, scope check) for the technical walkthrough.
