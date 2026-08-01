# PMC ENGINE

A Telegram broadcast engine built with Flask + SQLite + webhooks for Render.

## What this build does
- `/start` only shows language selection
- Languages: English, Hebrew, Serbian
- Hidden admin command: `/pmcisbasedbdw`
- Choose a saved group from inline buttons
- Send a message and choose how many times it should be sent
- Choose the delay between sends
- Broadcast runs in the background with retries to reduce failures
- SQLite storage for users and groups

## Setup
1. Install dependencies:
   ```bash
   pip install -r requirements.txt
   ```

2. Set environment variables on Render:
   - `BOT_TOKEN`
   - `WEBHOOK_URL` (base URL only, for example `https://pmc-bot-lbx1.onrender.com`)
   - `OWNER_ID` or `ADMIN_IDS` (optional, but required to open the secret admin panel)

3. Deploy on Render as a Web Service.

4. Open the bot in Telegram and send `/start`.

## Registering groups
Add the bot to each group, then send:
```text
/register
```
inside that group so it appears in the admin picker.

## Notes
- The hidden admin panel is only available to IDs listed in `ADMIN_IDS` or the `OWNER_ID`.
- The broadcast job now runs in the background so the webhook returns quickly.
- Retry logic helps when Telegram rate limits or temporary failures happen.
