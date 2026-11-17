"""Worker service: reads participant repos. No Google token, no Telegram token (§14.1).

M1: Cloud Scheduler calls /internal/work. Pub/Sub push replaces the trigger in M2;
the handler stays the same because state lives in Postgres, not in the message.
"""

from fastapi import FastAPI

from judgebench.config import settings
from judgebench.db.session import sessions
from judgebench.worker.github import GitHub
from judgebench.worker.pipeline import run_profile_stage

app = FastAPI(title="worker", docs_url=None, redoc_url=None)
MAX_PER_CALL = 5


@app.get("/healthz")
def healthz() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/internal/work")
def work() -> dict[str, int]:
    github = GitHub(settings().github_token)
    done = 0
    while done < MAX_PER_CALL and run_profile_stage(sessions(), github) is not None:
        done += 1
    return {"processed": done}
