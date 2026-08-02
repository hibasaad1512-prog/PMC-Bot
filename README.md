# PMC Bot

A Telegram bot built with Flask + SQLite + webhooks for Render.

## Current features
- `/start` opens the main menu
- `/help` shows the available commands
- `/ping` checks bot status
- `/stats` shows the current counters
- `/addbot` adds a bot in a short conversation flow
- `/removebot` shows saved bots and lets you delete one
- `/broadcast` delivers any Telegram message (text, photos, GIFs, voice, stickers, documents, and more) using the stored follower bots, fast
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
- `/broadcast` lets you choose one saved group and then copies the exact message there, including photos, GIFs, polls, stickers, and more.
- Groups are saved automatically when the bot joins them, and they are deactivated when the bot leaves.
- SQLite data needs persistent storage if you want it to survive redeploys.
- On Render, you need a mounted persistent disk and `DATABASE_PATH=/data/pmc_bot.sqlite3` for saved bots, groups, and user states to survive restarts.
- The app now also writes a JSON backup file next to the database and can restore from it on startup if the SQLite file is missing.
- If you keep the service on a free instance without a disk, local files will still be ephemeral and data can be lost after restart.
