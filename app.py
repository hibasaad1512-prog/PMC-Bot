import logging
import os
import re
import threading
import time

from flask import Flask, abort, request
import telebot
from telebot.apihelper import ApiTelegramException
from telebot.types import InlineKeyboardButton, InlineKeyboardMarkup

from config import BOT_TOKEN, WEBHOOK_URL, MAX_DELAY_SECONDS, MAX_GROUPS_PER_PAGE, MAX_REPEATS
from database import (
    add_bot_record,
    count_groups,
    count_bots,
    ensure_user,
    get_user,
    init_db,
    list_bots,
    list_groups,
    reset_pending,
    set_language,
    set_pending,
    set_state,
    upsert_group,
)
from translations import LANG_BUTTONS, t

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("pmc-bot")

app = Flask(__name__)

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN is missing in Render environment variables.")

bot = telebot.TeleBot(BOT_TOKEN, parse_mode="HTML", threaded=False)

SECRET_COMMAND = "/pmcisbasedbdw"
SUPPORTED_TYPES = ["text", "photo", "video", "document", "sticker", "voice", "audio", "animation"]


def current_lang(user_id: int) -> str:
    row = get_user(user_id)
    return row["language"] if row and row["language"] else "en"


def is_chat_admin(chat_id: int, user_id: int) -> bool:
    try:
        member = bot.get_chat_member(chat_id, user_id)
        return member.status in ("administrator", "creator")
    except Exception:
        return False


def manageable_groups_for_user(user_id: int):
    all_groups = list_groups(offset=0, limit=1000)
    return [g for g in all_groups if is_chat_admin(int(g["chat_id"]), user_id)]


def lang_keyboard():
    kb = InlineKeyboardMarkup()
    for code, label in LANG_BUTTONS:
        kb.add(InlineKeyboardButton(label, callback_data=f"lang:{code}"))
    return kb


def cancel_keyboard():
    kb = InlineKeyboardMarkup()
    kb.add(InlineKeyboardButton("❌ Cancel", callback_data="cancel"))
    return kb


def repeats_keyboard():
    kb = InlineKeyboardMarkup()
    kb.row(
        InlineKeyboardButton("×1", callback_data="repeat:1"),
        InlineKeyboardButton("×5", callback_data="repeat:5"),
        InlineKeyboardButton("×10", callback_data="repeat:10"),
    )
    kb.row(
        InlineKeyboardButton("×25", callback_data="repeat:25"),
        InlineKeyboardButton("×50", callback_data="repeat:50"),
        InlineKeyboardButton("×100", callback_data="repeat:100"),
    )
    kb.row(
        InlineKeyboardButton("×500", callback_data="repeat:500"),
        InlineKeyboardButton("✍️ Custom", callback_data="repeat:custom"),
    )
    kb.add(InlineKeyboardButton("❌ Cancel", callback_data="cancel"))
    return kb


def delay_keyboard():
    kb = InlineKeyboardMarkup()
    kb.row(
        InlineKeyboardButton("0s", callback_data="delay:0"),
        InlineKeyboardButton("1s", callback_data="delay:1"),
        InlineKeyboardButton("5s", callback_data="delay:5"),
    )
    kb.row(
        InlineKeyboardButton("10s", callback_data="delay:10"),
        InlineKeyboardButton("30s", callback_data="delay:30"),
        InlineKeyboardButton("60s", callback_data="delay:60"),
    )
    kb.row(
        InlineKeyboardButton("✍️ Custom", callback_data="delay:custom"),
    )
    kb.add(InlineKeyboardButton("❌ Cancel", callback_data="cancel"))
    return kb


def summary_keyboard():
    kb = InlineKeyboardMarkup()
    kb.row(
        InlineKeyboardButton("✅ Start", callback_data="broadcast_start"),
        InlineKeyboardButton("❌ Cancel", callback_data="cancel"),
    )
    return kb


def group_keyboard(user_id: int, page: int = 0):
    groups = manageable_groups_for_user(user_id)
    total = len(groups)
    start = page * MAX_GROUPS_PER_PAGE
    end = start + MAX_GROUPS_PER_PAGE
    page_groups = groups[start:end]
    kb = InlineKeyboardMarkup()

    for g in page_groups:
        title = g["title"] or str(g["chat_id"])
        if len(title) > 28:
            title = title[:25] + "..."
        kb.add(InlineKeyboardButton(title, callback_data=f"group:{g['chat_id']}"))

    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("◀️", callback_data=f"groups:{page-1}"))
    if end < total:
        nav.append(InlineKeyboardButton("▶️", callback_data=f"groups:{page+1}"))
    if nav:
        kb.row(*nav)

    kb.add(InlineKeyboardButton("❌ Cancel", callback_data="cancel"))
    return kb


def save_group_from_message(message):
    chat = message.chat
    if chat.type in ("group", "supergroup"):
        upsert_group(chat.id, chat.title or str(chat.id), getattr(chat, "username", None), chat.type)


def send_language_prompt(chat_id: int, user_id: int):
    lang = current_lang(user_id)
    row = get_user(user_id)
    if row and row["language"]:
        text = t(lang, "already_set")
    else:
        text = t(lang, "welcome")
    bot.send_message(chat_id, text, reply_markup=lang_keyboard())


def normalize_number(text: str, minimum: int, maximum: int) -> int | None:
    try:
        value = int(text.strip())
    except Exception:
        return None
    if value < minimum:
        return None
    return min(value, maximum)


BOT_TOKEN_RE = re.compile(r"^\d+:[A-Za-z0-9_-]{20,}$")


def validate_bot_token(token: str) -> bool:
    return bool(BOT_TOKEN_RE.fullmatch(token.strip()))


def broadcast_summary_text(lang: str, group_title: str, repeats: int, delay: int) -> str:
    return (
        f"{t(lang, 'summary')}\n\n"
        f"{t(lang, 'summary_group')}: {group_title}\n"
        f"{t(lang, 'summary_message')}: ✓\n"
        f"{t(lang, 'summary_repeats')}: {repeats}\n"
        f"{t(lang, 'summary_delay')}: {delay}s"
    )


def retry_after_seconds(exc: Exception) -> int | None:
    text = str(exc)

    # Common Telegram "Too Many Requests: retry after X" message.
    match = re.search(r"retry after (\d+)", text, re.IGNORECASE)
    if match:
        return int(match.group(1))

    # pyTelegramBotAPI / requests sometimes keep structured JSON inside the exception.
    for attr in ("result_json", "json", "response_json"):
        payload = getattr(exc, attr, None)
        if isinstance(payload, dict):
            params = payload.get("parameters") or {}
            if isinstance(params, dict) and params.get("retry_after"):
                try:
                    return int(params["retry_after"])
                except Exception:
                    pass

    return None


def copy_message_resilient(
    *,
    chat_id: int,
    from_chat_id: int,
    message_id: int,
    attempts: int = 4,
) -> bool:
    for attempt in range(1, attempts + 1):
        try:
            bot.copy_message(
                chat_id=chat_id,
                from_chat_id=from_chat_id,
                message_id=message_id,
            )
            return True
        except ApiTelegramException as exc:
            retry_after = retry_after_seconds(exc)
            if retry_after is not None and attempt < attempts:
                wait_for = min(max(retry_after + 1, 1), 30)
                log.warning("Telegram rate limit hit. Retrying copy_message in %ss", wait_for)
                time.sleep(wait_for)
                continue

            transient = any(
                key in str(exc).lower()
                for key in (
                    "timeout",
                    "temporarily unavailable",
                    "internal server error",
                    "bad gateway",
                    "connection",
                    "gateway timeout",
                )
            )
            if transient and attempt < attempts:
                backoff = min(2 ** attempt, 10)
                log.warning("Transient Telegram error. Retrying copy_message in %ss", backoff)
                time.sleep(backoff)
                continue

            log.exception("Telegram API error while copying message: %s", exc)
            return False
        except Exception as exc:
            if attempt < attempts:
                backoff = min(2 ** attempt, 10)
                log.warning("Unexpected broadcast error. Retrying in %ss", backoff)
                time.sleep(backoff)
                continue

            log.exception("Unexpected broadcast error: %s", exc)
            return False

    return False


def start_broadcast_worker(admin_id: int, control_chat_id: int, control_message_id: int):
    row = get_user(admin_id)
    if not row:
        return

    group_id = int(row["pending_group_id"])
    source_chat_id = int(row["pending_message_chat_id"])
    source_message_id = int(row["pending_message_id"])
    repeats = max(1, min(int(row["pending_repeats"] or 1), MAX_REPEATS))
    delay = max(0, min(int(row["pending_delay"] or 0), MAX_DELAY_SECONDS))
    lang = row["language"] or "en"

    group_rows = {int(g["chat_id"]): g for g in list_groups(offset=0, limit=1000)}
    group_row = group_rows.get(group_id)
    group_title = group_row["title"] if group_row else str(group_id)

    sent = 0
    failed = 0
    progress_step = 25 if repeats >= 100 else 10 if repeats >= 20 else 5

    try:
        bot.edit_message_text(
            chat_id=control_chat_id,
            message_id=control_message_id,
            text=t(lang, "broadcast_running"),
        )
    except Exception:
        pass

    for index in range(repeats):
        ok = copy_message_resilient(
            chat_id=group_id,
            from_chat_id=source_chat_id,
            message_id=source_message_id,
        )
        if ok:
            sent += 1
        else:
            failed += 1

        if index < repeats - 1 and delay:
            time.sleep(delay)

        if (index + 1) % progress_step == 0 or (index + 1) == repeats:
            try:
                bot.edit_message_text(
                    chat_id=control_chat_id,
                    message_id=control_message_id,
                    text=t(lang, "broadcast_progress").format(sent=sent, total=repeats, failed=failed),
                )
            except Exception:
                pass

    reset_pending(admin_id)
    try:
        bot.edit_message_text(
            chat_id=control_chat_id,
            message_id=control_message_id,
            text=(
                f"{t(lang, 'broadcast_finished').format(sent=sent, total=repeats, failed=failed)}\n\n"
                f"{t(lang, 'summary_group')}: {group_title}"
            ),
        )
    except Exception:
        pass


@app.route("/", methods=["GET"])
def index():
    return {"ok": True, "service": "PMC Bot"}


@app.route("/health", methods=["GET"])
def health():
    return {"ok": True}


@app.route("/webhook", methods=["POST"])
@app.route("/webhook/<path:subpath>", methods=["POST"])
def telegram_webhook(subpath=None):
    if not BOT_TOKEN:
        return {"ok": False, "error": "BOT_TOKEN is missing"}, 500

    content_type = request.headers.get("content-type", "")
    if "application/json" not in content_type:
        abort(403)

    try:
        raw_update = request.get_data(as_text=True)
        update = telebot.types.Update.de_json(raw_update)
        bot.process_new_updates([update])
    except Exception as exc:
        log.exception("Webhook processing failed: %s", exc)
        return {"ok": False, "error": "processing failed"}, 500

    return {"ok": True}


@bot.message_handler(commands=["start"])
def start(message):
    ensure_user(message.from_user.id)
    send_language_prompt(message.chat.id, message.from_user.id)


@bot.callback_query_handler(func=lambda call: call.data.startswith("lang:"))
def choose_language(call):
    code = call.data.split(":", 1)[1]
    if code not in ("en", "he", "sr"):
        bot.answer_callback_query(call.id)
        return
    ensure_user(call.from_user.id)
    set_language(call.from_user.id, code)
    bot.answer_callback_query(call.id)
    try:
        bot.edit_message_text(
            chat_id=call.message.chat.id,
            message_id=call.message.message_id,
            text=t(code, "language_saved"),
        )
    except Exception:
        pass


@bot.message_handler(commands=["register"])
def register_group(message):
    ensure_user(message.from_user.id)
    save_group_from_message(message)
    bot.reply_to(message, t(current_lang(message.from_user.id), "registered"))


@bot.message_handler(commands=["help"])
def help_cmd(message):
    ensure_user(message.from_user.id)
    bot.reply_to(message, t(current_lang(message.from_user.id), "help"))


@bot.message_handler(commands=["cancel"])
def cancel_cmd(message):
    ensure_user(message.from_user.id)
    reset_pending(message.from_user.id)
    bot.reply_to(message, t(current_lang(message.from_user.id), "cancelled"))


@bot.message_handler(commands=[SECRET_COMMAND.lstrip("/")])
def secret_panel(message):
    ensure_user(message.from_user.id)
    lang = current_lang(message.from_user.id)
    groups = manageable_groups_for_user(message.from_user.id)

    if not groups:
        bot.reply_to(message, t(lang, "no_groups"))
        return

    set_state(message.from_user.id, "choose_group")
    bot.reply_to(message, t(lang, "choose_group"), reply_markup=group_keyboard(message.from_user.id, page=0))


@bot.callback_query_handler(func=lambda call: call.data.startswith("groups:"))
def paginate_groups(call):
    ensure_user(call.from_user.id)
    lang = current_lang(call.from_user.id)
    try:
        page = max(0, int(call.data.split(":", 1)[1]))
    except Exception:
        page = 0

    groups = manageable_groups_for_user(call.from_user.id)
    if not groups:
        bot.answer_callback_query(call.id, t(lang, "no_groups"), show_alert=True)
        return

    try:
        bot.edit_message_text(
            chat_id=call.message.chat.id,
            message_id=call.message.message_id,
            text=t(lang, "choose_group"),
            reply_markup=group_keyboard(call.from_user.id, page=page),
        )
    except Exception:
        pass
    bot.answer_callback_query(call.id)


@bot.callback_query_handler(func=lambda call: call.data.startswith("group:"))
def select_group(call):
    ensure_user(call.from_user.id)
    lang = current_lang(call.from_user.id)
    try:
        group_id = int(call.data.split(":", 1)[1])
    except Exception:
        bot.answer_callback_query(call.id)
        return

    if not is_chat_admin(group_id, call.from_user.id):
        bot.answer_callback_query(call.id, t(lang, "not_admin"), show_alert=True)
        return

    set_pending(call.from_user.id, pending_group_id=group_id, state="await_message")
    bot.answer_callback_query(call.id)
    try:
        bot.edit_message_text(
            chat_id=call.message.chat.id,
            message_id=call.message.message_id,
            text=t(lang, "send_message"),
            reply_markup=cancel_keyboard(),
        )
    except Exception:
        pass


@bot.message_handler(content_types=SUPPORTED_TYPES)
def catch_broadcast_message(message):
    save_group_from_message(message)
    ensure_user(message.from_user.id)
    row = get_user(message.from_user.id)
    if not row or not row["state"]:
        return

    valid_states = (
        "await_message",
        "await_repeats",
        "await_repeats_text",
        "await_delay",
        "await_delay_text",
        "await_bot_label",
        "await_bot_token",
    )
    if row["state"] not in valid_states:
        return

    lang = row["language"] or "en"

    if getattr(message, "text", None) and message.text.startswith("/"):
        return

    if row["state"] == "await_bot_label":
        if not getattr(message, "text", None):
            bot.reply_to(message, t(lang, "text_required"))
            return

        label = message.text.strip()
        if not label:
            bot.reply_to(message, t(lang, "text_required"))
            return

        set_pending(
            message.from_user.id,
            pending_bot_label=label[:100],
            pending_bot_token=None,
            state="await_bot_token",
        )
        bot.reply_to(message, t(lang, "enter_bot_token"), reply_markup=cancel_keyboard())
        return

    if row["state"] == "await_bot_token":
        if not getattr(message, "text", None):
            bot.reply_to(message, t(lang, "invalid_bot_token"))
            return

        token = message.text.strip()
        if not validate_bot_token(token):
            bot.reply_to(message, t(lang, "invalid_bot_token"))
            return

        label = (row["pending_bot_label"] or "Untitled bot").strip()[:100]
        add_bot_record(label=label, token=token, added_by=message.from_user.id)
        reset_pending(message.from_user.id)
        bot.reply_to(message, t(lang, "bot_saved").format(label=label))
        return

    if row["state"] == "await_message":
        set_pending(
            message.from_user.id,
            pending_message_chat_id=message.chat.id,
            pending_message_id=message.message_id,
            pending_repeats=1,
            pending_delay=0,
            state="await_repeats",
        )
        bot.reply_to(message, t(lang, "choose_repeats"), reply_markup=repeats_keyboard())
        return

    if row["state"] in ("await_repeats", "await_repeats_text"):
        if not getattr(message, "text", None):
            return
        repeats = normalize_number(message.text, 1, MAX_REPEATS)
        if repeats is None:
            bot.reply_to(message, t(lang, "invalid_number"))
            return

        set_pending(message.from_user.id, pending_repeats=repeats, state="await_delay")
        bot.reply_to(message, t(lang, "choose_delay"), reply_markup=delay_keyboard())
        return

    if row["state"] in ("await_delay", "await_delay_text"):
        if not getattr(message, "text", None):
            return
        delay = normalize_number(message.text, 0, MAX_DELAY_SECONDS)
        if delay is None:
            bot.reply_to(message, t(lang, "invalid_number"))
            return

        set_pending(message.from_user.id, pending_delay=delay, state="confirm")
        group_rows = {int(g["chat_id"]): g for g in list_groups(offset=0, limit=1000)}
        group_id = int(row["pending_group_id"])
        group_row = group_rows.get(group_id)
        group_title = group_row["title"] if group_row else str(group_id)
        repeats = int(row["pending_repeats"] or 1)
        bot.reply_to(
            message,
            broadcast_summary_text(lang, group_title, repeats, delay),
            reply_markup=summary_keyboard(),
        )
        return


@bot.callback_query_handler(func=lambda call: call.data.startswith("repeat:"))
def choose_repeat_count(call):
    ensure_user(call.from_user.id)
    lang = current_lang(call.from_user.id)
    row = get_user(call.from_user.id)
    if not row or not row["state"]:
        bot.answer_callback_query(call.id)
        return

    if row["state"] not in ("await_repeats", "await_repeats_text"):
        bot.answer_callback_query(call.id)
        return

    choice = call.data.split(":", 1)[1]
    if choice == "custom":
        set_state(call.from_user.id, "await_repeats_text")
        bot.answer_callback_query(call.id)
        try:
            bot.edit_message_text(
                chat_id=call.message.chat.id,
                message_id=call.message.message_id,
                text=t(lang, "enter_repeat_number"),
                reply_markup=cancel_keyboard(),
            )
        except Exception:
            pass
        return

    repeats = normalize_number(choice, 1, MAX_REPEATS)
    if repeats is None:
        bot.answer_callback_query(call.id)
        return

    set_pending(call.from_user.id, pending_repeats=repeats, state="await_delay")
    bot.answer_callback_query(call.id)
    try:
        bot.edit_message_text(
            chat_id=call.message.chat.id,
            message_id=call.message.message_id,
            text=t(lang, "choose_delay"),
            reply_markup=delay_keyboard(),
        )
    except Exception:
        pass


@bot.callback_query_handler(func=lambda call: call.data.startswith("delay:"))
def choose_delay_count(call):
    ensure_user(call.from_user.id)
    lang = current_lang(call.from_user.id)
    row = get_user(call.from_user.id)
    if not row or not row["state"]:
        bot.answer_callback_query(call.id)
        return

    if row["state"] not in ("await_delay", "await_delay_text"):
        bot.answer_callback_query(call.id)
        return

    choice = call.data.split(":", 1)[1]
    if choice == "custom":
        set_state(call.from_user.id, "await_delay_text")
        bot.answer_callback_query(call.id)
        try:
            bot.edit_message_text(
                chat_id=call.message.chat.id,
                message_id=call.message.message_id,
                text=t(lang, "enter_delay_number"),
                reply_markup=cancel_keyboard(),
            )
        except Exception:
            pass
        return

    delay = normalize_number(choice, 0, MAX_DELAY_SECONDS)
    if delay is None:
        bot.answer_callback_query(call.id)
        return

    set_pending(call.from_user.id, pending_delay=delay, state="confirm")
    group_rows = {int(g["chat_id"]): g for g in list_groups(offset=0, limit=1000)}
    group_id = int(row["pending_group_id"])
    group_row = group_rows.get(group_id)
    group_title = group_row["title"] if group_row else str(group_id)
    repeats = int(row["pending_repeats"] or 1)

    bot.answer_callback_query(call.id)
    try:
        bot.edit_message_text(
            chat_id=call.message.chat.id,
            message_id=call.message.message_id,
            text=broadcast_summary_text(lang, group_title, repeats, delay),
            reply_markup=summary_keyboard(),
        )
    except Exception:
        pass


@bot.callback_query_handler(func=lambda call: call.data == "broadcast_start")
def start_broadcast(call):
    ensure_user(call.from_user.id)
    lang = current_lang(call.from_user.id)
    row = get_user(call.from_user.id)
    if not row or row["state"] != "confirm":
        bot.answer_callback_query(call.id)
        return

    if not row["pending_group_id"] or not row["pending_message_chat_id"] or not row["pending_message_id"]:
        bot.answer_callback_query(call.id)
        return

    group_id = int(row["pending_group_id"])
    if not is_chat_admin(group_id, call.from_user.id):
        bot.answer_callback_query(call.id, t(lang, "not_admin"), show_alert=True)
        return

    set_state(call.from_user.id, "broadcasting")
    bot.answer_callback_query(call.id)

    thread = threading.Thread(
        target=start_broadcast_worker,
        args=(call.from_user.id, call.message.chat.id, call.message.message_id),
        daemon=True,
    )
    thread.start()


@bot.callback_query_handler(func=lambda call: call.data == "cancel")
def cancel(call):
    ensure_user(call.from_user.id)
    reset_pending(call.from_user.id)
    lang = current_lang(call.from_user.id)
    try:
        bot.edit_message_text(
            chat_id=call.message.chat.id,
            message_id=call.message.message_id,
            text=t(lang, "cancelled"),
        )
    except Exception:
        pass
    bot.answer_callback_query(call.id)


@bot.message_handler(content_types=["new_chat_members", "left_chat_member"])
def membership_events(message):
    save_group_from_message(message)


def configure_webhook():
    if not BOT_TOKEN or not WEBHOOK_URL:
        log.warning("Webhook not configured yet: missing BOT_TOKEN or WEBHOOK_URL.")
        return

    webhook = f"{WEBHOOK_URL}/webhook"
    try:
        bot.remove_webhook()
    except Exception:
        pass
    bot.set_webhook(url=webhook)
    log.info("Webhook configured: %s", webhook)


init_db()
configure_webhook()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "5000")))
