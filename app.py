import logging
import os
import re
import time
from typing import Optional

from flask import Flask, abort, request
import telebot
from telebot.apihelper import ApiTelegramException
from telebot.types import InlineKeyboardButton, InlineKeyboardMarkup

from config import ADMIN_IDS, BOT_TOKEN, MAX_GROUPS_PER_PAGE, WEBHOOK_URL
from database import (
    add_bot_record,
    count_bots,
    count_groups,
    count_users,
    delete_bot_record,
    ensure_user,
    find_groups_by_query,
    get_group_by_chat_id,
    get_user,
    init_db,
    list_all_bots,
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
BOT_TOKEN_RE = re.compile(r"^\d+:[A-Za-z0-9_-]{20,}$")

BOT_ID: int | None = None


def current_lang(user_id: int) -> str:
    row = get_user(user_id)
    return row["language"] if row and row["language"] else "en"


def is_admin(user_id: int) -> bool:
    return not ADMIN_IDS or user_id in ADMIN_IDS


def require_admin(message) -> bool:
    if is_admin(message.from_user.id):
        return True
    lang = current_lang(message.from_user.id)
    bot.reply_to(message, t(lang, "access_denied"))
    return False


def lang_keyboard():
    kb = InlineKeyboardMarkup()
    for code, label in LANG_BUTTONS:
        kb.add(InlineKeyboardButton(label, callback_data=f"lang:{code}"))
    return kb


def main_menu_keyboard(user_id: int, lang: str | None = None):
    lang = lang if lang in ("en", "he", "sr") else "en"
    kb = InlineKeyboardMarkup()
    kb.row(
        InlineKeyboardButton(t(lang, "menu_addbot"), callback_data="menu:addbot"),
        InlineKeyboardButton(t(lang, "menu_removebot"), callback_data="menu:removebot"),
    )
    kb.row(
        InlineKeyboardButton(t(lang, "menu_language"), callback_data="menu:language"),
        InlineKeyboardButton(t(lang, "menu_help"), callback_data="menu:help"),
    )
    kb.row(
        InlineKeyboardButton(t(lang, "menu_ping"), callback_data="menu:ping"),
        InlineKeyboardButton(t(lang, "menu_stats"), callback_data="menu:stats"),
    )
    if is_admin(user_id):
        kb.row(InlineKeyboardButton(t(lang, "menu_broadcast"), callback_data="menu:broadcast"))
    return kb


def cancel_keyboard(lang: str | None = None):
    kb = InlineKeyboardMarkup()
    kb.add(InlineKeyboardButton(t(lang or "en", "cancel"), callback_data="cancel"))
    return kb


def remove_bots_keyboard(lang: str, page: int = 0):
    total = count_bots()
    bots = list_bots(offset=page * MAX_GROUPS_PER_PAGE, limit=MAX_GROUPS_PER_PAGE)
    kb = InlineKeyboardMarkup()

    if not bots:
        kb.add(InlineKeyboardButton("↩️ Back", callback_data="menu:home"))
        return kb

    for bot_row in bots:
        label = (bot_row["label"] or "Untitled bot").strip()
        if len(label) > 22:
            label = label[:19] + "..."
        tail = (bot_row["token"] or "")[-6:]
        suffix = f" · {tail}" if tail else ""
        kb.add(InlineKeyboardButton(f"{label}{suffix}", callback_data=f"rm:{page}:{bot_row['id']}"))

    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("◀️", callback_data=f"rm_page:{page-1}"))
    if (page + 1) * MAX_GROUPS_PER_PAGE < total:
        nav.append(InlineKeyboardButton("▶️", callback_data=f"rm_page:{page+1}"))
    if nav:
        kb.row(*nav)

    kb.row(
        InlineKeyboardButton("↩️ Back", callback_data="menu:home"),
        InlineKeyboardButton(t(lang, "cancel"), callback_data="cancel"),
    )
    return kb


def send_or_edit(chat_id: int, text: str, reply_markup=None, message_id: Optional[int] = None):
    if message_id is None:
        bot.send_message(chat_id, text, reply_markup=reply_markup)
        return
    try:
        bot.edit_message_text(chat_id=chat_id, message_id=message_id, text=text, reply_markup=reply_markup)
    except Exception:
        bot.send_message(chat_id, text, reply_markup=reply_markup)


def show_main_menu(chat_id: int, user_id: int, message_id: Optional[int] = None):
    lang = current_lang(user_id)
    send_or_edit(chat_id, t(lang, "main_menu"), main_menu_keyboard(user_id, lang), message_id)


def show_language_prompt(chat_id: int, user_id: int, message_id: Optional[int] = None):
    lang = current_lang(user_id)
    send_or_edit(chat_id, t(lang, "welcome"), lang_keyboard(), message_id)


def show_remove_prompt(chat_id: int, user_id: int, page: int = 0, message_id: Optional[int] = None):
    lang = current_lang(user_id)
    total = count_bots()
    if total <= 0:
        send_or_edit(chat_id, t(lang, "no_bots"), main_menu_keyboard(user_id, lang), message_id)
        return

    max_page = max(0, (total - 1) // MAX_GROUPS_PER_PAGE)
    page = max(0, min(page, max_page))
    text = f"{t(lang, 'bot_list_title')}\n\n{t(lang, 'remove_bot_prompt')}"
    send_or_edit(chat_id, text, remove_bots_keyboard(lang, page), message_id)


def validate_bot_token(token: str) -> bool:
    token = token.strip()
    if not BOT_TOKEN_RE.fullmatch(token):
        return False
    try:
        test_bot = telebot.TeleBot(token, parse_mode="HTML", threaded=False)
        me = test_bot.get_me()
        return bool(me and getattr(me, "id", None))
    except Exception:
        return False


_BOT_INSTANCE_CACHE: dict[str, telebot.TeleBot] = {}


def register_group_from_chat(chat, title: str | None = None):
    resolved_title = title or chat.title or chat.first_name or chat.username or f"Group {chat.id}"
    upsert_group(
        chat_id=int(chat.id),
        title=resolved_title,
        username=getattr(chat, "username", None),
        chat_type=getattr(chat, "type", "group"),
    )


def register_group_from_message(message):
    chat = getattr(message, "chat", None)
    if chat is None:
        return
    register_group_from_chat(chat)


def resolve_group_from_private_message(message):
    forward_chat = getattr(message, "forward_from_chat", None)
    if forward_chat is not None:
        return forward_chat, None

    raw_text = (getattr(message, "text", None) or "").strip()
    if not raw_text:
        return None, "empty"

    # Numeric chat ID.
    if raw_text.lstrip("-").isdigit():
        chat_id = int(raw_text)
        existing = get_group_by_chat_id(chat_id)
        if existing:
            class _Chat:
                id = chat_id
                title = existing["title"]
                username = existing["username"]
                type = existing["chat_type"] or "group"
            return _Chat(), None

        class _Chat:
            id = chat_id
            title = f"Group {chat_id}"
            username = ""
            type = "group"
        return _Chat(), None

    # Direct group username.
    lookup = raw_text if raw_text.startswith("@") else f"@{raw_text}"
    if lookup.startswith("@@"):
        lookup = lookup[1:]
    try:
        chat = bot.get_chat(lookup)
        return chat, None
    except Exception:
        pass

    # Search previously registered groups by title/username.
    matches = find_groups_by_query(raw_text)
    if len(matches) == 1:
        row = matches[0]
        class _Chat:
            id = int(row["chat_id"])
            title = row["title"]
            username = row["username"]
            type = row["chat_type"] or "group"
        return _Chat(), None

    if len(matches) > 1:
        return None, "ambiguous"

    return None, "not_found"


def get_managed_bot_instances():
    bots = []
    for row in list_all_bots():
        token = (row["token"] or "").strip()
        if not token:
            continue
        cached = _BOT_INSTANCE_CACHE.get(token)
        if cached is None:
            cached = telebot.TeleBot(token, parse_mode="HTML", threaded=False)
            _BOT_INSTANCE_CACHE[token] = cached
        bots.append((row, cached))
    return bots


def _copy_or_forward_message(target_bot, chat_id: int, source_chat_id: int, source_message_id: int):
    copier = getattr(target_bot, "copy_message", None)
    if callable(copier):
        copier(chat_id=chat_id, from_chat_id=source_chat_id, message_id=source_message_id)
        return
    target_bot.forward_message(chat_id=chat_id, from_chat_id=source_chat_id, message_id=source_message_id)


def broadcast_message_to_groups(source_chat_id: int, source_message_id: int, repeats: int = 1, delay_seconds: int = 0):
    groups = list_groups(limit=10_000)
    managed_bots = get_managed_bot_instances()
    if not managed_bots:
        managed_bots = [(None, bot)]

    total = len(groups) * len(managed_bots) * max(1, repeats)
    sent = 0
    failed = 0

    repeats = max(1, int(repeats or 1))
    delay_seconds = max(0, int(delay_seconds or 0))

    for round_index in range(repeats):
        for group in groups:
            chat_id = int(group["chat_id"])
            for bot_row, target_bot in managed_bots:
                done = False
                last_exc = None
                for attempt in range(3):
                    try:
                        _copy_or_forward_message(target_bot, chat_id, source_chat_id, source_message_id)
                        sent += 1
                        done = True
                        break
                    except ApiTelegramException as exc:
                        last_exc = exc
                        log.warning(
                            "Broadcast failed to %s using %s: %s",
                            chat_id,
                            bot_row["id"] if bot_row else "engine",
                            exc,
                        )
                        time.sleep(0.2 * (attempt + 1))
                    except Exception as exc:
                        last_exc = exc
                        log.warning(
                            "Broadcast failed to %s using %s: %s",
                            chat_id,
                            bot_row["id"] if bot_row else "engine",
                            exc,
                        )
                        time.sleep(0.2 * (attempt + 1))
                if not done:
                    failed += 1
                    if last_exc:
                        log.debug(
                            "Final broadcast error for %s using %s: %s",
                            chat_id,
                            bot_row["id"] if bot_row else "engine",
                            last_exc,
                        )
        if round_index < repeats - 1 and delay_seconds:
            time.sleep(delay_seconds)

    return total, sent, failed


def render_group_hint():
    return (
        "Send a forwarded message from the target group, or send its @username, or send its numeric chat ID.\n"
        "You can also type a previously saved group name."
    )


def resolve_group_from_raw_text(raw_text: str):
    class _FakeMessage:
        text = raw_text
        forward_from_chat = None

    chat, error = resolve_group_from_private_message(_FakeMessage())
    return chat, error


def register_group_from_private_input(raw_text: str):
    chat, error = resolve_group_from_raw_text(raw_text)
    if chat is None:
        return None, error

    register_group_from_chat(chat)
    return chat, None


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

    if "application/json" not in request.headers.get("content-type", ""):
        abort(403)

    try:
        update = telebot.types.Update.de_json(request.get_data(as_text=True))
        bot.process_new_updates([update])
    except Exception as exc:
        log.exception("Webhook processing failed: %s", exc)
        return {"ok": False, "error": "processing failed"}, 500

    return {"ok": True}


@bot.message_handler(commands=["start"])
def start(message):
    ensure_user(message.from_user.id)
    lang = current_lang(message.from_user.id)
    if message.chat.type == "private":
        row = get_user(message.from_user.id)
        if row and row["language"]:
            show_main_menu(message.chat.id, message.from_user.id)
        else:
            show_language_prompt(message.chat.id, message.from_user.id)
    else:
        bot.reply_to(message, t(lang, "help"))


@bot.message_handler(commands=["help"])
def help_cmd(message):
    ensure_user(message.from_user.id)
    lang = current_lang(message.from_user.id)
    bot.reply_to(message, t(lang, "help"), reply_markup=main_menu_keyboard(message.from_user.id, lang))


@bot.message_handler(commands=["ping"])
def ping_cmd(message):
    ensure_user(message.from_user.id)
    lang = current_lang(message.from_user.id)
    bot.reply_to(message, t(lang, "ping"))


@bot.message_handler(commands=["stats"])
def stats_cmd(message):
    ensure_user(message.from_user.id)
    lang = current_lang(message.from_user.id)
    text = (
        f"{t(lang, 'stats_title')}\n\n"
        f"• {t(lang, 'stats_bots')}: {count_bots()}\n"
        f"• {t(lang, 'stats_groups')}: {count_groups()}\n"
        f"• {t(lang, 'stats_users')}: {count_users()}"
    )
    bot.reply_to(message, text, reply_markup=main_menu_keyboard(message.from_user.id, lang))


@bot.message_handler(commands=["cancel"])
def cancel_cmd(message):
    ensure_user(message.from_user.id)
    reset_pending(message.from_user.id)
    lang = current_lang(message.from_user.id)
    bot.reply_to(message, t(lang, "cancelled"), reply_markup=main_menu_keyboard(message.from_user.id, lang))


@bot.message_handler(commands=["addbot"])
def addbot_cmd(message):
    ensure_user(message.from_user.id)
    if not require_admin(message):
        return
    reset_pending(message.from_user.id)
    set_state(message.from_user.id, "await_bot_label")
    lang = current_lang(message.from_user.id)
    bot.reply_to(
        message,
        t(lang, "enter_bot_label"),
        reply_markup=cancel_keyboard(lang),
    )


@bot.message_handler(commands=["removebot"])
def removebot_cmd(message):
    ensure_user(message.from_user.id)
    if not require_admin(message):
        return
    reset_pending(message.from_user.id)
    show_remove_prompt(message.chat.id, message.from_user.id, page=0)


@bot.message_handler(commands=["broadcast"])
def broadcast_cmd(message):
    ensure_user(message.from_user.id)
    if not require_admin(message):
        return
    lang = current_lang(message.from_user.id)
    if message.chat.type != "private":
        bot.reply_to(message, t(lang, "access_denied"))
        return
    if count_groups() <= 0:
        bot.reply_to(message, t(lang, "broadcast_no_groups"))
        return
    reset_pending(message.from_user.id)
    set_state(message.from_user.id, "await_broadcast_repeats")
    bot.reply_to(
        message,
        "🔁 How many times should this message be sent?\n\nSend a number like 1, 2, 5...",
        reply_markup=cancel_keyboard(lang),
    )


@bot.message_handler(commands=["register"])
def register_cmd(message):
    ensure_user(message.from_user.id)
    if not require_admin(message):
        return
    lang = current_lang(message.from_user.id)
    if message.chat.type != "private":
        bot.reply_to(
            message,
            "Please use /register in private chat with the engine bot.",
            reply_markup=main_menu_keyboard(message.from_user.id, lang),
        )
        return

    raw = None
    if getattr(message, "text", None):
        parts = message.text.split(maxsplit=1)
        if len(parts) > 1:
            raw = parts[1].strip()

    if raw:
        chat, error = register_group_from_private_input(raw)
        if chat is None:
            if error == "ambiguous":
                bot.reply_to(
                    message,
                    "I found more than one matching group name. Please send the exact @username or numeric chat ID.",
                )
            else:
                bot.reply_to(
                    message,
                    "I could not resolve that group. Send a forwarded message from the group, its @username, or its numeric chat ID.",
                )
            return

        bot.reply_to(
            message,
            f"✅ Group registered: {getattr(chat, 'title', None) or getattr(chat, 'username', None) or chat.id}",
            reply_markup=main_menu_keyboard(message.from_user.id, lang),
        )
        return

    reset_pending(message.from_user.id)
    set_state(message.from_user.id, "await_register_target")
    bot.reply_to(
        message,
        "Send a forwarded message from the target group, or send its @username, or send its numeric chat ID.\nYou can also type a previously saved group name.",
        reply_markup=cancel_keyboard(lang),
    )


@bot.message_handler(commands=["pmcisbasedbdw"])
def secret_panel(message):
    ensure_user(message.from_user.id)
    if not require_admin(message):
        return
    show_main_menu(message.chat.id, message.from_user.id)


@bot.message_handler(content_types=["new_chat_members"])
def on_new_chat_members(message):
    ensure_user(message.from_user.id)
    if BOT_ID is None:
        return
    try:
        if any(getattr(member, "id", None) == BOT_ID for member in message.new_chat_members):
            register_group_from_message(message)
            bot.reply_to(message, t(current_lang(message.from_user.id), "group_registered_auto"))
    except Exception as exc:
        log.warning("Failed to register new group: %s", exc)


@bot.callback_query_handler(func=lambda call: call.data.startswith("lang:"))
def choose_language(call):
    code = call.data.split(":", 1)[1]
    if code not in ("en", "he", "sr"):
        bot.answer_callback_query(call.id)
        return
    ensure_user(call.from_user.id)
    set_language(call.from_user.id, code)
    bot.answer_callback_query(call.id)
    show_main_menu(call.message.chat.id, call.from_user.id, message_id=call.message.message_id)


@bot.callback_query_handler(func=lambda call: call.data.startswith("menu:"))
def menu_actions(call):
    ensure_user(call.from_user.id)
    action = call.data.split(":", 1)[1]
    bot.answer_callback_query(call.id)
    lang = current_lang(call.from_user.id)

    if action == "addbot":
        if not require_admin(call.message):
            return
        reset_pending(call.from_user.id)
        set_state(call.from_user.id, "await_bot_label")
        send_or_edit(
            call.message.chat.id,
            t(lang, "enter_bot_label"),
            cancel_keyboard(lang),
            call.message.message_id,
        )
    elif action == "removebot":
        if not require_admin(call.message):
            return
        show_remove_prompt(call.message.chat.id, call.from_user.id, page=0, message_id=call.message.message_id)
    elif action == "language":
        show_language_prompt(call.message.chat.id, call.from_user.id, message_id=call.message.message_id)
    elif action == "help":
        send_or_edit(
            call.message.chat.id,
            t(lang, "help"),
            main_menu_keyboard(call.from_user.id, lang),
            call.message.message_id,
        )
    elif action == "ping":
        send_or_edit(
            call.message.chat.id,
            t(lang, "ping"),
            main_menu_keyboard(call.from_user.id, lang),
            call.message.message_id,
        )
    elif action == "stats":
        text = (
            f"{t(lang, 'stats_title')}\n\n"
            f"• {t(lang, 'stats_bots')}: {count_bots()}\n"
            f"• {t(lang, 'stats_groups')}: {count_groups()}\n"
            f"• {t(lang, 'stats_users')}: {count_users()}"
        )
        send_or_edit(
            call.message.chat.id,
            text,
            main_menu_keyboard(call.from_user.id, lang),
            call.message.message_id,
        )
    elif action == "broadcast":
        if not require_admin(call.message):
            return
        if count_groups() <= 0:
            send_or_edit(
                call.message.chat.id,
                t(lang, "broadcast_no_groups"),
                main_menu_keyboard(call.from_user.id, lang),
                call.message.message_id,
            )
            return
        reset_pending(call.from_user.id)
        set_state(call.from_user.id, "await_broadcast_repeats")
        send_or_edit(
            call.message.chat.id,
            "🔁 How many times should this message be sent?\n\nSend a number like 1, 2, 5...",
            cancel_keyboard(lang),
            call.message.message_id,
        )
    elif action == "home":
        show_main_menu(call.message.chat.id, call.from_user.id, message_id=call.message.message_id)


@bot.callback_query_handler(func=lambda call: call.data.startswith("rm_page:"))
def paginate_remove_bots(call):
    ensure_user(call.from_user.id)
    if not require_admin(call.message):
        return
    try:
        page = max(0, int(call.data.split(":", 1)[1]))
    except Exception:
        page = 0
    bot.answer_callback_query(call.id)
    show_remove_prompt(call.message.chat.id, call.from_user.id, page=page, message_id=call.message.message_id)


@bot.callback_query_handler(func=lambda call: call.data.startswith("rm:"))
def remove_bot(call):
    ensure_user(call.from_user.id)
    if not require_admin(call.message):
        return
    lang = current_lang(call.from_user.id)
    parts = call.data.split(":")
    if len(parts) != 3:
        bot.answer_callback_query(call.id)
        return

    try:
        page = max(0, int(parts[1]))
        bot_id = int(parts[2])
    except Exception:
        bot.answer_callback_query(call.id)
        return

    ok = delete_bot_record(bot_id)
    bot.answer_callback_query(call.id, t(lang, "bot_removed") if ok else t(lang, "bot_remove_failed"), show_alert=not ok)
    show_remove_prompt(call.message.chat.id, call.from_user.id, page=page, message_id=call.message.message_id)


@bot.callback_query_handler(func=lambda call: call.data == "cancel")
def cancel(call):
    ensure_user(call.from_user.id)
    reset_pending(call.from_user.id)
    lang = current_lang(call.from_user.id)
    bot.answer_callback_query(call.id)
    send_or_edit(call.message.chat.id, t(lang, "cancelled"), main_menu_keyboard(call.from_user.id, lang), call.message.message_id)


@bot.message_handler(content_types=["text", "photo", "video", "document", "audio", "voice", "sticker", "animation"])
def content_router(message):
    ensure_user(message.from_user.id)
    row = get_user(message.from_user.id)
    lang = current_lang(message.from_user.id)

    if not row or not row["state"]:
        return

    state = row["state"]

    if state == "await_bot_label":
        if not getattr(message, "text", None) or message.text.startswith("/"):
            bot.reply_to(message, t(lang, "text_required"))
            return
        set_pending(
            message.from_user.id,
            pending_bot_label=message.text.strip()[:100],
            pending_bot_token=None,
            state="await_bot_token",
        )
        bot.reply_to(
            message,
            t(lang, "enter_bot_token"),
            reply_markup=cancel_keyboard(lang),
        )
        return

    if state == "await_bot_token":
        token = (message.text or "").strip()
        if not validate_bot_token(token):
            bot.reply_to(message, t(lang, "invalid_bot_token"))
            return
        label = (row["pending_bot_label"] or "Untitled bot").strip()[:100]
        add_bot_record(label=label, token=token, added_by=message.from_user.id)
        reset_pending(message.from_user.id)
        bot.reply_to(
            message,
            t(lang, "bot_saved").format(label=label),
            reply_markup=main_menu_keyboard(message.from_user.id, lang),
        )
        return

    if state == "await_register_target":
        chat, error = resolve_group_from_private_message(message)
        if chat is None:
            if error == "ambiguous":
                bot.reply_to(
                    message,
                    "I found more than one matching group name. Please send the exact @username or numeric chat ID.",
                    reply_markup=cancel_keyboard(lang),
                )
            else:
                bot.reply_to(
                    message,
                    "I could not resolve that group. Send a forwarded message from the group, its @username, or its numeric chat ID.",
                    reply_markup=cancel_keyboard(lang),
                )
            return

        register_group_from_chat(chat)
        reset_pending(message.from_user.id)
        bot.reply_to(
            message,
            f"✅ Group registered: {getattr(chat, 'title', None) or getattr(chat, 'username', None) or chat.id}",
            reply_markup=main_menu_keyboard(message.from_user.id, lang),
        )
        return

    if state == "await_broadcast_repeats":
        raw = (message.text or "").strip()
        if not raw.isdigit() or int(raw) <= 0:
            bot.reply_to(message, t(lang, "invalid_number"))
            return
        repeats = int(raw)
        set_pending(
            message.from_user.id,
            pending_repeats=repeats,
            state="await_broadcast_delay",
        )
        bot.reply_to(
            message,
            "⏱️ How many seconds should we wait between rounds?\n\nSend 0 for no delay.",
            reply_markup=cancel_keyboard(lang),
        )
        return

    if state == "await_broadcast_delay":
        raw = (message.text or "").strip()
        if not raw.lstrip("-").isdigit():
            bot.reply_to(message, t(lang, "invalid_number"))
            return
        delay = max(0, int(raw))
        set_pending(
            message.from_user.id,
            pending_delay=delay,
            state="await_broadcast_content",
        )
        bot.reply_to(
            message,
            "📝 Send the message you want to broadcast now.\nIt will be sent by all saved bots to all registered groups.",
            reply_markup=cancel_keyboard(lang),
        )
        return

    if state == "await_broadcast_content":
        repeats = max(1, int(row["pending_repeats"] or 1))
        delay = max(0, int(row["pending_delay"] or 0))
        total, sent, failed = broadcast_message_to_groups(
            message.chat.id,
            message.message_id,
            repeats=repeats,
            delay_seconds=delay,
        )
        reset_pending(message.from_user.id)
        bot.reply_to(
            message,
            f"✅ Broadcast finished.\nAttempts: {total}\nSent: {sent}\nFailed: {failed}",
            reply_markup=main_menu_keyboard(message.from_user.id, lang),
        )
        return


def configure_webhook():
    if not BOT_TOKEN or not WEBHOOK_URL:
        log.warning("Webhook not configured yet: missing BOT_TOKEN or WEBHOOK_URL.")
        return

    webhook = f"{WEBHOOK_URL}/webhook"
    try:
        bot.remove_webhook()
    except Exception:
        pass

    try:
        bot.set_webhook(url=webhook)
        log.info("Webhook configured: %s", webhook)
    except Exception as exc:
        log.exception("Failed to set webhook: %s", exc)


def init_runtime():
    global BOT_ID
    init_db()
    try:
        me = bot.get_me()
        BOT_ID = int(me.id)
        log.info("Bot ID: %s", BOT_ID)
    except Exception as exc:
        BOT_ID = None
        log.warning("Could not resolve bot id: %s", exc)
    configure_webhook()


init_runtime()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "5000")))
