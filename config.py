import os
from pathlib import Path


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


def _default_database_path() -> str:
    raw = os.getenv("DATABASE_PATH", "").strip()
    if raw:
        return raw

    # On Render, a mounted persistent disk is typically exposed at /data.
    # Locally, keep the database inside the project folder so the bot still runs.
    if os.getenv("RENDER") or Path("/data").exists():
        return "/data/pmc_bot.sqlite3"

    return "pmc_bot.sqlite3"


BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()

# Keep this stable so the webhook path matches the one you set in Telegram.
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "pmc_secret").strip()
WEBHOOK_URL = os.getenv("WEBHOOK_URL", "").strip().rstrip("/")

DATABASE_PATH = _default_database_path()
BACKUP_PATH = os.getenv("BACKUP_PATH", str(Path(DATABASE_PATH).with_suffix(".backup.json")))

MAX_GROUPS_PER_PAGE = int(os.getenv("MAX_GROUPS_PER_PAGE", "5"))
MAX_REPEATS = int(os.getenv("MAX_REPEATS", "1000"))
MAX_DELAY_SECONDS = int(os.getenv("MAX_DELAY_SECONDS", "3600"))

# Optional. If empty, admin-only commands are open to everyone.
ADMIN_IDS = _parse_admin_ids(os.getenv("ADMIN_IDS", ""))
