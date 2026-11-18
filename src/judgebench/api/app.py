"""API service: the only component with the Google refresh token (§13.4).

Auth: deployed with --no-allow-unauthenticated; only Cloud Scheduler's SA can invoke it (§18).
"""

import uuid
from pathlib import Path

from fastapi import FastAPI, HTTPException
from sqlalchemy.orm import Session

from judgebench.api.ingest import sync_event
from judgebench.config import settings
from judgebench.db.models import Event
from judgebench.db.session import sessions
from judgebench.google import workspace
from judgebench.google.projection import publish_leaderboard
from judgebench.pubsub import no_publisher, rest_publisher

app = FastAPI(title="api", docs_url=None, redoc_url=None)


def _event(s: Session, event_id: uuid.UUID) -> Event:
    event = s.get(Event, event_id)
    if event is None or event.status != "active":
        raise HTTPException(404)
    return event


@app.get("/healthz")
def healthz() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/events/{event_id}/submissions/sync")
def sync(event_id: uuid.UUID) -> dict[str, int]:
    g = workspace.session(Path(settings().google_token_file))
    with sessions()() as s:
        new = sync_event(s, _event(s, event_id), lambda fid, since: workspace.list_responses(g, fid, since))
    topic = settings().pubsub_topic
    (rest_publisher(topic) if topic else no_publisher)(new)  # after commit; failures only delay work
    return {"new_submissions": len(new)}


@app.post("/events/{event_id}/leaderboard/publish")
def publish(event_id: uuid.UUID) -> dict[str, bool]:
    g = workspace.session(Path(settings().google_token_file))
    with sessions()() as s:
        wrote = publish_leaderboard(
            s, _event(s, event_id), lambda sid, tab, rows: workspace.write_tab(g, sid, tab, rows)
        )
    return {"written": wrote}
