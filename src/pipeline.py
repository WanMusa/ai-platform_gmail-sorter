from __future__ import annotations

import json
import logging
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any, TypedDict

from googleapiclient.errors import HttpError
from langgraph.graph import StateGraph, END

from src.config import Settings
from src.db import Repo
from src.register_watch import build_gmail_client

logger = logging.getLogger("gmail-sorter")


class EmailState(TypedDict, total=False):
    gmail_message_id: str
    history_id: str
    sender: str
    subject: str
    received_at: str | None
    snippet: str
    category: str
    confidence: float
    summary: str
    needs_human_review: bool
    proposed_actions: list[str]
    executed_actions: list[str]
    status: str


class GmailProcessor:
    def __init__(self, settings: Settings, repo: Repo):
        self.settings = settings
        self.repo = repo
        self._label_cache: dict[str, str] = {}
        self._graph = self._build_graph()

    def process_pubsub_event(self, event_id: int, decoded_payload: dict[str, Any]) -> None:
        self.repo.update_inbox_event_status(event_id, "processing")

        incoming_history_id = str(decoded_payload.get("historyId") or "")
        if not incoming_history_id:
            self.repo.update_inbox_event_status(event_id, "ignored", "Missing historyId")
            return

        last_history_id = self.repo.get_watch_state("last_history_id")
        if not last_history_id:
            self.repo.set_watch_state("last_history_id", incoming_history_id)
            self.repo.update_inbox_event_status(event_id, "checkpoint_initialized")
            return

        message_ids = self._fetch_changed_message_ids(last_history_id)
        if not message_ids:
            self.repo.set_watch_state("last_history_id", incoming_history_id)
            self.repo.update_inbox_event_status(event_id, "no_messages")
            return

        for message_id in message_ids:
            try:
                state = self._load_message_state(message_id, incoming_history_id)
                final_state = self._graph.invoke(state)

                self.repo.upsert_processed_email(
                    gmail_message_id=final_state["gmail_message_id"],
                    sender=final_state.get("sender", ""),
                    subject=final_state.get("subject", ""),
                    received_at=final_state.get("received_at"),
                    category=final_state.get("category", "others"),
                    confidence=float(final_state.get("confidence", 0.0)),
                    summary=final_state.get("summary", ""),
                    status=final_state.get("status", "processed"),
                )

                self.repo.insert_workflow_log(
                    gmail_message_id=final_state["gmail_message_id"],
                    action="graph_completed",
                    details={
                        "category": final_state.get("category"),
                        "confidence": final_state.get("confidence"),
                        "executed_actions": final_state.get("executed_actions", []),
                        "needs_human_review": final_state.get("needs_human_review", False),
                    },
                )
            except Exception as exc:  # pragma: no cover
                logger.exception("Failed processing message %s: %s", message_id, exc)
                self.repo.insert_workflow_log(
                    gmail_message_id=message_id,
                    action="processing_error",
                    details={"error": str(exc)},
                )

        self.repo.set_watch_state("last_history_id", incoming_history_id)
        self.repo.update_inbox_event_status(event_id, "processed")

    def _fetch_changed_message_ids(self, start_history_id: str) -> list[str]:
        gmail = build_gmail_client()
        message_ids: set[str] = set()
        page_token: str | None = None

        while True:
            try:
                request = (
                    gmail.users()
                    .history()
                    .list(
                        userId="me",
                        startHistoryId=start_history_id,
                        historyTypes=["messageAdded"],
                        pageToken=page_token,
                        maxResults=100,
                    )
                )
                response = request.execute()
            except HttpError as exc:
                logger.warning("history.list failed start_history_id=%s error=%s", start_history_id, exc)
                return []

            for item in response.get("history", []):
                for added in item.get("messagesAdded", []):
                    msg = added.get("message", {})
                    msg_id = msg.get("id")
                    if msg_id:
                        message_ids.add(msg_id)

            page_token = response.get("nextPageToken")
            if not page_token:
                break

        return sorted(message_ids)

    def _load_message_state(self, message_id: str, history_id: str) -> EmailState:
        gmail = build_gmail_client()
        message = (
            gmail.users()
            .messages()
            .get(
                userId="me",
                id=message_id,
                format="metadata",
                metadataHeaders=["From", "Subject", "Date"],
            )
            .execute()
        )

        headers = {
            header.get("name", ""): header.get("value", "")
            for header in message.get("payload", {}).get("headers", [])
        }

        date_value = headers.get("Date")
        received_at: str | None = None
        if date_value:
            try:
                received_at = parsedate_to_datetime(date_value).astimezone(timezone.utc).isoformat()
            except Exception:
                received_at = None

        return EmailState(
            gmail_message_id=message_id,
            history_id=history_id,
            sender=headers.get("From", "Unknown"),
            subject=headers.get("Subject", "No Subject"),
            received_at=received_at,
            snippet=message.get("snippet", ""),
            executed_actions=[],
            status="processing",
        )

    def _build_graph(self):
        graph = StateGraph(EmailState)
        graph.add_node("classify", self._node_classify)
        graph.add_node("route", self._node_route)
        graph.add_node("execute", self._node_execute)
        graph.add_node("review", self._node_review)

        graph.set_entry_point("classify")
        graph.add_edge("classify", "route")
        graph.add_conditional_edges(
            "route",
            self._route_decision,
            {"execute": "execute", "review": "review"},
        )
        graph.add_edge("execute", END)
        graph.add_edge("review", END)

        return graph.compile()

    def _node_classify(self, state: EmailState) -> EmailState:
        subject = state.get("subject", "").lower()
        snippet = state.get("snippet", "").lower()
        sender = state.get("sender", "").lower()

        category = "others"
        confidence = 0.70

        if any(token in subject for token in ["meeting", "invite", "calendar"]):
            category = "meeting"
            confidence = 0.90
        elif any(token in subject for token in ["newsletter", "sale", "promo", "discount"]):
            category = "marketing"
            confidence = 0.88
        elif "?" in state.get("subject", "") or "please" in snippet:
            category = "action_required"
            confidence = 0.82
        elif "noreply" in sender or "notification" in subject:
            category = "information"
            confidence = 0.80

        summary = self._summarize_text(state.get("subject", ""), state.get("snippet", ""))

        state["category"] = category
        state["confidence"] = confidence
        state["summary"] = summary
        return state

    def _node_route(self, state: EmailState) -> EmailState:
        category = state.get("category", "others")
        confidence = float(state.get("confidence", 0.0))

        proposed: list[str] = []
        if category == "marketing":
            proposed.append("label_marketing")
        elif category == "meeting":
            proposed.extend(["summarize", "create_calendar_event"])
        elif category == "action_required":
            proposed.extend(["summarize", "draft_reply"])
        elif category == "information":
            proposed.append("summarize")

        needs_review = confidence < self.settings.confidence_threshold
        if any(action in self.settings.approval_required_actions for action in proposed):
            needs_review = True

        state["proposed_actions"] = proposed
        state["needs_human_review"] = needs_review
        return state

    def _route_decision(self, state: EmailState) -> str:
        return "review" if state.get("needs_human_review") else "execute"

    def _node_execute(self, state: EmailState) -> EmailState:
        executed = list(state.get("executed_actions", []))
        for action in state.get("proposed_actions", []):
            if action not in self.settings.auto_actions:
                continue
            if action == "label_marketing":
                self._apply_marketing_labels(state["gmail_message_id"])
                executed.append(action)
            elif action == "summarize":
                self._send_telegram_summary(state, requires_review=False)
                executed.append(action)
            elif action == "draft_reply":
                self._send_telegram_reply_draft_notice(state)
                executed.append(action)

        state["executed_actions"] = executed
        state["status"] = "processed"
        return state

    def _node_review(self, state: EmailState) -> EmailState:
        self._send_telegram_summary(state, requires_review=True)
        state["status"] = "pending_human_review"
        return state

    def _summarize_text(self, subject: str, snippet: str) -> str:
        snippet_clean = (snippet or "").replace("\n", " ").strip()
        if not snippet_clean:
            return f"Email received: {subject}"
        if len(snippet_clean) > 240:
            snippet_clean = f"{snippet_clean[:237]}..."
        return f"{subject}: {snippet_clean}"

    def _apply_marketing_labels(self, gmail_message_id: str) -> None:
        gmail = build_gmail_client()
        include_label_id = self._resolve_label_id(self.settings.gmail_label_include)
        delete_label_id = self._resolve_label_id(self.settings.gmail_label_to_delete)

        add_label_ids = [label_id for label_id in [include_label_id, delete_label_id] if label_id]
        if not add_label_ids:
            return

        gmail.users().messages().modify(
            userId="me",
            id=gmail_message_id,
            body={"addLabelIds": add_label_ids},
        ).execute()

    def _resolve_label_id(self, label_name: str) -> str | None:
        if label_name in self._label_cache:
            return self._label_cache[label_name]

        gmail = build_gmail_client()
        labels_response = gmail.users().labels().list(userId="me").execute()
        for label in labels_response.get("labels", []):
            self._label_cache[label.get("name", "")] = label.get("id")

        return self._label_cache.get(label_name)

    def _send_telegram_summary(self, state: EmailState, requires_review: bool) -> None:
        if not self.settings.telegram_bot_token or not self.settings.telegram_chat_id:
            return

        review_line = "\nReview required: yes" if requires_review else "\nReview required: no"
        text = (
            f"Gmail update\n"
            f"Category: {state.get('category', 'unknown')}\n"
            f"Confidence: {state.get('confidence', 0.0):.2f}\n"
            f"From: {state.get('sender', 'unknown')}\n"
            f"Subject: {state.get('subject', 'no subject')}\n"
            f"Summary: {state.get('summary', '')}"
            f"{review_line}"
        )
        self._telegram_send(text)

    def _send_telegram_reply_draft_notice(self, state: EmailState) -> None:
        if not self.settings.telegram_bot_token or not self.settings.telegram_chat_id:
            return
        text = (
            "Reply draft suggested\n"
            f"Message ID: {state.get('gmail_message_id')}\n"
            f"Subject: {state.get('subject', 'no subject')}"
        )
        self._telegram_send(text)

    def _telegram_send(self, text: str) -> None:
        api_base = f"https://api.telegram.org/bot{self.settings.telegram_bot_token}/sendMessage"
        data = urllib.parse.urlencode(
            {
                "chat_id": self.settings.telegram_chat_id,
                "text": text,
            }
        ).encode("utf-8")

        request = urllib.request.Request(api_base, data=data, method="POST")
        with urllib.request.urlopen(request, timeout=10) as response:
            raw = response.read().decode("utf-8")
            payload = json.loads(raw)
            if not payload.get("ok"):
                logger.warning("Telegram send failed payload=%s", payload)


def parse_publish_time(value: str | None) -> str | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return dt.astimezone(timezone.utc).isoformat()
    except ValueError:
        return None
