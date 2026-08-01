import logging
import os
import re
from typing import Optional

from flask import Flask, abort, request
import telebot
from telebot.apihelper import ApiTelegramException
from telebot.types import InlineKeyboardButton, InlineKeyboardMarkup

from config import BOT_TOKEN, WEBHOOK_URL, MAX_GROUPS_PER_PAGE
from database import (
    add_bot_record,
    count_bots,
    delete_bot_record,
    ensure_user,
    get_user,
    init_db,
    list_bots,
    reset_pending,
    set_language,
    set_pending,
    set_state,
)
from translations import LANG_BUTTONS, t

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("pmc-bot")

app = Flask(__name__)

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN is missing in Render environment variables.")

bot = telebot.TeleBot(BOT_TOKEN, parse_mode="HTML", threaded=False)

BOT_TOKEN_RE = re.compile(r"^\d+:[A-Za-z0-9_-]{20,}$")


def current_lang(user_id: int) -> str:
    row = get_user(user_id)
    return row["language"] if row and row["language"] else "en"


def lang_keyboard():
    kb = InlineKeyboardMarkup()
    for code, label in LANG_BUTTONS:
        kb.add(InlineKeyboardButton(label, callback_data=f"lang:{code}"))
    return kb


def main_menu_keyboard(lang: str | None = None):
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
    send_or_edit(chat_id, t(lang, "main_menu"), main_menu_keyboard(lang), message_id)


def show_language_prompt(chat_id: int, user_id: int, message_id: Optional[int] = None):
    lang = current_lang(user_id)
    send_or_edit(chat_id, t(lang, "welcome"), lang_keyboard(), message_id)


def show_remove_prompt(chat_id: int, user_id: int, page: int = 0, message_id: Optional[int] = None):
    lang = current_lang(user_id)
    total = count_bots()
    if total <= 0:
        send_or_edit(chat_id, t(lang, "no_bots"), main_menu_keyboard(lang), message_id)
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
    row = get_user(message.from_user.id)
    if row and row["language"]:
        show_main_menu(message.chat.id, message.from_user.id)
    else:
        show_language_prompt(message.chat.id, message.from_user.id)


@bot.message_handler(commands=["help"])
def help_cmd(message):
    ensure_user(message.from_user.id)
    lang = current_lang(message.from_user.id)
    bot.reply_to(message, t(lang, "help"), reply_markup=main_menu_keyboard(lang))


@bot.message_handler(commands=["cancel"])
def cancel_cmd(message):
    ensure_user(message.from_user.id)
    reset_pending(message.from_user.id)
    bot.reply_to(message, t(current_lang(message.from_user.id), "cancelled"), reply_markup=main_menu_keyboard(current_lang(message.from_user.id)))


@bot.message_handler(commands=["addbot"])
def addbot_cmd(message):
    ensure_user(message.from_user.id)
    reset_pending(message.from_user.id)
    set_state(message.from_user.id, "await_bot_label")
    bot.reply_to(
        message,
        t(current_lang(message.from_user.id), "enter_bot_label"),
        reply_markup=cancel_keyboard(current_lang(message.from_user.id)),
    )


@bot.message_handler(commands=["removebot"])
def removebot_cmd(message):
    ensure_user(message.from_user.id)
    reset_pending(message.from_user.id)
    show_remove_prompt(message.chat.id, message.from_user.id, page=0)


@bot.message_handler(commands=["pmcisbasedbdw"])
def secret_panel(message):
    ensure_user(message.from_user.id)
    show_main_menu(message.chat.id, message.from_user.id)


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
    if action == "addbot":
        reset_pending(call.from_user.id)
        set_state(call.from_user.id, "await_bot_label")
        send_or_edit(
            call.message.chat.id,
            t(current_lang(call.from_user.id), "enter_bot_label"),
            cancel_keyboard(current_lang(call.from_user.id)),
            call.message.message_id,
        )
    elif action == "removebot":
        show_remove_prompt(call.message.chat.id, call.from_user.id, page=0, message_id=call.message.message_id)
    elif action == "language":
        show_language_prompt(call.message.chat.id, call.from_user.id, message_id=call.message.message_id)
    elif action == "help":
        send_or_edit(
            call.message.chat.id,
            t(current_lang(call.from_user.id), "help"),
            main_menu_keyboard(current_lang(call.from_user.id)),
            call.message.message_id,
        )
    elif action == "home":
        show_main_menu(call.message.chat.id, call.from_user.id, message_id=call.message.message_id)


@bot.callback_query_handler(func=lambda call: call.data.startswith("rm_page:"))
def paginate_remove_bots(call):
    ensure_user(call.from_user.id)
    try:
        page = max(0, int(call.data.split(":", 1)[1]))
    except Exception:
        page = 0
    bot.answer_callback_query(call.id)
    show_remove_prompt(call.message.chat.id, call.from_user.id, page=page, message_id=call.message.message_id)


@bot.callback_query_handler(func=lambda call: call.data.startswith("rm:"))
def remove_bot(call):
    ensure_user(call.from_user.id)
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


@bot.message_handler(content_types=["text"])
def text_router(message):
    ensure_user(message.from_user.id)
    row = get_user(message.from_user.id)
    lang = current_lang(message.from_user.id)

    if not row or not row["state"]:
        return

    if row["state"] == "await_bot_label":
        if not message.text.strip() or message.text.startswith("/"):
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

    if row["state"] == "await_bot_token":
        token = message.text.strip()
        if not validate_bot_token(token):
            bot.reply_to(message, t(lang, "invalid_bot_token"))
            return
        label = (row["pending_bot_label"] or "Untitled bot").strip()[:100]
        add_bot_record(label=label, token=token, added_by=message.from_user.id)
        reset_pending(message.from_user.id)
        bot.reply_to(message, t(lang, "bot_saved").format(label=label), reply_markup=main_menu_keyboard(lang))
        return


@bot.callback_query_handler(func=lambda call: call.data == "cancel")
def cancel(call):
    ensure_user(call.from_user.id)
    reset_pending(call.from_user.id)
    lang = current_lang(call.from_user.id)
    bot.answer_callback_query(call.id)
    send_or_edit(call.message.chat.id, t(lang, "cancelled"), main_menu_keyboard(lang), call.message.message_id)


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


init_db()
configure_webhook()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "5000")))
