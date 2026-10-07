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
2. Put OAuth token at repo root as `token.json` (or set `GOOGLE_TOKEN_PATH` to a custom path).
   In Docker deployment, `./token.json` is mounted to `/app/token.json`.
3. Run watch registration:

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

## Telegram Interactive Approval

The app supports Telegram inline approval buttons for human review actions.

Required env:

- `TELEGRAM_BOT_TOKEN`
- `TELEGRAM_CHAT_ID`
- `TELEGRAM_WEBHOOK_SECRET` (recommended)

Set Telegram webhook to app endpoint:

```text
https://wanagents.duckdns.org/webhooks/telegram
```

Set webhook with optional secret token:

```text
https://api.telegram.org/bot<TELEGRAM_BOT_TOKEN>/setWebhook?url=https://wanagents.duckdns.org/webhooks/telegram&secret_token=<TELEGRAM_WEBHOOK_SECRET>
```

## Calendar Event Creation

When meeting confirmation is approved, the app can create a Google Calendar event.

Required env:

- `GOOGLE_CALENDAR_ID` (default: `primary`)
- `GOOGLE_CALENDAR_TIMEZONE` (default: `Asia/Kuala_Lumpur`)
- `CALENDAR_DEFAULT_DURATION_MINUTES` (default: `60`)

Important:

- `GOOGLE_OAUTH_SCOPES` must include `https://www.googleapis.com/auth/calendar.events`.
- If your token was created before adding this scope, run OAuth again to regenerate `token.json`.