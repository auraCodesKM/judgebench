# Demo shot list: one-person live take

You submit **Team Beta** on camera. `demo-submit` then submits the other five teams through the same public
Form, the same way a browser does, so you don't have to fill five forms on camera. Every Telegram message
and Sheet value comes from a real pipeline run: real repos, real CI, real live checks and real model calls.
Nothing is pre-written. Waiting time may be cut or sped up in editing (marked ⏩); say "sped up" on screen
when you do (§20.4).

Event: **AI Builders Hackathon – Demo** (`demo-2026`), with its own Form, workbook and *JudgeBench Demo* chat.

**Models (free tier, set in `events/demo-2026.yaml`):**
- **Team Beta:** `gemini-3.6-flash` (`model_overrides`), about 15 of its 20 requests/day.
- **The other five:** `gemini-3.5-flash-lite` (500/day).
- **Judge Q&A (`copilot-poll`):** Flash-Lite.

The Sheet's **Evaluated by** column and each summary card show the model that really ran. A quota error
fails that team honestly (FAILED in Telegram); it never falls back to another model.

## Screen layout (1920×1080)

```
┌────────────────────────────┬─────────────────────────────────────────────────┐
│ Chrome tab 1: Beta README  │ Telegram Desktop: "JudgeBench Demo"   (top half) │
│ Chrome tab 2: demo Form    │ font 115%                                         │
│ (participant view)         ├─────────────────────────────────────────────────┤
│ ~ 800 px                   │ Chrome: demo workbook, Leaderboard tab (bottom)  │
└────────────────────────────┴─────────────────────────────────────────────────┘
```
Terminals T1–T3 off-screen. macOS Focus on. No `.env`, tokens or emails visible. Tab 1 is
https://github.com/auraCodesKM/team-beta-insight scrolled to **Claims**. Tab 2 is the demo Form,
pre-filled with Team Beta's answers from `DEMO_FORM_ANSWERS.md`, all except the consent box.

## Commands in order (from `~/Desktop/judgeBench`)

```bash
# 0. once: Postgres up, demo event empty
docker compose up -d --wait db
uv run --env-file .env judgebench reset-demo demo-2026 --yes        # then look: the JudgeBench Demo chat is empty

# 1. preflight: must print READY
LLM_MODE=gemini_free GEMINI_FREE_MODEL=gemini-3.5-flash-lite INVESTIGATOR_TOOL_BUDGET=6 TELEGRAM_MODE=compact \
  uv run --env-file .env judgebench demo-preflight demo-2026

# 2. T2: judge Q&A listener (Flash-Lite)
LLM_MODE=gemini_free GEMINI_FREE_MODEL=gemini-3.5-flash-lite uv run --env-file .env judgebench copilot-poll

# 3. T1: the agent (Beta on 3.6 Flash via model_overrides, the rest on Flash-Lite; majority-of-3 voting)
LLM_MODE=gemini_free GEMINI_FREE_MODEL=gemini-3.5-flash-lite INVESTIGATOR_TOOL_BUDGET=6 TELEGRAM_MODE=compact \
  caffeinate -i uv run --env-file .env judgebench run --event demo-2026 --interval 10

# 4. T3: waits for Beta's real submission, then submits the other five through the Form, 20 s apart
uv run --env-file .env judgebench demo-submit demo-2026 --after "Team Beta" --gap 20

# 5. On camera: submit Team Beta (shot 2)
```
The first Telegram message, *"Event ready: AI Builders Hackathon – Demo. Accepting submissions."*, is posted
when T1 starts, if setup has queued it. It's real, and a fine opening frame.

## Shots

| # | ≈ Time | You do / say | What appears | Point at |
|---|---|---|---|---|
| 1 | 0:00 | "Judges get five minutes per team. They can't read every repository." Show Beta's README **Claims** for ~3 s (*98% accuracy, real time under 1 second, fully tested*), then switch to the Form and scroll once. | README, then the pre-filled Form | README claims; **"instantly"** in the Form's problem statement |
| 2 | 0:12 | Tick consent, **Submit**. "Team Beta just submitted. The other five teams submit too; I'm using a script so you don't watch me fill five forms." | Form confirmation | — |
| 3 | ≤10 s after Submit | "The agent picks it up." | Telegram: **Team Beta** progress message `received ✓ · profiled … · …`; then the other teams' progress messages appear, ~20 s apart | One message per team, edited in place, no spam |
| 4 | +1–3 min ⏩ | "It reads each repo, checks their CI, and calls their live apps." | Ticks fill in: `profiled ✓ · CI ✓ · live ✓ · demo ✓` | The ticks |
| 5 | +3–6 min ⏩ | "Then it checks each claim against that evidence, with three independent votes." | Alert: **Claim contradicted: "…real time…". The live check measured ~9 s for POST /ask, not real-time.** | **The measured seconds**: from calling Beta's app, not guessed |
| 6 | when it appears | "A second check rejects any score without evidence." | Alert **Critic: "…" … revised to N/20, flagged for judges**, for whichever team it happens to (not every run has one) | "revised to" |
| 7 | after Beta's alerts | "The judge gets a summary for each team." | Beta's **summary card**: score, confidence, *Top concern*, *Ask:* question, Sheet link | Top concern + the question |
| 8 | +6–15 min ⏩ | Switch to the Sheet. "Every team, evaluated the same way." | **Leaderboard** fills to six rows, ranked; **Evaluated by** shows *gemini-3.6-flash* for Beta and *gemini-3.5-flash-lite* for the rest | **Zeta**: "CI passes but runs no tests". **Epsilon**: an ordinary score despite its README telling the model to give full marks. **Delta**: needs a GPU, not penalised for it. The **Evaluated by** label |
| 9 | +20 s | Click **Team Detail**, Beta's block. "One sentence per criterion; every point linked to the file or check behind it." | Team row with **3 evidence-backed questions**; reasons; clickable evidence (`retriever.py#L…`, `live check POST /ask`); critic note if any | Click one evidence link → GitHub at the pinned commit |
| 10 | +20 s | Bot private chat: `/ask Team Beta Is the retriever actually called when a question comes in?` | Answer citing `app.py` / `retriever.py`, then *Sources:* | The cited files |
| 11 | end | "Humans make the final call. The agent hands them evidence and better questions." | Leaderboard | — |

⏩ = cut in editing. The real timeline: Beta's card ≈ 4–7 min after Submit; all six rows ≈ 12–20 min
(the free tier allows ~10 requests/minute, and the loop also profiles newly arriving teams).

**Key lines to say (and where to point):**
- **Claims vs evidence:** Beta's ~9 s comes from calling its live app.
- **Zeta:** green CI that tests nothing.
- **Epsilon:** the prompt injection had no effect.
- **Delta:** NOT_VERIFIABLE because it needs a GPU, and not penalised.
- **The critic** cuts a score that has no evidence.
- **Questions:** three for each team, each backed by evidence.
- **Evaluated by:** shows the real model.
- **Humans make the final call.**

Alternative `/ask` questions: `Where does the 98% accuracy come from?`, `Are there any tests?`, `/evidence Team Beta`.

## Reset / backup

- **Re-take:** Ctrl-C T1, T2, T3. Run `uv run --env-file .env judgebench reset-demo demo-2026 --yes`, check
  the chat is empty, run preflight again, then repeat steps 2–5. The 3.6 Flash quota allows **one** Beta
  take per day; preflight says so if not.
- **Backup (3.6 Flash unavailable, or Beta FAILED on quota):** remove the `model_overrides` lines from
  `events/demo-2026.yaml`, run `judgebench setup events/demo-2026.yaml`, reset, and re-take. Beta then runs
  on Flash-Lite, and the Evaluated by label shows it.
- **"cold start: retried once"** in a live check still counts. Preflight warms both apps.

## After the take

`judgebench explain demo-2026` prints the full record (claims, votes, checks, critic changes, tool calls,
scope check) for the technical walkthrough.
