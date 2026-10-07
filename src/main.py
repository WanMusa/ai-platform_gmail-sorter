import base64
import json
import logging
from datetime import datetime, timezone

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("gmail-sorter")

app = FastAPI(title="gmail-sorter", version="0.1.0")


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