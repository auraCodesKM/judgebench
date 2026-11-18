"""Operator CLI.

  auth-google               one-time OAuth consent; stores the refresh token locally
  setup <events/x.yaml>     create/refresh event, Google Form + judging workbook, judges
  run [--once]              local loop: poll Form -> profile -> Telegram -> Leaderboard
  status <slug>             submissions and their states

On Cloud Run the same steps run as separate services (api / worker / telegram) instead of `run`.
"""

import argparse
import logging
import sys
import time
from pathlib import Path

from sqlalchemy import select

from judgebench.config import settings
from judgebench.db.models import Event, Submission, Team
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
    from judgebench.google.sheets_sync import sync_workbook
    from judgebench.telegram.notify import TelegramBot, drain
    from judgebench.worker.stages import Deps, run_next

    cfg = settings()
    if not cfg.telegram_bot_token:
        sys.exit("TELEGRAM_BOT_TOKEN is not set (see M0_SETUP.md)")
    factory = sessions()
    google = workspace.session(Path(cfg.google_token_file))
    deps = Deps.from_settings()
    bot = TelegramBot(cfg.telegram_bot_token)

    def poll() -> None:
        with factory() as s:
            for event in s.scalars(select(Event).where(Event.status == "active")).all():
                new = sync_event(s, event, lambda fid, since: workspace.list_responses(google, fid, since))
                if new:
                    log.info("%s: %d new submission(s)", event.slug, len(new))

    def work() -> None:
        while (sid := run_next(factory, deps)) is not None:
            log.info("processed %s", sid)

    def notify() -> None:
        with factory() as s:
            while drain(s, bot):
                pass

    def publish() -> None:
        with factory() as s:
            for event in s.scalars(select(Event).where(Event.status == "active")).all():
                r = sync_workbook(
                    s,
                    event,
                    lambda sid, tab: workspace.read_tab(google, sid, tab),
                    lambda sid, tab, rows: workspace.write_tab(google, sid, tab, rows),
                )
                if r.written_tabs or r.overrides:
                    log.info("%s: sheets %s, %d judge override(s) kept", event.slug, r.written_tabs, r.overrides)

    while True:
        for name, fn in [("poll", poll), ("notify", notify), ("work", work), ("notify", notify), ("publish", publish)]:
            _step(name, fn)
        if args.once:
            return
        time.sleep(args.interval)


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
    p = sub.add_parser("usage")
    p.add_argument("slug")
    p.set_defaults(fn=cmd_usage)
    p = sub.add_parser("setup")
    p.add_argument("config")
    p.set_defaults(fn=cmd_setup)
    p = sub.add_parser("run")
    p.add_argument("--once", action="store_true")
    p.add_argument("--interval", type=int, default=30)
    p.set_defaults(fn=cmd_run)
    p = sub.add_parser("status")
    p.add_argument("slug")
    p.set_defaults(fn=cmd_status)
    args = parser.parse_args(argv)
    args.fn(args)
