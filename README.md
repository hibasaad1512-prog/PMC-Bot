# PMC Bot

A Telegram bot built with Flask + SQLite + webhooks for Render.

## Current features
- `/start` opens the main menu
- `/help` shows the available commands
- `/ping` checks bot status
- `/stats` shows the current counters
- `/addbot` adds a bot in a short conversation flow
- `/removebot` shows saved bots and lets you delete one
- `/broadcast` sends a copied message to registered groups
- `/register` registers the current group
- Hidden command: `/pmcisbasedbdw`

## Admin access
If `ADMIN_IDS` is set, only those Telegram user IDs can use the admin commands:
- `/addbot`
- `/removebot`
- `/broadcast`
- `/register`

If `ADMIN_IDS` is empty, admin commands are open to everyone.

## Setup
1. Install dependencies:
   ```bash
   pip install -r requirements.txt
   ```

2. Set environment variables on Render:
   - `BOT_TOKEN`
   - `WEBHOOK_URL`
   - optional `ADMIN_IDS` as a comma-separated list

3. Deploy the service.

## Notes
- The bot automatically sets its webhook to:
  `https://YOUR-RENDER-URL/webhook`
- Added bots are validated with Telegram before saving.
- `/broadcast` currently copies the exact message you send to every registered group.
- SQLite data needs persistent storage if you want it to survive redeploys.
