"""Runtime configuration. Read from the environment once; no other module reads os.environ.

APP_NAME is the single source of the product name (see §0.2: rename before launch).
User-facing strings must use `settings().app_name`, never a literal.
"""

import os
from dataclasses import dataclass
from functools import cache

APP_NAME = "judgebench"


@dataclass(frozen=True)
class Settings:
    app_name: str
    database_url: str
    github_token: str | None
    telegram_bot_token: str | None
    google_token_file: str
    google_client_secrets_file: str
    probe_user_agent: str
    probe_url: str
    gemini_model: str
    gemini_model_strong: str
    pubsub_topic: str | None
    google_cloud_project: str | None
    submission_token_budget: int


@cache
def settings() -> Settings:
    env = os.environ.get
    name = env("APP_NAME", APP_NAME)
    return Settings(
        app_name=name,
        database_url=env("DATABASE_URL", f"postgresql+psycopg://{APP_NAME}:{APP_NAME}@localhost:5432/{APP_NAME}"),
        github_token=env("GITHUB_TOKEN"),
        telegram_bot_token=env("TELEGRAM_BOT_TOKEN"),
        google_token_file=env("GOOGLE_TOKEN_FILE", ".secrets/google.token.json"),
        google_client_secrets_file=env("GOOGLE_CLIENT_SECRETS_FILE", ".secrets/oauth_client.json"),
        probe_user_agent=env("PROBE_USER_AGENT", f"{name}-probe/0.1 (hackathon live check)"),
        probe_url=env("PROBE_URL", "http://localhost:8081"),
        gemini_model=env("GEMINI_MODEL", "gemini-2.5-flash"),
        gemini_model_strong=env("GEMINI_MODEL_STRONG", env("GEMINI_MODEL", "gemini-2.5-flash")),
        pubsub_topic=env("PUBSUB_TOPIC") or None,
        google_cloud_project=env("GOOGLE_CLOUD_PROJECT") or None,
        submission_token_budget=int(env("SUBMISSION_TOKEN_BUDGET", "400000")),
    )
