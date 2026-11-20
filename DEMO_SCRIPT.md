# Demo shot list: one-person live take (Flash-Lite, timed)

You submit **Team Beta** on camera; `demo-submit` sends **Team Zeta** and **Team Epsilon** through the same
public Form a few seconds later, on camera. After a cut, it sends the last three (Alpha, Gamma, Delta). Every
Telegram message and Sheet value comes from a real pipeline run: real repos, real CI, real live checks, real
model calls on **gemini-3.1-flash-lite** (the Sheet's *Evaluated by* column and every summary card say so).

Measured in the rehearsal (19 Nov, same flow, same commands):

| Moment | Seconds after you click Submit |
|---|---|
| Beta's progress message appears | ~3 |
| Beta: live check done (`live ✓`) | ~26 |
| Beta: *Heads up* alert + summary card | **~68** (3.5-flash-lite rehearsal; 3.1 ran ~5 min for 3 teams) |
| Zeta and Epsilon summary cards | ~108 and ~134 |
| All six rows on the Leaderboard (incl. a 60 s cut) | ~242 |

47 model requests for six teams, no rate-limit retries. The agent stays under the free tier's 15 requests a
minute; a quota error fails a team visibly (FAILED in Telegram) and never switches model.

## Screen layout (1920×1080)

```
┌────────────────────────────┬─────────────────────────────────────────────────┐
│ Chrome tab 1: Beta README  │ Telegram Desktop: "JudgeBench Demo"   (top half) │
│ Chrome tab 2: demo Form    │ font 115%                                         │
│ (participant view)         ├─────────────────────────────────────────────────┤
│ ~ 800 px                   │ Chrome: demo workbook, Leaderboard tab (bottom)  │
└────────────────────────────┴─────────────────────────────────────────────────┘
```
Terminals T1-T3 off-screen. macOS Focus on. Nothing secret visible. Tab 1:
https://github.com/auraCodesKM/team-beta-insight scrolled to **Claims**. Tab 2: the demo Form pre-filled with
Team Beta's answers from `DEMO_FORM_ANSWERS.md`, everything except the consent box.

## Commands, per terminal (from `~/Desktop/judgeBench`)

**Before recording (any terminal)**
```bash
docker compose up -d --wait db
uv run --env-file .env judgebench reset-demo demo-2026 --yes            # then look: the chat is empty
LLM_MODE=gemini_free GEMINI_FREE_MODEL=gemini-3.1-flash-lite INVESTIGATOR_TOOL_BUDGET=6 TELEGRAM_MODE=compact \
  uv run --env-file .env judgebench demo-preflight demo-2026             # must print READY
```

**T2: judge Q&A listener**
```bash
LLM_MODE=gemini_free GEMINI_FREE_MODEL=gemini-3.1-flash-lite uv run --env-file .env judgebench copilot-poll
```

**T1: the agent** (start right before recording)
```bash
LLM_MODE=gemini_free GEMINI_FREE_MODEL=gemini-3.1-flash-lite INVESTIGATOR_TOOL_BUDGET=6 TELEGRAM_MODE=compact \
  caffeinate -i uv run --env-file .env judgebench run --event demo-2026 --interval 5 --workers 4
```

**T3: on-camera teammates** (start right before recording; it waits for Beta)
```bash
uv run --env-file .env judgebench demo-submit demo-2026 --after "Team Beta" --teams "Team Zeta,Team Epsilon" --gap 20
```

**T3 again, after the cut** (off camera, once Epsilon's card is in)
```bash
uv run --env-file .env judgebench demo-submit demo-2026 --after "Team Beta" --rest --gap 10
```

## Shots

| # | ≈ Time | You do / say | What appears | Point at |
|---|---|---|---|---|
| 1 | 0:00 | "Judges get five minutes per team. They can't read every repository." Tab 1, ~3 s on Beta's **Claims** (*98% accuracy, real time under 1 second, fully tested*). Switch to the Form, scroll once. | README, then the Form | README claims; **"instantly"** in the problem statement |
| 2 | 0:12 | Tick consent, **Submit**. "Team Beta just submitted. The other teams submit too; I'm using a script so you don't watch me fill five forms." | Form confirmation | — |
| 3 | +3 s | "The agent picks it up." | Telegram: **Team Beta: reading code…**. ~20 s and ~40 s later, Zeta's and Epsilon's progress messages appear | One message per team, edited in place |
| 4 | +5-30 s | "It reads the code, checks their CI, and calls their live app." | Beta's message ticks `code ✓ CI ✓ live app ✓ checking claims…` | The ticks |
| 5 | **+~65 s** | "Then it checks each claim against that evidence." | Alert: **Heads up on Team Beta: they say "Answers in real time", but their app took 8.5 s when I tried it.** | **The seconds**: measured by calling Beta's app |
| 6 | +~68 s | "The judge gets a summary." | Beta's **summary card**: score, confidence, *Stood out*, *Ask them*, Sheet link | *Stood out* + *Ask them*; footer "Checked by gemini-3.1-flash-lite" |
| 7 | +~70 s | Switch to the Sheet → **Team Detail**, Beta's block. "One sentence per criterion; every point links to the file or check behind it." | 3 evidence-backed questions; reasons; clickable evidence (`retriever.py#L…`, `live check POST /ask`); a critic note if the critic revised a score | Click one evidence link → GitHub at the pinned commit |
| 8 | +~110-135 s | Back to Telegram. | Zeta's card (*CI passes but runs no tests*), Epsilon's card (ordinary score) | **Zeta**: green CI with no tests. **Epsilon**: its README tells the model to give full marks; no effect |
| ✂ | CUT | Caption: **"3 more teams submitted through the same pipeline"**. Off camera: run the T3 `--rest` command and wait ~2 min | — | — |
| 9 | after the cut | Leaderboard with all six, ranked. "Every team, evaluated the same way." | Six rows: score, confidence, flags, biggest concern, question, links, **Evaluated by** | **Delta**: NOT_VERIFIABLE (needs a GPU), not penalised. A **critic revision** if any team has one ("16 → 9: cited no evaluation evidence") |
| 10 | +20 s | Bot private chat: `/ask Team Beta Is the retriever actually called when a question comes in?` | Answer citing `app.py` / `retriever.py`, then *Sources:* | The cited files |
| 11 | end | "Humans make the final call. The agent hands them evidence and better questions." | Leaderboard | — |

The critic doesn't revise a score on every run; when it does, the alert appears in Telegram and the change
shows in Team Detail (*Critic note*). Point at whichever team has one.

Alternative `/ask` questions: `Where does the 98% accuracy come from?`, `Are there any tests?`, `/evidence Team Beta`.

## Reset / back

- **Re-take:** Ctrl-C T1, T2 and T3, then `uv run --env-file .env judgebench reset-demo demo-2026 --yes`, check
  the chat is empty, run preflight, and start T2, T1 and T3 again. Flash-Lite's 500 requests a day allow
  several takes; preflight says if the quota or the per-minute rate is short.
- **"cold start: retried once"** in a live check still counts; preflight warms both apps.

## After the take

`judgebench explain demo-2026` prints the full record (claims, votes, checks, critic changes, tool calls,
scope check) for the technical walkthrough.
