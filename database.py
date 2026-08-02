import os
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

from config import DATABASE_NAME

UTC = timezone.utc
_DB_LOCK = threading.RLock()

def utc_now_iso():
    return datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S")

def utc_today():
    return datetime.now(UTC).date().isoformat()

def utc_yesterday():
    return (datetime.now(UTC).date() - timedelta(days=1)).isoformat()

def utc_two_days_ago():
    return (datetime.now(UTC).date() - timedelta(days=2)).isoformat()

def connect():
    parent = os.path.dirname(DATABASE_NAME)
    if parent:
        os.makedirs(parent, exist_ok=True)
    conn = sqlite3.connect(DATABASE_NAME, timeout=60, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA synchronous=NORMAL;")
    conn.execute("PRAGMA busy_timeout = 60000;")
    return conn

def _create_schema(conn):
    cur = conn.cursor()
    cur.execute("""
    CREATE TABLE IF NOT EXISTS chats (
        chat_id INTEGER PRIMARY KEY,
        title TEXT,
        chat_type TEXT DEFAULT 'group',
        language TEXT DEFAULT 'en',
        daily_report_enabled INTEGER DEFAULT 1,
        last_daily_prompt_date TEXT DEFAULT '',
        added_at TEXT DEFAULT CURRENT_TIMESTAMP,
        updated_at TEXT DEFAULT CURRENT_TIMESTAMP
    )
    """)
    cur.execute("""
    CREATE TABLE IF NOT EXISTS users (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        chat_id INTEGER NOT NULL,
        user_id INTEGER NOT NULL,
        username TEXT,
        first_name TEXT,
        created_at TEXT DEFAULT CURRENT_TIMESTAMP,
        last_seen TEXT DEFAULT CURRENT_TIMESTAMP,
        UNIQUE(chat_id, user_id)
    )
    """)
    cur.execute("""
    CREATE TABLE IF NOT EXISTS daily_stats (
        chat_id INTEGER NOT NULL,
        date TEXT NOT NULL,
        messages INTEGER DEFAULT 0,
        active_users INTEGER DEFAULT 0,
        joined INTEGER DEFAULT 0,
        left_count INTEGER DEFAULT 0,
        PRIMARY KEY(chat_id, date)
    )
    """)
    cur.execute("""
    CREATE TABLE IF NOT EXISTS user_daily_stats (
        chat_id INTEGER NOT NULL,
        date TEXT NOT NULL,
        user_id INTEGER NOT NULL,
        username TEXT,
        first_name TEXT,
        messages INTEGER DEFAULT 0,
        PRIMARY KEY(chat_id, date, user_id)
    )
    """)
    cur.execute("""
    CREATE TABLE IF NOT EXISTS messages (
        chat_id INTEGER NOT NULL,
        message_id INTEGER NOT NULL,
        user_id INTEGER,
        username TEXT,
        first_name TEXT,
        text TEXT,
        message_type TEXT DEFAULT 'text',
        reply_to_message_id INTEGER,
        replies_count INTEGER DEFAULT 0,
        reactions_count INTEGER DEFAULT 0,
        created_at TEXT DEFAULT CURRENT_TIMESTAMP,
        PRIMARY KEY(chat_id, message_id)
    )
    """)
    cur.execute("""
    CREATE TABLE IF NOT EXISTS warnings (
        chat_id INTEGER NOT NULL,
        user_id INTEGER NOT NULL,
        warnings INTEGER DEFAULT 0,
        updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
        PRIMARY KEY(chat_id, user_id)
    )
    """)
    cur.execute("""
    CREATE TABLE IF NOT EXISTS moderation_log (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        chat_id INTEGER NOT NULL,
        actor_user_id INTEGER,
        target_user_id INTEGER,
        action TEXT NOT NULL,
        reason TEXT DEFAULT '',
        created_at TEXT DEFAULT CURRENT_TIMESTAMP
    )
    """)
    cur.execute("""
    CREATE TABLE IF NOT EXISTS support_payments (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER,
        chat_id INTEGER,
        stars INTEGER,
        provider_payment_charge_id TEXT,
        telegram_payment_charge_id TEXT,
        created_at TEXT DEFAULT CURRENT_TIMESTAMP
    )
    """)
    cur.execute("""
    CREATE TABLE IF NOT EXISTS meta (
        key TEXT PRIMARY KEY,
        value TEXT
    )
    """)

def _table_columns(conn, table):
    try:
        return {row["name"] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
    except sqlite3.OperationalError:
        return set()

def _ensure_column(conn, table, column_name, ddl):
    if column_name not in _table_columns(conn, table):
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {ddl}")

def _migrate_schema(conn):
    # safe additions for older DBs
    if "chats" in {r["name"] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}:
        _ensure_column(conn, "chats", "daily_report_enabled", "INTEGER DEFAULT 1")
        _ensure_column(conn, "chats", "last_daily_prompt_date", "TEXT DEFAULT ''")
        _ensure_column(conn, "chats", "language", "TEXT DEFAULT 'en'")
        _ensure_column(conn, "chats", "chat_type", "TEXT DEFAULT 'group'")
        _ensure_column(conn, "chats", "updated_at", "TEXT DEFAULT CURRENT_TIMESTAMP")
    if "users" in {r["name"] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}:
        _ensure_column(conn, "users", "created_at", "TEXT DEFAULT CURRENT_TIMESTAMP")
        _ensure_column(conn, "users", "last_seen", "TEXT DEFAULT CURRENT_TIMESTAMP")
    if "daily_stats" in {r["name"] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}:
        _ensure_column(conn, "daily_stats", "joined", "INTEGER DEFAULT 0")
        _ensure_column(conn, "daily_stats", "left_count", "INTEGER DEFAULT 0")

def _ensure_schema(conn):
    _create_schema(conn)
    _migrate_schema(conn)

@contextmanager
def db(write=False):
    with _DB_LOCK:
        conn = connect()
        try:
            yield conn
            if write:
                conn.commit()
        finally:
            conn.close()

def initialize():
    with db(write=True) as conn:
        _ensure_schema(conn)

def ensure_chat(chat_id, title, chat_type):
    with db(write=True) as conn:
        conn.execute("""
        INSERT INTO chats(chat_id, title, chat_type, updated_at)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(chat_id) DO UPDATE SET
            title=excluded.title,
            chat_type=excluded.chat_type,
            updated_at=excluded.updated_at
        """, (chat_id, title, chat_type, utc_now_iso()))

def get_chat(chat_id):
    with db() as conn:
        return conn.execute("SELECT * FROM chats WHERE chat_id=?", (chat_id,)).fetchone()

SUPPORTED_LANGUAGES = {'fa', 'pt', 'es', 'ar', 'de', 'ja', 'en', 'tr', 'id', 'fr', 'ko', 'ru', 'it', 'zh'}

def get_chat_language(chat_id):
    row = get_chat(chat_id)
    return row["language"] if row and row["language"] in SUPPORTED_LANGUAGES else "en"

def set_chat_language(chat_id, language):
    language = language if language in SUPPORTED_LANGUAGES else "en"
    with db(write=True) as conn:
        conn.execute(
            "UPDATE chats SET language=?, updated_at=? WHERE chat_id=?",
            (language, utc_now_iso(), chat_id),
        )

def set_daily_report_enabled(chat_id, enabled):
    with db(write=True) as conn:
        conn.execute(
            "UPDATE chats SET daily_report_enabled=?, updated_at=? WHERE chat_id=?",
            (1 if enabled else 0, utc_now_iso(), chat_id),
        )

def mark_daily_prompt_sent(chat_id, date_str):
    with db(write=True) as conn:
        conn.execute(
            "UPDATE chats SET last_daily_prompt_date=?, updated_at=? WHERE chat_id=?",
            (date_str, utc_now_iso(), chat_id),
        )

def chats_due_for_daily_prompt(date_str):
    with db() as conn:
        return conn.execute("""
        SELECT * FROM chats
        WHERE chat_type IN ('group', 'supergroup')
          AND daily_report_enabled = 1
          AND COALESCE(last_daily_prompt_date, '') <> ?
        ORDER BY title COLLATE NOCASE
        """, (date_str,)).fetchall()

def add_user(chat_id, user_id, username, first_name):
    if user_id is None:
        return
    with db(write=True) as conn:
        conn.execute("""
        INSERT INTO users(chat_id, user_id, username, first_name, created_at, last_seen)
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(chat_id, user_id) DO UPDATE SET
            username=excluded.username,
            first_name=excluded.first_name,
            last_seen=excluded.last_seen
        """, (chat_id, user_id, username, first_name, utc_now_iso(), utc_now_iso()))

def get_user(chat_id, user_id):
    with db() as conn:
        return conn.execute(
            "SELECT * FROM users WHERE chat_id=? AND user_id=?",
            (chat_id, user_id),
        ).fetchone()

def record_message(chat_id, message_id, user_id, username, first_name, text, message_type, reply_to_message_id, created_at=None):
    today = utc_today()
    with db(write=True) as conn:
        conn.execute("""
        INSERT INTO daily_stats(chat_id, date, messages, active_users, joined, left_count)
        VALUES (?, ?, 1, 0, 0, 0)
        ON CONFLICT(chat_id, date) DO UPDATE SET
            messages = messages + 1
        """, (chat_id, today))

        row = None
        if user_id is not None:
            row = conn.execute("""
                SELECT messages FROM user_daily_stats
                WHERE chat_id=? AND date=? AND user_id=?
            """, (chat_id, today, user_id)).fetchone()

            if row is None:
                conn.execute("""
                INSERT INTO user_daily_stats(chat_id, date, user_id, username, first_name, messages)
                VALUES (?, ?, ?, ?, ?, 1)
                """, (chat_id, today, user_id, username, first_name))
                conn.execute("""
                UPDATE daily_stats
                SET active_users = active_users + 1
                WHERE chat_id=? AND date=?
                """, (chat_id, today))
            else:
                conn.execute("""
                UPDATE user_daily_stats
                SET messages = messages + 1,
                    username = ?,
                    first_name = ?
                WHERE chat_id=? AND date=? AND user_id=?
                """, (username, first_name, chat_id, today, user_id))

        conn.execute("""
        INSERT INTO messages(
            chat_id, message_id, user_id, username, first_name,
            text, message_type, reply_to_message_id, created_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(chat_id, message_id) DO UPDATE SET
            user_id=excluded.user_id,
            username=excluded.username,
            first_name=excluded.first_name,
            text=excluded.text,
            message_type=excluded.message_type,
            reply_to_message_id=excluded.reply_to_message_id
        """, (
            chat_id, message_id, user_id, username, first_name,
            text, message_type, reply_to_message_id, created_at or utc_now_iso()
        ))

        if reply_to_message_id is not None:
            conn.execute("""
            UPDATE messages
            SET replies_count = replies_count + 1
            WHERE chat_id=? AND message_id=?
            """, (chat_id, reply_to_message_id))

def record_membership_event(chat_id, user_id, username, first_name, kind):
    today = utc_today()
    kind = kind.lower()
    if kind not in ("joined", "left"):
        return
    with db(write=True) as conn:
        conn.execute("""
        INSERT INTO daily_stats(chat_id, date, messages, active_users, joined, left_count)
        VALUES (?, ?, 0, 0, 0, 0)
        ON CONFLICT(chat_id, date) DO NOTHING
        """, (chat_id, today))
        if kind == "joined":
            conn.execute("UPDATE daily_stats SET joined = joined + 1 WHERE chat_id=? AND date=?", (chat_id, today))
        else:
            conn.execute("UPDATE daily_stats SET left_count = left_count + 1 WHERE chat_id=? AND date=?", (chat_id, today))
    add_user(chat_id, user_id, username, first_name)

def record_reaction(chat_id, message_id, delta):
    if delta == 0:
        return
    with db(write=True) as conn:
        conn.execute("""
        UPDATE messages
        SET reactions_count = MAX(reactions_count + ?, 0)
        WHERE chat_id=? AND message_id=?
        """, (delta, chat_id, message_id))

def set_reaction_count(chat_id, message_id, total_count):
    total_count = max(int(total_count or 0), 0)
    with db(write=True) as conn:
        conn.execute("""
        UPDATE messages
        SET reactions_count = ?
        WHERE chat_id=? AND message_id=?
        """, (total_count, chat_id, message_id))

def get_daily_stats(chat_id, date_str):
    with db() as conn:
        return conn.execute("SELECT * FROM daily_stats WHERE chat_id=? AND date=?", (chat_id, date_str)).fetchone()

def get_previous_message_count(chat_id, date_str):
    with db() as conn:
        row = conn.execute("""
        SELECT messages FROM daily_stats
        WHERE chat_id=? AND date=date(?, '-1 day')
        """, (chat_id, date_str)).fetchone()
        return int(row["messages"]) if row else 0

def top_members(chat_id, date_str, limit=5):
    with db() as conn:
        return conn.execute("""
        SELECT user_id, username, first_name, messages
        FROM user_daily_stats
        WHERE chat_id=? AND date=?
        ORDER BY messages DESC, first_name COLLATE NOCASE
        LIMIT ?
        """, (chat_id, date_str, limit)).fetchall()

def peak_hour(chat_id, date_str):
    with db() as conn:
        return conn.execute("""
        SELECT strftime('%H', created_at) AS hour_bucket, COUNT(*) AS count
        FROM messages
        WHERE chat_id=? AND date(created_at)=?
        GROUP BY hour_bucket
        ORDER BY count DESC, hour_bucket ASC
        LIMIT 1
        """, (chat_id, date_str)).fetchone()

def most_replied_message(chat_id, date_str):
    with db() as conn:
        row = conn.execute("""
        SELECT *
        FROM messages
        WHERE chat_id=? AND date(created_at)=? AND COALESCE(replies_count, 0) > 0
        ORDER BY replies_count DESC, reactions_count DESC, message_id ASC
        LIMIT 1
        """, (chat_id, date_str)).fetchone()
        if row:
            return row
        return conn.execute("""
        SELECT *
        FROM messages
        WHERE chat_id=? AND COALESCE(replies_count, 0) > 0
        ORDER BY replies_count DESC, reactions_count DESC, message_id ASC
        LIMIT 1
        """, (chat_id,)).fetchone()

def most_reacted_message(chat_id, date_str):
    with db() as conn:
        row = conn.execute("""
        SELECT *
        FROM messages
        WHERE chat_id=? AND date(created_at)=? AND COALESCE(reactions_count, 0) > 0
        ORDER BY reactions_count DESC, replies_count DESC, message_id ASC
        LIMIT 1
        """, (chat_id, date_str)).fetchone()
        if row:
            return row
        return conn.execute("""
        SELECT *
        FROM messages
        WHERE chat_id=? AND COALESCE(reactions_count, 0) > 0
        ORDER BY reactions_count DESC, replies_count DESC, message_id ASC
        LIMIT 1
        """, (chat_id,)).fetchone()

def topic_candidates(chat_id, date_str):
    with db() as conn:
        rows = conn.execute("""
        SELECT text FROM messages
        WHERE chat_id=? AND date(created_at)=? AND COALESCE(text, '') <> ''
        """, (chat_id, date_str)).fetchall()
        return [r["text"] for r in rows]

def get_new_users_count(chat_id, date_str):
    with db() as conn:
        row = conn.execute("""
        SELECT COUNT(*) AS count
        FROM users
        WHERE chat_id=? AND date(created_at)=?
        """, (chat_id, date_str)).fetchone()
        return int(row["count"]) if row else 0

def get_joined_count(chat_id, date_str):
    row = get_daily_stats(chat_id, date_str)
    return int(row["joined"]) if row else 0

def get_left_count(chat_id, date_str):
    row = get_daily_stats(chat_id, date_str)
    return int(row["left_count"]) if row else 0

def get_net_growth(chat_id, date_str):
    return get_joined_count(chat_id, date_str) - get_left_count(chat_id, date_str)




def count_tracked_users():
    with db() as conn:
        row = conn.execute("SELECT COUNT(*) AS count FROM users").fetchone()
        return int(row["count"]) if row else 0

def support_payment_log(user_id, chat_id, stars, provider_payment_charge_id, telegram_payment_charge_id):
    with db(write=True) as conn:
        conn.execute("""
        INSERT INTO support_payments(user_id, chat_id, stars, provider_payment_charge_id, telegram_payment_charge_id)
        VALUES (?, ?, ?, ?, ?)
        """, (user_id, chat_id, stars, provider_payment_charge_id, telegram_payment_charge_id))

def set_warning_count(chat_id, user_id, count):
    with db(write=True) as conn:
        conn.execute("""
        INSERT INTO warnings(chat_id, user_id, warnings, updated_at)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(chat_id, user_id) DO UPDATE SET
            warnings=excluded.warnings,
            updated_at=excluded.updated_at
        """, (chat_id, user_id, max(int(count), 0), utc_now_iso()))

def add_warning(chat_id, user_id, delta=1):
    current = get_warning_count(chat_id, user_id)
    new_count = max(current + delta, 0)
    set_warning_count(chat_id, user_id, new_count)
    return new_count

def get_warning_count(chat_id, user_id):
    with db() as conn:
        row = conn.execute("SELECT warnings FROM warnings WHERE chat_id=? AND user_id=?", (chat_id, user_id)).fetchone()
        return int(row["warnings"]) if row else 0

def clear_warning_count(chat_id, user_id):
    with db(write=True) as conn:
        conn.execute("DELETE FROM warnings WHERE chat_id=? AND user_id=?", (chat_id, user_id))

def log_moderation_action(chat_id, actor_user_id, target_user_id, action, reason=""):
    with db(write=True) as conn:
        conn.execute("""
        INSERT INTO moderation_log(chat_id, actor_user_id, target_user_id, action, reason, created_at)
        VALUES (?, ?, ?, ?, ?, ?)
        """, (chat_id, actor_user_id, target_user_id, action, reason or "", utc_now_iso()))

def claim_daily_dispatch(date_str):
    with db(write=True) as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute("SELECT value FROM meta WHERE key='last_daily_dispatch_date'").fetchone()
        if row and row["value"] == date_str:
            return False
        conn.execute("""
        INSERT INTO meta(key, value) VALUES (?, ?)
        ON CONFLICT(key) DO UPDATE SET value=excluded.value
        """, ("last_daily_dispatch_date", date_str))
        return True

def set_meta(key, value):
    with db(write=True) as conn:
        conn.execute("""
        INSERT INTO meta(key, value) VALUES (?, ?)
        ON CONFLICT(key) DO UPDATE SET value=excluded.value
        """, (key, value))

def get_meta(key, default=None):
    with db() as conn:
        row = conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return row["value"] if row else default
