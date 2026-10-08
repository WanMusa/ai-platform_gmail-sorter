import asyncio
import base64
import json
import logging
from contextlib import suppress
from datetime import datetime, timezone

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from src.config import get_settings
from src.db import Database, Repo
from src.pipeline import GmailProcessor, parse_publish_time
from src.register_watch import register_watch

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("gmail-sorter")

app = FastAPI(title="gmail-sorter", version="0.1.0")
watch_renew_task: asyncio.Task | None = None
settings = get_settings()
database = Database(settings)
repo = Repo(database)
processor = GmailProcessor(settings=settings, repo=repo)


async def _watch_renew_loop(interval_seconds: int) -> None:
    while True:
        try:
            response = await asyncio.to_thread(register_watch)
            logger.info(
                "Gmail watch renewed: history_id=%s expiration=%s",
                response.get("historyId"),
                response.get("expiration"),
            )
        except Exception as exc:  # pragma: no cover
            logger.exception("Gmail watch renewal failed: %s", exc)

        await asyncio.sleep(interval_seconds)


@app.on_event("startup")
async def startup_event() -> None:
    global watch_renew_task

    database.apply_migrations()

    if not settings.watch_auto_renew:
        logger.info("Gmail watch auto-renew disabled")
        return

    interval_seconds = settings.watch_renew_interval_seconds
    watch_renew_task = asyncio.create_task(_watch_renew_loop(interval_seconds))
    logger.info("Gmail watch auto-renew started (interval=%ss)", interval_seconds)


@app.on_event("shutdown")
async def shutdown_event() -> None:
    global watch_renew_task

    if watch_renew_task is None:
        return

    watch_renew_task.cancel()
    with suppress(asyncio.CancelledError):
        await watch_renew_task

    watch_renew_task = None


async def _process_pubsub_event_task(event_id: int, decoded_payload: dict) -> None:
    try:
        await asyncio.to_thread(processor.process_pubsub_event, event_id, decoded_payload)
    except Exception as exc:  # pragma: no cover
        logger.exception("Background processing failed for event_id=%s error=%s", event_id, exc)
        repo.update_inbox_event_status(event_id, "failed", str(exc))


@app.get("/")
async def root() -> dict:
    return {
        "service": "gmail-sorter",
        "status": "ok",
        "health": "/healthz",
        "webhook": "/webhooks/gmail/pubsub",
    }


@app.get("/healthz")
async def healthz() -> dict:
    return {
        "status": "ok",
        "service": "gmail-sorter",
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


@app.post("/webhooks/gmail/pubsub")
async def gmail_pubsub_webhook(request: Request) -> JSONResponse:
    payload = await request.json()

    message = payload.get("message", {})
    encoded_data = message.get("data")

    decoded_data = {}
    if encoded_data:
        try:
            raw = base64.b64decode(encoded_data).decode("utf-8")
            decoded_data = json.loads(raw)
        except Exception as exc:  # pragma: no cover
            logger.warning("Failed to decode Pub/Sub message data: %s", exc)

    logger.info(
        "Received Gmail Pub/Sub notification: message_id=%s publish_time=%s payload=%s",
        message.get("messageId"),
        message.get("publishTime"),
        decoded_data,
    )

    event_id, inserted = repo.create_inbox_event(
        pubsub_message_id=str(message.get("messageId") or ""),
        publish_time=parse_publish_time(message.get("publishTime")),
        email_address=decoded_data.get("emailAddress"),
        history_id=str(decoded_data.get("historyId") or ""),
        payload=payload,
    )

    if inserted:
        asyncio.create_task(_process_pubsub_event_task(event_id, decoded_data))
    else:
        logger.info("Duplicate Pub/Sub event ignored event_id=%s", event_id)

    return JSONResponse(status_code=200, content={"ok": True})


@app.post("/webhooks/telegram")
async def telegram_webhook(request: Request) -> JSONResponse:
    secret = settings.telegram_webhook_secret
    if secret:
        header_secret = request.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
        if header_secret != secret:
            return JSONResponse(status_code=401, content={"ok": False, "error": "invalid secret"})

    payload = await request.json()
    callback_query = payload.get("callback_query")
    if callback_query:
        callback_data = callback_query.get("data", "")
        callback_id = callback_query.get("id", "")
        callback_message = callback_query.get("message", {})
        callback_chat_id = str(callback_message.get("chat", {}).get("id") or "")
        callback_message_id = int(callback_message.get("message_id") or 0)
        user = callback_query.get("from", {})
        decision_by = user.get("username") or str(user.get("id") or "unknown")

        result = processor.handle_telegram_callback(callback_data=callback_data, decision_by=decision_by)
        if callback_id:
            try:
                await asyncio.to_thread(processor.answer_telegram_callback_query, callback_id, result)
            except Exception as exc:  # pragma: no cover
                logger.warning("Failed to answer callback query: %s", exc)
        if callback_chat_id and callback_message_id:
            try:
                await asyncio.to_thread(
                    processor.clear_telegram_inline_keyboard,
                    callback_chat_id,
                    callback_message_id,
                )
            except Exception as exc:  # pragma: no cover
                logger.warning("Failed to clear callback inline keyboard: %s", exc)
        return JSONResponse(status_code=200, content={"ok": True, "result": result})

    message = payload.get("message", {})
    if message:
        text = (message.get("text") or "").strip()
        user = message.get("from", {})
        decision_by = user.get("username") or str(user.get("id") or "unknown")
        chat_id = str(message.get("chat", {}).get("id") or settings.telegram_chat_id)
        if text:
            result = processor.handle_telegram_text(text=text, decision_by=decision_by, chat_id=chat_id)
            return JSONResponse(status_code=200, content={"ok": True, "result": result})

    return JSONResponse(status_code=200, content={"ok": True})