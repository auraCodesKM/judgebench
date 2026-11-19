# Demo shot list: live take (Team Beta)

You submit Team Beta's Google Form on camera; the agent works live in Telegram and the Sheet.
Everything on screen is real and live. Waiting time may be cut or sped up in editing (marked ⏩); say
"sped up" on screen when you do (§20.4).

Event: **AI Builders Hackathon – Demo** (`demo-2026`), its own Form, workbook and the *JudgeBench Demo*
Telegram chat. The live event is never touched.

**Quotas.** Both free-tier daily quotas (`gemini-3.6-flash`: 20 requests, `gemini-3.5-flash-lite`: 500)
reset at **13:30 IST** every day (midnight Pacific). After 13:30 IST, the rehearsal and the final take can
both happen the **same day**. One Beta run uses ~15 requests at `INVESTIGATOR_TOOL_BUDGET=6` (retries
count too), so the 3.6 Flash quota allows **one** final take per day.

## Order of the day

1. **Rehearsal take** on `gemini-3.5-flash-lite` (record it: it is your practice footage).
2. `reset-demo`, then check the JudgeBench Demo chat is empty (the bot cannot read its history; look).
3. **Final take** on `gemini-3.6-flash`.
4. If the final take fails: the **backup take** on `gemini-3.5-flash-lite`.

## Rehearsal take (Flash-Lite)

Same steps and shots as the final take, with `GEMINI_FREE_MODEL=gemini-3.5-flash-lite` in every command
(see "Commands in order" below). While it runs, note:

- [ ] **Readability at your screen size**: Telegram progress line and alerts, the Leaderboard row, the
      Team Detail sentences. Adjust Telegram font / Chrome zoom if anything is small.
- [ ] **Where the long waits are**: write down the clock time of each tick (received, live ✓, claims ✓,
      the contradiction alert, the summary card). Those gaps are your ⏩ cuts.
- [ ] **Shot 10**: is the `/ask` answer clean (short, cites `app.py` / `retriever.py`, no odd wording)?
      If not, try one of the alternative questions below and use that one in the final take.
- [ ] Did the critic revise a score (shot 6)? If not, skip shot 6 in the final take unless it happens.

## Screen layout (1920×1080)

```
┌────────────────────────────┬─────────────────────────────────────────────────┐
│ Chrome: demo Google Form   │ Telegram Desktop: "JudgeBench Demo"   (top half) │
│ (participant view)         │ font 115%                                         │
│ + a tab: Beta's README     ├─────────────────────────────────────────────────┤
│ ~ 800 px                   │ Chrome: demo workbook, Leaderboard tab (bottom)  │
│                            │ zoom 100%, bookmarks bar hidden                   │
└────────────────────────────┴─────────────────────────────────────────────────┘
```
Terminals (run loop, copilot-poll) off-screen. macOS Focus on. No `.env`, tokens or emails visible.
Files to have open: `DEMO_FORM_ANSWERS.md` (off-screen, to copy from), this shot list, and a browser tab on
https://github.com/auraCodesKM/team-beta-insight scrolled to **Claims**.

Pre-fill the Form with Team Beta's answers from `DEMO_FORM_ANSWERS.md` before recording, except the consent
box, so the take starts on a nearly finished Form.

## Shots

| # | Time | You do / say | What appears | Point at |
|---|---|---|---|---|
| 1 | 0:00 | "Judges get five minutes per team. They can't read every repository." Show Beta's README **Claims** section on GitHub for ~3 s (*98% accuracy, real time under 1 second, fully tested*), then switch to the Form and scroll once (team, repo, live app URL, problem statement). | GitHub README, then the pre-filled Form | README: the claims list. Form: **"instantly"** in the problem statement |
| 2 | 0:12 | Tick consent, click **Submit**. "Team Beta just submitted." | Form confirmation | — |
| 3 | ~0:20 (≤10 s after Submit) | "The agent picks it up." | Telegram: **Team Beta** progress message: `received ✓ · profiled … · CI … · live … · …` | The single message |
| 4 | +5–40 s ⏩ | "It reads the code, checks CI, and calls their live app." | The same message ticks: `profiled ✓ · CI ✓ · live ✓ · demo ✓` (edited in place, no spam) | The ticks filling in |
| 5 | +1–3 min ⏩ | "Now it checks each claim against what it found. Three independent votes per claim." | Alert: **Claim contradicted: "…real time…". The live check measured ~9 s for POST /ask, not real-time.** Possibly also "Fully tested…" contradicted | **The measured seconds**: from calling their app, not guessed |
| 6 | next | "A second check rejects any score without evidence." | Alert: **Critic: "…" … revised to N/20, flagged for judges** (only if the critic revises; not every run does) | "revised to" |
| 7 | next | "And the judge gets a summary." | **Summary card**: score /100, confidence, *Top concern*, *Ask:* question, Sheet link | Top concern + the question |
| 8 | +5 s | Switch focus to the Sheet. "The judging sheet fills in." | Leaderboard row: rank, score, confidence (coloured), flags, biggest concern, question, Repo / Live app links, **"Evaluated by: Gemini API free tier: <model>"** | Biggest concern; the **Evaluated by** label |
| 9 | +10 s | Click **Team Detail**. "One sentence per criterion, every claim linked to the file or check behind it." | Team row with **3 evidence-backed questions**; criteria with reasons, clickable evidence (`retriever.py#L…`, `live check POST /ask`), critic note | Click one evidence link → GitHub file at the pinned commit |
| 10 | +20 s | Open the bot's private chat. Type: `/ask Team Beta Is the retriever actually called when a question comes in?` | Answer citing `app.py` / `retriever.py`, then "Sources:" | The cited file paths |
| 11 | end | "Humans make the final call. The agent hands them evidence and better questions." | Leaderboard | — |

⏩ = cut or speed up in editing (shots 4–7 are where the waiting is; a real run takes ≈ 2–4 min).

### Judge Q&A (shot 10)

The Copilot listener (second off-screen terminal) always uses Flash-Lite, so it never spends the 3.6 Flash
quota. Alternatives if the first answer isn't clean: `/ask Team Beta Where does the 98% accuracy come from?`,
`/ask Team Beta Are there any tests?`, `/evidence Team Beta`.

## Commands in order

Run from `~/Desktop/judgeBench`. `docker compose up -d --wait db` first if Postgres isn't running.

**A. Rehearsal take (Flash-Lite)**
```bash
# 1. preflight: must print READY
LLM_MODE=gemini_free GEMINI_FREE_MODEL=gemini-3.5-flash-lite INVESTIGATOR_TOOL_BUDGET=6 TELEGRAM_MODE=compact \
  uv run --env-file .env judgebench demo-preflight demo-2026
# 2. terminal 2 (off-screen): judge Q&A listener
LLM_MODE=gemini_free GEMINI_FREE_MODEL=gemini-3.5-flash-lite uv run --env-file .env judgebench copilot-poll
# 3. terminal 1 (off-screen), just before you click Submit: the agent
LLM_MODE=gemini_free GEMINI_FREE_MODEL=gemini-3.5-flash-lite INVESTIGATOR_TOOL_BUDGET=6 TELEGRAM_MODE=compact \
  caffeinate -i uv run --env-file .env judgebench run --event demo-2026 --interval 10
# 4. record shots 1-11, then Ctrl-C both terminals
```

**B. Reset between takes**
```bash
uv run --env-file .env judgebench reset-demo demo-2026 --yes
# then open the JudgeBench Demo chat and check it is empty (delete anything left by hand)
```

**C. Final take (3.6 Flash)**
```bash
LLM_MODE=gemini_free GEMINI_FREE_MODEL=gemini-3.6-flash INVESTIGATOR_TOOL_BUDGET=6 TELEGRAM_MODE=compact \
  uv run --env-file .env judgebench demo-preflight demo-2026                       # must print READY
LLM_MODE=gemini_free GEMINI_FREE_MODEL=gemini-3.5-flash-lite uv run --env-file .env judgebench copilot-poll   # terminal 2
LLM_MODE=gemini_free GEMINI_FREE_MODEL=gemini-3.6-flash INVESTIGATOR_TOOL_BUDGET=6 TELEGRAM_MODE=compact \
  caffeinate -i uv run --env-file .env judgebench run --event demo-2026 --interval 10   # terminal 1, just before Submit
```
Majority-of-3 claim voting is automatic with a model; `--event` keeps the agent on the demo event only.

## Backup take (if the 3.6 Flash take fails)

If the final take fails (quota exhausted mid-run, `503 high demand`, or a broken shot), stop both terminals
and record on Flash-Lite instead:
```bash
uv run --env-file .env judgebench reset-demo demo-2026 --yes      # then check the chat is empty
```
Then repeat **A** (steps 1-4). The Leaderboard's **"Evaluated by"** column and the summary card show
*Gemini API free tier: gemini-3.5-flash-lite*, so the video stays honest about which model ran. Flash-Lite has
room for several takes a day.

## If the live check says "cold start: retried once"

It still counts: the app was asleep and answered on the retry. Preflight warms both apps beforehand.

## After the take

`judgebench explain demo-2026` prints the full record of the run (claims, votes, checks, critic changes,
tool calls, scope check) for the technical walkthrough.
