# Pulse Bot v4.5

This version adds:
- `/growth`
- `/warn`
- `/unwarn`
- `/warnings`
- `/mute`
- `/unmute`
- `/ban`
- `/unban`
- more lively stats and reports
- midnight prompt in all supported languages
- inline action buttons for moderation replies

## Before running
1. Put a fresh BotFather token in `config.py`
2. Set `WEBHOOK_URL` to your Render URL
3. Redeploy
4. Set the Telegram webhook to `/webhook`
5. The midnight prompt is sent automatically at UTC midnight if enabled

## Notes
- Moderation commands require the command to be sent as a reply to the target member.
- Warnings are stored in SQLite.
- When a user reaches 3 warnings, the bot auto-mutes for 24h.


Supported languages: English, Arabic, Russian, French, Spanish, German, Portuguese, Italian, Turkish, Persian, Indonesian, Japanese, Korean, Chinese.
