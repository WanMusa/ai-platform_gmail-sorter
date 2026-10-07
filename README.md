# ai-platform_gmail-sorter
A Gmail agentic sorter that manages gmail and sends summary through to Telegram (auto deployment)

# Gmail Sorter

Event-driven Gmail assistant using:

- LangGraph
- Gmail API
- Google Calendar API
- Telegram
- SQLite
- Docker
- GitHub Actions

## Gmail Watch Registration

After OAuth token setup, register Gmail webhook watch locally:

1. Set values in `.env`:
	- `GCP_PROJECT_ID`
	- `GMAIL_PUBSUB_TOPIC`
	- `GMAIL_WATCH_LABEL_IDS` (recommended: `INBOX`)
2. Run watch registration:

```powershell
python -m src.register_watch register
```

To stop watch:

```powershell
python -m src.register_watch stop
```

Gmail watches expire periodically by design. This app can renew automatically when running on VPS:

- `GMAIL_WATCH_AUTO_RENEW=true`
- `GMAIL_WATCH_RENEW_INTERVAL_SECONDS=21600`

When enabled, the app renews watch in the background and logs renewal results.