import json
import sqlite3
from pathlib import Path

from config import BACKUP_PATH, DATABASE_PATH



def get_connection():
    db_path = Path(DATABASE_PATH)
    if db_path.parent != Path("."):
        db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path), check_same_thread=False, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def _backup_path() -> Path:
    path = Path(BACKUP_PATH)
    if path.parent != Path("."):
        path.parent.mkdir(parents=True, exist_ok=True)
    return path


def _rows_to_dicts(rows):
    return [dict(row) for row in rows]


def _write_backup_snapshot():
    conn = get_connection()
    try:
        cur = conn.cursor()
        snapshot = {}
        for table in ("users", "groups", "bots"):
            cur.execute(f"SELECT * FROM {table}")
            snapshot[table] = _rows_to_dicts(cur.fetchall())
        backup_file = _backup_path()
        tmp_file = backup_file.with_suffix(backup_file.suffix + ".tmp")
        tmp_file.write_text(json.dumps(snapshot, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp_file.replace(backup_file)
    finally:
        conn.close()


def _restore_from_backup_if_needed():
    backup_file = _backup_path()
    if not backup_file.exists():
        return

    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) AS c FROM users")
        users_count = int(cur.fetchone()["c"] or 0)
        cur.execute("SELECT COUNT(*) AS c FROM groups")
        groups_count = int(cur.fetchone()["c"] or 0)
        cur.execute("SELECT COUNT(*) AS c FROM bots")
        bots_count = int(cur.fetchone()["c"] or 0)
        if users_count or groups_count or bots_count:
            return

        try:
            snapshot = json.loads(backup_file.read_text(encoding="utf-8"))
        except Exception:
            return

        for row in snapshot.get("users", []):
            cur.execute(
                """
                INSERT OR REPLACE INTO users (
                    user_id, language, state,
                    pending_group_id, pending_message_chat_id, pending_message_id,
                    pending_repeats, pending_delay,
                    pending_bot_label, pending_bot_token, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    row.get("user_id"),
                    row.get("language"),
                    row.get("state"),
                    row.get("pending_group_id"),
                    row.get("pending_message_chat_id"),
                    row.get("pending_message_id"),
                    row.get("pending_repeats", 1),
                    row.get("pending_delay", 0),
                    row.get("pending_bot_label"),
                    row.get("pending_bot_token"),
                    row.get("updated_at"),
                ),
            )

        for row in snapshot.get("groups", []):
            cur.execute(
                """
                INSERT OR REPLACE INTO groups (
                    chat_id, title, username, chat_type, is_active, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    row.get("chat_id"),
                    row.get("title"),
                    row.get("username"),
                    row.get("chat_type"),
                    row.get("is_active", 1),
                    row.get("updated_at"),
                ),
            )

        for row in snapshot.get("bots", []):
            cur.execute(
                """
                INSERT OR REPLACE INTO bots (
                    id, label, token, added_by, is_active, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    row.get("id"),
                    row.get("label"),
                    row.get("token"),
                    row.get("added_by"),
                    row.get("is_active", 1),
                    row.get("created_at"),
                    row.get("updated_at"),
                ),
            )

        conn.commit()
    finally:
        conn.close()



def _table_columns(cur, table_name: str) -> set[str]:
    cur.execute(f"PRAGMA table_info({table_name})")
    return {row[1] for row in cur.fetchall()}


def _ensure_column(cur, table_name: str, column_name: str, column_sql: str):
    columns = _table_columns(cur, table_name)
    if column_name not in columns:
        cur.execute(f"ALTER TABLE {table_name} ADD COLUMN {column_sql}")


def init_db():
    conn = get_connection()
    cur = conn.cursor()
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            language TEXT,
            state TEXT,
            pending_group_id INTEGER,
            pending_message_chat_id INTEGER,
            pending_message_id INTEGER,
            pending_repeats INTEGER DEFAULT 1,
            pending_delay INTEGER DEFAULT 0,
            pending_bot_label TEXT,
            pending_bot_token TEXT,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS groups (
            chat_id INTEGER PRIMARY KEY,
            title TEXT,
            username TEXT,
            chat_type TEXT,
            is_active INTEGER DEFAULT 1,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS bots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            label TEXT NOT NULL,
            token TEXT NOT NULL UNIQUE,
            added_by INTEGER,
            is_active INTEGER DEFAULT 1,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        """
    )

    # Simple migrations for existing databases.
    _ensure_column(cur, "users", "pending_bot_label", "pending_bot_label TEXT")
    _ensure_column(cur, "users", "pending_bot_token", "pending_bot_token TEXT")

    conn.commit()
    conn.close()
    _restore_from_backup_if_needed()


def upsert_group(chat_id: int, title: str, username: str | None, chat_type: str):
    conn = get_connection()
    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO groups (chat_id, title, username, chat_type, is_active, updated_at)
        VALUES (?, ?, ?, ?, 1, CURRENT_TIMESTAMP)
        ON CONFLICT(chat_id) DO UPDATE SET
            title=excluded.title,
            username=excluded.username,
            chat_type=excluded.chat_type,
            is_active=1,
            updated_at=CURRENT_TIMESTAMP
        """,
        (chat_id, title, username or "", chat_type),
    )
    conn.commit()
    _write_backup_snapshot()
    conn.close()


def get_group_by_chat_id(chat_id: int):
    conn = get_connection()
    cur = conn.cursor()
    cur.execute(
        """
        SELECT chat_id, title, username, chat_type, is_active, updated_at
        FROM groups
        WHERE chat_id=?
        """,
        (chat_id,),
    )
    row = cur.fetchone()
    conn.close()
    return row


def find_groups_by_query(query: str):
    raw = (query or "").strip()
    if not raw:
        return []

    conn = get_connection()
    cur = conn.cursor()
    if raw.lstrip("-").isdigit():
        chat_id = int(raw)
        cur.execute(
            """
            SELECT chat_id, title, username, chat_type, is_active, updated_at
            FROM groups
            WHERE chat_id=?
            """,
            (chat_id,),
        )
    else:
        lowered = f"%{raw.lower()}%"
        cur.execute(
            """
            SELECT chat_id, title, username, chat_type, is_active, updated_at
            FROM groups
            WHERE LOWER(title) LIKE ?
               OR LOWER(username) LIKE ?
            ORDER BY is_active DESC, title COLLATE NOCASE ASC
            LIMIT 20
            """,
            (lowered, lowered),
        )
    rows = cur.fetchall()
    conn.close()
    return rows


def list_groups(offset: int = 0, limit: int = 100):
    conn = get_connection()
    cur = conn.cursor()
    cur.execute(
        """
        SELECT chat_id, title, username, chat_type
        FROM groups
        WHERE is_active=1
        ORDER BY title COLLATE NOCASE ASC
        LIMIT ? OFFSET ?
        """,
        (limit, offset),
    )
    rows = cur.fetchall()
    conn.close()
    return rows


def count_groups() -> int:
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) AS c FROM groups WHERE is_active=1")
    value = cur.fetchone()["c"]
    conn.close()
    return int(value)


def deactivate_group(chat_id: int):
    conn = get_connection()
    cur = conn.cursor()
    cur.execute(
        "UPDATE groups SET is_active=0, updated_at=CURRENT_TIMESTAMP WHERE chat_id=?",
        (chat_id,),
    )
    conn.commit()
    updated = cur.rowcount
    _write_backup_snapshot()
    conn.close()
    return updated > 0


def delete_group_record(chat_id: int):
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("DELETE FROM groups WHERE chat_id=?", (chat_id,))
    conn.commit()
    deleted = cur.rowcount
    _write_backup_snapshot()
    conn.close()
    return deleted > 0


def count_users() -> int:
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) AS c FROM users")
    value = cur.fetchone()["c"]
    conn.close()
    return int(value)


def get_user(user_id: int):
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("SELECT * FROM users WHERE user_id=?", (user_id,))
    row = cur.fetchone()
    conn.close()
    return row


def ensure_user(user_id: int):
    conn = get_connection()
    cur = conn.cursor()
    cur.execute(
        """
        INSERT OR IGNORE INTO users (
            user_id, language, state,
            pending_group_id, pending_message_chat_id, pending_message_id,
            pending_repeats, pending_delay,
            pending_bot_label, pending_bot_token
        )
        VALUES (?, NULL, NULL, NULL, NULL, NULL, 1, 0, NULL, NULL)
        """,
        (user_id,),
    )
    conn.commit()
    _write_backup_snapshot()
    conn.close()


def update_user(user_id: int, **fields):
    if not fields:
        return
    ensure_user(user_id)
    conn = get_connection()
    cur = conn.cursor()
    assignments = ", ".join([f"{key}=?" for key in fields.keys()])
    values = list(fields.values()) + [user_id]
    cur.execute(
        f"UPDATE users SET {assignments}, updated_at=CURRENT_TIMESTAMP WHERE user_id=?",
        values,
    )
    conn.commit()
    _write_backup_snapshot()
    conn.close()


def set_language(user_id: int, language: str):
    update_user(user_id, language=language)


def set_state(user_id: int, state: str | None):
    update_user(user_id, state=state)


def set_pending(user_id: int, **fields):
    update_user(user_id, **fields)


def reset_pending(user_id: int):
    update_user(
        user_id,
        state=None,
        pending_group_id=None,
        pending_message_chat_id=None,
        pending_message_id=None,
        pending_repeats=1,
        pending_delay=0,
        pending_bot_label=None,
        pending_bot_token=None,
    )


def add_bot_record(label: str, token: str, added_by: int | None = None):
    conn = get_connection()
    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO bots (label, token, added_by, is_active, created_at, updated_at)
        VALUES (?, ?, ?, 1, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
        ON CONFLICT(token) DO UPDATE SET
            label=excluded.label,
            added_by=excluded.added_by,
            is_active=1,
            updated_at=CURRENT_TIMESTAMP
        """,
        (label, token, added_by),
    )
    conn.commit()
    _write_backup_snapshot()
    conn.close()


def list_bots(offset: int = 0, limit: int = 100):
    conn = get_connection()
    cur = conn.cursor()
    cur.execute(
        """
        SELECT id, label, token, added_by, is_active, created_at, updated_at
        FROM bots
        WHERE is_active=1
        ORDER BY id ASC
        LIMIT ? OFFSET ?
        """,
        (limit, offset),
    )
    rows = cur.fetchall()
    conn.close()
    return rows


def list_all_bots():
    conn = get_connection()
    cur = conn.cursor()
    cur.execute(
        """
        SELECT id, label, token, added_by, is_active, created_at, updated_at
        FROM bots
        WHERE is_active=1
        ORDER BY id ASC
        """
    )
    rows = cur.fetchall()
    conn.close()
    return rows


def count_bots() -> int:
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) AS c FROM bots WHERE is_active=1")
    value = cur.fetchone()["c"]
    conn.close()
    return int(value)


def get_bot_by_id(bot_id: int):
    conn = get_connection()
    cur = conn.cursor()
    cur.execute(
        """
        SELECT id, label, token, added_by, is_active, created_at, updated_at
        FROM bots
        WHERE id=?
        """,
        (bot_id,),
    )
    row = cur.fetchone()
    conn.close()
    return row


def delete_bot_record(bot_id: int):
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("DELETE FROM bots WHERE id=?", (bot_id,))
    conn.commit()
    deleted = cur.rowcount
    _write_backup_snapshot()
    conn.close()
    return deleted > 0


def deactivate_bot_record(bot_id: int):
    conn = get_connection()
    cur = conn.cursor()
    cur.execute(
        "UPDATE bots SET is_active=0, updated_at=CURRENT_TIMESTAMP WHERE id=?",
        (bot_id,),
    )
    conn.commit()
    updated = cur.rowcount
    _write_backup_snapshot()
    conn.close()
    return updated > 0
