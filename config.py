import os


def _parse_admin_ids(raw: str) -> set[int]:
    ids: set[int] = set()
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        try:
            ids.add(int(part))
        except ValueError:
            continue
    return ids


BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()

# Keep this stable so the webhook path matches the one you set in Telegram.
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "pmc_secret").strip()
WEBHOOK_URL = os.getenv("WEBHOOK_URL", "").strip().rstrip("/")

DATABASE_PATH = os.getenv("DATABASE_PATH", "pmc_bot.sqlite3").strip()

MAX_GROUPS_PER_PAGE = int(os.getenv("MAX_GROUPS_PER_PAGE", "5"))
MAX_REPEATS = int(os.getenv("MAX_REPEATS", "1000"))
MAX_DELAY_SECONDS = int(os.getenv("MAX_DELAY_SECONDS", "3600"))

# Optional. If empty, admin-only commands are open to everyone.
ADMIN_IDS = _parse_admin_ids(os.getenv("ADMIN_IDS", ""))
