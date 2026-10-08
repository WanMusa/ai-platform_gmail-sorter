from dataclasses import dataclass
import os


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


def _env_csv(name: str, default: str) -> list[str]:
	raw = os.getenv(name, default)
	return [item.strip() for item in raw.split(",") if item.strip()]


@dataclass(frozen=True)
class Settings:
	app_domain: str
	app_port: int
	log_level: str
	retry_max_attempts: int

	google_token_path: str
	google_oauth_scopes: list[str]
	gmail_webhook_path: str
	gmail_label_include: str
	gmail_label_to_delete: str
	calendar_id: str
	calendar_timezone: str
	calendar_default_duration_minutes: int

	watch_auto_renew: bool
	watch_renew_interval_seconds: int

	gcp_project_id: str
	gmail_pubsub_topic: str
	gmail_watch_label_ids: list[str]
	gmail_watch_label_filter_behavior: str

	telegram_bot_token: str
	telegram_chat_id: str
	telegram_webhook_secret: str

	auto_actions: list[str]
	approval_required_actions: list[str]
	telegram_notify_categories: list[str]
	confidence_threshold: float

	db_host: str
	db_port: int
	db_name: str
	db_user: str
	db_password: str


def get_settings() -> Settings:
	confidence_raw = os.getenv("CONFIDENCE_THRESHOLD", "0.80")
	try:
		confidence = float(confidence_raw)
	except ValueError:
		confidence = 0.80

	return Settings(
		app_domain=os.getenv("APP_DOMAIN", ""),
		app_port=_env_int("APP_PORT", 8000),
		log_level=os.getenv("LOG_LEVEL", "INFO"),
		retry_max_attempts=_env_int("RETRY_MAX_ATTEMPTS", 2),
		google_token_path=os.getenv("GOOGLE_TOKEN_PATH", "token.json"),
		google_oauth_scopes=_env_csv(
			"GOOGLE_OAUTH_SCOPES",
			"https://www.googleapis.com/auth/gmail.modify,https://www.googleapis.com/auth/gmail.send,https://www.googleapis.com/auth/calendar.events",
		),
		gmail_webhook_path=os.getenv("GMAIL_WEBHOOK_PATH", "/webhooks/gmail/pubsub"),
		gmail_label_include=os.getenv("GMAIL_LABEL_INCLUDE", "AI"),
		gmail_label_to_delete=os.getenv("GMAIL_LABEL_TO_DELETE", "AI/to-delete"),
		calendar_id=os.getenv("GOOGLE_CALENDAR_ID", "primary"),
		calendar_timezone=os.getenv("GOOGLE_CALENDAR_TIMEZONE", "Asia/Kuala_Lumpur"),
		calendar_default_duration_minutes=_env_int("CALENDAR_DEFAULT_DURATION_MINUTES", 60),
		watch_auto_renew=_env_bool("GMAIL_WATCH_AUTO_RENEW", True),
		watch_renew_interval_seconds=_env_int("GMAIL_WATCH_RENEW_INTERVAL_SECONDS", 21600),
		gcp_project_id=os.getenv("GCP_PROJECT_ID", ""),
		gmail_pubsub_topic=os.getenv("GMAIL_PUBSUB_TOPIC", "gmail-sorter-events"),
		gmail_watch_label_ids=_env_csv("GMAIL_WATCH_LABEL_IDS", "INBOX"),
		gmail_watch_label_filter_behavior=os.getenv(
			"GMAIL_WATCH_LABEL_FILTER_BEHAVIOR", "INCLUDE"
		).upper(),
		telegram_bot_token=os.getenv("TELEGRAM_BOT_TOKEN", ""),
		telegram_chat_id=os.getenv("TELEGRAM_CHAT_ID", ""),
		telegram_webhook_secret=os.getenv("TELEGRAM_WEBHOOK_SECRET", ""),
		auto_actions=_env_csv(
			"AUTO_ACTIONS", "label_marketing,summarize,draft_reply"
		),
		approval_required_actions=_env_csv(
			"APPROVAL_REQUIRED_ACTIONS", "send_reply,create_calendar_event,delete"
		),
		telegram_notify_categories=_env_csv(
			"TELEGRAM_NOTIFY_CATEGORIES", "meeting,action_required"
		),
		confidence_threshold=confidence,
		db_host=os.getenv("DATABASE_HOST", "postgres"),
		db_port=_env_int("DATABASE_PORT", 5432),
		db_name=os.getenv("DATABASE_NAME", "gmail_sorter"),
		db_user=os.getenv("DATABASE_USER", "gmail_sorter_user"),
		db_password=os.getenv("DATABASE_PASSWORD", ""),
	)
