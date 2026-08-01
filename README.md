# PMC Bot

A Telegram bot built with Flask + SQLite + webhooks for Render.

## Current features
- `/start` opens the main menu
- Language selection: English, Hebrew, Serbian
- `/addbot` adds a bot in a short conversation flow
- `/removebot` shows saved bots and lets you delete one
- Hidden command: `/pmcisbasedbdw`

## Setup
1. Install dependencies:
   ```bash
   pip install -r requirements.txt
   ```

2. Set environment variables on Render:
   - `BOT_TOKEN`
   - `WEBHOOK_URL`

3. Deploy the service.

## Notes
- The bot automatically sets its webhook to:
  `https://YOUR-RENDER-URL/webhook`
- Added bots are validated with Telegram before saving.
- SQLite data needs persistent storage if you want it to survive redeploys.
