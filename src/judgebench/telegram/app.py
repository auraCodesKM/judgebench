"""Telegram service: the only component with the bot token.

  POST /integrations/telegram/webhook   Judge Copilot. Telegram must send our secret in
                                        X-Telegram-Bot-Api-Secret-Token (set via setWebhook).
  POST /internal/notifications/drain    Outbox drain (Cloud Scheduler).

Copilot answers only in private chats, never in the notifications channel or groups, and replies
in the webhook response body, so no extra API call is made with the token.
"""

import hmac
from functools import cache
from typing import Any

from fastapi import FastAPI, Header, HTTPException

from judgebench import tracing
from judgebench.config import settings
from judgebench.db.session import sessions
from judgebench.telegram.copilot import Copilot
from judgebench.telegram.notify import TelegramBot, drain

app = FastAPI(title="telegram", docs_url=None, redoc_url=None)
tracing.setup("telegram")


@cache
def copilot() -> Copilot:
    from judgebench.retrieval import default_embedder

    return Copilot(embedder=default_embedder(), model=settings().gemini_model)


@app.get("/healthz")
def healthz() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/integrations/telegram/webhook")
def webhook(
    update: dict[str, Any], x_telegram_bot_api_secret_token: str | None = Header(default=None)
) -> dict[str, Any]:
    secret = settings().telegram_webhook_secret
    if not secret or not hmac.compare_digest(secret, x_telegram_bot_api_secret_token or ""):
        raise HTTPException(status_code=403)
    msg = update.get("message") or {}
    chat, sender, text = msg.get("chat") or {}, msg.get("from") or {}, msg.get("text")
    if chat.get("type") != "private" or not isinstance(sender.get("id"), int) or not isinstance(text, str):
        return {}  # ignore channel posts, groups, edits, stickers...
    with sessions()() as s:
        reply = copilot().handle(s, sender["id"], text[:2000])
    return {"method": "sendMessage", "chat_id": chat["id"], "text": reply, "disable_web_page_preview": True}


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
