import argparse
import os
from pathlib import Path
from typing import Any

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build


DEFAULT_SCOPES = [
    "https://www.googleapis.com/auth/gmail.modify",
    "https://www.googleapis.com/auth/gmail.send",
]


def _read_env(name: str, default: str | None = None, required: bool = False) -> str:
    value = os.getenv(name, default)
    if required and not value:
        raise ValueError(f"Missing required environment variable: {name}")
    return value or ""


def _load_dotenv_file(env_path: Path = Path(".env")) -> None:
    if not env_path.exists():
        return

    for line in env_path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue

        key, value = stripped.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")

        os.environ.setdefault(key, value)


def _load_credentials(token_path: Path, scopes: list[str]) -> Credentials:
    if not token_path.exists():
        raise FileNotFoundError(
            f"Token file not found at {token_path}. Generate it first with your OAuth flow."
        )

    creds = Credentials.from_authorized_user_file(str(token_path), scopes)

    if creds.expired and creds.refresh_token:
        creds.refresh(Request())
        token_path.write_text(creds.to_json(), encoding="utf-8")

    return creds


def _load_scopes() -> list[str]:
    scopes_raw = _read_env("GOOGLE_OAUTH_SCOPES", default=",".join(DEFAULT_SCOPES))
    return [scope.strip() for scope in scopes_raw.split(",") if scope.strip()]


def build_google_service(service_name: str, version: str):
    token_path = Path(_read_env("GOOGLE_TOKEN_PATH", default="token.json"))
    scopes = _load_scopes()
    creds = _load_credentials(token_path=token_path, scopes=scopes)
    return build(service_name, version, credentials=creds)


def build_gmail_client():
    return build_google_service("gmail", "v1")


def register_watch() -> dict[str, Any]:
    project_id = _read_env("GCP_PROJECT_ID", required=True)
    topic_name = _read_env("GMAIL_PUBSUB_TOPIC", default="gmail-sorter-events")

    label_ids_raw = _read_env("GMAIL_WATCH_LABEL_IDS", default="INBOX")
    label_ids = [item.strip() for item in label_ids_raw.split(",") if item.strip()]

    label_filter_behavior = _read_env(
        "GMAIL_WATCH_LABEL_FILTER_BEHAVIOR", default="INCLUDE"
    ).upper()

    gmail = build_gmail_client()

    request_body = {
        "topicName": f"projects/{project_id}/topics/{topic_name}",
        "labelIds": label_ids,
        "labelFilterBehavior": label_filter_behavior,
    }

    response = gmail.users().watch(userId="me", body=request_body).execute()

    return response


def register_watch_cli() -> None:
    response = register_watch()

    print("Watch registration successful")
    print(f"History ID: {response.get('historyId')}")
    print(f"Expiration (ms epoch): {response.get('expiration')}")


def stop_watch() -> None:
    gmail = build_gmail_client()

    gmail.users().stop(userId="me").execute()
    print("Watch stopped")


def main() -> None:
    _load_dotenv_file()

    parser = argparse.ArgumentParser(description="Register or stop Gmail Pub/Sub watch")
    parser.add_argument(
        "action",
        choices=["register", "stop"],
        help="register = create/refresh watch, stop = remove watch",
    )

    args = parser.parse_args()

    if args.action == "register":
        register_watch_cli()
        return

    stop_watch()


if __name__ == "__main__":
    main()