import logging
import os
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Optional
from urllib.parse import urlparse, unquote

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
    deactivate_group,
    delete_bot_record,
    delete_group_record,
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
BROADCAST_MAX_WORKERS = max(4, int(os.getenv("BROADCAST_MAX_WORKERS", "32")))
BROADCAST_RETRY_SLEEP = float(os.getenv("BROADCAST_RETRY_SLEEP", "0.12"))


def _get_attr_or_key(obj, name, default=None):
    if obj is None:
        return default
    if isinstance(obj, dict):
        return obj.get(name, default)
    return getattr(obj, name, default)


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


def remove_groups_keyboard(lang: str, page: int = 0):
    total = count_groups()
    groups = list_groups(offset=page * MAX_GROUPS_PER_PAGE, limit=MAX_GROUPS_PER_PAGE)
    kb = InlineKeyboardMarkup()

    if not groups:
        kb.add(InlineKeyboardButton("↩️ Back", callback_data="menu:home"))
        return kb

    for group_row in groups:
        title = (group_row["title"] or "Untitled group").strip()
        if len(title) > 22:
            title = title[:19] + "..."
        chat_id = int(group_row["chat_id"])
        kb.add(InlineKeyboardButton(f"{title} · {chat_id}", callback_data=f"grm:{page}:{chat_id}"))

    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("◀️", callback_data=f"grm_page:{page-1}"))
    if (page + 1) * MAX_GROUPS_PER_PAGE < total:
        nav.append(InlineKeyboardButton("▶️", callback_data=f"grm_page:{page+1}"))
    if nav:
        kb.row(*nav)

    kb.row(
        InlineKeyboardButton("↩️ Back", callback_data="menu:home"),
        InlineKeyboardButton(t(lang, "cancel"), callback_data="cancel"),
    )
    return kb


def show_remove_group_prompt(chat_id: int, user_id: int, page: int = 0, message_id: Optional[int] = None):
    lang = current_lang(user_id)
    total = count_groups()
    if total <= 0:
        send_or_edit(chat_id, t(lang, "broadcast_no_groups"), main_menu_keyboard(user_id, lang), message_id)
        return

    max_page = max(0, (total - 1) // MAX_GROUPS_PER_PAGE)
    page = max(0, min(page, max_page))
    text = f"🗑️ Remove a group\n\nSelect a saved group to delete it from the list."
    send_or_edit(chat_id, text, remove_groups_keyboard(lang, page), message_id)


def broadcast_groups_keyboard(lang: str, page: int = 0):
    total = count_groups()
    groups = list_groups(offset=page * MAX_GROUPS_PER_PAGE, limit=MAX_GROUPS_PER_PAGE)
    kb = InlineKeyboardMarkup()

    if not groups:
        kb.add(InlineKeyboardButton("↩️ Back", callback_data="menu:home"))
        return kb

    for group_row in groups:
        title = (group_row["title"] or "Untitled group").strip()
        if len(title) > 22:
            title = title[:19] + "..."
        chat_id = int(group_row["chat_id"])
        username = (group_row["username"] or "").strip()
        suffix = f" @{username.lstrip('@')}" if username else ""
        kb.add(InlineKeyboardButton(f"{title}{suffix}", callback_data=f"bg:{page}:{chat_id}"))

    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("◀️", callback_data=f"bg_page:{page-1}"))
    if (page + 1) * MAX_GROUPS_PER_PAGE < total:
        nav.append(InlineKeyboardButton("▶️", callback_data=f"bg_page:{page+1}"))
    if nav:
        kb.row(*nav)

    kb.row(
        InlineKeyboardButton("↩️ Back", callback_data="menu:home"),
        InlineKeyboardButton(t(lang, "cancel"), callback_data="cancel"),
    )
    return kb


def show_broadcast_group_prompt(chat_id: int, user_id: int, page: int = 0, message_id: Optional[int] = None):
    lang = current_lang(user_id)
    total = count_groups()
    if total <= 0:
        send_or_edit(chat_id, t(lang, "broadcast_no_groups"), main_menu_keyboard(user_id, lang), message_id)
        return

    max_page = max(0, (total - 1) // MAX_GROUPS_PER_PAGE)
    page = max(0, min(page, max_page))
    text = f"{t(lang, 'choose_group')}\n\n{t(lang, 'select_broadcast_group')}"
    send_or_edit(chat_id, text, broadcast_groups_keyboard(lang, page), message_id)


def normalize_group_reference(raw_text: str) -> str:
    raw = (raw_text or "").strip()
    if not raw:
        return ""

    # If user pasted a t.me link, keep only the username/path.
    if raw.startswith("http://") or raw.startswith("https://"):
        parsed = urlparse(raw)
        raw = (parsed.path or "").strip("/")
    elif raw.startswith("t.me/") or raw.startswith("telegram.me/"):
        raw = raw.split("/", 1)[1].strip()

    raw = raw.strip()
    raw = raw.split("?", 1)[0].split("#", 1)[0].strip()

    if raw.startswith("@") and len(raw) > 1:
        return raw
    if raw.startswith("+" ) or raw.lower().startswith("joinchat/"):
        return raw
    if raw.lstrip("-").isdigit():
        return raw
    # Convert plain username to @username.
    if raw:
        return f"@{raw}"
    return ""


def _extract_forwarded_chat(message):
    forward_chat = _get_attr_or_key(message, "forward_from_chat", None)
    if forward_chat is not None:
        return forward_chat
    forward_origin = _get_attr_or_key(message, "forward_origin", None)
    if forward_origin is not None:
        origin_chat = _get_attr_or_key(forward_origin, "chat", None) or _get_attr_or_key(forward_origin, "sender_chat", None)
        if origin_chat is not None:
            return origin_chat
    return None


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
    resolved_title = title or _get_attr_or_key(chat, "title") or _get_attr_or_key(chat, "first_name") or _get_attr_or_key(chat, "username") or f"Group {_get_attr_or_key(chat, 'id')}"
    upsert_group(
        chat_id=int(_get_attr_or_key(chat, "id")),
        title=resolved_title,
        username=_get_attr_or_key(chat, "username", None),
        chat_type=_get_attr_or_key(chat, "type", "group"),
    )


def register_group_from_message(message):
    chat = getattr(message, "chat", None)
    if chat is None:
        return
    register_group_from_chat(chat)


def resolve_group_from_private_message(message):
    forward_chat = _extract_forwarded_chat(message)
    if forward_chat is not None:
        return forward_chat, None

    raw_text = (getattr(message, "text", None) or "").strip()
    if not raw_text:
        return None, "empty"

    raw_text = normalize_group_reference(raw_text)
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
    for bot_row, resolver_bot in get_group_resolver_bots():
        try:
            chat = resolver_bot.get_chat(lookup)
            if chat is not None:
                return chat, None
        except Exception as exc:
            log.debug("get_chat failed via %s for %s: %s", bot_row["id"] if bot_row else "engine", lookup, exc)

    # Search previously registered groups by title/username.
    matches = find_groups_by_query(raw_text.lstrip("@"))
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
        if not token or token == BOT_TOKEN:
            continue
        cached = _BOT_INSTANCE_CACHE.get(token)
        if cached is None:
            cached = telebot.TeleBot(token, parse_mode="HTML", threaded=False)
            _BOT_INSTANCE_CACHE[token] = cached
        bots.append((row, cached))
    return bots


def get_group_resolver_bots():
    resolver_bots = []
    seen_tokens = set()
    for row, target_bot in get_managed_bot_instances():
        token = (row["token"] or "").strip()
        if token and token not in seen_tokens:
            resolver_bots.append((row, target_bot))
            seen_tokens.add(token)
    if BOT_TOKEN and BOT_TOKEN not in seen_tokens:
        resolver_bots.append((None, bot))
    return resolver_bots



def _copy_or_forward_message(target_bot, chat_id: int, source_chat_id: int, source_message_id: int):
    copier = getattr(target_bot, "copy_message", None)
    if callable(copier):
        copier(chat_id=chat_id, from_chat_id=source_chat_id, message_id=source_message_id)
        return
    target_bot.forward_message(chat_id=chat_id, from_chat_id=source_chat_id, message_id=source_message_id)



def _clean_kwargs(kwargs: dict):
    return {key: value for key, value in kwargs.items() if value is not None}


def _should_retry_api_exception(exc: Exception) -> bool:
    code = getattr(exc, "error_code", None)
    if code in (400, 401, 403, 404):
        return False
    if code == 429:
        return True
    description = str(exc).lower()
    if "forbidden" in description or "bad request" in description:
        return False
    return True



def extract_broadcast_payload(message):
    """
    Capture the incoming message in a bot-agnostic structure so follower bots
    can send it themselves, without needing access to the engine's private chat.
    Supports the most common Telegram message types, including media and rich content.
    """
    content_type = getattr(message, "content_type", None) or (
        "text" if getattr(message, "text", None) else "unknown"
    )

    payload = {
        "type": content_type,
        "text": None,
        "file_id": None,
        "caption": None,
        "phone_number": None,
        "first_name": None,
        "last_name": None,
        "vcard": None,
        "latitude": None,
        "longitude": None,
        "live_period": None,
        "horizontal_accuracy": None,
        "heading": None,
        "proximity_alert_radius": None,
        "title": None,
        "address": None,
        "foursquare_id": None,
        "foursquare_type": None,
        "google_place_id": None,
        "google_place_type": None,
        "question": None,
        "options": None,
        "is_anonymous": None,
        "poll_type": None,
        "allows_multiple_answers": None,
        "correct_option_id": None,
        "explanation": None,
        "open_period": None,
        "close_date": None,
        "is_closed": None,
        "emoji": None,
    }

    if content_type == "text":
        payload["text"] = getattr(message, "text", "") or ""
        return payload

    if content_type == "photo":
        photos = getattr(message, "photo", None) or []
        if photos:
            payload["file_id"] = photos[-1].file_id
        payload["caption"] = getattr(message, "caption", None)
        return payload

    if content_type in {"video", "document", "audio", "voice", "animation", "sticker", "video_note"}:
        obj = getattr(message, content_type, None)
        if obj is not None:
            payload["file_id"] = getattr(obj, "file_id", None)
        if content_type not in {"sticker", "video_note"}:
            payload["caption"] = getattr(message, "caption", None)
        return payload

    if content_type == "contact":
        contact = getattr(message, "contact", None)
        if contact is not None:
            payload["phone_number"] = getattr(contact, "phone_number", None)
            payload["first_name"] = getattr(contact, "first_name", None)
            payload["last_name"] = getattr(contact, "last_name", None)
            payload["vcard"] = getattr(contact, "vcard", None)
        return payload

    if content_type == "location":
        location = getattr(message, "location", None)
        if location is not None:
            payload["latitude"] = getattr(location, "latitude", None)
            payload["longitude"] = getattr(location, "longitude", None)
            payload["live_period"] = getattr(location, "live_period", None)
            payload["horizontal_accuracy"] = getattr(location, "horizontal_accuracy", None)
            payload["heading"] = getattr(location, "heading", None)
            payload["proximity_alert_radius"] = getattr(location, "proximity_alert_radius", None)
        return payload

    if content_type == "venue":
        venue = getattr(message, "venue", None)
        if venue is not None:
            payload["latitude"] = getattr(venue, "location", None).latitude if getattr(venue, "location", None) else None
            payload["longitude"] = getattr(venue, "location", None).longitude if getattr(venue, "location", None) else None
            payload["title"] = getattr(venue, "title", None)
            payload["address"] = getattr(venue, "address", None)
            payload["foursquare_id"] = getattr(venue, "foursquare_id", None)
            payload["foursquare_type"] = getattr(venue, "foursquare_type", None)
            payload["google_place_id"] = getattr(venue, "google_place_id", None)
            payload["google_place_type"] = getattr(venue, "google_place_type", None)
        return payload

    if content_type == "poll":
        poll = getattr(message, "poll", None)
        if poll is not None:
            payload["question"] = getattr(poll, "question", None)
            payload["options"] = [getattr(opt, "text", str(opt)) for opt in (getattr(poll, "options", None) or [])]
            payload["is_anonymous"] = getattr(poll, "is_anonymous", None)
            payload["poll_type"] = getattr(poll, "type", None)
            payload["allows_multiple_answers"] = getattr(poll, "allows_multiple_answers", None)
            payload["correct_option_id"] = getattr(poll, "correct_option_id", None)
            payload["explanation"] = getattr(poll, "explanation", None)
            payload["open_period"] = getattr(poll, "open_period", None)
            payload["close_date"] = getattr(poll, "close_date", None)
            payload["is_closed"] = getattr(poll, "is_closed", None)
        return payload

    if content_type == "dice":
        dice = getattr(message, "dice", None)
        if dice is not None:
            payload["emoji"] = getattr(dice, "emoji", None)
        return payload

    # Fallback for unsupported types; we'll try to use text if present.
    payload["text"] = getattr(message, "text", None)
    payload["caption"] = getattr(message, "caption", None)
    return payload



def send_payload_with_bot(target_bot, chat_id: int, payload: dict):
    content_type = payload.get("type") or "text"

    if content_type == "text":
        text = payload.get("text") or ""
        return target_bot.send_message(chat_id, text)

    file_id = payload.get("file_id")
    caption = payload.get("caption")

    if content_type == "photo":
        return target_bot.send_photo(chat_id, file_id, caption=caption or None)
    if content_type == "video":
        return target_bot.send_video(chat_id, file_id, caption=caption or None)
    if content_type == "document":
        return target_bot.send_document(chat_id, file_id, caption=caption or None)
    if content_type == "audio":
        return target_bot.send_audio(chat_id, file_id, caption=caption or None)
    if content_type == "voice":
        return target_bot.send_voice(chat_id, file_id, caption=caption or None)
    if content_type == "animation":
        return target_bot.send_animation(chat_id, file_id, caption=caption or None)
    if content_type == "sticker":
        return target_bot.send_sticker(chat_id, file_id)
    if content_type == "video_note":
        return target_bot.send_video_note(chat_id, file_id)

    if content_type == "contact":
        kwargs = _clean_kwargs(
            {
                "phone_number": payload.get("phone_number"),
                "first_name": payload.get("first_name"),
                "last_name": payload.get("last_name"),
                "vcard": payload.get("vcard"),
            }
        )
        return target_bot.send_contact(chat_id, **kwargs)

    if content_type == "location":
        kwargs = _clean_kwargs(
            {
                "latitude": payload.get("latitude"),
                "longitude": payload.get("longitude"),
                "live_period": payload.get("live_period"),
                "horizontal_accuracy": payload.get("horizontal_accuracy"),
                "heading": payload.get("heading"),
                "proximity_alert_radius": payload.get("proximity_alert_radius"),
            }
        )
        return target_bot.send_location(chat_id, **kwargs)

    if content_type == "venue":
        kwargs = _clean_kwargs(
            {
                "latitude": payload.get("latitude"),
                "longitude": payload.get("longitude"),
                "title": payload.get("title"),
                "address": payload.get("address"),
                "foursquare_id": payload.get("foursquare_id"),
                "foursquare_type": payload.get("foursquare_type"),
                "google_place_id": payload.get("google_place_id"),
                "google_place_type": payload.get("google_place_type"),
            }
        )
        return target_bot.send_venue(chat_id, **kwargs)

    if content_type == "poll":
        options = [opt for opt in (payload.get("options") or []) if opt]
        if not payload.get("question") or len(options) < 2:
            text = payload.get("text") or caption or "Unsupported poll payload."
            return target_bot.send_message(chat_id, text)

        kwargs = _clean_kwargs(
            {
                "is_anonymous": payload.get("is_anonymous"),
                "type": payload.get("poll_type"),
                "allows_multiple_answers": payload.get("allows_multiple_answers"),
                "correct_option_id": payload.get("correct_option_id"),
                "explanation": payload.get("explanation"),
                "open_period": payload.get("open_period"),
                "close_date": payload.get("close_date"),
                "is_closed": payload.get("is_closed"),
            }
        )
        return target_bot.send_poll(chat_id, payload["question"], options, **kwargs)

    if content_type == "dice":
        kwargs = _clean_kwargs({"emoji": payload.get("emoji")})
        return target_bot.send_dice(chat_id, **kwargs)

    # Safe fallback: try plain text if available.
    text = payload.get("text") or caption or ""
    return target_bot.send_message(chat_id, text)



def broadcast_message_to_groups(payload: dict, repeats: int = 1, delay_seconds: int = 0, target_group_id: int | None = None):
    if target_group_id is None:
        groups = list_groups(limit=10_000)
    else:
        target = get_group_by_chat_id(int(target_group_id))
        groups = [target] if target else []
    managed_bots = get_managed_bot_instances()

    repeats = max(1, int(repeats or 1))
    delay_seconds = max(0, int(delay_seconds or 0))

    if not groups or not managed_bots:
        total = len(groups) * len(managed_bots) * repeats
        return total, 0, total

    total = len(groups) * len(managed_bots) * repeats
    sent = 0
    failed = 0

    def _broadcast_one(target_bot, bot_row, chat_id: int):
        nonlocal sent, failed
        last_exc = None
        for attempt in range(4):
            try:
                send_payload_with_bot(target_bot, chat_id, payload)
                return True, None
            except ApiTelegramException as exc:
                last_exc = exc
                error_code = getattr(exc, "error_code", None)
                description = str(exc).lower()
                # Fail fast for permanent errors.
                if error_code in (400, 401, 403, 404) or "forbidden" in description or "bad request" in description:
                    log.warning(
                        "Permanent broadcast failure to %s using bot %s: %s",
                        chat_id,
                        bot_row["id"] if bot_row else "engine",
                        exc,
                    )
                    return False, last_exc
                log.warning(
                    "Temporary broadcast failure to %s using bot %s: %s",
                    chat_id,
                    bot_row["id"] if bot_row else "engine",
                    exc,
                )
                time.sleep(BROADCAST_RETRY_SLEEP * (attempt + 1))
            except Exception as exc:
                last_exc = exc
                log.warning(
                    "Broadcast failed to %s using %s: %s",
                    chat_id,
                    bot_row["id"] if bot_row else "engine",
                    exc,
                )
                time.sleep(BROADCAST_RETRY_SLEEP * (attempt + 1))
        return False, last_exc

    def _run_bot_worker(bot_row, target_bot):
        local_sent = 0
        local_failed = 0
        for round_index in range(repeats):
            for group in groups:
                chat_id = int(group["chat_id"])
                ok, _err = _broadcast_one(target_bot, bot_row, chat_id)
                if ok:
                    local_sent += 1
                else:
                    local_failed += 1
            if round_index < repeats - 1 and delay_seconds:
                time.sleep(delay_seconds)
        return local_sent, local_failed

    # Parallelize by bot (not by every bot/group pair). This is usually faster,
    # lowers pressure on Telegram rate limits, and keeps all follower bots working
    # at the same time.
    max_workers = min(BROADCAST_MAX_WORKERS, max(1, len(managed_bots)))

    from concurrent.futures import ThreadPoolExecutor, as_completed

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = [executor.submit(_run_bot_worker, bot_row, target_bot) for bot_row, target_bot in managed_bots]
        for future in as_completed(futures):
            local_sent, local_failed = future.result()
            sent += local_sent
            failed += local_failed

    return total, sent, failed


def render_group_hint():

    return (
        "Send a forwarded message from the target group, or send its @username, a t.me link, or numeric chat ID.\n"
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


@bot.message_handler(commands=["removegroup"])
def removegroup_cmd(message):
    ensure_user(message.from_user.id)
    if not require_admin(message):
        return
    reset_pending(message.from_user.id)
    show_remove_group_prompt(message.chat.id, message.from_user.id, page=0)


@bot.message_handler(commands=["groups"])
def groups_cmd(message):
    ensure_user(message.from_user.id)
    if not require_admin(message):
        return
    lang = current_lang(message.from_user.id)
    groups = list_groups(offset=0, limit=100)
    if not groups:
        bot.reply_to(message, t(lang, "broadcast_no_groups"), reply_markup=main_menu_keyboard(message.from_user.id, lang))
        return

    lines = [f"{t(lang, 'stats_title')}"]
    for row in groups:
        title = (row["title"] or "Untitled group").strip()
        chat_id = row["chat_id"]
        username = row["username"] or ""
        suffix = f" (@{username})" if username and not username.startswith("@") else (f" ({username})" if username else "")
        lines.append(f"• {title}{suffix} — {chat_id}")
    bot.reply_to(message, "\n".join(lines), reply_markup=main_menu_keyboard(message.from_user.id, lang))


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
    if count_bots() <= 0:
        bot.reply_to(message, "No follower bots are active yet. Add at least one bot first.")
        return
    reset_pending(message.from_user.id)
    set_state(message.from_user.id, "await_broadcast_group")
    show_broadcast_group_prompt(message.chat.id, message.from_user.id)


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
                    "I could not resolve that group. Send a forwarded message from the group, its @username, a t.me link, or its numeric chat ID. I will also try the saved follower bots if the engine cannot resolve it.",
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
        render_group_hint(),
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


@bot.message_handler(content_types=["left_chat_member"])
def on_left_chat_member(message):
    ensure_user(message.from_user.id)
    if BOT_ID is None:
        return
    try:
        left = getattr(message, "left_chat_member", None)
        if left is not None and getattr(left, "id", None) == BOT_ID:
            deactivate_group(int(message.chat.id))
    except Exception as exc:
        log.warning("Failed to deactivate left group: %s", exc)


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
        if count_bots() <= 0:
            send_or_edit(
                call.message.chat.id,
                "No follower bots are active yet. Add at least one bot first.",
                main_menu_keyboard(call.from_user.id, lang),
                call.message.message_id,
            )
            return
        reset_pending(call.from_user.id)
        set_state(call.from_user.id, "await_broadcast_group")
        show_broadcast_group_prompt(call.message.chat.id, call.from_user.id, message_id=call.message.message_id)
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


@bot.callback_query_handler(func=lambda call: call.data.startswith("grm_page:"))
def paginate_remove_groups(call):
    ensure_user(call.from_user.id)
    if not require_admin(call.message):
        return
    try:
        page = max(0, int(call.data.split(":", 1)[1]))
    except Exception:
        page = 0
    bot.answer_callback_query(call.id)
    show_remove_group_prompt(call.message.chat.id, call.from_user.id, page=page, message_id=call.message.message_id)


@bot.callback_query_handler(func=lambda call: call.data.startswith("grm:"))
def remove_group(call):
    ensure_user(call.from_user.id)
    if not require_admin(call.message):
        return
    parts = call.data.split(":")
    if len(parts) != 3:
        bot.answer_callback_query(call.id)
        return

    try:
        page = max(0, int(parts[1]))
        chat_id = int(parts[2])
    except Exception:
        bot.answer_callback_query(call.id)
        return

    ok = delete_group_record(chat_id)
    bot.answer_callback_query(call.id, "Group removed successfully." if ok else "Could not remove that group.", show_alert=not ok)
    show_remove_group_prompt(call.message.chat.id, call.from_user.id, page=page, message_id=call.message.message_id)


@bot.callback_query_handler(func=lambda call: call.data.startswith("bg_page:"))
def paginate_broadcast_groups(call):
    ensure_user(call.from_user.id)
    if not require_admin(call.message):
        return
    try:
        page = max(0, int(call.data.split(":", 1)[1]))
    except Exception:
        page = 0
    bot.answer_callback_query(call.id)
    show_broadcast_group_prompt(call.message.chat.id, call.from_user.id, page=page, message_id=call.message.message_id)


@bot.callback_query_handler(func=lambda call: call.data.startswith("bg:"))
def choose_broadcast_group(call):
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
        chat_id = int(parts[2])
    except Exception:
        bot.answer_callback_query(call.id)
        return

    group = get_group_by_chat_id(chat_id)
    if not group:
        bot.answer_callback_query(call.id, "That group is no longer saved.", show_alert=True)
        show_broadcast_group_prompt(call.message.chat.id, call.from_user.id, page=page, message_id=call.message.message_id)
        return

    title = (group["title"] or group["username"] or f"Group {chat_id}").strip()
    set_pending(call.from_user.id, pending_group_id=chat_id, state="await_broadcast_repeats")
    bot.answer_callback_query(call.id, f"Selected: {title}")
    send_or_edit(
        call.message.chat.id,
        f"✅ Group selected: {title}\n\n🔁 How many times should this message be sent?\nSend a number like 1, 2, 5...",
        cancel_keyboard(lang),
        call.message.message_id,
    )


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

    # Let dedicated command handlers process slash commands. This avoids
    # swallowing /help, /start, /ping, /cancel, etc. while a flow is active.
    if getattr(message, "text", None) and message.text.lstrip().startswith("/"):
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
        if token == BOT_TOKEN:
            bot.reply_to(message, "Please add a follower bot token, not the engine bot token.")
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
                    "I could not resolve that group. Send a forwarded message from the group, its @username, a t.me link, or its numeric chat ID. I will also try the saved follower bots if the engine cannot resolve it.",
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

    if state == "await_broadcast_group":
        chat, error = resolve_group_from_private_message(message)
        if chat is None:
            if error == "ambiguous":
                bot.reply_to(
                    message,
                    "I found more than one matching group name. Please send the exact @username or numeric chat ID, or choose a group from the buttons below.",
                    reply_markup=cancel_keyboard(lang),
                )
            else:
                bot.reply_to(
                    message,
                    "I could not resolve that group. Send a forwarded message from the group, its @username, a t.me link, or its numeric chat ID.",
                    reply_markup=cancel_keyboard(lang),
                )
            return

        register_group_from_chat(chat)
        set_pending(message.from_user.id, pending_group_id=int(chat.id), state="await_broadcast_repeats")
        bot.reply_to(
            message,
            f"✅ Group selected: {getattr(chat, 'title', None) or getattr(chat, 'username', None) or chat.id}\n\n🔁 How many times should this message be sent?\nSend a number like 1, 2, 5...",
            reply_markup=cancel_keyboard(lang),
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
            "📝 Send the message you want to broadcast now.\nIt will be sent by all saved bots to all registered groups.\n\nSupported: text, photo, video, GIF, document, sticker, voice, audio, video note, contact, location, venue, poll, and dice.",
            reply_markup=cancel_keyboard(lang),
        )
        return

    if state == "await_broadcast_content":
        repeats = max(1, int(row["pending_repeats"] or 1))
        delay = max(0, int(row["pending_delay"] or 0))
        payload = extract_broadcast_payload(message)
        try:
            total, sent, failed = broadcast_message_to_groups(
                payload,
                repeats=repeats,
                delay_seconds=delay,
                target_group_id=int(row["pending_group_id"] or 0) or None,
            )
            result_text = f"✅ Broadcast finished.\nAttempts: {total}\nSent: {sent}\nFailed: {failed}"
        except Exception as exc:
            log.exception("Broadcast crashed for user %s", message.from_user.id)
            total = sent = 0
            failed = 0
            result_text = f"⚠️ Broadcast failed unexpectedly.\n{exc}"
        finally:
            reset_pending(message.from_user.id)
        bot.reply_to(
            message,
            result_text,
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
