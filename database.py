import sqlite3
from pathlib import Path

from config import DATABASE_PATH


def get_connection():
    db_path = Path(DATABASE_PATH)
    if db_path.parent != Path("."):
        db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


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
    conn.close()


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


def count_bots() -> int:
    conn = get_connection()
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) AS c FROM bots WHERE is_active=1")
    value = cur.fetchone()["c"]
    conn.close()
    return int(value)
