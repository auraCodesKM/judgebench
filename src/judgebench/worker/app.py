"""Worker service: reads participant repos, runs agents. Holds the GitHub token; no Google
Workspace token, no Telegram token (§14.1). Triggers:
  /internal/pubsub  Pub/Sub push (wake-up per new submission; dead-letter topic after 5 failures)
  /internal/work    Cloud Scheduler sweep: catches anything a lost message missed, plus retries
Both just advance whatever work is claimable: state is in Postgres, not in the message.
"""

import logging
from typing import Any

from fastapi import FastAPI, Response

from judgebench.db.session import sessions
from judgebench.pubsub import parse_push
from judgebench.worker.stages import Deps, run_next

app = FastAPI(title="worker", docs_url=None, redoc_url=None)
log = logging.getLogger(__name__)
MAX_STEPS = 12  # one submission end to end is 5 stages; keep each call well inside the push deadline


def advance(limit: int = MAX_STEPS) -> int:
    deps, done = Deps.from_settings(), 0
    while done < limit and run_next(sessions(), deps) is not None:
        done += 1
    return done


@app.get("/healthz")
def healthz() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/internal/work")
def work() -> dict[str, int]:
    return {"steps": advance()}


@app.post("/internal/pubsub", status_code=204)
def pubsub_push(envelope: dict[str, Any]) -> Response:
    sid = parse_push(envelope)
    if sid is None:  # malformed: ack (204) so it isn't redelivered forever
        log.warning("dropping malformed Pub/Sub message")
        return Response(status_code=204)
    log.info("wake-up for submission %s", sid)
    advance()  # infra errors (DB down) raise -> 500 -> Pub/Sub retries, then dead-letters
    return Response(status_code=204)
