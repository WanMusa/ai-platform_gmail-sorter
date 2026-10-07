from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import psycopg
from psycopg.rows import dict_row

from src.config import Settings


def _dsn(settings: Settings) -> str:
	return (
		f"host={settings.db_host} "
		f"port={settings.db_port} "
		f"dbname={settings.db_name} "
		f"user={settings.db_user} "
		f"password={settings.db_password}"
	)


class Database:
	def __init__(self, settings: Settings):
		self.settings = settings
		self._dsn = _dsn(settings)

	def connect(self) -> psycopg.Connection:
		return psycopg.connect(self._dsn, row_factory=dict_row)

	def apply_migrations(self) -> None:
		migration_dir = Path(__file__).resolve().parent.parent / "migrations"
		migration_files = sorted(migration_dir.glob("*.sql"))

		with self.connect() as conn:
			with conn.cursor() as cur:
				cur.execute(
					"""
					CREATE TABLE IF NOT EXISTS schema_migrations (
						id SERIAL PRIMARY KEY,
						filename TEXT UNIQUE NOT NULL,
						applied_at TIMESTAMP DEFAULT NOW() NOT NULL
					);
					"""
				)

				cur.execute("SELECT filename FROM schema_migrations")
				applied = {row["filename"] for row in cur.fetchall()}

				for migration in migration_files:
					if migration.name in applied:
						continue

					sql = migration.read_text(encoding="utf-8")
					cur.execute(sql)
					cur.execute(
						"INSERT INTO schema_migrations (filename) VALUES (%s)",
						(migration.name,),
					)

			conn.commit()


class Repo:
	def __init__(self, db: Database):
		self.db = db

	def create_inbox_event(
		self,
		pubsub_message_id: str,
		publish_time: str | None,
		email_address: str | None,
		history_id: str | None,
		payload: dict[str, Any],
	) -> tuple[int, bool]:
		with self.db.connect() as conn:
			with conn.cursor() as cur:
				cur.execute(
					"""
					INSERT INTO inbox_events (
						pubsub_message_id,
						publish_time,
						email_address,
						history_id,
						payload,
						status
					)
					VALUES (%s, %s, %s, %s, %s::jsonb, 'received')
					ON CONFLICT (pubsub_message_id) DO NOTHING
					RETURNING id
					""",
					(
						pubsub_message_id,
						publish_time,
						email_address,
						history_id,
						json.dumps(payload),
					),
				)
				row = cur.fetchone()
				conn.commit()

				if row:
					return row["id"], True

				cur.execute(
					"SELECT id FROM inbox_events WHERE pubsub_message_id = %s",
					(pubsub_message_id,),
				)
				existing = cur.fetchone()
				return existing["id"], False

	def update_inbox_event_status(self, event_id: int, status: str, error: str | None = None) -> None:
		with self.db.connect() as conn:
			with conn.cursor() as cur:
				cur.execute(
					"""
					UPDATE inbox_events
					SET status = %s,
						error = %s,
						updated_at = NOW()
					WHERE id = %s
					""",
					(status, error, event_id),
				)
			conn.commit()

	def get_watch_state(self, key: str) -> str | None:
		with self.db.connect() as conn:
			with conn.cursor() as cur:
				cur.execute("SELECT value FROM watch_state WHERE key = %s", (key,))
				row = cur.fetchone()
				return row["value"] if row else None

	def set_watch_state(self, key: str, value: str) -> None:
		with self.db.connect() as conn:
			with conn.cursor() as cur:
				cur.execute(
					"""
					INSERT INTO watch_state (key, value)
					VALUES (%s, %s)
					ON CONFLICT (key)
					DO UPDATE SET value = EXCLUDED.value, updated_at = NOW()
					""",
					(key, value),
				)
			conn.commit()

	def upsert_processed_email(
		self,
		gmail_message_id: str,
		sender: str,
		subject: str,
		received_at: str | None,
		category: str,
		confidence: float,
		summary: str,
		status: str,
	) -> None:
		with self.db.connect() as conn:
			with conn.cursor() as cur:
				cur.execute(
					"""
					INSERT INTO processed_emails (
						gmail_message_id,
						sender,
						subject,
						received_at,
						processed_at,
						category,
						confidence,
						summary,
						status
					)
					VALUES (%s, %s, %s, %s, NOW(), %s, %s, %s, %s)
					ON CONFLICT (gmail_message_id)
					DO UPDATE SET
						sender = EXCLUDED.sender,
						subject = EXCLUDED.subject,
						received_at = EXCLUDED.received_at,
						processed_at = NOW(),
						category = EXCLUDED.category,
						confidence = EXCLUDED.confidence,
						summary = EXCLUDED.summary,
						status = EXCLUDED.status
					""",
					(
						gmail_message_id,
						sender,
						subject,
						received_at,
						category,
						confidence,
						summary,
						status,
					),
				)
			conn.commit()

	def insert_workflow_log(self, gmail_message_id: str, action: str, details: dict[str, Any]) -> None:
		with self.db.connect() as conn:
			with conn.cursor() as cur:
				cur.execute(
					"""
					INSERT INTO workflow_log (
						gmail_message_id,
						action,
						timestamp,
						details
					)
					VALUES (%s, %s, NOW(), %s)
					""",
					(gmail_message_id, action, json.dumps(details)),
				)
			conn.commit()

	def create_pending_review(
		self,
		review_id: str,
		gmail_message_id: str,
		state_payload: dict[str, Any],
		telegram_chat_id: str,
	) -> None:
		with self.db.connect() as conn:
			with conn.cursor() as cur:
				cur.execute(
					"""
					INSERT INTO pending_reviews (
						review_id,
						gmail_message_id,
						state_payload,
						telegram_chat_id,
						status
					)
					VALUES (%s, %s, %s::jsonb, %s, 'pending')
					ON CONFLICT (review_id) DO NOTHING
					""",
					(review_id, gmail_message_id, json.dumps(state_payload), telegram_chat_id),
				)
			conn.commit()

	def get_pending_review(self, review_id: str) -> dict[str, Any] | None:
		with self.db.connect() as conn:
			with conn.cursor() as cur:
				cur.execute(
					"""
					SELECT review_id, gmail_message_id, state_payload, telegram_chat_id, status
					FROM pending_reviews
					WHERE review_id = %s
					""",
					(review_id,),
				)
				return cur.fetchone()

	def resolve_pending_review(
		self,
		review_id: str,
		decision: str,
		decision_by: str | None,
	) -> None:
		with self.db.connect() as conn:
			with conn.cursor() as cur:
				cur.execute(
					"""
					UPDATE pending_reviews
					SET status = %s,
						decision_by = %s,
						decided_at = NOW(),
						updated_at = NOW()
					WHERE review_id = %s
					""",
					(decision, decision_by, review_id),
				)
			conn.commit()
