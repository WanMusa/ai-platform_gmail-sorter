# Telegram Prompt Templates

## telegram_summary
From: {sender}
Update: {summary}

## reply_send_confirmation_request
I drafted a reply for this email from {sender}.

Should I send this?
"{suggested_reply}"

## reply_sent_notice
Sent.
I have replied to {sender}.

## calendar_created_notice
Calendar event created
Message ID: {gmail_message_id}
Event ID: {event_id}
Link: {event_link}

## calendar_parse_failed
Calendar event not created
I could not confidently extract meeting date/time from this email.
Subject: {subject}

## calendar_api_failed
Calendar event creation failed
Subject: {subject}
Error: {error}

## calendar_scope_failed
Calendar event creation failed
Subject: {subject}
Reason: OAuth token lacks calendar.events scope.
Action: regenerate token.json with GOOGLE_OAUTH_SCOPES including https://www.googleapis.com/auth/calendar.events and redeploy.

## manual_reply_capture_notice
Reply text captured
Message ID: {gmail_message_id}
Reply: {reply_instruction_text}

## category_selection_request
Low confidence classification
From: {sender}
Subject: {subject}
Summary: {summary}

Select the correct category:

## reply_mode_request
Action required email
From: {sender}
Subject: {subject}
Summary: {summary}

How should I handle the reply?

## meeting_confirmation_request
Meeting email detected
From: {sender}
Subject: {subject}
Summary: {summary}

Confirm appointment and create calendar event?

## reply_text_prompt
Please send the exact reply text in your next message.
Subject: {subject}
