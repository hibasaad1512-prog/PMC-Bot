import os


def _int_env(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default


def _float_env(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default


BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "change-this-secret").strip()
WEBHOOK_URL = os.getenv("WEBHOOK_URL", "").strip().rstrip("/")

ADMIN_IDS = {
    int(x.strip())
    for x in os.getenv("ADMIN_IDS", "").split(",")
    if x.strip().isdigit()
}

DATABASE_PATH = os.getenv("DATABASE_PATH", "pmc_bot.sqlite3").strip()
MAX_GROUPS_PER_PAGE = max(1, _int_env("MAX_GROUPS_PER_PAGE", 5))
MAX_REPEATS = max(1, _int_env("MAX_REPEATS", 1000))
MAX_DELAY_SECONDS = max(0, _int_env("MAX_DELAY_SECONDS", 300))
BROADCAST_WORKERS = max(1, _int_env("BROADCAST_WORKERS", 4))
RETRY_ATTEMPTS = max(1, _int_env("RETRY_ATTEMPTS", 5))
RETRY_BASE_DELAY = max(0.1, _float_env("RETRY_BASE_DELAY", 0.8))
DEFAULT_REPEAT_DELAY = max(0.0, _float_env("DEFAULT_REPEAT_DELAY", 0.05))
OWNER_ID = _int_env("OWNER_ID", 0)
