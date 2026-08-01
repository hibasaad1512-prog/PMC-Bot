import logging
import os
import re
import time
import tempfile
import threading
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

SUPPORTED_BROADCAST_CONTENT_TYPES = [
    "text",
    "photo",
    "video",
    "document",
    "audio",
    "voice",
    "sticker",
    "animation",
    "video_note",
    "contact",
    "location",
    "venue",
    "dice",
    "poll",
]


BROADCAST_MEDIA_TYPES = {
    "photo",
    "video",
    "document",
    "audio",
    "voice",
    "animation",
    "sticker",
    "video_note",
}

BROADCAST_MEDIA_SUFFIXES = {
    "photo": ".jpg",
    "video": ".mp4",
    "document": ".bin",
    "audio": ".mp3",
    "voice": ".ogg",
    "animation": ".mp4",
    "sticker": ".webp",
    "video_note": ".mp4",
}

ACTIVE_LOOP_JOBS: dict[int, dict] = {}
ACTIVE_LOOP_JOBS_LOCK = threading.RLock()
LOOP_MAX_INTERVAL_SECONDS = 300  # 5 minutes


def is_command_message(message) -> bool:
    text = getattr(message, "text", None)
    return bool(text and text.lstrip().startswith("/"))


def _get_attr_or_key(obj, name, default=None):
    if obj is None:
        return default
    if isinstance(obj, dict):
        return obj.get(name, default)
    return getattr(obj, name, default)


def extract_retry_after(exc) -> int:
    try:
        data = getattr(exc, "result_json", None)
        if isinstance(data, dict):
            params = data.get("parameters") or {}
            retry_after = params.get("retry_after")
            if isinstance(retry_after, int) and retry_after > 0:
                return retry_after
    except Exception:
        pass

    desc = str(exc or "")
    m = re.search(r"retry after\s+(\d+)", desc, re.IGNORECASE)
    if m:
        try:
            return int(m.group(1))
        except Exception:
            return 0
    return 0


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


def loop_status_keyboard(lang: str | None = None):
    kb = InlineKeyboardMarkup()
    kb.row(
        InlineKeyboardButton("🛑 Stop", callback_data="loopstop"),
        InlineKeyboardButton(t(lang or "en", "cancel"), callback_data="cancel"),
    )
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




def get_broadcast_worker_bots():
    """
    Returns the stored follower bots that should actually deliver broadcasts.
    The engine bot is only used as a last fallback when no follower bots exist.
    """
    workers = []
    seen_tokens = set()

    for row in list_all_bots():
        token = (row["token"] or "").strip()
        if not token or token == BOT_TOKEN or token in seen_tokens:
            continue

        cached = _BOT_INSTANCE_CACHE.get(token)
        if cached is None:
            cached = telebot.TeleBot(token, parse_mode="HTML", threaded=False)
            _BOT_INSTANCE_CACHE[token] = cached

        workers.append((row, cached))
        seen_tokens.add(token)

    if not workers and BOT_TOKEN:
        workers.append((None, bot))

    return workers


def _prepare_broadcast_media(payload: dict):
    """
    Download media once from the engine bot so every follower bot can reuse it.
    This avoids relying on cross-bot file_id compatibility and is much more
    reliable for photos, GIFs, voice notes, stickers, and documents.
    """
    content_type = payload.get("type") or "text"
    file_id = payload.get("file_id")
    if content_type not in BROADCAST_MEDIA_TYPES or not file_id:
        return payload

    if payload.get("local_path"):
        return payload

    try:
        file_info = bot.get_file(file_id)
        file_path = getattr(file_info, "file_path", None) or ""
        suffix = BROADCAST_MEDIA_SUFFIXES.get(content_type) or os.path.splitext(file_path)[1] or ".bin"
        downloaded = bot.download_file(file_info.file_path)

        tmp = tempfile.NamedTemporaryFile(delete=False, suffix=suffix)
        try:
            tmp.write(downloaded)
            tmp.flush()
        finally:
            tmp.close()

        payload["local_path"] = tmp.name
    except Exception as exc:
        log.warning("Could not cache broadcast media (%s): %s", content_type, exc)

    return payload


def _cleanup_broadcast_media(payload: dict):
    local_path = payload.get("local_path")
    if not local_path:
        return
    try:
        if os.path.exists(local_path):
            os.unlink(local_path)
    except Exception as exc:
        log.debug("Could not remove temp media file %s: %s", local_path, exc)
    finally:
        payload.pop("local_path", None)


def _bot_name(bot_row):
    if not bot_row:
        return "engine"
    label = (bot_row["label"] or "").strip()
    return f"{bot_row['id']}:{label}" if label else f"{bot_row['id']}"
def _copy_or_forward_message(target_bot, chat_id: int, source_chat_id: int, source_message_id: int):
    copier = getattr(target_bot, "copy_message", None)
    if callable(copier):
        copier(chat_id=chat_id, from_chat_id=source_chat_id, message_id=source_message_id)
        return
    target_bot.forward_message(chat_id=chat_id, from_chat_id=source_chat_id, message_id=source_message_id)


def extract_broadcast_payload(message):
    """
    Capture the incoming message in a bot-agnostic structure so follower bots
    can send it themselves, without needing access to the engine's private chat.
    When possible, we also keep the original source chat/message ids so the
    engine bot can copy the message directly. That is much faster and supports
    almost every Telegram message type.
    """
    content_type = getattr(message, "content_type", None) or (
        "text" if getattr(message, "text", None) else "unknown"
    )

    payload = {
        "type": content_type,
        "text": None,
        "file_id": None,
        "caption": None,
        "contact": None,
        "location": None,
        "venue": None,
        "dice": None,
        "poll": None,
        "source_chat_id": getattr(getattr(message, "chat", None), "id", None),
        "source_message_id": getattr(message, "message_id", None),
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

    if content_type in {"video", "document", "audio", "voice", "animation", "sticker", "video_note", "contact", "location", "venue", "dice", "poll"}:
        obj = getattr(message, content_type, None)
        if obj is not None and content_type not in {"contact", "location", "venue", "dice", "poll"}:
            payload["file_id"] = getattr(obj, "file_id", None)
        else:
            payload[content_type] = obj
        if content_type == "contact":
            payload["contact"] = obj
        elif content_type == "location":
            payload["location"] = obj
        elif content_type == "venue":
            payload["venue"] = obj
        elif content_type == "dice":
            payload["dice"] = obj
        elif content_type == "poll":
            payload["poll"] = obj
        if content_type != "sticker":
            payload["caption"] = getattr(message, "caption", None)
        return payload

    # Fallback for any unsupported type; the source ids still let copy_message
    # do the heavy lifting on the engine bot.
    payload["text"] = getattr(message, "text", None)
    payload["caption"] = getattr(message, "caption", None)
    return payload


def send_payload_with_bot(target_bot, chat_id: int, payload: dict):
    content_type = payload.get("type") or "text"
    caption = payload.get("caption")
    file_id = payload.get("file_id")
    local_path = payload.get("local_path")

    # Best-effort copy for the engine bot only. Follower bots should send the
    # payload directly so broadcasts do not depend on the engine's private chat.
    if target_bot is bot:
        source_chat_id = payload.get("source_chat_id")
        source_message_id = payload.get("source_message_id")
        copier = getattr(target_bot, "copy_message", None)
        if callable(copier) and source_chat_id is not None and source_message_id is not None:
            try:
                return copier(
                    chat_id=chat_id,
                    from_chat_id=source_chat_id,
                    message_id=source_message_id,
                )
            except Exception:
                pass

    if content_type == "text":
        text = payload.get("text") or ""
        return target_bot.send_message(chat_id, text)

    def _send_local_file(send_func, *args, **kwargs):
        if local_path and os.path.exists(local_path):
            with open(local_path, "rb") as fh:
                return send_func(chat_id, fh, *args, **kwargs)
        return None

    if content_type == "photo":
        sent = _send_local_file(target_bot.send_photo, caption=caption or None)
        if sent is not None:
            return sent
        if file_id:
            return target_bot.send_photo(chat_id, file_id, caption=caption or None)
        return target_bot.send_message(chat_id, caption or "")
    if content_type == "video":
        sent = _send_local_file(target_bot.send_video, caption=caption or None)
        if sent is not None:
            return sent
        if file_id:
            return target_bot.send_video(chat_id, file_id, caption=caption or None)
        return target_bot.send_message(chat_id, caption or "")
    if content_type == "document":
        sent = _send_local_file(target_bot.send_document, caption=caption or None)
        if sent is not None:
            return sent
        if file_id:
            return target_bot.send_document(chat_id, file_id, caption=caption or None)
        return target_bot.send_message(chat_id, caption or "")
    if content_type == "audio":
        sent = _send_local_file(target_bot.send_audio, caption=caption or None)
        if sent is not None:
            return sent
        if file_id:
            return target_bot.send_audio(chat_id, file_id, caption=caption or None)
        return target_bot.send_message(chat_id, caption or "")
    if content_type == "voice":
        sent = _send_local_file(target_bot.send_voice, caption=caption or None)
        if sent is not None:
            return sent
        if file_id:
            return target_bot.send_voice(chat_id, file_id, caption=caption or None)
        return target_bot.send_message(chat_id, caption or "")
    if content_type == "animation":
        sent = _send_local_file(target_bot.send_animation, caption=caption or None)
        if sent is not None:
            return sent
        if file_id:
            return target_bot.send_animation(chat_id, file_id, caption=caption or None)
        return target_bot.send_message(chat_id, caption or "")
    if content_type == "video_note":
        sent = _send_local_file(target_bot.send_video_note)
        if sent is not None:
            return sent
        if file_id:
            return target_bot.send_video_note(chat_id, file_id)
        return target_bot.send_message(chat_id, "🎥 Video note")
    if content_type == "sticker":
        sent = _send_local_file(target_bot.send_sticker)
        if sent is not None:
            return sent
        if file_id:
            return target_bot.send_sticker(chat_id, file_id)
        return target_bot.send_message(chat_id, "🙂 Sticker")
    if content_type == "contact":
        contact = payload.get("contact")
        if contact is not None:
            return target_bot.send_contact(
                chat_id,
                phone_number=getattr(contact, "phone_number", ""),
                first_name=getattr(contact, "first_name", "Contact"),
                last_name=getattr(contact, "last_name", None),
                vcard=getattr(contact, "vcard", None),
            )
    if content_type == "location":
        location = payload.get("location")
        if location is not None:
            return target_bot.send_location(
                chat_id,
                latitude=getattr(location, "latitude", 0.0),
                longitude=getattr(location, "longitude", 0.0),
            )
    if content_type == "venue":
        venue = payload.get("venue")
        if venue is not None:
            venue_location = getattr(venue, "location", None)
            return target_bot.send_venue(
                chat_id,
                latitude=venue_location.latitude if venue_location else 0.0,
                longitude=venue_location.longitude if venue_location else 0.0,
                title=getattr(venue, "title", "Venue"),
                address=getattr(venue, "address", ""),
                foursquare_id=getattr(venue, "foursquare_id", None),
                foursquare_type=getattr(venue, "foursquare_type", None),
            )
    if content_type == "dice":
        dice = payload.get("dice")
        if dice is not None:
            return target_bot.send_dice(chat_id, emoji=getattr(dice, "emoji", None) or "🎲")
    if content_type == "poll":
        poll = payload.get("poll")
        question = getattr(poll, "question", None) if poll is not None else None
        options = []
        if poll is not None:
            for opt in getattr(poll, "options", []) or []:
                options.append(getattr(opt, "text", str(opt)))
        if question and options:
            try:
                return target_bot.send_poll(
                    chat_id,
                    question,
                    options,
                    is_anonymous=getattr(poll, "is_anonymous", True),
                    type=getattr(poll, "type", "regular"),
                    allows_multiple_answers=getattr(poll, "allows_multiple_answers", False),
                )
            except Exception:
                pass
        return target_bot.send_message(chat_id, question or payload.get("text") or caption or "Poll")

    # Safe fallback: try plain text if available.
    text = payload.get("text") or caption or ""
    return target_bot.send_message(chat_id, text)


def broadcast_message_to_groups(payload: dict, repeats: int = 1, delay_seconds: int = 0, target_group_id: int | None = None):
    if target_group_id is None:
        groups = list_groups(limit=10_000)
    else:
        target = get_group_by_chat_id(int(target_group_id))
        groups = [target] if target else []

    repeats = max(1, int(repeats or 1))
    delay_seconds = max(0, int(delay_seconds or 0))

    if not groups:
        return 0, 0, 0

    workers = get_broadcast_worker_bots()
    if not workers:
        return 0, 0, 0

    payload = _prepare_broadcast_media(dict(payload))

    total = len(groups) * repeats
    sent = 0
    failed = 0

    def _broadcast_one(group_index: int, chat_id: int):
        last_exc = None
        if not workers:
            return False, None

        rotation = group_index % len(workers)
        ordered_workers = workers[rotation:] + workers[:rotation]

        for bot_row, target_bot in ordered_workers:
            for attempt in range(2):
                try:
                    send_payload_with_bot(target_bot, chat_id, payload)
                    return True, None
                except ApiTelegramException as exc:
                    last_exc = exc
                    err_text = str(exc).lower()
                    # If this looks temporary, retry once quickly; otherwise move
                    # on to the next worker bot.
                    if "retry after" in err_text or "too many requests" in err_text:
                        time.sleep(0.12 * (attempt + 1))
                        continue
                    break
                except Exception as exc:
                    last_exc = exc
                    time.sleep(0.05 * (attempt + 1))
                    break

        return False, last_exc

    from concurrent.futures import ThreadPoolExecutor, as_completed

    max_workers = min(32, max(8, len(groups)))

    try:
        for round_index in range(repeats):
            futures = []
            with ThreadPoolExecutor(max_workers=max_workers) as executor:
                for group_index, group in enumerate(groups):
                    chat_id = int(group["chat_id"])
                    futures.append(executor.submit(_broadcast_one, group_index + round_index, chat_id))

                for future in as_completed(futures):
                    ok, _err = future.result()
                    if ok:
                        sent += 1
                    else:
                        failed += 1

            if round_index < repeats - 1 and delay_seconds:
                time.sleep(delay_seconds)
    finally:
        _cleanup_broadcast_media(payload)

    return total, sent, failed


def _send_payload_with_retry(target_bot, chat_id: int, payload: dict, max_attempts: int = 3):
    last_exc = None
    for attempt in range(max_attempts):
        try:
            return True, send_payload_with_bot(target_bot, chat_id, payload)
        except ApiTelegramException as exc:
            last_exc = exc
            retry_after = extract_retry_after(exc)
            desc = str(exc or "").lower()
            if retry_after > 0:
                time.sleep(min(retry_after, 60) + 1)
                continue
            if "too many requests" in desc or "retry after" in desc:
                time.sleep(1.5 * (attempt + 1))
                continue
            return False, exc
        except Exception as exc:
            last_exc = exc
            if attempt < max_attempts - 1:
                time.sleep(0.35 * (attempt + 1))
                continue
            return False, exc
    return False, last_exc


def _loop_job_status_text(lang: str, group_title: str, sent: int, failed: int, batch_size: int, interval_seconds: int, round_no: int):
    return (
        f"♾️ {t(lang, 'loop_started')}\n\n"
        f"📂 {t(lang, 'summary_group')}: {group_title}\n"
        f"📦 {t(lang, 'loop_batch')}: {batch_size}\n"
        f"⏱️ {t(lang, 'loop_interval')}: {interval_seconds}s\n"
        f"🔁 {t(lang, 'loop_round')}: {round_no}\n"
        f"✅ Sent: {sent}\n"
        f"❌ Failed: {failed}\n\n"
        f"{t(lang, 'loop_stop_hint')}"
    )


def _run_loop_broadcast_job(owner_user_id: int):
    with ACTIVE_LOOP_JOBS_LOCK:
        job = ACTIVE_LOOP_JOBS.get(owner_user_id)
    if not job:
        return

    payload = _prepare_broadcast_media(dict(job["payload"]))
    batch_size = max(1, int(job.get("batch_size") or 1))
    interval_seconds = max(0, int(job.get("interval_seconds") or 0))
    target_group_id = int(job.get("target_group_id") or 0)
    status_chat_id = int(job.get("status_chat_id") or 0)
    status_message_id = int(job.get("status_message_id") or 0)
    lang = job.get("lang") or "en"
    group_title = job.get("group_title") or str(target_group_id)

    sent = 0
    failed = 0
    round_no = 0

    try:
        workers = get_broadcast_worker_bots()
        if not workers:
            try:
                bot.edit_message_text(
                    chat_id=status_chat_id,
                    message_id=status_message_id,
                    text=f"⚠️ {t(lang, 'broadcast_failed')}\nNo worker bots available.",
                )
            except Exception:
                pass
            return

        from concurrent.futures import ThreadPoolExecutor, as_completed

        while True:
            with ACTIVE_LOOP_JOBS_LOCK:
                current = ACTIVE_LOOP_JOBS.get(owner_user_id)
                if not current or current.get("stop_event") is None or current["stop_event"].is_set():
                    break

            round_no += 1
            futures = []
            workers_count = len(workers)
            max_workers = max(1, min(batch_size, workers_count, 32))

            with ThreadPoolExecutor(max_workers=max_workers) as executor:
                for index in range(batch_size):
                    worker_row, worker_bot = workers[(round_no - 1 + index) % workers_count]
                    futures.append(executor.submit(_send_payload_with_retry, worker_bot, target_group_id, payload))

                for future in as_completed(futures):
                    ok, _result = future.result()
                    if ok:
                        sent += 1
                    else:
                        failed += 1

            try:
                bot.edit_message_text(
                    chat_id=status_chat_id,
                    message_id=status_message_id,
                    text=_loop_job_status_text(lang, group_title, sent, failed, batch_size, interval_seconds, round_no),
                    reply_markup=loop_status_keyboard(lang),
                )
            except Exception:
                pass

            with ACTIVE_LOOP_JOBS_LOCK:
                current = ACTIVE_LOOP_JOBS.get(owner_user_id)
                if not current or current.get("stop_event") is None or current["stop_event"].is_set():
                    break

            if interval_seconds > 0 and job.get("stop_event") is not None:
                if job["stop_event"].wait(interval_seconds):
                    break
            elif interval_seconds == 0:
                time.sleep(0.05)

    finally:
        _cleanup_broadcast_media(payload)
        with ACTIVE_LOOP_JOBS_LOCK:
            ACTIVE_LOOP_JOBS.pop(owner_user_id, None)
        try:
            bot.edit_message_text(
                chat_id=status_chat_id,
                message_id=status_message_id,
                text=f"✅ {t(lang, 'loop_stopped')}\n\nSent: {sent}\nFailed: {failed}",
                reply_markup=main_menu_keyboard(owner_user_id, lang),
            )
        except Exception:
            pass


def start_loop_broadcast_job(owner_user_id: int, lang: str, target_group_id: int, payload: dict, batch_size: int, interval_seconds: int, status_chat_id: int, status_message_id: int, group_title: str):
    stop_event = threading.Event()
    job = {
        "owner_user_id": owner_user_id,
        "lang": lang,
        "target_group_id": target_group_id,
        "payload": payload,
        "batch_size": batch_size,
        "interval_seconds": interval_seconds,
        "status_chat_id": status_chat_id,
        "status_message_id": status_message_id,
        "group_title": group_title,
        "stop_event": stop_event,
    }
    with ACTIVE_LOOP_JOBS_LOCK:
        existing = ACTIVE_LOOP_JOBS.get(owner_user_id)
        if existing and existing.get("stop_event") is not None:
            try:
                existing["stop_event"].set()
            except Exception:
                pass
        ACTIVE_LOOP_JOBS[owner_user_id] = job

    thread = threading.Thread(target=_run_loop_broadcast_job, args=(owner_user_id,), daemon=True)
    thread.start()
    return job


def stop_loop_broadcast_job(owner_user_id: int) -> bool:
    with ACTIVE_LOOP_JOBS_LOCK:
        job = ACTIVE_LOOP_JOBS.get(owner_user_id)
        if not job or job.get("stop_event") is None:
            return False
        job["stop_event"].set()
        return True


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
    set_state(message.from_user.id, "await_broadcast_group")
    show_broadcast_group_prompt(message.chat.id, message.from_user.id)


@bot.message_handler(commands=["loopbroadcast"])
def loopbroadcast_cmd(message):
    ensure_user(message.from_user.id)
    reset_pending(message.from_user.id)
    if not require_admin(message):
        return
    lang = current_lang(message.from_user.id)
    if message.chat.type != "private":
        bot.reply_to(message, t(lang, "access_denied"))
        return
    if count_groups() <= 0:
        bot.reply_to(message, t(lang, "broadcast_no_groups"))
        return
    set_state(message.from_user.id, "await_loop_group")
    show_broadcast_group_prompt(message.chat.id, message.from_user.id)


@bot.message_handler(commands=["stopbroadcast"])
def stopbroadcast_cmd(message):
    ensure_user(message.from_user.id)
    lang = current_lang(message.from_user.id)
    reset_pending(message.from_user.id)
    if stop_loop_broadcast_job(message.from_user.id):
        bot.reply_to(message, t(lang, "loop_stopped"), reply_markup=main_menu_keyboard(message.from_user.id, lang))
    else:
        bot.reply_to(message, "No active loop broadcast is running.", reply_markup=main_menu_keyboard(message.from_user.id, lang))


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
    row = get_user(call.from_user.id)
    state = row["state"] if row else None
    if state == "await_loop_group":
        set_pending(call.from_user.id, pending_group_id=chat_id, state="await_loop_batch")
        bot.answer_callback_query(call.id, f"Selected: {title}")
        send_or_edit(
            call.message.chat.id,
            f"✅ Group selected: {title}\n\n📦 How many messages should be sent in each batch?\nSend 10 for a safe default.",
            cancel_keyboard(lang),
            call.message.message_id,
        )
        return

    set_pending(call.from_user.id, pending_group_id=chat_id, state="await_broadcast_repeats")
    bot.answer_callback_query(call.id, f"Selected: {title}")
    send_or_edit(
        call.message.chat.id,
        f"✅ Group selected: {title}\n\n🔁 How many times should this message be sent?\nSend a number like 1, 2, 5...",
        cancel_keyboard(lang),
        call.message.message_id,
    )


@bot.callback_query_handler(func=lambda call: call.data == "loopstop")
def loopstop_callback(call):
    ensure_user(call.from_user.id)
    lang = current_lang(call.from_user.id)
    if stop_loop_broadcast_job(call.from_user.id):
        bot.answer_callback_query(call.id, t(lang, "loop_stopped"), show_alert=True)
    else:
        bot.answer_callback_query(call.id, "No active loop broadcast is running.", show_alert=True)


@bot.callback_query_handler(func=lambda call: call.data == "cancel")
@bot.callback_query_handler(func=lambda call: call.data == "cancel")
def cancel(call):
    ensure_user(call.from_user.id)
    reset_pending(call.from_user.id)
    lang = current_lang(call.from_user.id)
    bot.answer_callback_query(call.id)
    send_or_edit(call.message.chat.id, t(lang, "cancelled"), main_menu_keyboard(call.from_user.id, lang), call.message.message_id)


@bot.message_handler(content_types=SUPPORTED_BROADCAST_CONTENT_TYPES, func=lambda message: not is_command_message(message))
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
            "📝 Send the message you want to broadcast now.\nIt will be copied exactly and sent faster.",
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
            result_text = f"✅ Broadcast finished.\nTargets: {total}\nSent: {sent}\nFailed: {failed}"
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

    if state == "await_loop_batch":
        raw = (message.text or "").strip()
        if not raw.isdigit() or int(raw) <= 0:
            bot.reply_to(message, t(lang, "invalid_number"))
            return
        batch_size = min(100, int(raw))
        set_pending(
            message.from_user.id,
            pending_repeats=batch_size,
            state="await_loop_delay",
        )
        bot.reply_to(
            message,
            f"⏱️ {t(lang, 'loop_enter_interval')}\n\nSend a number from 0 to {LOOP_MAX_INTERVAL_SECONDS} seconds (5 minutes).",
            reply_markup=cancel_keyboard(lang),
        )
        return

    if state == "await_loop_delay":
        raw = (message.text or "").strip()
        if not raw.isdigit():
            bot.reply_to(message, t(lang, "invalid_number"))
            return
        interval = int(raw)
        if interval < 0 or interval > LOOP_MAX_INTERVAL_SECONDS:
            bot.reply_to(
                message,
                f"⚠️ Please send a number between 0 and {LOOP_MAX_INTERVAL_SECONDS}.",
                reply_markup=cancel_keyboard(lang),
            )
            return
        set_pending(
            message.from_user.id,
            pending_delay=interval,
            state="await_loop_content",
        )
        bot.reply_to(
            message,
            f"📝 {t(lang, 'loop_enter_message')}",
            reply_markup=cancel_keyboard(lang),
        )
        return

    if state == "await_loop_content":
        batch_size = max(1, min(100, int(row["pending_repeats"] or 10)))
        interval = max(0, int(row["pending_delay"] or 0))
        group_id = int(row["pending_group_id"] or 0)
        group = get_group_by_chat_id(group_id)
        group_title = (group["title"] or group["username"] or f"Group {group_id}") if group else f"Group {group_id}"
        payload = extract_broadcast_payload(message)
        try:
            reset_pending(message.from_user.id)
            status_msg = bot.reply_to(
                message,
                _loop_job_status_text(lang, group_title, 0, 0, batch_size, interval, 1),
                reply_markup=loop_status_keyboard(lang),
            )
            start_loop_broadcast_job(
                owner_user_id=message.from_user.id,
                lang=lang,
                target_group_id=group_id,
                payload=payload,
                batch_size=batch_size,
                interval_seconds=interval,
                status_chat_id=status_msg.chat.id,
                status_message_id=status_msg.message_id,
                group_title=group_title,
            )
        except Exception as exc:
            log.exception("Loop broadcast crashed for user %s", message.from_user.id)
            reset_pending(message.from_user.id)
            bot.reply_to(message, f"⚠️ Loop broadcast failed unexpectedly.\n{exc}", reply_markup=main_menu_keyboard(message.from_user.id, lang))
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
