import os

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()

# Keep this stable so the webhook path matches the one you set in Telegram.
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "pmc_secret").strip()
WEBHOOK_URL = os.getenv("WEBHOOK_URL", "").strip().rstrip("/")

DATABASE_PATH = os.getenv("DATABASE_PATH", "pmc_bot.sqlite3").strip()

MAX_GROUPS_PER_PAGE = int(os.getenv("MAX_GROUPS_PER_PAGE", "5"))
MAX_REPEATS = int(os.getenv("MAX_REPEATS", "1000"))
MAX_DELAY_SECONDS = int(os.getenv("MAX_DELAY_SECONDS", "3600"))
