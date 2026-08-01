# PMC Bot

A Telegram broadcast bot built with Flask + SQLite + webhooks for Render.

## What this build does
- `/start` shows only language selection
- Languages: English, Hebrew, Serbian
- Hidden command: `/pmcisbasedbdw`
- Choose a saved group from inline buttons
- Send any message once or many times
- Pick a delay between sends
- No OWNER_ID or ADMIN_IDS needed

## Setup
1. Install dependencies:
   ```bash
   pip install -r requirements.txt
   ```

2. Set environment variables on Render:
   - `BOT_TOKEN`
   - `WEBHOOK_URL`

3. Deploy the service.

4. Add the bot to each group and send:
   ```text
   /register
   ```
   inside that group so it appears in the admin picker.

5. Open the bot in Telegram and send `/start`.

## Webhook
- The app automatically sets the webhook to:
  `https://YOUR-RENDER-URL/webhook`
- Any path under `/webhook/...` is accepted, so you do not need to manage a secret path manually.

## Notes
- The hidden command only shows groups where you are an admin.
- Broadcasts are sent with `copy_message`, so text and media are supported.
- If you redeploy without a persistent disk, SQLite data may reset.
