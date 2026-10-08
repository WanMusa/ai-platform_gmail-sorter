from __future__ import annotations

import json
import logging
import urllib.error
import urllib.parse
import urllib.request
import base64
import re
import threading
from uuid import uuid4
from datetime import datetime, timedelta, timezone
from email.mime.text import MIMEText
from email.utils import parsedate_to_datetime, parseaddr
from typing import Any, TypedDict

from googleapiclient.errors import HttpError
from langgraph.graph import StateGraph, END

from src.config import Settings
from src.db import Repo
from src.register_watch import build_gmail_client, build_google_service

logger = logging.getLogger("gmail-sorter")


class EmailState(TypedDict, total=False):
    gmail_message_id: str
    history_id: str
    sender: str
    subject: str
    received_at: str | None
    thread_id: str
    sender_email: str
    message_rfc822_id: str
    snippet: str
    category: str
    confidence: float
    summary: str
    needs_human_review: bool
    proposed_actions: list[str]
    executed_actions: list[str]
    review_type: str
    reply_instruction_text: str
    status: str


class GmailProcessor:
    def __init__(self, settings: Settings, repo: Repo):
        self.settings = settings
        self.repo = repo
        self._label_cache: dict[str, str] = {}
        self._processing_lock = threading.Lock()
        self._graph = self._build_graph()

    def process_pubsub_event(self, event_id: int, decoded_payload: dict[str, Any]) -> None:
        with self._processing_lock:
            self._process_pubsub_event_locked(event_id, decoded_payload)

    def _process_pubsub_event_locked(self, event_id: int, decoded_payload: dict[str, Any]) -> None:
        logger.info("Processing inbox event id=%s payload=%s", event_id, decoded_payload)
        self.repo.update_inbox_event_status(event_id, "processing")

        incoming_history_id = str(decoded_payload.get("historyId") or "")
        if not incoming_history_id:
            self.repo.update_inbox_event_status(event_id, "ignored", "Missing historyId")
            return

        last_history_id = self.repo.get_watch_state("last_history_id")
        if not last_history_id:
            self.repo.set_watch_state("last_history_id", incoming_history_id)
            self.repo.update_inbox_event_status(event_id, "checkpoint_initialized")
            logger.info(
                "Initialized history checkpoint event_id=%s history_id=%s",
                event_id,
                incoming_history_id,
            )
            return

        message_ids = self._fetch_changed_message_ids(last_history_id)
        if not message_ids:
            self.repo.set_watch_state("last_history_id", incoming_history_id)
            self.repo.update_inbox_event_status(event_id, "no_messages")
            logger.info(
                "No new messageAdded records event_id=%s from_history_id=%s to_history_id=%s",
                event_id,
                last_history_id,
                incoming_history_id,
            )
            return

        logger.info(
            "Found %s changed message(s) event_id=%s from_history_id=%s",
            len(message_ids),
            event_id,
            last_history_id,
        )

        for message_id in message_ids:
            if self.repo.has_processed_email(message_id):
                logger.info("Skipping already processed message_id=%s", message_id)
                continue

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
                logger.info(
                    "Graph complete message_id=%s category=%s confidence=%.2f status=%s actions=%s",
                    final_state["gmail_message_id"],
                    final_state.get("category", "others"),
                    float(final_state.get("confidence", 0.0)),
                    final_state.get("status", "processed"),
                    final_state.get("executed_actions", []),
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
                metadataHeaders=["From", "Subject", "Date", "Message-ID"],
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
            sender_email=parseaddr(headers.get("From", ""))[1],
            subject=headers.get("Subject", "No Subject"),
            received_at=received_at,
            thread_id=message.get("threadId", ""),
            message_rfc822_id=headers.get("Message-ID", ""),
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
        else:
            proposed.append("summarize")

        review_type = "none"
        needs_review = False
        if confidence < self.settings.confidence_threshold:
            needs_review = True
            review_type = "category_select"
        elif category == "action_required":
            needs_review = True
            review_type = "reply_mode"
        elif category == "meeting":
            needs_review = True
            review_type = "meeting_confirm"

        state["proposed_actions"] = proposed
        state["needs_human_review"] = needs_review
        state["review_type"] = review_type
        return state

    def _route_decision(self, state: EmailState) -> str:
        return "review" if state.get("needs_human_review") else "execute"

    def _node_execute(self, state: EmailState) -> EmailState:
        state = self._execute_actions(state, allow_sensitive=False)
        state["status"] = "processed"
        return state

    def _node_review(self, state: EmailState) -> EmailState:
        review_id = uuid4().hex
        self.repo.create_pending_review(
            review_id=review_id,
            gmail_message_id=state["gmail_message_id"],
            state_payload=dict(state),
            telegram_chat_id=self.settings.telegram_chat_id,
        )
        self._send_telegram_review_request(state, review_id, state.get("review_type", "none"))
        state["status"] = "pending_human_review"
        return state

    def handle_telegram_callback(self, callback_data: str, decision_by: str | None) -> str:
        if not callback_data.startswith("review:"):
            return "Unsupported action"

        parts = callback_data.split(":")
        if len(parts) < 4:
            return "Malformed review action"

        _, review_id, action_type, action_value = parts[0], parts[1], parts[2], parts[3]
        review = self.repo.get_pending_review(review_id)
        if not review:
            return "Review not found"

        if review.get("status") not in {"pending", "awaiting_reply_text"}:
            return f"Review already resolved as {review.get('status')}"

        state_payload = review.get("state_payload") or {}
        state = EmailState(**state_payload)

        if action_type == "cat":
            state["category"] = action_value
            state["confidence"] = 1.0
            state = self._apply_category_override(state)
            self.repo.update_pending_review_payload(review_id, dict(state))

            next_review = state.get("review_type", "none")
            if next_review == "reply_mode":
                self._send_reply_mode_request(state, review_id)
                self.repo.set_pending_review_status(review_id, "pending", decision_by)
                return "Category set to action_required. Choose reply mode"
            if next_review == "meeting_confirm":
                self._send_meeting_confirmation_request(state, review_id)
                self.repo.set_pending_review_status(review_id, "pending", decision_by)
                return "Category set to meeting. Confirm calendar action"

            state = self._execute_actions(state, allow_sensitive=False)
            state["status"] = "approved_and_processed"
            self.repo.resolve_pending_review(review_id, "approved", decision_by)
            self._persist_state_after_review(state, review_id, decision_by, "review_category_override")
            return f"Category updated to {action_value}. Actions executed"

        if action_type == "reply":
            if action_value == "draft":
                state = self._execute_actions(state, allow_sensitive=False)
                state["status"] = "approved_and_processed"
                self.repo.resolve_pending_review(review_id, "approved", decision_by)
                self._persist_state_after_review(state, review_id, decision_by, "review_reply_draft")
                return "Draft reply flow approved"

            if action_value == "manual":
                self.repo.set_pending_review_status(review_id, "awaiting_reply_text", decision_by)
                self._send_reply_text_prompt(state)
                return "Send the exact reply text in your next message"

            if action_value == "skip":
                state["proposed_actions"] = [
                    action for action in state.get("proposed_actions", []) if action != "draft_reply"
                ]
                state = self._execute_actions(state, allow_sensitive=False)
                state["status"] = "approved_and_processed"
                self.repo.resolve_pending_review(review_id, "approved", decision_by)
                self._persist_state_after_review(state, review_id, decision_by, "review_reply_skip")
                return "Reply skipped. Summary sent"

        if action_type == "meeting":
            if action_value == "yes":
                state = self._execute_actions(state, allow_sensitive=True)
                state["status"] = "approved_and_processed"
                self.repo.resolve_pending_review(review_id, "approved", decision_by)
                self._persist_state_after_review(state, review_id, decision_by, "review_meeting_approved")
                if "calendar_event_created" in state.get("executed_actions", []):
                    return "Meeting confirmed. Calendar event created"
                return "Meeting confirmed, but event was not created (date/time may be unclear)"

            if action_value == "no":
                state["proposed_actions"] = [
                    action
                    for action in state.get("proposed_actions", [])
                    if action != "create_calendar_event"
                ]
                state = self._execute_actions(state, allow_sensitive=False)
                state["status"] = "rejected"
                self.repo.resolve_pending_review(review_id, "rejected", decision_by)
                self._persist_state_after_review(state, review_id, decision_by, "review_meeting_rejected")
                return "Meeting action skipped"

        return "Unknown review decision"

    def handle_telegram_text(self, text: str, decision_by: str, chat_id: str) -> str:
        pending = self.repo.get_pending_reply_prompt(decision_by)
        if not pending:
            if text.strip() == "/start":
                self._telegram_send(
                    "Gmail Sorter bot is active. I will send summaries and approval requests here.",
                    chat_id=chat_id,
                )
                return "Start acknowledged"
            return "No pending reply request"

        review_id = pending["review_id"]
        state_payload = pending.get("state_payload") or {}
        state = EmailState(**state_payload)
        state["reply_instruction_text"] = text.strip()

        state = self._execute_actions(state, allow_sensitive=True)
        self._send_telegram_manual_reply_capture_notice(state)
        state["status"] = "approved_and_processed"
        self.repo.resolve_pending_review(review_id, "approved", decision_by)
        self._persist_state_after_review(state, review_id, decision_by, "review_manual_reply_captured")
        return "Reply text captured"

    def answer_telegram_callback_query(self, callback_query_id: str, text: str) -> None:
        if not self.settings.telegram_bot_token:
            return
        api_base = f"https://api.telegram.org/bot{self.settings.telegram_bot_token}/answerCallbackQuery"
        self._telegram_post(
            api_base,
            {
                "callback_query_id": callback_query_id,
                "text": text,
                "show_alert": False,
            },
        )

    def _execute_actions(self, state: EmailState, allow_sensitive: bool) -> EmailState:
        executed = list(state.get("executed_actions", []))
        for action in state.get("proposed_actions", []):
            if not allow_sensitive and action not in self.settings.auto_actions:
                continue
            if action == "label_marketing":
                self._apply_marketing_labels(state["gmail_message_id"])
                executed.append(action)
            elif action == "summarize":
                self._send_telegram_summary(state, requires_review=False)
                executed.append(action)
            elif action == "draft_reply":
                result = self._handle_reply_action(state)
                if result:
                    executed.append(result)
                executed.append(action)
            elif action == "create_calendar_event":
                event_result = self._create_calendar_event(state)
                if event_result:
                    executed.append("calendar_event_created")
                    executed.append(action)

        state["executed_actions"] = executed
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
            logger.warning(
                "No marketing labels resolved for message_id=%s include=%s delete=%s",
                gmail_message_id,
                self.settings.gmail_label_include,
                self.settings.gmail_label_to_delete,
            )
            return

        gmail.users().messages().modify(
            userId="me",
            id=gmail_message_id,
            body={"addLabelIds": add_label_ids},
        ).execute()

    def _resolve_label_id(self, label_name: str) -> str | None:
        if label_name in self._label_cache:
            return self._label_cache[label_name]

        normalized = label_name.lower().strip()
        cache_key = f"__norm__:{normalized}"
        if cache_key in self._label_cache:
            return self._label_cache[cache_key]

        gmail = build_gmail_client()
        labels_response = gmail.users().labels().list(userId="me").execute()
        for label in labels_response.get("labels", []):
            name = label.get("name", "")
            label_id = label.get("id")
            self._label_cache[name] = label_id
            self._label_cache[f"__norm__:{name.lower().strip()}"] = label_id

        resolved = self._label_cache.get(label_name) or self._label_cache.get(cache_key)
        if not resolved:
            logger.warning("Label not found for configured name=%s", label_name)
        return resolved

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

    def _send_telegram_review_request(self, state: EmailState, review_id: str, review_type: str) -> None:
        if not self.settings.telegram_bot_token or not self.settings.telegram_chat_id:
            return

        if review_type == "category_select":
            self._send_category_selection_request(state, review_id)
            return

        if review_type == "reply_mode":
            self._send_reply_mode_request(state, review_id)
            return

        if review_type == "meeting_confirm":
            self._send_meeting_confirmation_request(state, review_id)
            return

        self._send_telegram_summary(state, requires_review=False)

    def _send_telegram_reply_draft_notice(self, state: EmailState) -> None:
        if not self.settings.telegram_bot_token or not self.settings.telegram_chat_id:
            return
        text = (
            "Reply draft suggested\n"
            f"Message ID: {state.get('gmail_message_id')}\n"
            f"Subject: {state.get('subject', 'no subject')}"
        )
        self._telegram_send(text)

    def _handle_reply_action(self, state: EmailState) -> str | None:
        reply_text = state.get("reply_instruction_text", "").strip()
        if reply_text:
            sent_id = self._send_gmail_reply(state, reply_text)
            if sent_id:
                self._send_telegram_reply_sent_notice(state, sent_id)
                return "reply_sent"
            return None

        draft_text = self._build_default_reply_text(state)
        draft_id = self._create_gmail_reply_draft(state, draft_text)
        if draft_id:
            self._send_telegram_reply_draft_notice(state)
            self._send_telegram_draft_created_notice(state, draft_id)
            return "reply_draft_created"
        return None

    def _build_default_reply_text(self, state: EmailState) -> str:
        return (
            "Thanks for your email.\n\n"
            "I have reviewed your message and will get back to you shortly.\n\n"
            "Best regards"
        )

    def _create_gmail_reply_draft(self, state: EmailState, reply_text: str) -> str | None:
        gmail = build_gmail_client()
        sender_email = state.get("sender_email", "")
        if not sender_email:
            return None

        mime = MIMEText(reply_text)
        mime["to"] = sender_email
        mime["subject"] = self._reply_subject(state.get("subject", ""))
        if state.get("message_rfc822_id"):
            mime["In-Reply-To"] = state["message_rfc822_id"]
            mime["References"] = state["message_rfc822_id"]

        raw = base64.urlsafe_b64encode(mime.as_bytes()).decode()
        response = (
            gmail.users()
            .drafts()
            .create(
                userId="me",
                body={
                    "message": {
                        "raw": raw,
                        "threadId": state.get("thread_id", ""),
                    }
                },
            )
            .execute()
        )
        return response.get("id")

    def _send_gmail_reply(self, state: EmailState, reply_text: str) -> str | None:
        gmail = build_gmail_client()
        sender_email = state.get("sender_email", "")
        if not sender_email:
            return None

        mime = MIMEText(reply_text)
        mime["to"] = sender_email
        mime["subject"] = self._reply_subject(state.get("subject", ""))
        if state.get("message_rfc822_id"):
            mime["In-Reply-To"] = state["message_rfc822_id"]
            mime["References"] = state["message_rfc822_id"]

        raw = base64.urlsafe_b64encode(mime.as_bytes()).decode()
        response = (
            gmail.users()
            .messages()
            .send(
                userId="me",
                body={
                    "raw": raw,
                    "threadId": state.get("thread_id", ""),
                },
            )
            .execute()
        )
        return response.get("id")

    def _reply_subject(self, subject: str) -> str:
        clean = subject.strip()
        if clean.lower().startswith("re:"):
            return clean
        return f"Re: {clean}"

    def _send_telegram_draft_created_notice(self, state: EmailState, draft_id: str) -> None:
        if not self.settings.telegram_bot_token or not self.settings.telegram_chat_id:
            return
        text = (
            "Draft created\n"
            f"Message ID: {state.get('gmail_message_id')}\n"
            f"Draft ID: {draft_id}"
        )
        self._telegram_send(text)

    def _send_telegram_reply_sent_notice(self, state: EmailState, sent_message_id: str) -> None:
        if not self.settings.telegram_bot_token or not self.settings.telegram_chat_id:
            return
        text = (
            "Reply sent\n"
            f"Message ID: {state.get('gmail_message_id')}\n"
            f"Sent Message ID: {sent_message_id}"
        )
        self._telegram_send(text)

    def _create_calendar_event(self, state: EmailState) -> str | None:
        event_window = self._infer_event_window(state)
        if not event_window:
            self._send_telegram_calendar_parse_failed(state)
            return None

        start_dt, end_dt = event_window
        try:
            calendar = build_google_service("calendar", "v3")

            event = {
                "summary": state.get("subject", "Meeting"),
                "description": (
                    f"From: {state.get('sender', 'unknown')}\n"
                    f"Summary: {state.get('summary', '')}\n"
                    f"Gmail Message ID: {state.get('gmail_message_id', '')}"
                ),
                "start": {
                    "dateTime": start_dt.isoformat(),
                    "timeZone": self.settings.calendar_timezone,
                },
                "end": {
                    "dateTime": end_dt.isoformat(),
                    "timeZone": self.settings.calendar_timezone,
                },
            }

            response = (
                calendar.events()
                .insert(calendarId=self.settings.calendar_id, body=event, sendUpdates="none")
                .execute()
            )

            event_id = response.get("id")
            event_link = response.get("htmlLink", "")
            logger.info(
                "Calendar event created message_id=%s event_id=%s start=%s end=%s",
                state.get("gmail_message_id", ""),
                event_id,
                start_dt.isoformat(),
                end_dt.isoformat(),
            )
            self._send_telegram_calendar_created_notice(state, event_id or "", event_link)
            return event_id
        except HttpError as exc:
            logger.warning(
                "Calendar API error message_id=%s error=%s",
                state.get("gmail_message_id", ""),
                exc,
            )
            self._send_telegram_calendar_api_failed(state, str(exc))
            return None
        except Exception as exc:  # pragma: no cover
            logger.exception("Unexpected calendar creation error: %s", exc)
            self._send_telegram_calendar_api_failed(state, str(exc))
            return None

    def _infer_event_window(self, state: EmailState) -> tuple[datetime, datetime] | None:
        text = f"{state.get('subject', '')} {state.get('snippet', '')}".lower()
        base_dt = datetime.now(timezone.utc)
        if state.get("received_at"):
            try:
                base_dt = datetime.fromisoformat(str(state["received_at"]))
            except ValueError:
                pass

        weekday_map = {
            "monday": 0,
            "tuesday": 1,
            "wednesday": 2,
            "thursday": 3,
            "friday": 4,
            "saturday": 5,
            "sunday": 6,
        }

        target_date = None
        for day_name, day_idx in weekday_map.items():
            if day_name in text:
                delta = (day_idx - base_dt.weekday()) % 7
                if delta == 0:
                    delta = 7
                target_date = (base_dt + timedelta(days=delta)).date()
                break

        date_match = re.search(r"(\d{4}-\d{2}-\d{2})", text)
        if date_match:
            try:
                target_date = datetime.strptime(date_match.group(1), "%Y-%m-%d").date()
            except ValueError:
                pass

        time_match = re.search(r"\b(\d{1,2})(?::(\d{2}))?\s*(am|pm)\b", text)
        hour = None
        minute = 0
        if time_match:
            hour = int(time_match.group(1))
            minute = int(time_match.group(2) or 0)
            ampm = time_match.group(3)
            if ampm == "pm" and hour != 12:
                hour += 12
            if ampm == "am" and hour == 12:
                hour = 0

        if target_date is None or hour is None:
            return None

        start = datetime(
            year=target_date.year,
            month=target_date.month,
            day=target_date.day,
            hour=hour,
            minute=minute,
            tzinfo=timezone.utc,
        )
        end = start + timedelta(minutes=self.settings.calendar_default_duration_minutes)
        return start, end

    def _send_telegram_calendar_created_notice(
        self,
        state: EmailState,
        event_id: str,
        event_link: str,
    ) -> None:
        if not self.settings.telegram_bot_token or not self.settings.telegram_chat_id:
            return
        text = (
            "Calendar event created\n"
            f"Message ID: {state.get('gmail_message_id')}\n"
            f"Event ID: {event_id}\n"
            f"Link: {event_link}"
        )
        self._telegram_send(text)

    def _send_telegram_calendar_parse_failed(self, state: EmailState) -> None:
        if not self.settings.telegram_bot_token or not self.settings.telegram_chat_id:
            return
        text = (
            "Calendar event not created\n"
            "I could not confidently extract meeting date/time from this email.\n"
            f"Subject: {state.get('subject', 'no subject')}"
        )
        self._telegram_send(text)

    def _send_telegram_calendar_api_failed(self, state: EmailState, error: str) -> None:
        if not self.settings.telegram_bot_token or not self.settings.telegram_chat_id:
            return
        text = (
            "Calendar event creation failed\n"
            f"Subject: {state.get('subject', 'no subject')}\n"
            f"Error: {error}"
        )
        self._telegram_send(text)

    def _send_telegram_manual_reply_capture_notice(self, state: EmailState) -> None:
        if not self.settings.telegram_bot_token or not self.settings.telegram_chat_id:
            return
        text = (
            "Reply text captured\n"
            f"Message ID: {state.get('gmail_message_id')}\n"
            f"Reply: {state.get('reply_instruction_text', '')}"
        )
        self._telegram_send(text)

    def _send_category_selection_request(self, state: EmailState, review_id: str) -> None:
        text = (
            "Low confidence classification\n"
            f"From: {state.get('sender', 'unknown')}\n"
            f"Subject: {state.get('subject', 'no subject')}\n"
            f"Summary: {state.get('summary', '')}\n\n"
            "Select the correct category:"
        )
        reply_markup = {
            "inline_keyboard": [
                [
                    {"text": "Information", "callback_data": f"review:{review_id}:cat:information"},
                    {"text": "Action", "callback_data": f"review:{review_id}:cat:action_required"},
                ],
                [
                    {"text": "Meeting", "callback_data": f"review:{review_id}:cat:meeting"},
                    {"text": "Marketing", "callback_data": f"review:{review_id}:cat:marketing"},
                ],
                [
                    {"text": "Other", "callback_data": f"review:{review_id}:cat:others"},
                ],
            ]
        }
        self._telegram_send(text, reply_markup=reply_markup)

    def _send_reply_mode_request(self, state: EmailState, review_id: str) -> None:
        text = (
            "Action required email\n"
            f"From: {state.get('sender', 'unknown')}\n"
            f"Subject: {state.get('subject', 'no subject')}\n"
            f"Summary: {state.get('summary', '')}\n\n"
            "How should I handle the reply?"
        )
        reply_markup = {
            "inline_keyboard": [
                [
                    {"text": "Draft Reply", "callback_data": f"review:{review_id}:reply:draft"},
                    {"text": "I will type reply", "callback_data": f"review:{review_id}:reply:manual"},
                ],
                [
                    {"text": "Skip reply", "callback_data": f"review:{review_id}:reply:skip"},
                ],
            ]
        }
        self._telegram_send(text, reply_markup=reply_markup)

    def _send_meeting_confirmation_request(self, state: EmailState, review_id: str) -> None:
        text = (
            "Meeting email detected\n"
            f"From: {state.get('sender', 'unknown')}\n"
            f"Subject: {state.get('subject', 'no subject')}\n"
            f"Summary: {state.get('summary', '')}\n\n"
            "Confirm appointment and create calendar event?"
        )
        reply_markup = {
            "inline_keyboard": [
                [
                    {"text": "Yes, add to calendar", "callback_data": f"review:{review_id}:meeting:yes"},
                    {"text": "No", "callback_data": f"review:{review_id}:meeting:no"},
                ]
            ]
        }
        self._telegram_send(text, reply_markup=reply_markup)

    def _send_reply_text_prompt(self, state: EmailState) -> None:
        text = (
            "Please send the exact reply text in your next message.\n"
            f"Subject: {state.get('subject', 'no subject')}"
        )
        self._telegram_send(text)

    def _telegram_send(
        self,
        text: str,
        reply_markup: dict[str, Any] | None = None,
        chat_id: str | None = None,
    ) -> None:
        api_base = f"https://api.telegram.org/bot{self.settings.telegram_bot_token}/sendMessage"
        payload = {
            "chat_id": chat_id or self.settings.telegram_chat_id,
            "text": text,
        }
        if reply_markup is not None:
            payload["reply_markup"] = reply_markup
        self._telegram_post(api_base, payload)

    def _telegram_post(self, url: str, payload: dict[str, Any]) -> bool:
        data = json.dumps(payload).encode("utf-8")
        request = urllib.request.Request(
            url,
            data=data,
            method="POST",
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                raw = response.read().decode("utf-8")
                body = json.loads(raw)
                if not body.get("ok"):
                    logger.warning(
                        "Telegram call failed payload=%s response=%s",
                        payload,
                        body,
                    )
                    return False
                return True
        except urllib.error.HTTPError as exc:
            logger.warning("Telegram HTTP error code=%s reason=%s", exc.code, exc.reason)
            return False
        except urllib.error.URLError as exc:
            logger.warning("Telegram network error=%s", exc)
            return False

    def _humanize_action(self, action: str) -> str:
        mapping = {
            "label_marketing": "Apply labels AI and AI/to-delete",
            "summarize": "Send summary to Telegram",
            "draft_reply": "Send reply-draft suggestion",
            "create_calendar_event": "Create a Google Calendar event",
        }
        return mapping.get(action, action)

    def _apply_category_override(self, state: EmailState) -> EmailState:
        category = state.get("category", "others")
        if category == "information":
            state["proposed_actions"] = ["summarize"]
            state["review_type"] = "none"
        elif category == "action_required":
            state["proposed_actions"] = ["summarize", "draft_reply"]
            state["review_type"] = "reply_mode"
        elif category == "meeting":
            state["proposed_actions"] = ["summarize", "create_calendar_event"]
            state["review_type"] = "meeting_confirm"
        elif category == "marketing":
            state["proposed_actions"] = ["label_marketing"]
            state["review_type"] = "none"
        else:
            state["proposed_actions"] = ["summarize"]
            state["review_type"] = "none"
        return state

    def _persist_state_after_review(
        self,
        state: EmailState,
        review_id: str,
        decision_by: str | None,
        action: str,
    ) -> None:
        self.repo.upsert_processed_email(
            gmail_message_id=state.get("gmail_message_id", ""),
            sender=state.get("sender", ""),
            subject=state.get("subject", ""),
            received_at=state.get("received_at"),
            category=state.get("category", "others"),
            confidence=float(state.get("confidence", 0.0)),
            summary=state.get("summary", ""),
            status=state.get("status", "processed"),
        )
        self.repo.insert_workflow_log(
            gmail_message_id=state.get("gmail_message_id", ""),
            action=action,
            details={
                "review_id": review_id,
                "decision_by": decision_by,
                "executed_actions": state.get("executed_actions", []),
                "review_type": state.get("review_type", "none"),
            },
        )


def parse_publish_time(value: str | None) -> str | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return dt.astimezone(timezone.utc).isoformat()
    except ValueError:
        return None
