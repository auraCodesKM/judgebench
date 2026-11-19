"""Operator CLI.

  auth-google               one-time OAuth consent; stores the refresh token locally
  setup <events/x.yaml>     create/refresh event, Google Form + judging workbook, judges
  run [--once]              local loop: poll Form -> profile -> Telegram -> Leaderboard
  status <slug>             submissions and their states
  doctor                    check every credential and dependency
  usage <slug>              tokens, cost, latency per submission and agent
  reevaluate <slug> <team>  re-run a team's latest submission as the next version
  team-codes <slug> --count N   generate codes for registered teams (printable list)
  replay <run> --into <demo> [--speed X]   replay a stored run into a demo channel + workbook
  reset-demo <slug>         wipe a demo event back to empty (re-takes)
  demo-preflight <slug>     everything a live demo take needs; prints READY
  demo-submit <slug> --after T   after T's real submission, submit the other demo teams via the Form
  purge <slug> --team T     remove a team completely (dry runs, test submissions)
  explain <slug>            per team: claims, checks, critic changes, tools, scope
  agreement <slug>          AI vs human judge scores per criterion
  bench [--runs N]          demo-cast benchmark (retrieval + agent trajectories)
  copilot-poll              local Judge Copilot via getUpdates

On Cloud Run the same steps run as separate services (api / worker / telegram) instead of `run`.
"""

import argparse
import logging
import sys
import time
from pathlib import Path

from sqlalchemy import select

from judgebench.config import settings
from judgebench.db.models import Event, EventIntegration, Submission, Team
from judgebench.db.session import sessions

log = logging.getLogger("cli")


def cmd_auth_google(_: argparse.Namespace) -> None:
    from judgebench.google import workspace

    cfg = settings()
    workspace.run_oauth_flow(Path(cfg.google_client_secrets_file), Path(cfg.google_token_file))
    print(f"Saved refresh token to {cfg.google_token_file} (keep it out of git).")


def cmd_setup(args: argparse.Namespace) -> None:
    from judgebench.cli.setup import announce_ready, setup_google, sync_judges, upsert_event
    from judgebench.event_config import load_event_config
    from judgebench.google import workspace

    cfg = load_event_config(Path(args.config))
    with sessions()() as s:
        event = upsert_event(s, cfg)
        sync_judges(s, event, cfg)
        s.commit()
        ids = setup_google(s, event, cfg, workspace.session(Path(settings().google_token_file)))
        announce_ready(s, event)
        s.commit()
    form, book = ids["form"], ids["workbook"]
    print(f"Event '{cfg.slug}' ready.\n")
    print(f"  Participant form : {form['responder_uri']}")
    print(f"  Form editor      : {form['edit_url']}")
    print(f"  Judging workbook : {book['url']}\n")
    if not form.get("published"):
        print("  ! The Forms API could not publish the form. Open the Form editor and click Publish.")
    print("  Manual step (once): Form editor -> Responses -> Link to Sheets -> Create a new spreadsheet.")
    print("  (Ingestion reads the Forms API directly; the linked Sheet is only for the operator.)")
    print("\n  Then run: judgebench run   (sends 'Event ready' to Telegram)")


def _step(name: str, fn: object) -> None:
    try:
        fn()  # type: ignore[operator]
    except Exception:
        log.exception("%s failed; continuing", name)


def cmd_run(args: argparse.Namespace) -> None:
    from judgebench.api.ingest import sync_event
    from judgebench.google import workspace
    from judgebench.google.layout import apply_layout
    from judgebench.google.sheets_sync import sync_workbook
    from judgebench.telegram.notify import TelegramBot, drain
    from judgebench.worker.stages import Deps, run_next

    cfg = settings()
    if not cfg.telegram_bot_token:
        sys.exit("TELEGRAM_BOT_TOKEN is not set (see M0_SETUP.md)")
    if msg := run_mode_problem(cfg.llm_mode, args.billed_ok, args.event):
        sys.exit(msg)
    factory = sessions()
    google = workspace.session(Path(cfg.google_token_file))
    deps = Deps.from_settings()
    bot = TelegramBot(cfg.telegram_bot_token, min_gap=3.0)  # ~20 posts/min per chat: Telegram's group limit

    scope = select(Event).where(Event.status == "active")
    only = None
    if args.event:  # one event only: poll, evaluate and sync just this one (demo recordings)
        with factory() as s0:
            only = s0.scalars(select(Event.id).where(Event.slug == args.event)).one()
        scope = select(Event).where(Event.id == only)

    def poll() -> None:
        with factory() as s:
            for event in s.scalars(scope).all():
                new = sync_event(s, event, lambda fid, since: workspace.list_responses(google, fid, since))
                if new:
                    log.info("%s: %d new submission(s)", event.slug, len(new))

    def notify() -> None:
        with factory() as s:
            while drain(s, bot):
                pass

    def work() -> None:
        while (sid := run_next(factory, deps, only)) is not None:
            log.info("processed %s", sid)
            _step("notify", notify)  # each stage's Telegram line goes out as soon as the stage commits

    def publish() -> None:
        with factory() as s:
            for event in s.scalars(scope).all():
                r = sync_workbook(
                    s,
                    event,
                    lambda sid, tab: workspace.read_tab(google, sid, tab),
                    lambda sid, tab, rows: workspace.write_tab(google, sid, tab, rows),
                    lambda sid: apply_layout(google, sid),
                )
                if r.written_tabs or r.overrides:
                    log.info("%s: sheets %s, %d judge override(s) kept", event.slug, r.written_tabs, r.overrides)

    if args.workers > 1 and not args.once:  # parallel workers; the model rate limiter is shared by all of them
        import threading

        from judgebench.agents.modes import limiter_stats

        def worker() -> None:
            while True:
                try:
                    sid = run_next(factory, deps, only)
                except Exception:  # noqa: BLE001 - a worker must never die; the stage records its own failure
                    log.exception("worker step failed")
                    sid = None
                if sid is None:
                    time.sleep(2)
                else:
                    log.info("processed %s", sid)
                    _step("notify", notify)

        for _ in range(args.workers):
            threading.Thread(target=worker, daemon=True).start()
        last = None
        while True:
            for name, fn in [("poll", poll), ("notify", notify), ("publish", publish)]:
                _step(name, fn)
            if (stats := limiter_stats()) != last:
                log.info("model requests sent (retries): %s", {m: f"{a} ({b})" for m, (a, b) in stats.items()})
                last = dict(stats)
            time.sleep(min(args.interval, 5))

    while True:
        for name, fn in [("poll", poll), ("notify", notify), ("work", work), ("notify", notify), ("publish", publish)]:
            _step(name, fn)
        if args.once:
            return
        time.sleep(args.interval)


def run_mode_problem(mode: str, billed_ok: bool, event: str | None = None) -> str | None:
    """Real events: offline is the safe default. gemini_free would refuse (FAIL) every real submission,
    so it needs --event (one demo event); vertex is billed and must be asked for explicitly."""
    if mode == "gemini_free" and not event:
        return (
            "LLM_MODE=gemini_free only evaluates the demo cast; real submissions would FAIL. "
            "Use LLM_MODE=offline, or --event <demo-slug> to run one demo event only."
        )
    if mode == "vertex" and not billed_ok:
        return "LLM_MODE=vertex is billed. Re-run with --billed-ok if that is intended, or use LLM_MODE=offline."
    return None


def cmd_status(args: argparse.Namespace) -> None:
    with sessions()() as s:
        rows = s.execute(
            select(
                Team.name,
                Submission.version,
                Submission.state,
                Submission.commit_sha,
                Submission.attempts,
                Submission.error_category,
            )
            .join(Submission, Submission.team_id == Team.id)
            .join(Event, Event.id == Team.event_id)
            .where(Event.slug == args.slug)
            .order_by(Team.name_key, Submission.version)
        ).all()
    for team, ver, state, sha, attempts, err in rows:
        print(f"{team:30.30} v{ver:<3} {state.value:20} {(sha or '-')[:7]:8} attempts={attempts} {err or ''}")
    if not rows:
        print("no submissions")


def cmd_usage(args: argparse.Namespace) -> None:
    from judgebench import usage

    with sessions()() as s:
        event = s.scalars(select(Event).where(Event.slug == args.slug)).one()
        print(usage.render(*usage.report(s, event.id)))


def cmd_agreement(args: argparse.Namespace) -> None:
    from judgebench import agreement

    with sessions()() as s:
        event = s.scalars(select(Event).where(Event.slug == args.slug)).one()
        print(agreement.render(*agreement.report(s, event.id)))


def cmd_reevaluate(args: argparse.Namespace) -> None:
    from judgebench.api.ingest import reevaluate

    with sessions()() as s:
        event = s.scalars(select(Event).where(Event.slug == args.slug)).one()
        sub = reevaluate(s, event, args.team)
        s.commit()
        print(f"{args.team}: v{sub.version} queued (same answers, same pinned commit); `run` picks it up")


def cmd_team_codes(args: argparse.Namespace) -> None:
    from judgebench.event_config import load_event_config
    from judgebench.team_codes import add_codes

    path = Path(args.event_file or f"events/{args.slug}.yaml")
    cfg = load_event_config(path)
    codes = add_codes(path, args.count, cfg.team_codes)
    load_event_config(path)  # the file must still validate
    print(f"{cfg.name}: {len(codes)} new team code(s), saved to {path} (gitignored)\n")
    for n, c in enumerate(codes, len(cfg.team_codes) + 1):
        print(f"  {n:>3}.  {c}      Team: ______________________")
    print(f"\nRun `judgebench setup {path}` to activate them. Each code binds to the first team name it is used with.")


def cmd_replay(args: argparse.Namespace) -> None:
    from judgebench.google import workspace
    from judgebench.google.layout import apply_layout
    from judgebench.google.sheets_sync import write_rows
    from judgebench.replay import grant_judges, replay, run_label
    from judgebench.telegram.notify import TelegramBot

    cfg = settings()
    if not cfg.telegram_bot_token:
        sys.exit("TELEGRAM_BOT_TOKEN is not set")
    google, bot = workspace.session(Path(cfg.google_token_file)), TelegramBot(cfg.telegram_bot_token, min_gap=1.1)
    with sessions()() as s:
        source = s.scalars(select(Event).where(Event.slug == args.run)).one()
        demo = s.scalars(select(Event).where(Event.slug == args.into)).one()
        if demo.id == source.id or s.scalar(select(Submission.id).where(Submission.event_id == demo.id).limit(1)):
            sys.exit(f"{args.into} must be an empty demo event (it has submissions, or is the run itself)")
        book = s.scalars(
            select(EventIntegration).where(
                EventIntegration.event_id == demo.id, EventIntegration.provider == "google_sheets"
            )
        ).one()
        sid, chat = book.external_ids["spreadsheet_id"], demo.telegram_chat_id
        if not chat:
            sys.exit(f"{args.into} has no telegram_chat_id")
        label = run_label(s, source)
        granted = grant_judges(s, demo, source)  # demo judges can /ask the Copilot about the replayed teams
        s.commit()
        apply_layout(google, sid)
        workspace.set_title(google, sid, f"{demo.name}: {label}")
        write_rows(lambda tab, rows: workspace.write_tab(google, sid, tab, rows), {})  # start empty
        r = replay(
            s,
            source,
            lambda text: bot.send(chat, text),
            lambda tabs: write_rows(lambda tab, rows: workspace.write_tab(google, sid, tab, rows), tabs),
            speed=args.speed,
            edit=(lambda mid, text: bot.edit(chat, mid, text)) if settings().telegram_mode == "compact" else None,
        )
    print(f"{r.label}: {r.messages} messages, {r.teams} team(s) replayed; Copilot access for {granted} new judge(s)")


def cmd_reset_demo(args: argparse.Namespace) -> None:
    from judgebench.demo import require_demo, reset_demo
    from judgebench.google import workspace
    from judgebench.google.layout import apply_layout
    from judgebench.telegram.notify import TelegramBot

    cfg = settings()
    google = workspace.session(Path(cfg.google_token_file))
    bot = TelegramBot(cfg.telegram_bot_token or "", min_gap=0.3)
    with sessions()() as s:
        event = s.scalars(select(Event).where(Event.slug == args.slug)).one()
        require_demo(event)
        if (
            not args.yes
            and input(f"Wipe demo event {args.slug} (teams, its Telegram posts, workbook rows)? [y/N] ") != "y"
        ):
            sys.exit("cancelled")

        def clear(sid: str) -> None:
            for tab in workspace.TABS:
                if tab in workspace.JUDGE_EDITABLE:
                    workspace._call(google, "POST", f"{workspace.SHEETS}/{sid}/values/'{tab}'!A2:Z:clear", json={})
                else:
                    workspace.write_tab(google, sid, tab, [])
            apply_layout(google, sid)
            workspace.set_title(google, sid, f"{event.name}: judging")

        r = reset_demo(s, event, lambda c, m: bot.delete(c, m), clear)
        s.commit()
    print(
        f"{args.slug} reset: {r.teams} team(s) removed, Telegram {r.telegram_deleted} post(s) deleted"
        + (f", {r.telegram_failed} could not be deleted (delete them by hand)" if r.telegram_failed else "")
        + ". Workbook empty and formatted; earlier Form responses will not be re-read."
    )


def cmd_demo_preflight(args: argparse.Namespace) -> None:
    import httpx
    import yaml

    from judgebench.demo import Preflight, event_empty, quota_check, require_demo
    from judgebench.google import workspace
    from judgebench.worker.live_check import InProcessProbe

    cfg, p = settings(), Preflight()
    with sessions()() as s:
        event = s.scalars(select(Event).where(Event.slug == args.slug)).one()
        require_demo(event)
        book = s.scalars(
            select(EventIntegration).where(
                EventIntegration.event_id == event.id, EventIntegration.provider == "google_sheets"
            )
        ).one()
        rows = -1
        try:
            google = workspace.session(Path(cfg.google_token_file))
            rows = len(workspace.read_tab(google, book.external_ids["spreadsheet_id"], "Leaderboard")) - 1
            p.ok.append("Google auth ok, workbook readable")
        except Exception as exc:  # noqa: BLE001 - reported, never raised
            p.problems.append(f"Google: {type(exc).__name__} (run `judgebench auth-google`)")
        tg = cfg.telegram_bot_token or ""
        try:
            chat = httpx.get(
                f"https://api.telegram.org/bot{tg}/getChat", params={"chat_id": event.telegram_chat_id}, timeout=15
            ).json()
            p.check(
                bool(chat.get("ok")),
                f'Telegram: bot can reach "{chat.get("result", {}).get("title", "?")}"',
                "Telegram: bot cannot reach the demo chat (admin with Post Messages?)",
            )
        except httpx.HTTPError as exc:
            p.problems.append(f"Telegram: {type(exc).__name__}")
        try:
            gh = httpx.get(
                "https://api.github.com/rate_limit",
                headers={"Authorization": f"Bearer {cfg.github_token}"} if cfg.github_token else {},
                timeout=15,
            ).json()
            left = gh["resources"]["core"]["remaining"]
            p.check(
                left >= 100, f"GitHub: {left} requests left this hour", f"GitHub: only {left} requests left this hour"
            )
        except Exception as exc:  # noqa: BLE001
            p.problems.append(f"GitHub: {type(exc).__name__}")
        p.check(
            cfg.llm_mode == "gemini_free",
            "LLM_MODE=gemini_free",
            f"LLM_MODE={cfg.llm_mode}: start with LLM_MODE=gemini_free",
        )
        p.check(cfg.telegram_mode == "compact", "TELEGRAM_MODE=compact", f"TELEGRAM_MODE={cfg.telegram_mode}")
        p.check(bool(cfg.gemini_api_key), "GEMINI_API_KEY set", "GEMINI_API_KEY missing in .env")
        teams = yaml.safe_load(Path("benchmark/demo_teams.yaml").read_text())
        overrides = (event.config or {}).get("model_overrides") or {}
        per_model: dict[str, int] = {}
        for t in teams:  # every demo team is submitted in this flow (on camera + demo-submit)
            model = overrides.get(t["team_name"], cfg.gemini_free_model)
            per_model[model] = per_model.get(model, 0) + 1
        for model, n in per_model.items():
            quota_check(p, s, model, teams=n, extra=3 if model == cfg.gemini_free_model else 0)  # + Copilot /ask
        from judgebench.demo import live_rate_check

        for model in per_model:
            live_rate_check(p, model, cfg.gemini_api_key or "")
        for t in teams:
            if url := t.get("deployed_url"):
                out = InProcessProbe().probe([{"method": "GET", "url": url.rstrip("/") + "/health"}])[0]
                if out.get("error"):  # cold: the first request wakes it
                    out = InProcessProbe().probe([{"method": "GET", "url": url.rstrip("/") + "/health"}])[0]
                p.check(
                    out.get("status") == 200,
                    f"{t['team_name']} app warm ({out.get('latency_ms')} ms)",
                    f"{t['team_name']} app {url}: {out.get('status') or out.get('error')}",
                )
        event_empty(p, s, event, max(rows, 0))
    print(p.render())
    if p.problems:
        sys.exit(1)


def cmd_demo_submit(args: argparse.Namespace) -> None:
    import yaml

    from judgebench.demo import demo_submit
    from judgebench.event_config import load_event_config

    cfg = load_event_config(Path(f"events/{args.slug}.yaml"))
    teams = yaml.safe_load(Path("benchmark/demo_teams.yaml").read_text())  # = DEMO_FORM_ANSWERS.md
    with sessions()() as s:
        event = s.scalars(select(Event).where(Event.slug == args.slug)).one()
    only = [t.strip() for t in args.teams.split(",")] if args.teams else None
    demo_submit(sessions(), event, teams, cfg.team_codes, args.after, args.gap, only=only, rest=args.rest)


def cmd_purge(args: argparse.Namespace) -> None:
    from judgebench.admin import purge_team
    from judgebench.telegram.notify import TelegramBot

    token = settings().telegram_bot_token
    bot = TelegramBot(token) if token else None
    with sessions()() as s:
        event = s.scalars(select(Event).where(Event.slug == args.slug)).one()
        for name in args.team:
            if (
                not args.yes
                and input(f"Permanently remove team {name!r} from {args.slug}? Type the team name: ") != name
            ):
                print(f"skipped {name!r}")
                continue
            r = purge_team(s, event, name, (lambda c, m: bot.delete(c, m)) if bot else (lambda c, m: False))
            s.commit()
            print(
                f"purged {r.team}: {r.submissions} submission(s), Telegram {r.telegram_deleted} deleted, {r.telegram_failed} not"
            )


def cmd_explain(args: argparse.Namespace) -> None:
    from judgebench.explain import explain

    with sessions()() as s:
        event = s.scalars(select(Event).where(Event.slug == args.slug)).one()
        print(explain(s, event.id))


def cmd_bench(args: argparse.Namespace) -> None:
    from judgebench import agent_eval, bench
    from judgebench.worker.stages import Deps

    deps = Deps.from_settings()
    metrics, reports = bench.run(sessions(), deps, runs=args.runs, k=args.k, only=args.only)
    print(
        f"Retrieval ({deps.embedder.model}, {metrics.queries} labelled queries): "
        f"Recall@{metrics.k}={metrics.recall_at_k} MRR={metrics.mrr}\n"
    )
    if settings().llm_mode == "offline":
        print("Agents: MOCK (LLM_MODE=offline). Routing, tools and budgets are real; scores and variance are not.")
    elif settings().llm_mode == "gemini_free":
        print(f"Agents: Gemini API free tier: {settings().gemini_free_model} ({args.runs} run(s) per repo)")
    else:
        print(f"Agents ({settings().gemini_model} / {settings().gemini_model_strong}, {args.runs} runs per repo)")
    print(agent_eval.render(reports))


def cmd_copilot_poll(_: argparse.Namespace) -> None:
    """Local dev: run the Judge Copilot with getUpdates long polling instead of a public webhook.
    Same handler and the same membership checks; private chats only."""
    import httpx

    from judgebench.agents.modes import configured
    from judgebench.retrieval import default_embedder
    from judgebench.telegram.copilot import Copilot
    from judgebench.telegram.notify import TelegramBot

    token = settings().telegram_bot_token
    if not token:
        sys.exit("TELEGRAM_BOT_TOKEN is not set")
    bot, copilot = TelegramBot(token), Copilot(default_embedder(), configured(settings().gemini_model))
    base, offset = f"https://api.telegram.org/bot{token}", 0
    httpx.post(f"{base}/deleteWebhook", timeout=15)  # getUpdates and webhooks are mutually exclusive
    print("Copilot polling. Message the bot privately: /help")
    while True:
        try:
            updates = httpx.get(
                f"{base}/getUpdates",
                params={"offset": offset, "timeout": 50, "allowed_updates": '["message"]'},
                timeout=60,
            ).json()
        except httpx.HTTPError:
            continue
        for u in updates.get("result", []):
            offset = u["update_id"] + 1
            msg = u.get("message") or {}
            if (msg.get("chat") or {}).get("type") != "private" or not isinstance(msg.get("text"), str):
                continue
            with sessions()() as s:
                reply = copilot.handle(s, msg["from"]["id"], msg["text"][:2000])
            bot.send(msg["chat"]["id"], reply)


def cmd_doctor(_: argparse.Namespace) -> None:
    from judgebench.cli import doctor

    results = doctor.run()
    print(doctor.render(results))
    if any(r.status == "FAIL" for r in results):
        sys.exit(1)


def main(argv: list[str] | None = None) -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    # httpx logs full request URLs at INFO; Telegram URLs contain the bot token.
    for noisy in ("httpx", "httpcore", "alembic", "google_adk"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    from judgebench import tracing

    tracing.setup("cli")
    parser = argparse.ArgumentParser(prog=settings().app_name)
    sub = parser.add_subparsers(required=True)
    sub.add_parser("auth-google").set_defaults(fn=cmd_auth_google)
    sub.add_parser("doctor").set_defaults(fn=cmd_doctor)
    sub.add_parser("copilot-poll").set_defaults(fn=cmd_copilot_poll)
    p = sub.add_parser("bench")
    p.add_argument("--runs", type=int, default=3)
    p.add_argument("--k", type=int, default=3)
    p.add_argument("--only", action="append", help="cast repo name (repeatable), e.g. --only beta")
    p.set_defaults(fn=cmd_bench)
    p = sub.add_parser("reevaluate")
    p.add_argument("slug")
    p.add_argument("team")
    p.set_defaults(fn=cmd_reevaluate)
    p = sub.add_parser("team-codes")
    p.add_argument("slug")
    p.add_argument("--count", type=int, required=True)
    p.add_argument("--event-file")
    p.set_defaults(fn=cmd_team_codes)
    p = sub.add_parser("replay")
    p.add_argument("run", help="slug of the stored run (e.g. a bench-... event)")
    p.add_argument("--into", required=True, help="an empty demo event (own channel + workbook)")
    p.add_argument("--speed", type=float, default=10.0)
    p.set_defaults(fn=cmd_replay)
    p = sub.add_parser("reset-demo")
    p.add_argument("slug")
    p.add_argument("--yes", action="store_true")
    p.set_defaults(fn=cmd_reset_demo)
    p = sub.add_parser("demo-preflight")
    p.add_argument("slug")
    p.set_defaults(fn=cmd_demo_preflight)
    p = sub.add_parser("demo-submit")
    p.add_argument("slug")
    p.add_argument("--after", required=True, help='team you submit on camera, e.g. "Team Beta"')
    p.add_argument("--gap", type=float, default=20.0)
    p.add_argument("--teams", help='only these teams, in demo order, e.g. "Team Zeta,Team Epsilon"')
    p.add_argument("--rest", action="store_true", help="submit every team not yet in the event, without waiting")
    p.set_defaults(fn=cmd_demo_submit)
    p = sub.add_parser("purge")
    p.add_argument("slug")
    p.add_argument("--team", action="append", required=True)
    p.add_argument("--yes", action="store_true", help="skip the typed confirmation")
    p.set_defaults(fn=cmd_purge)
    p = sub.add_parser("explain")
    p.add_argument("slug")
    p.set_defaults(fn=cmd_explain)
    p = sub.add_parser("agreement")
    p.add_argument("slug")
    p.set_defaults(fn=cmd_agreement)
    p = sub.add_parser("usage")
    p.add_argument("slug")
    p.set_defaults(fn=cmd_usage)
    p = sub.add_parser("setup")
    p.add_argument("config")
    p.set_defaults(fn=cmd_setup)
    p = sub.add_parser("run")
    p.add_argument("--once", action="store_true")
    p.add_argument("--interval", type=int, default=30)
    p.add_argument("--billed-ok", action="store_true", help="allow LLM_MODE=vertex (billed)")
    p.add_argument("--event", help="process only this event (required for LLM_MODE=gemini_free)")
    p.add_argument("--workers", type=int, default=1, help="parallel pipeline workers (share the model rate limiter)")
    p.set_defaults(fn=cmd_run)
    p = sub.add_parser("status")
    p.add_argument("slug")
    p.set_defaults(fn=cmd_status)
    args = parser.parse_args(argv)
    args.fn(args)
