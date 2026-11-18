"""Telegram service: the only component with the bot token.

  POST /integrations/telegram/webhook   Judge Copilot. Telegram must send our secret in
                                        X-Telegram-Bot-Api-Secret-Token (set via setWebhook).
  POST /internal/notifications/drain    Outbox drain (Cloud Scheduler).

Copilot answers only in private chats, never in the notifications channel or groups, and replies
in the webhook response body, so no extra API call is made with the token.
"""

import hmac
import os
from functools import cache
from typing import Any

from fastapi import Depends, FastAPI, Header, HTTPException

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


def require_scheduler(authorization: str | None = Header(default=None)) -> None:
    """This service is public (Telegram can't send Google ID tokens), so internal routes verify the
    Cloud Scheduler OIDC token themselves: signature, audience and the scheduler SA's email."""
    expected = os.environ.get("SCHEDULER_SA_EMAIL")
    if not expected:
        if os.environ.get("K_SERVICE"):  # on Cloud Run without config: fail closed
            raise HTTPException(status_code=503)
        return  # local dev
    from google.auth.transport.requests import Request
    from google.oauth2 import id_token

    try:
        claims = id_token.verify_oauth2_token(
            (authorization or "").removeprefix("Bearer "), Request(), audience=os.environ["INTERNAL_AUDIENCE"]
        )
    except (ValueError, KeyError) as exc:
        raise HTTPException(status_code=401) from exc
    if claims.get("email") != expected or not claims.get("email_verified"):
        raise HTTPException(status_code=403)


@app.post("/internal/notifications/drain", dependencies=[Depends(require_scheduler)])
def drain_outbox() -> dict[str, int]:
    token = settings().telegram_bot_token
    if not token:
        return {"sent": 0}
    bot, total = TelegramBot(token), 0
    with sessions()() as s:
        while sent := drain(s, bot):
            total += sent
    return {"sent": total}
