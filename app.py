import logging
import os
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from flask import Flask, abort, request
import telebot
from telebot.types import InlineKeyboardButton, InlineKeyboardMarkup

from config import (
    ADMIN_IDS,
    BROADCAST_WORKERS,
    BOT_TOKEN,
    DEFAULT_REPEAT_DELAY,
    DATABASE_PATH,
    MAX_DELAY_SECONDS,
    MAX_GROUPS_PER_PAGE,
    MAX_REPEATS,
    OWNER_ID,
    RETRY_ATTEMPTS,
    RETRY_BASE_DELAY,
    WEBHOOK_SECRET,
    WEBHOOK_URL,
)
from database import (
    count_groups,
    ensure_user,
    get_user,
    init_db,
    list_groups,
    reset_pending,
    set_language,
    set_pending,
    set_state,
    upsert_group,
)
from translations import LANG_BUTTONS, t

LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger("pmc_engine")

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN is missing in Render environment variables.")
if not WEBHOOK_URL:
    raise RuntimeError("WEBHOOK_URL is missing in Render environment variables.")

app = Flask(__name__)
bot = telebot.TeleBot(BOT_TOKEN, parse_mode="HTML", threaded=False)
executor = ThreadPoolExecutor(max_workers=BROADCAST_WORKERS)
SECRET_COMMAND = "/pmcisbasedbdw"
active_broadcasts: set[int] = set()
active_broadcasts_lock = threading.Lock()


def is_admin(user_id: int) -> bool:
    return (user_id == OWNER_ID) or (user_id in ADMIN_IDS)


def get_lang(user_id: int) -> str:
    row = get_user(user_id)
    if row and row["language"]:
        return row["language"]
    return "en"


def lang_keyboard():
    kb = InlineKeyboardMarkup()
    for code, label in LANG_BUTTONS:
        kb.add(InlineKeyboardButton(label, callback_data=f"lang:{code}"))
    return kb


def cancel_keyboard():
    kb = InlineKeyboardMarkup()
    kb.add(InlineKeyboardButton("❌ Cancel", callback_data="cancel"))
    return kb


def group_keyboard(page: int = 0):
    total = count_groups()
    groups = list_groups(offset=page * MAX_GROUPS_PER_PAGE, limit=MAX_GROUPS_PER_PAGE)
    kb = InlineKeyboardMarkup()
    for g in groups:
        title = g["title"] or str(g["chat_id"])
        if len(title) > 28:
            title = title[:25] + "..."
        kb.add(InlineKeyboardButton(title, callback_data=f"group:{g['chat_id']}"))
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("◀️", callback_data=f"groups:{page-1}"))
    if (page + 1) * MAX_GROUPS_PER_PAGE < total:
        nav.append(InlineKeyboardButton("▶️", callback_data=f"groups:{page+1}"))
    if nav:
        kb.row(*nav)
    kb.add(InlineKeyboardButton("❌ Cancel", callback_data="cancel"))
    return kb


def summary_keyboard():
    kb = InlineKeyboardMarkup()
    kb.row(
        InlineKeyboardButton("✅ Start", callback_data="broadcast_start"),
        InlineKeyboardButton("❌ Cancel", callback_data="cancel"),
    )
    return kb


def save_group_from_message(message):
    chat = message.chat
    if chat.type in ("group", "supergroup"):
        upsert_group(chat.id, chat.title or str(chat.id), getattr(chat, "username", None), chat.type)


def send_language_prompt(chat_id: int, user_id: int):
    row = get_user(user_id)
    current = row["language"] if row and row["language"] else None
    text = t(current or "en", "welcome")
    if current:
        text = t(current, "already_set")
    bot.send_message(chat_id, text, reply_markup=lang_keyboard())


def _normalize_base_url() -> str:
    base = WEBHOOK_URL.rstrip("/")
    if base.endswith("/webhook"):
        base = base[: -len("/webhook")]
    return base


def configure_webhook() -> None:
    webhook = f"{_normalize_base_url()}/webhook/{WEBHOOK_SECRET}"
    try:
        bot.remove_webhook()
    except Exception:
        logger.exception("Failed to remove previous webhook (continuing)")
    bot.set_webhook(url=webhook)
    logger.info("Webhook configured: %s", webhook)


def safe_edit_message(chat_id: int, message_id: int, text: str):
    try:
        bot.edit_message_text(chat_id=chat_id, message_id=message_id, text=text)
    except Exception:
        logger.exception("Failed to edit message, sending a new one instead")
        try:
            bot.send_message(chat_id, text)
        except Exception:
            logger.exception("Failed to send fallback message")


def parse_int_or_none(value):
    if value is None:
        return None
    try:
        return int(str(value).strip())
    except Exception:
        return None


def retry_after_seconds(exc: Exception) -> int | None:
    match = re.search(r"retry after\s+(\d+)", str(exc), re.IGNORECASE)
    if match:
        return int(match.group(1))
    return None


def safe_copy_message(target_chat_id: int, from_chat_id: int, message_id: int):
    last_exc = None
    for attempt in range(1, RETRY_ATTEMPTS + 1):
        try:
            return bot.copy_message(
                chat_id=target_chat_id,
                from_chat_id=from_chat_id,
                message_id=message_id,
            )
        except Exception as exc:
            last_exc = exc
            wait = retry_after_seconds(exc)
            if wait is None:
                wait = RETRY_BASE_DELAY * attempt
            wait = max(0.2, float(wait))
            logger.warning(
                "copy_message failed (attempt %s/%s) target=%s source=%s msg=%s wait=%.2f error=%s",
                attempt,
                RETRY_ATTEMPTS,
                target_chat_id,
                from_chat_id,
                message_id,
                wait,
                exc,
            )
            if attempt < RETRY_ATTEMPTS:
                time.sleep(wait)
    raise last_exc


def run_broadcast(admin_id: int, status_chat_id: int, status_message_id: int):
    try:
        row = get_user(admin_id)
        if not row:
            safe_edit_message(status_chat_id, status_message_id, "❌ Broadcast failed.")
            return

        lang = row["language"] or "en"
        group_id = parse_int_or_none(row["pending_group_id"])
        source_chat_id = parse_int_or_none(row["pending_message_chat_id"])
        source_message_id = parse_int_or_none(row["pending_message_id"])
        repeats = parse_int_or_none(row["pending_repeats"]) or 1
        delay = parse_int_or_none(row["pending_delay"]) or 0

        repeats = max(1, min(repeats, MAX_REPEATS))
        delay = max(0, min(delay, MAX_DELAY_SECONDS))

        if not group_id or not source_chat_id or not source_message_id:
            safe_edit_message(status_chat_id, status_message_id, t(lang, "broadcast_failed"))
            return

        sent = 0
        failed = 0
        for index in range(repeats):
            try:
                safe_copy_message(group_id, source_chat_id, source_message_id)
                sent += 1
            except Exception as exc:
                failed += 1
                logger.exception("Broadcast send failed on iteration %s/%s", index + 1, repeats)
                # If Telegram tells us to wait, respect it and continue.
                wait = retry_after_seconds(exc)
                if wait:
                    time.sleep(max(1, wait))
            if index < repeats - 1:
                time.sleep(delay if delay > 0 else DEFAULT_REPEAT_DELAY)

        reset_pending(admin_id)
        result = (
            f"{t(lang, 'done')}\n\n"
            f"Sent: {sent}\n"
            f"Failed: {failed}\n"
            f"Requested: {repeats}"
        )
        safe_edit_message(status_chat_id, status_message_id, result)
    except Exception:
        logger.exception("Broadcast job crashed")
        try:
            row = get_user(admin_id)
            lang = row["language"] or "en" if row else "en"
            safe_edit_message(status_chat_id, status_message_id, t(lang, "broadcast_failed"))
        except Exception:
            logger.exception("Could not report broadcast crash")
    finally:
        with active_broadcasts_lock:
            active_broadcasts.discard(admin_id)


@app.route("/", methods=["GET"])
def index():
    return {"ok": True, "service": "PMC ENGINE", "database": DATABASE_PATH}


@app.route("/health", methods=["GET"])
def health():
    return {"ok": True}


@app.route("/webhook", methods=["POST"])
@app.route("/webhook/<path:secret>", methods=["POST"])
def telegram_webhook(secret=None):
    if secret is not None and secret != WEBHOOK_SECRET:
        logger.warning("Webhook secret mismatch.")
        abort(403)

    raw = request.get_data(as_text=True, cache=False)
    if not raw:
        abort(400)

    try:
        update = telebot.types.Update.de_json(raw)
        bot.process_new_updates([update])
        return {"ok": True}
    except Exception:
        logger.exception("Failed to process Telegram update")
        abort(500)


@bot.message_handler(commands=["start"])
def start(message):
    ensure_user(message.from_user.id)
    send_language_prompt(message.chat.id, message.from_user.id)
    logger.info("Handled /start from user=%s chat=%s", message.from_user.id, message.chat.id)


@bot.callback_query_handler(func=lambda call: call.data.startswith("lang:"))
def choose_language(call):
    code = call.data.split(":", 1)[1]
    if code not in ("en", "he", "sr"):
        bot.answer_callback_query(call.id)
        return
    set_language(call.from_user.id, code)
    bot.answer_callback_query(call.id)
    bot.edit_message_text(
        chat_id=call.message.chat.id,
        message_id=call.message.message_id,
        text=t(code, "language_saved"),
    )
    logger.info("Language set user=%s lang=%s", call.from_user.id, code)


@bot.message_handler(commands=["register"])
def register_group(message):
    ensure_user(message.from_user.id)
    chat = message.chat
    if chat.type not in ("group", "supergroup"):
        lang = get_lang(message.from_user.id)
        bot.reply_to(message, t(lang, "register_only_group"))
        return
    save_group_from_message(message)
    lang = get_lang(message.from_user.id)
    bot.reply_to(message, t(lang, "registered"))
    logger.info("Registered group chat=%s title=%s", chat.id, chat.title)


@bot.message_handler(commands=["help"])
def help_cmd(message):
    lang = get_lang(message.from_user.id)
    bot.reply_to(message, t(lang, "help"))


@bot.message_handler(commands=["cancel"])
def cancel_cmd(message):
    ensure_user(message.from_user.id)
    reset_pending(message.from_user.id)
    lang = get_lang(message.from_user.id)
    bot.reply_to(message, t(lang, "cancelled"))


@bot.message_handler(commands=[SECRET_COMMAND.lstrip("/")])
def secret_panel(message):
    ensure_user(message.from_user.id)
    if not is_admin(message.from_user.id):
        return bot.reply_to(message, t(get_lang(message.from_user.id), "access_denied"))

    lang = get_lang(message.from_user.id)
    if count_groups() == 0:
        return bot.reply_to(message, t(lang, "no_groups"))

    set_state(message.from_user.id, "choose_group")
    bot.reply_to(message, t(lang, "choose_group"), reply_markup=group_keyboard(page=0))
    logger.info("Opened secret panel for user=%s", message.from_user.id)


@bot.callback_query_handler(func=lambda call: call.data.startswith("groups:"))
def paginate_groups(call):
    if not is_admin(call.from_user.id):
        bot.answer_callback_query(call.id, t(get_lang(call.from_user.id), "access_denied"), show_alert=True)
        return

    page = parse_int_or_none(call.data.split(":", 1)[1]) or 0
    lang = get_lang(call.from_user.id)
    try:
        bot.edit_message_text(
            chat_id=call.message.chat.id,
            message_id=call.message.message_id,
            text=t(lang, "choose_group"),
            reply_markup=group_keyboard(page=page),
        )
    except Exception:
        logger.exception("Failed to paginate groups")
    bot.answer_callback_query(call.id)


@bot.callback_query_handler(func=lambda call: call.data.startswith("group:"))
def select_group(call):
    if not is_admin(call.from_user.id):
        bot.answer_callback_query(call.id, t(get_lang(call.from_user.id), "access_denied"), show_alert=True)
        return

    group_id = parse_int_or_none(call.data.split(":", 1)[1])
    if not group_id:
        bot.answer_callback_query(call.id)
        return

    lang = get_lang(call.from_user.id)
    set_pending(
        call.from_user.id,
        pending_group_id=group_id,
        state="await_message",
        pending_repeats=1,
        pending_delay=0,
    )
    bot.answer_callback_query(call.id)
    bot.edit_message_text(
        chat_id=call.message.chat.id,
        message_id=call.message.message_id,
        text=t(lang, "send_message"),
        reply_markup=cancel_keyboard(),
    )
    logger.info("Selected group=%s by user=%s", group_id, call.from_user.id)


@bot.message_handler(content_types=["text", "photo", "video", "document", "sticker", "voice", "audio", "animation"])
def catch_broadcast_message(message):
    save_group_from_message(message)
    ensure_user(message.from_user.id)
    row = get_user(message.from_user.id)
    if not row or not row["state"]:
        return
    if not is_admin(message.from_user.id):
        return

    state = row["state"]
    lang = row["language"] or "en"

    if state == "await_message":
        set_pending(
            message.from_user.id,
            pending_message_chat_id=message.chat.id,
            pending_message_id=message.message_id,
            state="await_repeats",
        )
        bot.reply_to(message, t(lang, "repeats"), reply_markup=cancel_keyboard())
        return

    if state == "await_repeats":
        repeats = parse_int_or_none(message.text)
        if repeats is None or repeats < 1:
            bot.reply_to(message, t(lang, "invalid_repeats"))
            return

        repeats = min(repeats, MAX_REPEATS)
        set_pending(message.from_user.id, pending_repeats=repeats, state="await_delay")
        bot.reply_to(message, t(lang, "delay"), reply_markup=cancel_keyboard())
        logger.info("Repeat count set user=%s repeats=%s", message.from_user.id, repeats)
        return

    if state == "await_delay":
        delay = parse_int_or_none(message.text)
        if delay is None or delay < 0:
            bot.reply_to(message, t(lang, "invalid_delay"))
            return

        delay = min(delay, MAX_DELAY_SECONDS)
        set_pending(message.from_user.id, pending_delay=delay, state="confirm")
        group_id = parse_int_or_none(row["pending_group_id"])
        groups = {g["chat_id"]: g for g in list_groups(limit=1000)}
        group_title = groups[group_id]["title"] if group_id in groups else str(group_id)

        repeats = parse_int_or_none(row["pending_repeats"]) or 1
        summary = (
            f"{t(lang, 'summary')}\n\n"
            f"{t(lang, 'summary_group')}: {group_title}\n"
            f"{t(lang, 'summary_message')}: ✓\n"
            f"{t(lang, 'summary_repeats')}: {repeats}\n"
            f"{t(lang, 'summary_delay')}: {delay}s"
        )
        bot.reply_to(message, summary, reply_markup=summary_keyboard())
        logger.info("Prepared summary user=%s group=%s repeats=%s delay=%s", message.from_user.id, group_id, repeats, delay)
        return


@bot.callback_query_handler(func=lambda call: call.data == "broadcast_start")
def start_broadcast(call):
    if not is_admin(call.from_user.id):
        bot.answer_callback_query(call.id, t(get_lang(call.from_user.id), "access_denied"), show_alert=True)
        return

    with active_broadcasts_lock:
        if call.from_user.id in active_broadcasts:
            bot.answer_callback_query(call.id, t(get_lang(call.from_user.id), "job_in_progress"), show_alert=True)
            return
        active_broadcasts.add(call.from_user.id)

    row = get_user(call.from_user.id)
    lang = row["language"] or "en" if row else "en"
    if not row or not row["pending_group_id"] or not row["pending_message_chat_id"] or not row["pending_message_id"]:
        with active_broadcasts_lock:
            active_broadcasts.discard(call.from_user.id)
        bot.answer_callback_query(call.id)
        return

    bot.answer_callback_query(call.id, t(lang, "running"))
    safe_edit_message(call.message.chat.id, call.message.message_id, t(lang, "running"))
    executor.submit(run_broadcast, call.from_user.id, call.message.chat.id, call.message.message_id)
    logger.info("Broadcast started by user=%s", call.from_user.id)


@bot.callback_query_handler(func=lambda call: call.data == "cancel")
def cancel(call):
    reset_pending(call.from_user.id)
    lang = get_lang(call.from_user.id)
    try:
        bot.edit_message_text(
            chat_id=call.message.chat.id,
            message_id=call.message.message_id,
            text=t(lang, "cancelled"),
        )
    except Exception:
        logger.exception("Failed to edit cancel message")
    bot.answer_callback_query(call.id)


@bot.message_handler(content_types=["new_chat_members", "left_chat_member"])
def membership_events(message):
    save_group_from_message(message)


init_db()
logger.info("Bootstrapping database and bot registry...")
configure_webhook()
logger.info("PMC ENGINE ready")

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "5000")))
