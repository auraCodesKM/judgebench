"""Pub/Sub between ingestion and the worker (§12.1). Messages are wake-up signals, not state:
the submission's state lives in Postgres, so a lost message only delays work until the Scheduler
sweep (/internal/work) and a duplicate is harmless (compare-and-set transitions).

Publish happens after the ingestion transaction commits. A publish failure is logged, never raised:
the rows are already durable and the sweep will pick them up.
"""

import base64
import json
import logging
import uuid
from collections.abc import Callable, Sequence
from typing import Any

log = logging.getLogger(__name__)
Publisher = Callable[[Sequence[uuid.UUID]], None]


def message(sid: uuid.UUID) -> dict[str, Any]:
    return {
        "data": base64.b64encode(json.dumps({"submission_id": str(sid)}).encode()).decode(),
        "attributes": {"kind": "evaluate"},
    }


def rest_publisher(topic: str) -> Publisher:
    """topic: projects/<p>/topics/<t>. Uses ADC (the API service account on Cloud Run)."""

    def publish(ids: Sequence[uuid.UUID]) -> None:
        if not ids:
            return
        import google.auth
        from google.auth.transport.requests import AuthorizedSession

        try:
            creds, _ = google.auth.default(scopes=["https://www.googleapis.com/auth/pubsub"])
            resp = AuthorizedSession(creds).post(
                f"https://pubsub.googleapis.com/v1/{topic}:publish",
                json={"messages": [message(i) for i in ids]},
                timeout=15,
            )
            resp.raise_for_status()
        except Exception:
            log.warning(
                "pubsub publish failed for %d submission(s); the work sweep will pick them up", len(ids), exc_info=True
            )

    return publish


def no_publisher(ids: Sequence[uuid.UUID]) -> None:
    """Local mode: the `run` loop polls the DB directly."""


def parse_push(envelope: Any) -> uuid.UUID | None:
    """Decode a Pub/Sub push envelope. Returns None for anything malformed (acked and dropped)."""
    try:
        data = json.loads(base64.b64decode(envelope["message"]["data"]))
        return uuid.UUID(str(data["submission_id"]))
    except (KeyError, TypeError, ValueError):
        return None
