"""Compact Telegram: one progress message per submission, edited in place, plus separate messages
only for what a judge must notice. Pure: used by the outbox drain and by `replay`, so a replay looks
exactly like the live run did.

  progress  RECEIVED .. READY: one line per team, ticks added as stages finish (edited in place)
  alert     a contradicted claim, a critic revision, a rejected submission, a failure, human review,
            the event-ready line, and the final summary card: each a new message
  silent    COST: already part of the summary card
"""

PROGRESS = [
    ("RECEIVED", "received"),
    ("PROFILED", "profiled"),
    ("CI", "CI"),
    ("LIVE_CHECK", "live"),
    ("DEMO", "demo"),
    ("CLAIMS", "claims"),
    ("CRITIC", "critic"),
    ("READY", "ready"),
]
PROGRESS_STAGES = {s for s, _ in PROGRESS}
ALERT_STAGES = {"ALERT", "CARD", "REJECTED", "FAILED", "REQUIRES_HUMAN", "EVENT_READY", "PARTIAL", "CANCELLED"}
SILENT_STAGES = {"COST"}


def classify(stage: str, text: str) -> set[str]:
    """{"progress"}, {"alert"}, {"progress", "alert"} (a critic revision), or {"silent"}."""
    kinds: set[str] = set()
    if stage in PROGRESS_STAGES:
        kinds.add("progress")
    if stage in ALERT_STAGES or (stage == "CRITIC" and "revised" in text):
        kinds.add("alert")
    if stage in SILENT_STAGES:
        kinds.add("silent")
    return kinds or {"alert"}  # unknown stages are shown, never dropped


def progress_text(team: str | None, version: int | None, done: set[str], failed: bool = False) -> str:
    head = f"{team or 'Submission'}" + (f" (v{version})" if version and version > 1 else "")
    steps = " · ".join(f"{label} {'✓' if stage in done else '…'}" for stage, label in PROGRESS)
    return f"{head}\n{steps}" + ("\n✗ stopped: see the alert below" if failed else "")
