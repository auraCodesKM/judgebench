"""Compact Telegram: one progress message per submission, edited in place, plus separate messages
only for what a judge must notice. Pure: used by the outbox drain and by `replay`, so a replay looks
exactly like the live run did.

  progress  RECEIVED .. READY: one line per team, ticks added as stages finish (edited in place)
  alert     a contradicted claim, a critic revision, a rejected submission, a failure, human review,
            the event-ready line, and the final summary card: each a new message
  silent    COST: already part of the summary card
"""

PROGRESS = [  # (stage, done label, doing label)
    ("RECEIVED", "", "got it"),
    ("PROFILED", "code", "reading code"),
    ("CI", "CI", "checking CI"),
    ("LIVE_CHECK", "live app", "trying the live app"),
    ("DEMO", "", "looking for a demo"),
    ("CLAIMS", "claims", "checking claims"),
    ("CRITIC", "scores", "double-checking scores"),
    ("READY", "", "writing the summary"),
]
PROGRESS_STAGES = {s for s, _, _ in PROGRESS}
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
    """One short line that updates: "Team Beta: code ✓ CI ✓ live app ✓ checking claims…", then "done ✓"."""
    head = (team or "Submission") + (f" (v{version})" if version and version > 1 else "")
    if failed:
        return f"{head}: stopped (see below)"
    if "READY" in done:
        return f"{head}: done ✓"
    ticks = [f"{label} ✓" for stage, label, _ in PROGRESS if stage in done and label]
    doing = next((doing for stage, _, doing in PROGRESS if stage not in done and stage != "RECEIVED"), "")
    return f"{head}: " + " ".join([*ticks, f"{doing}…"])
