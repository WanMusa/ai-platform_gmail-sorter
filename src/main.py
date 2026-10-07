import asyncio
import base64
import json
import logging
import os
from datetime import datetime, timezone

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from src.register_watch import register_watch

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("gmail-sorter")

app = FastAPI(title="gmail-sorter", version="0.1.0")
watch_renew_task: asyncio.Task | None = None


def _env_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _env_int(name: str, default: int) -> int:
    value = os.getenv(name)
    if value is None:
        return default
    try:
        parsed = int(value)
        return parsed if parsed > 0 else default
    except ValueError:
        return default


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

    if not _env_bool("GMAIL_WATCH_AUTO_RENEW", True):
        logger.info("Gmail watch auto-renew disabled")
        return

    interval_seconds = _env_int("GMAIL_WATCH_RENEW_INTERVAL_SECONDS", 21600)
    watch_renew_task = asyncio.create_task(_watch_renew_loop(interval_seconds))
    logger.info("Gmail watch auto-renew started (interval=%ss)", interval_seconds)


@app.on_event("shutdown")
async def shutdown_event() -> None:
    global watch_renew_task

    if watch_renew_task is None:
        return

    watch_renew_task.cancel()
    try:
        await watch_renew_task
    except asyncio.CancelledError:
        pass

    watch_renew_task = None


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

    return JSONResponse(status_code=200, content={"ok": True})