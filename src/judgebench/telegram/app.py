"""Telegram service: the only component with the bot token. M1: drains the notification
outbox. M3 adds the Judge Copilot webhook (authorised via telegram.auth)."""

from fastapi import FastAPI

from judgebench.config import settings
from judgebench.db.session import sessions
from judgebench.telegram.notify import TelegramBot, drain

app = FastAPI(title="telegram", docs_url=None, redoc_url=None)


@app.get("/healthz")
def healthz() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/internal/notifications/drain")
def drain_outbox() -> dict[str, int]:
    token = settings().telegram_bot_token
    if not token:
        return {"sent": 0}
    bot, total = TelegramBot(token), 0
    with sessions()() as s:
        while sent := drain(s, bot):
            total += sent
    return {"sent": total}
