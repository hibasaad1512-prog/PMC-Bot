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


def _is_writable_path(candidate: Path) -> bool:
    try:
        candidate.parent.mkdir(parents=True, exist_ok=True)
        test_file = candidate.parent / f".{candidate.name}.write_test"
        test_file.write_text("ok", encoding="utf-8")
        test_file.unlink(missing_ok=True)
        return True
    except Exception:
        return False


def _resolve_storage_path(raw_value: str | None, default_name: str, fallback_name: str) -> str:
    candidates: list[Path] = []

    raw = (raw_value or "").strip()
    if raw:
        candidates.append(Path(raw))

    # Render persistent disks are often mounted under /data, but free
    # instances or misconfigured deploys may not allow writing there.
    candidates.append(Path("/data") / default_name)

    # Always keep a local fallback inside the app workspace so the bot can
    # still run even if /data is unavailable.
    candidates.append(Path.cwd() / fallback_name)
    candidates.append(Path(__file__).resolve().parent / fallback_name)

    seen: set[str] = set()
    for candidate in candidates:
        key = str(candidate)
        if key in seen:
            continue
        seen.add(key)
        if _is_writable_path(candidate):
            return key

    return str(Path.cwd() / fallback_name)


def _default_database_path() -> str:
    return _resolve_storage_path(
        os.getenv("DATABASE_PATH"),
        default_name="pmc_bot.sqlite3",
        fallback_name="pmc_bot.sqlite3",
    )


BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()

# Keep this stable so the webhook path matches the one you set in Telegram.
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "pmc_secret").strip()
WEBHOOK_URL = os.getenv("WEBHOOK_URL", "").strip().rstrip("/")

DATABASE_PATH = _default_database_path()

_default_backup_name = str(Path(DATABASE_PATH).with_suffix(".backup.json"))
BACKUP_PATH = _resolve_storage_path(
    os.getenv("BACKUP_PATH"),
    default_name=Path(_default_backup_name).name,
    fallback_name=_default_backup_name,
)

MAX_GROUPS_PER_PAGE = int(os.getenv("MAX_GROUPS_PER_PAGE", "5"))
MAX_REPEATS = int(os.getenv("MAX_REPEATS", "1000"))
MAX_DELAY_SECONDS = int(os.getenv("MAX_DELAY_SECONDS", "3600"))

# Optional. If empty, admin-only commands are open to everyone.
ADMIN_IDS = _parse_admin_ids(os.getenv("ADMIN_IDS", ""))
