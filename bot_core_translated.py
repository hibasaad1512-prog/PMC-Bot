import logging
import re
import threading
import time
from collections import Counter
from datetime import datetime, timedelta, timezone
from html import escape

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from config import (
    TOKEN,
    BOT_NAME,
    BOT_USERNAME,
    BOT_VERSION,
    CREATOR_NAME,
    SUPPORT_USERNAME,
    STAR_SUPPORT_AMOUNT,
    CREATOR_GIFT_URL,
)
from database import (
    ensure_chat,
    add_user,
    record_message,
    record_reaction,
    set_reaction_count,
    claim_daily_dispatch,
    record_membership_event,
    get_chat_language,
    set_chat_language,
    set_daily_report_enabled,
    mark_daily_prompt_sent,
    chats_due_for_daily_prompt,
    get_daily_stats,
    get_previous_message_count,
    top_members,
    peak_hour,
    most_replied_message,
    most_reacted_message,
    topic_candidates,
    get_new_users_count,
    get_joined_count,
    get_left_count,
    get_net_growth,
    support_payment_log,
    set_meta,
    get_meta,
    utc_today,
    utc_yesterday,
    get_warning_count,
    add_warning,
    clear_warning_count,
    set_warning_count,
    log_moderation_action,
    get_user,
)

UTC = timezone.utc
LOG = logging.getLogger("pulsebot")

SESSION = requests.Session()
SESSION.trust_env = False
SESSION.headers.update({"User-Agent": "PulseBot/4.5"})
SESSION.mount(
    "https://",
    HTTPAdapter(
        max_retries=Retry(
            total=2,
            backoff_factor=0.4,
            status_forcelist=(429, 500, 502, 503, 504),
            allowed_methods=frozenset(["GET", "POST"]),
        )
    ),
)

STOPWORDS_EN = {
    "the","and","for","with","that","this","from","you","are","but","not","have","has",
    "was","were","your","about","into","what","when","where","why","how","can","could",
    "would","will","shall","should","a","an","to","of","in","on","is","it","be","as",
    "at","by","or","if","we","they","he","she","them","our","us","i"
}
STOPWORDS_AR = {
    "في","من","على","و","او","أو","ما","هذا","هذه","ذلك","تلك","الى","إلى","عن","مع","كل",
    "هل","كيف","ماذا","لماذا","لما","اذا","إن","أن","انا","أنت","هو","هي","هم","هن","نحن"
}
STOPWORDS_RU = {
    "и","в","во","на","не","что","это","как","я","мы","вы","он","она","они","а","но","или",
    "к","из","за","по","для","от","до","у","о","об","про","бы","же","то","так","да","нет"
}

TEXTS = {
    "en": {
        "welcome": "👋 Welcome to <b>{bot}</b>!\n\nAdd me to a Telegram group to start tracking statistics.",
        "help": (
            "🤖 <b>Pulse Bot</b>\n\n"
            "━━━━━━━━━━━━━━\n\n"
            "📊 <b>Statistics</b>\n"
            "• /stats — Live group statistics\n"
            "• /report — Full daily report\n"
            "• /yesterday — Yesterday's report\n"
            "• /growth — Server growth compared to yesterday\n\n"
            "⚙️ <b>Settings</b>\n"
            "• /language — Change bot language\n\n"
            "🛡️ <b>Moderation (admins only)</b>\n"
            "• /warn — Warn a member (reply to message)\n"
            "• /unwarn — Remove one warning (reply to message)\n"
            "• /warnings — Show member warnings (reply to message)\n"
            "• /mute — Mute a member (reply to message)\n"
            "• /unmute — Unmute a member (reply to message)\n"
            "• /ban — Ban a member (reply to message)\n"
            "• /unban — Unban a member (reply to message)\n"
            "ℹ️ <b>Information</b>\n"
            "• /start — Start the bot\n"
            "• /help — Show help menu\n"
            "• /support — Contact support\n"
            "• /creator — Show creator information\n"
            "• /info — Show server & bot info\n\n"
            "━━━━━━━━━━━━━━\n\n"
            "✨ Thanks for using Pulse Bot!"
        ),
        "group_only": "⚠️ This command only works inside Telegram groups.",
        "admin_only": "⚠️ This setting can only be changed by group admins.",
        "reply_required": "⚠️ Reply to a member's message to use this command.",
        "target_is_admin": "⚠️ You cannot moderate a group admin or creator.",
        "bot_lacks_rights": "⚠️ I need admin rights in this group to do that.",
        "bot_lacks_promote_rights": "⚠️ I need promote rights in this group to do that.",
        "admin_done": "👑 {user} has been promoted to admin.",
        "demote_done": "🔻 {user} has been demoted.",
        "warn_done": "⚠️ {user} warned. Warnings: {count}/3.",
        "unwarn_done": "✅ Warning removed for {user}. Warnings: {count}/3.",
        "warnings_show": "📌 {user} has {count} warning(s).",
        "warn_limit_hit": "🚨 {user} reached 3/3 warnings and was muted for 24 hours.",
        "mute_done": "🔇 {user} has been muted.",
        "unmute_done": "🔊 {user} has been unmuted.",
        "ban_done": "⛔ {user} has been banned.",
        "unban_done": "✅ {user} has been unbanned.",
        "stats_title": "📊 <b>Today in {bot}</b>",
        "report_title": "📄 <b>Daily Report</b>",
        "yesterday_title": "📅 <b>Yesterday's Report</b>",
        "growth_title": "📈 <b>Server Growth</b>",
        "no_data": "No statistics yet.",
        "language_menu": "🌍 Choose a language for this chat:",
        "language_set": "✅ Language updated.",
        "support_text": "⭐ <b>Support {bot}</b>\n\nYou can support the project with <b>{stars} Telegram Stars</b>.",
        "creator_text": (
            "👨‍💻 <b>{bot}</b>\n\n"
            "Created and maintained by {creator}.\n\n"
            "Built for Telegram communities that need fast, clear and reliable activity statistics.\n"
            "Every update is crafted to make group management easier, cleaner, and more useful."
        ),
        "daily_prompt": "🌙 <b>Midnight update is ready.</b>\n\nWould you like to view yesterday's report?",
        "yes": "✅ Yes",
        "no": "❌ No",
        "support_button": f"⭐ Support with {STAR_SUPPORT_AMOUNT} Stars",
        "gift_button": "🎁 Creator gift link",
        "open_report": "📄 View Report",
        "open_support": "💬 Open Support",
        "add_group": "➕ Add to Group",
        "today": "Today",
        "yesterday": "Yesterday",
        "messages": "Messages",
        "active_members": "Active members",
        "joined": "Joined",
        "left": "Left",
        "net_growth": "Net growth",
        "change_vs_prev": "Change vs previous day",
        "top_member": "Most active member",
        "top_admin": "Most active admin",
        "peak_hour": "Peak hour",
        "most_replied": "Most replied message",
        "most_reacted": "Most reacted message",
        "top_topics": "Top topics",
        "new_members": "New members",
        "left_members": "Members left",
    },
    "ar": {
        "welcome": "👋 أهلاً بك في <b>{bot}</b>!\n\nأضفني إلى مجموعة تيليجرام حتى أبدأ بتتبع الإحصائيات.",
        "help": (
            "🤖 <b>Pulse Bot</b>\n\n"
            "━━━━━━━━━━━━━━\n\n"
            "📊 <b>الإحصائيات</b>\n"
            "• /stats — إحصائيات اليوم\n"
            "• /report — التقرير الكامل لليوم\n"
            "• /yesterday — تقرير الأمس\n"
            "• /growth — نمو السيرفر مقارنة بالأمس\n\n"
            "⚙️ <b>الإعدادات</b>\n"
            "• /language — تغيير لغة البوت\n\n"
            "🛡️ <b>الإدارة (للمشرفين فقط)</b>\n"
            "• /warn — إنذار عضو (بالرد على رسالته)\n"
            "• /unwarn — إزالة إنذار (بالرد على رسالته)\n"
            "• /warnings — عرض إنذارات العضو (بالرد على رسالته)\n"
            "• /mute — كتم عضو (بالرد على رسالته)\n"
            "• /unmute — فك كتم عضو (بالرد على رسالته)\n"
            "• /ban — حظر عضو (بالرد على رسالته)\n"
            "• /unban — فك حظر عضو (بالرد على رسالته)\n"
            "ℹ️ <b>المعلومات</b>\n"
            "• /start — تشغيل البوت\n"
            "• /help — عرض القائمة\n"
            "• /support — التواصل مع الدعم\n"
            "• /creator — معلومات الصانع\n\n"
            "━━━━━━━━━━━━━━\n\n"
            "✨ شكرًا لاستخدامك Pulse Bot!"
        ),
        "group_only": "⚠️ هذا الأمر يعمل داخل المجموعات فقط.",
        "admin_only": "⚠️ هذا الإعداد يمكن للمشرفين تغييره فقط.",
        "reply_required": "⚠️ رد على رسالة العضو لاستخدام هذا الأمر.",
        "target_is_admin": "⚠️ لا يمكنك إدارة مشرف أو مالك المجموعة.",
        "bot_lacks_rights": "⚠️ أحتاج صلاحيات المشرف داخل هذه المجموعة لتنفيذ ذلك.",
        "warn_done": "⚠️ تم إنذار {user}. الإنذارات: {count}/3.",
        "unwarn_done": "✅ تم حذف إنذار من {user}. الإنذارات: {count}/3.",
        "warnings_show": "📌 لدى {user} عدد {count} من الإنذارات.",
        "warn_limit_hit": "🚨 وصل {user} إلى 3/3 من الإنذارات وتم كتمه لمدة 24 ساعة.",
        "mute_done": "🔇 تم كتم {user}.",
        "unmute_done": "🔊 تم فك كتم {user}.",
        "ban_done": "⛔ تم حظر {user}.",
        "unban_done": "✅ تم فك حظر {user}.",
        "stats_title": "📊 <b>إحصائيات اليوم في {bot}</b>",
        "report_title": "📄 <b>التقرير اليومي</b>",
        "yesterday_title": "📅 <b>تقرير الأمس</b>",
        "growth_title": "📈 <b>نمو السيرفر</b>",
        "no_data": "لا توجد إحصائيات بعد.",
        "language_menu": "🌍 اختر لغة هذه المحادثة:",
        "language_set": "✅ تم تحديث اللغة.",
        "support_text": "⭐ <b>دعم {bot}</b>\n\nيمكنك دعم المشروع بـ <b>{stars} نجوم تيليجرام</b>.",
        "creator_text": (
            "👨‍💻 <b>{bot}</b>\n\n"
            "تم الإنشاء والمتابعة بواسطة {creator}.\n\n"
            "مخصص لمجتمعات تيليجرام التي تحتاج إحصائيات سريعة وواضحة وموثوقة.\n"
            "كل تحديث هنا مصمم لتسهيل إدارة المجموعة وجعلها أكثر فائدة."
        ),
        "daily_prompt": "🌙 <b>تحديث منتصف الليل جاهز.</b>\n\nهل تريد مشاهدة تقرير الأمس؟",
        "yes": "✅ نعم",
        "no": "❌ لا",
        "support_button": f"⭐ دعم بـ {STAR_SUPPORT_AMOUNT} نجوم",
        "gift_button": "🎁 رابط هدية الصانع",
        "open_report": "📄 عرض التقرير",
        "open_support": "💬 فتح الدعم",
        "add_group": "➕ إضافة إلى المجموعة",
        "today": "اليوم",
        "yesterday": "الأمس",
        "messages": "الرسائل",
        "active_members": "الأعضاء النشطون",
        "joined": "المنضمون",
        "left": "المغادرون",
        "net_growth": "النمو الصافي",
        "change_vs_prev": "التغير مقارنة باليوم السابق",
        "top_member": "أكثر عضو نشاطًا",
        "top_admin": "أكثر إداري نشاطًا",
        "peak_hour": "ساعة الذروة",
        "most_replied": "أكثر رسالة عليها ردود",
        "most_reacted": "أكثر رسالة عليها تفاعلات",
        "top_topics": "أبرز المواضيع",
        "new_members": "الأعضاء الجدد",
        "left_members": "الأعضاء المغادرون",
    },
    "ru": {
        "welcome": "👋 Добро пожаловать в <b>{bot}</b>!\n\nДобавьте меня в группу Telegram, чтобы начать отслеживать статистику.",
        "help": (
            "🤖 <b>Pulse Bot</b>\n\n"
            "━━━━━━━━━━━━━━\n\n"
            "📊 <b>Статистика</b>\n"
            "• /stats — Статистика сегодня\n"
            "• /report — Полный отчёт за день\n"
            "• /yesterday — Отчёт за вчера\n"
            "• /growth — Рост сервера по сравнению со вчера\n\n"
            "⚙️ <b>Настройки</b>\n"
            "• /language — Изменить язык бота\n\n"
            "🛡️ <b>Модерация</b>\n"
            "• /warn — Предупредить участника\n"
            "• /unwarn — Снять предупреждение\n"
            "• /warnings — Показать предупреждения\n"
            "• /mute — Заглушить участника\n"
            "• /unmute — Разглушить участника\n"
            "• /ban — Забанить участника\n"
            "• /unban — Разбанить участника\n"
            "ℹ️ <b>Информация</b>\n"
            "• /start — Запустить бота\n"
            "• /help — Показать меню\n"
            "• /support — Поддержка\n"
            "• /creator — О создателе\n\n"
            "━━━━━━━━━━━━━━\n\n"
            "✨ Спасибо за использование Pulse Bot!"
        ),
        "group_only": "⚠️ Эта команда работает только в группах.",
        "admin_only": "⚠️ Настройку могут менять только админы группы.",
        "reply_required": "⚠️ Ответьте на сообщение участника, чтобы использовать эту команду.",
        "target_is_admin": "⚠️ Нельзя управлять администратором или создателем группы.",
        "bot_lacks_rights": "⚠️ Мне нужны права администратора в этой группе, чтобы выполнить это.",
        "warn_done": "⚠️ {user} получил предупреждение. Предупреждения: {count}/3.",
        "unwarn_done": "✅ Предупреждение снято у {user}. Предупреждения: {count}/3.",
        "warnings_show": "📌 У {user} {count} предупреждений.",
        "warn_limit_hit": "🚨 {user} достиг 3/3 предупреждений и был заглушён на 24 часа.",
        "mute_done": "🔇 {user} заглушён.",
        "unmute_done": "🔊 {user} снова может писать.",
        "ban_done": "⛔ {user} забанен.",
        "unban_done": "✅ {user} разбанен.",
        "stats_title": "📊 <b>Статистика дня в {bot}</b>",
        "report_title": "📄 <b>Ежедневный отчёт</b>",
        "yesterday_title": "📅 <b>Отчёт за вчера</b>",
        "growth_title": "📈 <b>Рост сервера</b>",
        "no_data": "Пока нет статистики.",
        "language_menu": "🌍 Выберите язык чата:",
        "language_set": "✅ Язык обновлён.",
        "support_text": "⭐ <b>Поддержать {bot}</b>\n\nМожно поддержать проект <b>{stars} звёздами Telegram</b>.",
        "creator_text": (
            "👨‍💻 <b>{bot}</b>\n\n"
            "Создан и поддерживается {creator}.\n\n"
            "Создан для Telegram-сообществ, которым нужна быстрая, понятная и надёжная статистика активности.\n"
            "Каждое обновление делает управление группой проще и полезнее."
        ),
        "daily_prompt": "🌙 <b>Ночной отчёт готов.</b>\n\nХотите посмотреть отчёт за вчера?",
        "yes": "✅ Да",
        "no": "❌ Нет",
        "support_button": f"⭐ Поддержать {STAR_SUPPORT_AMOUNT} звёздами",
        "gift_button": "🎁 Подарочная ссылка",
        "open_report": "📄 Показать отчёт",
        "open_support": "💬 Поддержка",
        "add_group": "➕ Добавить в группу",
        "today": "Сегодня",
        "yesterday": "Вчера",
        "messages": "Сообщения",
        "active_members": "Активные участники",
        "joined": "Присоединились",
        "left": "Ушли",
        "net_growth": "Чистый рост",
        "change_vs_prev": "Изменение по сравнению с предыдущим днём",
        "top_member": "Самый активный участник",
        "top_admin": "Самый активный админ",
        "peak_hour": "Час пика",
        "most_replied": "Сообщение с ответами",
        "most_reacted": "Сообщение с реакциями",
        "top_topics": "Главные темы",
        "new_members": "Новые участники",
        "left_members": "Ушедшие участники",
    },
}

LANGUAGE_OVERRIDES = {
    "fr": {
        "welcome": "👋 Bienvenue sur <b>{bot}</b> !\n\nAjoutez-moi à un groupe Telegram pour commencer à suivre les statistiques.",
        "help": "🤖 <b>Pulse Bot</b>\n\n━━━━━━━━━━━━━━\n\n📊 <b>Statistiques</b>\n• /stats — Statistiques du jour\n• /report — Rapport complet du jour\n• /yesterday — Rapport d’hier\n• /growth — Croissance par rapport à hier\n\n⚙️ <b>Paramètres</b>\n• /language — Changer la langue du bot\n\n🛡️ <b>Modération</b>\n• /warn — Avertir un membre\n• /unwarn — Retirer un avertissement\n• /warnings — Voir les avertissements\n• /mute — Rendre muet un membre\n• /unmute — Réactiver un membre\n• /ban — Bannir un membre\n• /unban — Débannir un membre\n\nℹ️ <b>Infos</b>\n• /start — Démarrer le bot\n• /help — Afficher l’aide\n• /support — Contacter le support\n• /creator — Voir le créateur\n• /info — Infos du groupe et du bot\n\n━━━━━━━━━━━━━━\n\n✨ Merci d’utiliser Pulse Bot !",
        "group_only": "⚠️ Cette commande fonctionne uniquement dans les groupes Telegram.",
        "admin_only": "⚠️ Ce paramètre peut être modifié uniquement par les administrateurs du groupe.",
        "reply_required": "⚠️ Répondez au message d’un membre pour utiliser cette commande.",
        "target_is_admin": "⚠️ Vous ne pouvez pas modérer un administrateur ou le créateur du groupe.",
        "bot_lacks_rights": "⚠️ J’ai besoin des droits d’administrateur dans ce groupe pour faire cela.",
        "bot_lacks_promote_rights": "⚠️ J’ai besoin du droit de promouvoir des membres dans ce groupe pour faire cela.",
        "warn_done": "⚠️ {user} averti. Avertissements : {count}/3.",
        "unwarn_done": "✅ Avertissement retiré pour {user}. Avertissements : {count}/3.",
        "warnings_show": "📌 {user} a {count} avertissement(s).",
        "warn_limit_hit": "🚨 {user} a atteint 3/3 avertissements et a été muté pendant 24 heures.",
        "mute_done": "🔇 {user} a été muet.",
        "unmute_done": "🔊 {user} peut de nouveau parler.",
        "ban_done": "⛔ {user} a été banni.",
        "unban_done": "✅ {user} a été débanni.",
        "stats_title": "📊 <b>Statistiques du jour sur {bot}</b>",
        "report_title": "📄 <b>Rapport quotidien</b>",
        "yesterday_title": "📅 <b>Rapport d’hier</b>",
        "growth_title": "📈 <b>Croissance du serveur</b>",
        "no_data": "Aucune statistique pour le moment.",
        "language_menu": "🌍 Choisissez une langue pour ce chat :",
        "language_set": "✅ Langue mise à jour.",
        "support_text": "⭐ <b>Soutenir {bot}</b>\n\nVous pouvez soutenir le projet avec <b>{stars} étoiles Telegram</b>.",
        "creator_text": "👨‍💻 <b>{bot}</b>\n\nCréé et maintenu par {creator}.\n\nConçu pour les communautés Telegram qui ont besoin de statistiques rapides, claires et fiables.\nChaque mise à jour améliore la gestion du groupe.",
        "daily_prompt": "🌙 <b>La mise à jour de minuit est prête.</b>\n\nVoulez-vous voir le rapport d’hier ?",
        "yes": "✅ Oui",
        "no": "❌ Non",
        "support_button": "⭐ Soutenir avec {stars} étoiles",
        "gift_button": "🎁 Lien cadeau du créateur",
        "open_report": "📄 Voir le rapport",
        "open_support": "💬 Ouvrir le support",
        "add_group": "➕ Ajouter au groupe"
    },
    "es": {
        "welcome": "👋 Bienvenido a <b>{bot}</b>!\n\nAñádeme a un grupo de Telegram para empezar a seguir las estadísticas.",
        "help": "🤖 <b>Pulse Bot</b>\n\n━━━━━━━━━━━━━━\n\n📊 <b>Estadísticas</b>\n• /stats — Estadísticas del día\n• /report — Informe completo del día\n• /yesterday — Informe de ayer\n• /growth — Crecimiento respecto a ayer\n\n⚙️ <b>Ajustes</b>\n• /language — Cambiar idioma del bot\n\n🛡️ <b>Moderación</b>\n• /warn — Advertir a un miembro\n• /unwarn — Quitar una advertencia\n• /warnings — Ver advertencias\n• /mute — Silenciar a un miembro\n• /unmute — Quitar silencio\n• /ban — Banear a un miembro\n• /unban — Desbanear a un miembro\n\nℹ️ <b>Información</b>\n• /start — Iniciar el bot\n• /help — Mostrar ayuda\n• /support — Contactar soporte\n• /creator — Ver al creador\n• /info — Info del grupo y del bot\n\n━━━━━━━━━━━━━━\n\n✨ ¡Gracias por usar Pulse Bot!",
        "group_only": "⚠️ Este comando solo funciona en grupos de Telegram.",
        "admin_only": "⚠️ Este ajuste solo puede cambiarlo un administrador del grupo.",
        "reply_required": "⚠️ Responde al mensaje de un miembro para usar este comando.",
        "target_is_admin": "⚠️ No puedes moderar a un administrador o al creador del grupo.",
        "bot_lacks_rights": "⚠️ Necesito permisos de administrador en este grupo para hacer eso.",
        "bot_lacks_promote_rights": "⚠️ Necesito permiso para promover miembros en este grupo para hacer eso.",
        "warn_done": "⚠️ {user} advertido. Advertencias: {count}/3.",
        "unwarn_done": "✅ Advertencia eliminada para {user}. Advertencias: {count}/3.",
        "warnings_show": "📌 {user} tiene {count} advertencia(s).",
        "warn_limit_hit": "🚨 {user} llegó a 3/3 advertencias y fue silenciado durante 24 horas.",
        "mute_done": "🔇 {user} fue silenciado.",
        "unmute_done": "🔊 {user} ya puede hablar.",
        "ban_done": "⛔ {user} fue baneado.",
        "unban_done": "✅ {user} fue desbaneado.",
        "stats_title": "📊 <b>Estadísticas de hoy en {bot}</b>",
        "report_title": "📄 <b>Informe diario</b>",
        "yesterday_title": "📅 <b>Informe de ayer</b>",
        "growth_title": "📈 <b>Crecimiento del servidor</b>",
        "no_data": "Todavía no hay estadísticas.",
        "language_menu": "🌍 Elige un idioma para este chat:",
        "language_set": "✅ Idioma actualizado.",
        "support_text": "⭐ <b>Apoyar a {bot}</b>\n\nPuedes apoyar el proyecto con <b>{stars} estrellas de Telegram</b>.",
        "creator_text": "👨‍💻 <b>{bot}</b>\n\nCreado y mantenido por {creator}.\n\nDiseñado para comunidades de Telegram que necesitan estadísticas rápidas, claras y fiables.\nCada actualización mejora la gestión del grupo.",
        "daily_prompt": "🌙 <b>La actualización de medianoche está lista.</b>\n\n¿Quieres ver el informe de ayer?",
        "yes": "✅ Sí",
        "no": "❌ No",
        "support_button": "⭐ Apoyar con {stars} estrellas",
        "gift_button": "🎁 Enlace regalo del creador",
        "open_report": "📄 Ver informe",
        "open_support": "💬 Abrir soporte",
        "add_group": "➕ Añadir al grupo"
    },
    "de": {
        "welcome": "👋 Willkommen bei <b>{bot}</b>!\n\nFüge mich zu einer Telegram-Gruppe hinzu, um Statistiken zu verfolgen.",
        "help": "🤖 <b>Pulse Bot</b>\n\n━━━━━━━━━━━━━━\n\n📊 <b>Statistiken</b>\n• /stats — Heutige Statistiken\n• /report — Vollständiger Tagesbericht\n• /yesterday — Bericht von gestern\n• /growth — Wachstum im Vergleich zu gestern\n\n⚙️ <b>Einstellungen</b>\n• /language — Bot-Sprache ändern\n\n🛡️ <b>Moderation</b>\n• /warn — Mitglied verwarnen\n• /unwarn — Verwarnung entfernen\n• /warnings — Verwarnungen anzeigen\n• /mute — Mitglied stummschalten\n• /unmute — Stummschaltung aufheben\n• /ban — Mitglied sperren\n• /unban — Sperre aufheben\n\nℹ️ <b>Info</b>\n• /start — Bot starten\n• /help — Hilfe anzeigen\n• /support — Support kontaktieren\n• /creator — Ersteller anzeigen\n• /info — Gruppen- und Bot-Infos\n\n━━━━━━━━━━━━━━\n\n✨ Danke, dass du Pulse Bot benutzt!",
        "group_only": "⚠️ Dieser Befehl funktioniert nur in Telegram-Gruppen.",
        "admin_only": "⚠️ Diese Einstellung kann nur von Gruppen-Admins geändert werden.",
        "reply_required": "⚠️ Antworte auf die Nachricht eines Mitglieds, um diesen Befehl zu verwenden.",
        "target_is_admin": "⚠️ Du kannst keinen Administrator oder den Ersteller der Gruppe moderieren.",
        "bot_lacks_rights": "⚠️ Ich brauche Admin-Rechte in dieser Gruppe, um das zu tun.",
        "bot_lacks_promote_rights": "⚠️ Ich brauche die Berechtigung, Mitglieder zu befördern, um das zu tun.",
        "warn_done": "⚠️ {user} verwarnt. Verwarnungen: {count}/3.",
        "unwarn_done": "✅ Verwarnung für {user} entfernt. Verwarnungen: {count}/3.",
        "warnings_show": "📌 {user} hat {count} Verwarnung(en).",
        "warn_limit_hit": "🚨 {user} hat 3/3 Verwarnungen erreicht und wurde für 24 Stunden stummgeschaltet.",
        "mute_done": "🔇 {user} wurde stummgeschaltet.",
        "unmute_done": "🔊 {user} kann wieder sprechen.",
        "ban_done": "⛔ {user} wurde gesperrt.",
        "unban_done": "✅ Sperre für {user} aufgehoben.",
        "stats_title": "📊 <b>Heutige Statistiken in {bot}</b>",
        "report_title": "📄 <b>Tagesbericht</b>",
        "yesterday_title": "📅 <b>Bericht von gestern</b>",
        "growth_title": "📈 <b>Serverwachstum</b>",
        "no_data": "Noch keine Statistiken.",
        "language_menu": "🌍 Wähle eine Sprache für diesen Chat:",
        "language_set": "✅ Sprache aktualisiert.",
        "support_text": "⭐ <b>{bot} unterstützen</b>\n\nDu kannst das Projekt mit <b>{stars} Telegram-Sternen</b> unterstützen.",
        "creator_text": "👨‍💻 <b>{bot}</b>\n\nErstellt und gepflegt von {creator}.\n\nFür Telegram-Communities entwickelt, die schnelle, klare und zuverlässige Aktivitätsstatistiken brauchen.\nJedes Update macht die Gruppenverwaltung besser.",
        "daily_prompt": "🌙 <b>Das Mitternachts-Update ist bereit.</b>\n\nMöchtest du den Bericht von gestern sehen?",
        "yes": "✅ Ja",
        "no": "❌ Nein",
        "support_button": "⭐ Mit {stars} Sternen unterstützen",
        "gift_button": "🎁 Geschenk-Link des Erstellers",
        "open_report": "📄 Bericht ansehen",
        "open_support": "💬 Support öffnen",
        "add_group": "➕ Zur Gruppe hinzufügen"
    },
    "pt": {
        "welcome": "👋 Bem-vindo ao <b>{bot}</b>!\n\nAdicione-me a um grupo do Telegram para começar a acompanhar as estatísticas.",
        "help": "🤖 <b>Pulse Bot</b>\n\n━━━━━━━━━━━━━━\n\n📊 <b>Estatísticas</b>\n• /stats — Estatísticas de hoje\n• /report — Relatório completo do dia\n• /yesterday — Relatório de ontem\n• /growth — Crescimento em relação a ontem\n\n⚙️ <b>Configurações</b>\n• /language — Alterar o idioma do bot\n\n🛡️ <b>Moderação</b>\n• /warn — Avisar um membro\n• /unwarn — Remover um aviso\n• /warnings — Ver avisos\n• /mute — Silenciar um membro\n• /unmute — Remover silêncio\n• /ban — Banir um membro\n• /unban — Desbanir um membro\n\nℹ️ <b>Informação</b>\n• /start — Iniciar o bot\n• /help — Mostrar ajuda\n• /support — Contatar suporte\n• /creator — Ver o criador\n• /info — Info do grupo e do bot\n\n━━━━━━━━━━━━━━\n\n✨ Obrigado por usar o Pulse Bot!",
        "group_only": "⚠️ Este comando funciona apenas em grupos do Telegram.",
        "admin_only": "⚠️ Esta definição só pode ser alterada por administradores do grupo.",
        "reply_required": "⚠️ Responda à mensagem de um membro para usar este comando.",
        "target_is_admin": "⚠️ Você não pode moderar um administrador ou o criador do grupo.",
        "bot_lacks_rights": "⚠️ Preciso de permissões de admin neste grupo para fazer isso.",
        "bot_lacks_promote_rights": "⚠️ Preciso de permissão para promover membros neste grupo para fazer isso.",
        "warn_done": "⚠️ {user} advertido. Advertências: {count}/3.",
        "unwarn_done": "✅ Advertência removida para {user}. Advertências: {count}/3.",
        "warnings_show": "📌 {user} tem {count} advertência(s).",
        "warn_limit_hit": "🚨 {user} atingiu 3/3 advertências e foi silenciado por 24 horas.",
        "mute_done": "🔇 {user} foi silenciado.",
        "unmute_done": "🔊 {user} pode falar novamente.",
        "ban_done": "⛔ {user} foi banido.",
        "unban_done": "✅ {user} foi desbanido.",
        "stats_title": "📊 <b>Estatísticas de hoje em {bot}</b>",
        "report_title": "📄 <b>Relatório diário</b>",
        "yesterday_title": "📅 <b>Relatório de ontem</b>",
        "growth_title": "📈 <b>Crescimento do servidor</b>",
        "no_data": "Ainda não há estatísticas.",
        "language_menu": "🌍 Escolha um idioma para este chat:",
        "language_set": "✅ Idioma atualizado.",
        "support_text": "⭐ <b>Apoiar {bot}</b>\n\nVocê pode apoiar o projeto com <b>{stars} estrelas do Telegram</b>.",
        "creator_text": "👨‍💻 <b>{bot}</b>\n\nCriado e mantido por {creator}.\n\nFeito para comunidades do Telegram que precisam de estatísticas rápidas, claras e confiáveis.\nCada atualização melhora a gestão do grupo.",
        "daily_prompt": "🌙 <b>A atualização da meia-noite está pronta.</b>\n\nQuer ver o relatório de ontem?",
        "yes": "✅ Sim",
        "no": "❌ Não",
        "support_button": "⭐ Apoiar com {stars} estrelas",
        "gift_button": "🎁 Link de presente do criador",
        "open_report": "📄 Ver relatório",
        "open_support": "💬 Abrir suporte",
        "add_group": "➕ Adicionar ao grupo"
    },
    "it": {
        "welcome": "👋 Benvenuto in <b>{bot}</b>!\n\nAggiungimi a un gruppo Telegram per iniziare a monitorare le statistiche.",
        "help": "🤖 <b>Pulse Bot</b>\n\n━━━━━━━━━━━━━━\n\n📊 <b>Statistiche</b>\n• /stats — Statistiche di oggi\n• /report — Report completo del giorno\n• /yesterday — Report di ieri\n• /growth — Crescita rispetto a ieri\n\n⚙️ <b>Impostazioni</b>\n• /language — Cambia lingua del bot\n\n🛡️ <b>Moderazione</b>\n• /warn — Avvisa un membro\n• /unwarn — Rimuovi un avviso\n• /warnings — Mostra gli avvisi\n• /mute — Silenzia un membro\n• /unmute — Riattiva un membro\n• /ban — Bannare un membro\n• /unban — Rimuovere il ban\n\nℹ️ <b>Informazioni</b>\n• /start — Avvia il bot\n• /help — Mostra l’aiuto\n• /support — Contatta il supporto\n• /creator — Vedi il creatore\n• /info — Info del gruppo e del bot\n\n━━━━━━━━━━━━━━\n\n✨ Grazie per aver usato Pulse Bot!",
        "group_only": "⚠️ Questo comando funziona solo nei gruppi Telegram.",
        "admin_only": "⚠️ Questa impostazione può essere modificata solo dagli amministratori del gruppo.",
        "reply_required": "⚠️ Rispondi al messaggio di un membro per usare questo comando.",
        "target_is_admin": "⚠️ Non puoi moderare un amministratore o il creatore del gruppo.",
        "bot_lacks_rights": "⚠️ Mi servono i permessi di amministratore in questo gruppo per farlo.",
        "bot_lacks_promote_rights": "⚠️ Mi serve il permesso di promuovere membri in questo gruppo per farlo.",
        "warn_done": "⚠️ {user} avvisato. Avvisi: {count}/3.",
        "unwarn_done": "✅ Avviso rimosso per {user}. Avvisi: {count}/3.",
        "warnings_show": "📌 {user} ha {count} avviso/i.",
        "warn_limit_hit": "🚨 {user} ha raggiunto 3/3 avvisi ed è stato silenziato per 24 ore.",
        "mute_done": "🔇 {user} è stato silenziato.",
        "unmute_done": "🔊 {user} può parlare di nuovo.",
        "ban_done": "⛔ {user} è stato bannato.",
        "unban_done": "✅ Ban rimosso per {user}.",
        "stats_title": "📊 <b>Statistiche di oggi in {bot}</b>",
        "report_title": "📄 <b>Report giornaliero</b>",
        "yesterday_title": "📅 <b>Report di ieri</b>",
        "growth_title": "📈 <b>Crescita del server</b>",
        "no_data": "Nessuna statistica per ora.",
        "language_menu": "🌍 Scegli una lingua per questa chat:",
        "language_set": "✅ Lingua aggiornata.",
        "support_text": "⭐ <b>Supporta {bot}</b>\n\nPuoi sostenere il progetto con <b>{stars} stelle Telegram</b>.",
        "creator_text": "👨‍💻 <b>{bot}</b>\n\nCreato e mantenuto da {creator}.\n\nPensato per le community Telegram che hanno bisogno di statistiche rapide, chiare e affidabili.\nOgni aggiornamento rende la gestione del gruppo più semplice.",
        "daily_prompt": "🌙 <b>L’aggiornamento di mezzanotte è pronto.</b>\n\nVuoi vedere il report di ieri?",
        "yes": "✅ Sì",
        "no": "❌ No",
        "support_button": "⭐ Supporta con {stars} stelle",
        "gift_button": "🎁 Link regalo del creatore",
        "open_report": "📄 Vedi report",
        "open_support": "💬 Apri supporto",
        "add_group": "➕ Aggiungi al gruppo"
    },
    "tr": {
        "welcome": "👋 <b>{bot}</b> botuna hoş geldin!\n\nİstatistikleri takip etmeye başlamak için beni bir Telegram grubuna ekle.",
        "help": "🤖 <b>Pulse Bot</b>\n\n━━━━━━━━━━━━━━\n\n📊 <b>İstatistikler</b>\n• /stats — Bugünün istatistikleri\n• /report — Günün tam raporu\n• /yesterday — Dünün raporu\n• /growth — Düne göre büyüme\n\n⚙️ <b>Ayarlar</b>\n• /language — Bot dilini değiştir\n\n🛡️ <b>Moderasyon</b>\n• /warn — Üyeyi uyar\n• /unwarn — Uyarıyı kaldır\n• /warnings — Uyarıları göster\n• /mute — Üyeyi sustur\n• /unmute — Sustuğu kaldır\n• /ban — Üyeyi yasakla\n• /unban — Yasağı kaldır\n\nℹ️ <b>Bilgi</b>\n• /start — Botu başlat\n• /help — Yardımı göster\n• /support — Destekle iletişim\n• /creator — Oluşturucuyu göster\n• /info — Grup ve bot bilgileri\n\n━━━━━━━━━━━━━━\n\n✨ Pulse Bot’u kullandığın için teşekkürler!",
        "group_only": "⚠️ Bu komut yalnızca Telegram gruplarında çalışır.",
        "admin_only": "⚠️ Bu ayar yalnızca grup yöneticileri tarafından değiştirilebilir.",
        "reply_required": "⚠️ Bu komutu kullanmak için bir üyenin mesajına cevap ver.",
        "target_is_admin": "⚠️ Bir yöneticiyi veya grup sahibini yönetemezsin.",
        "bot_lacks_rights": "⚠️ Bunu yapmak için bu grupta yönetici yetkilerine ihtiyacım var.",
        "bot_lacks_promote_rights": "⚠️ Bunu yapmak için üye terfi ettirme yetkisine ihtiyacım var.",
        "warn_done": "⚠️ {user} uyarıldı. Uyarılar: {count}/3.",
        "unwarn_done": "✅ {user} için uyarı kaldırıldı. Uyarılar: {count}/3.",
        "warnings_show": "📌 {user} için {count} uyarı var.",
        "warn_limit_hit": "🚨 {user} 3/3 uyarıya ulaştı ve 24 saat susturuldu.",
        "mute_done": "🔇 {user} susturuldu.",
        "unmute_done": "🔊 {user} tekrar konuşabilir.",
        "ban_done": "⛔ {user} yasaklandı.",
        "unban_done": "✅ {user} yasağı kaldırıldı.",
        "stats_title": "📊 <b>{bot} için bugünün istatistikleri</b>",
        "report_title": "📄 <b>Günlük rapor</b>",
        "yesterday_title": "📅 <b>Dünün raporu</b>",
        "growth_title": "📈 <b>Sunucu büyümesi</b>",
        "no_data": "Henüz istatistik yok.",
        "language_menu": "🌍 Bu sohbet için bir dil seç:",
        "language_set": "✅ Dil güncellendi.",
        "support_text": "⭐ <b>{bot} destekle</b>\n\nProjeyi <b>{stars} Telegram yıldızı</b> ile destekleyebilirsin.",
        "creator_text": "👨‍💻 <b>{bot}</b>\n\n{creator} tarafından oluşturuldu ve sürdürülüyor.\n\nTelegram toplulukları için hızlı, net ve güvenilir istatistikler sunar.\nHer güncelleme grup yönetimini daha kolay hale getirir.",
        "daily_prompt": "🌙 <b>Gece yarısı güncellemesi hazır.</b>\n\nDünün raporunu görmek ister misin?",
        "yes": "✅ Evet",
        "no": "❌ Hayır",
        "support_button": "⭐ {stars} yıldızla destekle",
        "gift_button": "🎁 Oluşturucunun hediye bağlantısı",
        "open_report": "📄 Raporu gör",
        "open_support": "💬 Desteği aç",
        "add_group": "➕ Gruba ekle"
    },
    "fa": {
        "welcome": "👋 به <b>{bot}</b> خوش آمدی!\n\nبرای شروع آمارگیری مرا به یک گروه تلگرام اضافه کن.",
        "help": "🤖 <b>Pulse Bot</b>\n\n━━━━━━━━━━━━━━\n\n📊 <b>آمار</b>\n• /stats — آمار امروز\n• /report — گزارش کامل امروز\n• /yesterday — گزارش دیروز\n• /growth — رشد نسبت به دیروز\n\n⚙️ <b>تنظیمات</b>\n• /language — تغییر زبان ربات\n\n🛡️ <b>مدیریت</b>\n• /warn — هشدار به عضو\n• /unwarn — حذف یک هشدار\n• /warnings — نمایش هشدارها\n• /mute — ساکت کردن عضو\n• /unmute — رفع سکوت\n• /ban — بن کردن عضو\n• /unban — رفع بن\n\nℹ️ <b>اطلاعات</b>\n• /start — شروع ربات\n• /help — نمایش راهنما\n• /support — ارتباط با پشتیبانی\n• /creator — سازنده ربات\n• /info — اطلاعات گروه و ربات\n\n━━━━━━━━━━━━━━\n\n✨ ممنون که از Pulse Bot استفاده می‌کنی!",
        "group_only": "⚠️ این دستور فقط در گروه‌های تلگرام کار می‌کند.",
        "admin_only": "⚠️ این تنظیم فقط توسط مدیران گروه قابل تغییر است.",
        "reply_required": "⚠️ برای استفاده از این دستور به پیام یک عضو پاسخ بده.",
        "target_is_admin": "⚠️ نمی‌توانی روی مدیر یا سازنده گروه این کار را انجام دهی.",
        "bot_lacks_rights": "⚠️ برای این کار به دسترسی مدیر در این گروه نیاز دارم.",
        "bot_lacks_promote_rights": "⚠️ برای این کار به اجازه ارتقای اعضا نیاز دارم.",
        "warn_done": "⚠️ به {user} هشدار داده شد. هشدارها: {count}/3.",
        "unwarn_done": "✅ یک هشدار از {user} حذف شد. هشدارها: {count}/3.",
        "warnings_show": "📌 {user} {count} هشدار دارد.",
        "warn_limit_hit": "🚨 {user} به 3/3 هشدار رسید و 24 ساعت ساکت شد.",
        "mute_done": "🔇 {user} ساکت شد.",
        "unmute_done": "🔊 {user} دوباره می‌تواند صحبت کند.",
        "ban_done": "⛔ {user} بن شد.",
        "unban_done": "✅ بن {user} برداشته شد.",
        "stats_title": "📊 <b>آمار امروز در {bot}</b>",
        "report_title": "📄 <b>گزارش روزانه</b>",
        "yesterday_title": "📅 <b>گزارش دیروز</b>",
        "growth_title": "📈 <b>رشد سرور</b>",
        "no_data": "هنوز آماری وجود ندارد.",
        "language_menu": "🌍 یک زبان برای این گفتگو انتخاب کن:",
        "language_set": "✅ زبان به‌روزرسانی شد.",
        "support_text": "⭐ <b>پشتیبانی از {bot}</b>\n\nمی‌توانی پروژه را با <b>{stars} ستاره تلگرام</b> پشتیبانی کنی.",
        "creator_text": "👨‍💻 <b>{bot}</b>\n\nساخته و نگهداری شده توسط {creator}.\n\nبرای جوامع تلگرامی که به آمار سریع، واضح و قابل اعتماد نیاز دارند ساخته شده است.\nهر به‌روزرسانی مدیریت گروه را بهتر می‌کند.",
        "daily_prompt": "🌙 <b>به‌روزرسانی نیمه‌شب آماده است.</b>\n\nآیا می‌خواهی گزارش دیروز را ببینی؟",
        "yes": "✅ بله",
        "no": "❌ نه",
        "support_button": "⭐ پشتیبانی با {stars} ستاره",
        "gift_button": "🎁 لینک هدیه سازنده",
        "open_report": "📄 مشاهده گزارش",
        "open_support": "💬 باز کردن پشتیبانی",
        "add_group": "➕ افزودن به گروه"
    },
    "id": {
        "welcome": "👋 Selamat datang di <b>{bot}</b>!\n\nTambahkan saya ke grup Telegram untuk mulai melacak statistik.",
        "help": "🤖 <b>Pulse Bot</b>\n\n━━━━━━━━━━━━━━\n\n📊 <b>Statistik</b>\n• /stats — Statistik hari ini\n• /report — Laporan lengkap hari ini\n• /yesterday — Laporan kemarin\n• /growth — Pertumbuhan dibanding kemarin\n\n⚙️ <b>Pengaturan</b>\n• /language — Ubah bahasa bot\n\n🛡️ <b>Moderasi</b>\n• /warn — Peringatkan anggota\n• /unwarn — Hapus peringatan\n• /warnings — Tampilkan peringatan\n• /mute — Bisukan anggota\n• /unmute — Buka bisu\n• /ban — Ban anggota\n• /unban — Buka ban\n\nℹ️ <b>Info</b>\n• /start — Mulai bot\n• /help — Tampilkan bantuan\n• /support — Hubungi dukungan\n• /creator — Lihat pembuat\n• /info — Info grup & bot\n\n━━━━━━━━━━━━━━\n\n✨ Terima kasih telah menggunakan Pulse Bot!",
        "group_only": "⚠️ Perintah ini hanya berfungsi di grup Telegram.",
        "admin_only": "⚠️ Pengaturan ini hanya bisa diubah oleh admin grup.",
        "reply_required": "⚠️ Balas pesan anggota untuk menggunakan perintah ini.",
        "target_is_admin": "⚠️ Kamu tidak bisa memoderasi admin atau pembuat grup.",
        "bot_lacks_rights": "⚠️ Saya butuh hak admin di grup ini untuk melakukan itu.",
        "bot_lacks_promote_rights": "⚠️ Saya butuh izin untuk mempromosikan anggota di grup ini.",
        "warn_done": "⚠️ {user} diperingatkan. Peringatan: {count}/3.",
        "unwarn_done": "✅ Peringatan untuk {user} dihapus. Peringatan: {count}/3.",
        "warnings_show": "📌 {user} memiliki {count} peringatan.",
        "warn_limit_hit": "🚨 {user} mencapai 3/3 peringatan dan dibisukan selama 24 jam.",
        "mute_done": "🔇 {user} dibisukan.",
        "unmute_done": "🔊 {user} bisa berbicara lagi.",
        "ban_done": "⛔ {user} diban.",
        "unban_done": "✅ Ban {user} dibuka.",
        "stats_title": "📊 <b>Statistik hari ini di {bot}</b>",
        "report_title": "📄 <b>Laporan harian</b>",
        "yesterday_title": "📅 <b>Laporan kemarin</b>",
        "growth_title": "📈 <b>Pertumbuhan server</b>",
        "no_data": "Belum ada statistik.",
        "language_menu": "🌍 Pilih bahasa untuk chat ini:",
        "language_set": "✅ Bahasa diperbarui.",
        "support_text": "⭐ <b>Dukung {bot}</b>\n\nKamu bisa mendukung proyek ini dengan <b>{stars} bintang Telegram</b>.",
        "creator_text": "👨‍💻 <b>{bot}</b>\n\nDibuat dan dikelola oleh {creator}.\n\nDibuat untuk komunitas Telegram yang membutuhkan statistik cepat, jelas, dan andal.\nSetiap pembaruan membuat pengelolaan grup lebih baik.",
        "daily_prompt": "🌙 <b>Pembaruan tengah malam siap.</b>\n\nIngin melihat laporan kemarin?",
        "yes": "✅ Ya",
        "no": "❌ Tidak",
        "support_button": "⭐ Dukung dengan {stars} bintang",
        "gift_button": "🎁 Tautan hadiah pembuat",
        "open_report": "📄 Lihat laporan",
        "open_support": "💬 Buka dukungan",
        "add_group": "➕ Tambahkan ke grup"
    }
}

LANGUAGE_BUTTONS = [('🇬🇧 English', 'en'), ('🇸🇦 العربية', 'ar'), ('🇷🇺 Русский', 'ru'), ('🇫🇷 Français', 'fr'), ('🇪🇸 Español', 'es'), ('🇩🇪 Deutsch', 'de'), ('🇵🇹 Português', 'pt'), ('🇮🇹 Italiano', 'it'), ('🇹🇷 Türkçe', 'tr'), ('🇮🇷 فارسی', 'fa'), ('🇮🇩 Bahasa Indonesia', 'id'), ('🇯🇵 日本語', 'ja'), ('🇰🇷 한국어', 'ko'), ('🇨🇳 中文', 'zh')]
LANGUAGE_CODES = {code for _, code in LANGUAGE_BUTTONS}
LANGUAGE_ALIASES = {
    "en-us": "en",
    "en-gb": "en",
    "pt-br": "pt",
    "pt-pt": "pt",
    "zh-cn": "zh",
    "zh-tw": "zh",
    "fa-ir": "fa",
    "id-id": "id",
}


BUTTON_LABELS = {
    "en": {
        "stats": "📊 Stats",
        "growth": "📈 Growth",
        "info": "ℹ️ Info",
        "language": "🌍 Language",
        "creator": "👨‍💻 Creator",
    },
    "ar": {
        "stats": "📊 الإحصائيات",
        "growth": "📈 النمو",
        "info": "ℹ️ المعلومات",
        "language": "🌍 اللغة",
        "creator": "👨‍💻 الصانع",
    },
    "ru": {
        "stats": "📊 Статистика",
        "growth": "📈 Рост",
        "info": "ℹ️ Инфо",
        "language": "🌍 Язык",
        "creator": "👨‍💻 Создатель",
    },
    "fr": {
        "stats": "📊 Statistiques",
        "growth": "📈 Croissance",
        "info": "ℹ️ Infos",
        "language": "🌍 Langue",
        "creator": "👨‍💻 Créateur",
    },
    "es": {
        "stats": "📊 Estadísticas",
        "growth": "📈 Crecimiento",
        "info": "ℹ️ Información",
        "language": "🌍 Idioma",
        "creator": "👨‍💻 Creador",
    },
    "de": {
        "stats": "📊 Statistiken",
        "growth": "📈 Wachstum",
        "info": "ℹ️ Info",
        "language": "🌍 Sprache",
        "creator": "👨‍💻 Ersteller",
    },
    "pt": {
        "stats": "📊 Estatísticas",
        "growth": "📈 Crescimento",
        "info": "ℹ️ Informações",
        "language": "🌍 Idioma",
        "creator": "👨‍💻 Criador",
    },
    "it": {
        "stats": "📊 Statistiche",
        "growth": "📈 Crescita",
        "info": "ℹ️ Info",
        "language": "🌍 Lingua",
        "creator": "👨‍💻 Creatore",
    },
    "tr": {
        "stats": "📊 İstatistikler",
        "growth": "📈 Büyüme",
        "info": "ℹ️ Bilgi",
        "language": "🌍 Dil",
        "creator": "👨‍💻 Oluşturucu",
    },
    "fa": {
        "stats": "📊 آمار",
        "growth": "📈 رشد",
        "info": "ℹ️ اطلاعات",
        "language": "🌍 زبان",
        "creator": "👨‍💻 سازنده",
    },
    "id": {
        "stats": "📊 Statistik",
        "growth": "📈 Pertumbuhan",
        "info": "ℹ️ Info",
        "language": "🌍 Bahasa",
        "creator": "👨‍💻 Pembuat",
    },
}

def menu_label(lang, key):
    lang = normalize_lang(lang)
    return BUTTON_LABELS.get(lang, BUTTON_LABELS["en"]).get(key, BUTTON_LABELS["en"][key])

LANGUAGE_OVERRIDES = {
    "fr": {
        "welcome": "👋 Bienvenue sur <b>{bot}</b> !\n\nAjoutez-moi à un groupe Telegram pour commencer à suivre les statistiques.",
        "help": "🤖 <b>Pulse Bot</b>\n\n━━━━━━━━━━━━━━\n\n📊 <b>Statistiques</b>\n• /stats — Statistiques du jour\n• /report — Rapport complet du jour\n• /yesterday — Rapport d’hier\n• /growth — Croissance par rapport à hier\n\n⚙️ <b>Paramètres</b>\n• /language — Changer la langue du bot\n\n🛡️ <b>Modération</b>\n• /warn — Avertir un membre\n• /unwarn — Retirer un avertissement\n• /warnings — Voir les avertissements\n• /mute — Rendre muet un membre\n• /unmute — Réactiver un membre\n• /ban — Bannir un membre\n• /unban — Débannir un membre\n\nℹ️ <b>Infos</b>\n• /start — Démarrer le bot\n• /help — Afficher l’aide\n• /support — Contacter le support\n• /creator — Voir le créateur\n• /info — Infos du groupe et du bot\n\n━━━━━━━━━━━━━━\n\n✨ Merci d’utiliser Pulse Bot !",
        "group_only": "⚠️ Cette commande fonctionne uniquement dans les groupes Telegram.",
        "admin_only": "⚠️ Ce paramètre peut être modifié uniquement par les administrateurs du groupe.",
        "reply_required": "⚠️ Répondez au message d’un membre pour utiliser cette commande.",
        "target_is_admin": "⚠️ Vous ne pouvez pas modérer un administrateur ou le créateur du groupe.",
        "bot_lacks_rights": "⚠️ J’ai besoin des droits d’administrateur dans ce groupe pour faire cela.",
        "bot_lacks_promote_rights": "⚠️ J’ai besoin du droit de promouvoir des membres dans ce groupe pour faire cela.",
        "warn_done": "⚠️ {user} averti. Avertissements : {count}/3.",
        "unwarn_done": "✅ Avertissement retiré pour {user}. Avertissements : {count}/3.",
        "warnings_show": "📌 {user} a {count} avertissement(s).",
        "warn_limit_hit": "🚨 {user} a atteint 3/3 avertissements et a été muté pendant 24 heures.",
        "mute_done": "🔇 {user} a été muet.",
        "unmute_done": "🔊 {user} peut de nouveau parler.",
        "ban_done": "⛔ {user} a été banni.",
        "unban_done": "✅ {user} a été débanni.",
        "stats_title": "📊 <b>Statistiques du jour sur {bot}</b>",
        "report_title": "📄 <b>Rapport quotidien</b>",
        "yesterday_title": "📅 <b>Rapport d’hier</b>",
        "growth_title": "📈 <b>Croissance du serveur</b>",
        "no_data": "Aucune statistique pour le moment.",
        "language_menu": "🌍 Choisissez une langue pour ce chat :",
        "language_set": "✅ Langue mise à jour.",
        "support_text": "⭐ <b>Soutenir {bot}</b>\n\nVous pouvez soutenir le projet avec <b>{stars} étoiles Telegram</b>.",
        "creator_text": "👨‍💻 <b>{bot}</b>\n\nCréé et maintenu par {creator}.\n\nConçu pour les communautés Telegram qui ont besoin de statistiques rapides, claires et fiables.\nChaque mise à jour améliore la gestion du groupe.",
        "daily_prompt": "🌙 <b>La mise à jour de minuit est prête.</b>\n\nVoulez-vous voir le rapport d’hier ?",
        "yes": "✅ Oui",
        "no": "❌ Non",
        "support_button": "⭐ Soutenir avec {stars} étoiles",
        "gift_button": "🎁 Lien cadeau du créateur",
        "open_report": "📄 Voir le rapport",
        "open_support": "💬 Ouvrir le support",
        "add_group": "➕ Ajouter au groupe"
    },
    "es": {
        "welcome": "👋 Bienvenido a <b>{bot}</b>!\n\nAñádeme a un grupo de Telegram para empezar a seguir las estadísticas.",
        "help": "🤖 <b>Pulse Bot</b>\n\n━━━━━━━━━━━━━━\n\n📊 <b>Estadísticas</b>\n• /stats — Estadísticas del día\n• /report — Informe completo del día\n• /yesterday — Informe de ayer\n• /growth — Crecimiento respecto a ayer\n\n⚙️ <b>Ajustes</b>\n• /language — Cambiar idioma del bot\n\n🛡️ <b>Moderación</b>\n• /warn — Advertir a un miembro\n• /unwarn — Quitar una advertencia\n• /warnings — Ver advertencias\n• /mute — Silenciar a un miembro\n• /unmute — Quitar silencio\n• /ban — Banear a un miembro\n• /unban — Desbanear a un miembro\n\nℹ️ <b>Información</b>\n• /start — Iniciar el bot\n• /help — Mostrar ayuda\n• /support — Contactar soporte\n• /creator — Ver al creador\n• /info — Info del grupo y del bot\n\n━━━━━━━━━━━━━━\n\n✨ ¡Gracias por usar Pulse Bot!",
        "group_only": "⚠️ Este comando solo funciona en grupos de Telegram.",
        "admin_only": "⚠️ Este ajuste solo puede cambiarlo un administrador del grupo.",
        "reply_required": "⚠️ Responde al mensaje de un miembro para usar este comando.",
        "target_is_admin": "⚠️ No puedes moderar a un administrador o al creador del grupo.",
        "bot_lacks_rights": "⚠️ Necesito permisos de administrador en este grupo para hacer eso.",
        "bot_lacks_promote_rights": "⚠️ Necesito permiso para promover miembros en este grupo para hacer eso.",
        "warn_done": "⚠️ {user} advertido. Advertencias: {count}/3.",
        "unwarn_done": "✅ Advertencia eliminada para {user}. Advertencias: {count}/3.",
        "warnings_show": "📌 {user} tiene {count} advertencia(s).",
        "warn_limit_hit": "🚨 {user} llegó a 3/3 advertencias y fue silenciado durante 24 horas.",
        "mute_done": "🔇 {user} fue silenciado.",
        "unmute_done": "🔊 {user} ya puede hablar.",
        "ban_done": "⛔ {user} fue baneado.",
        "unban_done": "✅ {user} fue desbaneado.",
        "stats_title": "📊 <b>Estadísticas de hoy en {bot}</b>",
        "report_title": "📄 <b>Informe diario</b>",
        "yesterday_title": "📅 <b>Informe de ayer</b>",
        "growth_title": "📈 <b>Crecimiento del servidor</b>",
        "no_data": "Todavía no hay estadísticas.",
        "language_menu": "🌍 Elige un idioma para este chat:",
        "language_set": "✅ Idioma actualizado.",
        "support_text": "⭐ <b>Apoyar a {bot}</b>\n\nPuedes apoyar el proyecto con <b>{stars} estrellas de Telegram</b>.",
        "creator_text": "👨‍💻 <b>{bot}</b>\n\nCreado y mantenido por {creator}.\n\nDiseñado para comunidades de Telegram que necesitan estadísticas rápidas, claras y fiables.\nCada actualización mejora la gestión del grupo.",
        "daily_prompt": "🌙 <b>La actualización de medianoche está lista.</b>\n\n¿Quieres ver el informe de ayer?",
        "yes": "✅ Sí",
        "no": "❌ No",
        "support_button": "⭐ Apoyar con {stars} estrellas",
        "gift_button": "🎁 Enlace regalo del creador",
        "open_report": "📄 Ver informe",
        "open_support": "💬 Abrir soporte",
        "add_group": "➕ Añadir al grupo"
    },
    "de": {
        "welcome": "👋 Willkommen bei <b>{bot}</b>!\n\nFüge mich zu einer Telegram-Gruppe hinzu, um Statistiken zu verfolgen.",
        "help": "🤖 <b>Pulse Bot</b>\n\n━━━━━━━━━━━━━━\n\n📊 <b>Statistiken</b>\n• /stats — Heutige Statistiken\n• /report — Vollständiger Tagesbericht\n• /yesterday — Bericht von gestern\n• /growth — Wachstum im Vergleich zu gestern\n\n⚙️ <b>Einstellungen</b>\n• /language — Bot-Sprache ändern\n\n🛡️ <b>Moderation</b>\n• /warn — Mitglied verwarnen\n• /unwarn — Verwarnung entfernen\n• /warnings — Verwarnungen anzeigen\n• /mute — Mitglied stummschalten\n• /unmute — Stummschaltung aufheben\n• /ban — Mitglied sperren\n• /unban — Sperre aufheben\n\nℹ️ <b>Info</b>\n• /start — Bot starten\n• /help — Hilfe anzeigen\n• /support — Support kontaktieren\n• /creator — Ersteller anzeigen\n• /info — Gruppen- und Bot-Infos\n\n━━━━━━━━━━━━━━\n\n✨ Danke, dass du Pulse Bot benutzt!",
        "group_only": "⚠️ Dieser Befehl funktioniert nur in Telegram-Gruppen.",
        "admin_only": "⚠️ Diese Einstellung kann nur von Gruppen-Admins geändert werden.",
        "reply_required": "⚠️ Antworte auf die Nachricht eines Mitglieds, um diesen Befehl zu verwenden.",
        "target_is_admin": "⚠️ Du kannst keinen Administrator oder den Ersteller der Gruppe moderieren.",
        "bot_lacks_rights": "⚠️ Ich brauche Admin-Rechte in dieser Gruppe, um das zu tun.",
        "bot_lacks_promote_rights": "⚠️ Ich brauche die Berechtigung, Mitglieder zu befördern, um das zu tun.",
        "warn_done": "⚠️ {user} verwarnt. Verwarnungen: {count}/3.",
        "unwarn_done": "✅ Verwarnung für {user} entfernt. Verwarnungen: {count}/3.",
        "warnings_show": "📌 {user} hat {count} Verwarnung(en).",
        "warn_limit_hit": "🚨 {user} hat 3/3 Verwarnungen erreicht und wurde für 24 Stunden stummgeschaltet.",
        "mute_done": "🔇 {user} wurde stummgeschaltet.",
        "unmute_done": "🔊 {user} kann wieder sprechen.",
        "ban_done": "⛔ {user} wurde gesperrt.",
        "unban_done": "✅ Sperre für {user} aufgehoben.",
        "stats_title": "📊 <b>Heutige Statistiken in {bot}</b>",
        "report_title": "📄 <b>Tagesbericht</b>",
        "yesterday_title": "📅 <b>Bericht von gestern</b>",
        "growth_title": "📈 <b>Serverwachstum</b>",
        "no_data": "Noch keine Statistiken.",
        "language_menu": "🌍 Wähle eine Sprache für diesen Chat:",
        "language_set": "✅ Sprache aktualisiert.",
        "support_text": "⭐ <b>{bot} unterstützen</b>\n\nDu kannst das Projekt mit <b>{stars} Telegram-Sternen</b> unterstützen.",
        "creator_text": "👨‍💻 <b>{bot}</b>\n\nErstellt und gepflegt von {creator}.\n\nFür Telegram-Communities entwickelt, die schnelle, klare und zuverlässige Aktivitätsstatistiken brauchen.\nJedes Update macht die Gruppenverwaltung besser.",
        "daily_prompt": "🌙 <b>Das Mitternachts-Update ist bereit.</b>\n\nMöchtest du den Bericht von gestern sehen?",
        "yes": "✅ Ja",
        "no": "❌ Nein",
        "support_button": "⭐ Mit {stars} Sternen unterstützen",
        "gift_button": "🎁 Geschenk-Link des Erstellers",
        "open_report": "📄 Bericht ansehen",
        "open_support": "💬 Support öffnen",
        "add_group": "➕ Zur Gruppe hinzufügen"
    },
    "pt": {
        "welcome": "👋 Bem-vindo ao <b>{bot}</b>!\n\nAdicione-me a um grupo do Telegram para começar a acompanhar as estatísticas.",
        "help": "🤖 <b>Pulse Bot</b>\n\n━━━━━━━━━━━━━━\n\n📊 <b>Estatísticas</b>\n• /stats — Estatísticas de hoje\n• /report — Relatório completo do dia\n• /yesterday — Relatório de ontem\n• /growth — Crescimento em relação a ontem\n\n⚙️ <b>Configurações</b>\n• /language — Alterar o idioma do bot\n\n🛡️ <b>Moderação</b>\n• /warn — Avisar um membro\n• /unwarn — Remover um aviso\n• /warnings — Ver avisos\n• /mute — Silenciar um membro\n• /unmute — Remover silêncio\n• /ban — Banir um membro\n• /unban — Desbanir um membro\n\nℹ️ <b>Informação</b>\n• /start — Iniciar o bot\n• /help — Mostrar ajuda\n• /support — Contatar suporte\n• /creator — Ver o criador\n• /info — Info do grupo e do bot\n\n━━━━━━━━━━━━━━\n\n✨ Obrigado por usar o Pulse Bot!",
        "group_only": "⚠️ Este comando funciona apenas em grupos do Telegram.",
        "admin_only": "⚠️ Esta definição só pode ser alterada por administradores do grupo.",
        "reply_required": "⚠️ Responda à mensagem de um membro para usar este comando.",
        "target_is_admin": "⚠️ Você não pode moderar um administrador ou o criador do grupo.",
        "bot_lacks_rights": "⚠️ Preciso de permissões de admin neste grupo para fazer isso.",
        "bot_lacks_promote_rights": "⚠️ Preciso de permissão para promover membros neste grupo para fazer isso.",
        "warn_done": "⚠️ {user} advertido. Advertências: {count}/3.",
        "unwarn_done": "✅ Advertência removida para {user}. Advertências: {count}/3.",
        "warnings_show": "📌 {user} tem {count} advertência(s).",
        "warn_limit_hit": "🚨 {user} atingiu 3/3 advertências e foi silenciado por 24 horas.",
        "mute_done": "🔇 {user} foi silenciado.",
        "unmute_done": "🔊 {user} pode falar novamente.",
        "ban_done": "⛔ {user} foi banido.",
        "unban_done": "✅ {user} foi desbanido.",
        "stats_title": "📊 <b>Estatísticas de hoje em {bot}</b>",
        "report_title": "📄 <b>Relatório diário</b>",
        "yesterday_title": "📅 <b>Relatório de ontem</b>",
        "growth_title": "📈 <b>Crescimento do servidor</b>",
        "no_data": "Ainda não há estatísticas.",
        "language_menu": "🌍 Escolha um idioma para este chat:",
        "language_set": "✅ Idioma atualizado.",
        "support_text": "⭐ <b>Apoiar {bot}</b>\n\nVocê pode apoiar o projeto com <b>{stars} estrelas do Telegram</b>.",
        "creator_text": "👨‍💻 <b>{bot}</b>\n\nCriado e mantido por {creator}.\n\nFeito para comunidades do Telegram que precisam de estatísticas rápidas, claras e confiáveis.\nCada atualização melhora a gestão do grupo.",
        "daily_prompt": "🌙 <b>A atualização da meia-noite está pronta.</b>\n\nQuer ver o relatório de ontem?",
        "yes": "✅ Sim",
        "no": "❌ Não",
        "support_button": "⭐ Apoiar com {stars} estrelas",
        "gift_button": "🎁 Link de presente do criador",
        "open_report": "📄 Ver relatório",
        "open_support": "💬 Abrir suporte",
        "add_group": "➕ Adicionar ao grupo"
    },
    "it": {
        "welcome": "👋 Benvenuto in <b>{bot}</b>!\n\nAggiungimi a un gruppo Telegram per iniziare a monitorare le statistiche.",
        "help": "🤖 <b>Pulse Bot</b>\n\n━━━━━━━━━━━━━━\n\n📊 <b>Statistiche</b>\n• /stats — Statistiche di oggi\n• /report — Report completo del giorno\n• /yesterday — Report di ieri\n• /growth — Crescita rispetto a ieri\n\n⚙️ <b>Impostazioni</b>\n• /language — Cambia lingua del bot\n\n🛡️ <b>Moderazione</b>\n• /warn — Avvisa un membro\n• /unwarn — Rimuovi un avviso\n• /warnings — Mostra gli avvisi\n• /mute — Silenzia un membro\n• /unmute — Riattiva un membro\n• /ban — Bannare un membro\n• /unban — Rimuovere il ban\n\nℹ️ <b>Informazioni</b>\n• /start — Avvia il bot\n• /help — Mostra l’aiuto\n• /support — Contatta il supporto\n• /creator — Vedi il creatore\n• /info — Info del gruppo e del bot\n\n━━━━━━━━━━━━━━\n\n✨ Grazie per aver usato Pulse Bot!",
        "group_only": "⚠️ Questo comando funziona solo nei gruppi Telegram.",
        "admin_only": "⚠️ Questa impostazione può essere modificata solo dagli amministratori del gruppo.",
        "reply_required": "⚠️ Rispondi al messaggio di un membro per usare questo comando.",
        "target_is_admin": "⚠️ Non puoi moderare un amministratore o il creatore del gruppo.",
        "bot_lacks_rights": "⚠️ Mi servono i permessi di amministratore in questo gruppo per farlo.",
        "bot_lacks_promote_rights": "⚠️ Mi serve il permesso di promuovere membri in questo gruppo per farlo.",
        "warn_done": "⚠️ {user} avvisato. Avvisi: {count}/3.",
        "unwarn_done": "✅ Avviso rimosso per {user}. Avvisi: {count}/3.",
        "warnings_show": "📌 {user} ha {count} avviso/i.",
        "warn_limit_hit": "🚨 {user} ha raggiunto 3/3 avvisi ed è stato silenziato per 24 ore.",
        "mute_done": "🔇 {user} è stato silenziato.",
        "unmute_done": "🔊 {user} può parlare di nuovo.",
        "ban_done": "⛔ {user} è stato bannato.",
        "unban_done": "✅ Ban rimosso per {user}.",
        "stats_title": "📊 <b>Statistiche di oggi in {bot}</b>",
        "report_title": "📄 <b>Report giornaliero</b>",
        "yesterday_title": "📅 <b>Report di ieri</b>",
        "growth_title": "📈 <b>Crescita del server</b>",
        "no_data": "Nessuna statistica per ora.",
        "language_menu": "🌍 Scegli una lingua per questa chat:",
        "language_set": "✅ Lingua aggiornata.",
        "support_text": "⭐ <b>Supporta {bot}</b>\n\nPuoi sostenere il progetto con <b>{stars} stelle Telegram</b>.",
        "creator_text": "👨‍💻 <b>{bot}</b>\n\nCreato e mantenuto da {creator}.\n\nPensato per le community Telegram che hanno bisogno di statistiche rapide, chiare e affidabili.\nOgni aggiornamento rende la gestione del gruppo più semplice.",
        "daily_prompt": "🌙 <b>L’aggiornamento di mezzanotte è pronto.</b>\n\nVuoi vedere il report di ieri?",
        "yes": "✅ Sì",
        "no": "❌ No",
        "support_button": "⭐ Supporta con {stars} stelle",
        "gift_button": "🎁 Link regalo del creatore",
        "open_report": "📄 Vedi report",
        "open_support": "💬 Apri supporto",
        "add_group": "➕ Aggiungi al gruppo"
    },
    "tr": {
        "welcome": "👋 <b>{bot}</b> botuna hoş geldin!\n\nİstatistikleri takip etmeye başlamak için beni bir Telegram grubuna ekle.",
        "help": "🤖 <b>Pulse Bot</b>\n\n━━━━━━━━━━━━━━\n\n📊 <b>İstatistikler</b>\n• /stats — Bugünün istatistikleri\n• /report — Günün tam raporu\n• /yesterday — Dünün raporu\n• /growth — Düne göre büyüme\n\n⚙️ <b>Ayarlar</b>\n• /language — Bot dilini değiştir\n\n🛡️ <b>Moderasyon</b>\n• /warn — Üyeyi uyar\n• /unwarn — Uyarıyı kaldır\n• /warnings — Uyarıları göster\n• /mute — Üyeyi sustur\n• /unmute — Sustuğu kaldır\n• /ban — Üyeyi yasakla\n• /unban — Yasağı kaldır\n\nℹ️ <b>Bilgi</b>\n• /start — Botu başlat\n• /help — Yardımı göster\n• /support — Destekle iletişim\n• /creator — Oluşturucuyu göster\n• /info — Grup ve bot bilgileri\n\n━━━━━━━━━━━━━━\n\n✨ Pulse Bot’u kullandığın için teşekkürler!",
        "group_only": "⚠️ Bu komut yalnızca Telegram gruplarında çalışır.",
        "admin_only": "⚠️ Bu ayar yalnızca grup yöneticileri tarafından değiştirilebilir.",
        "reply_required": "⚠️ Bu komutu kullanmak için bir üyenin mesajına cevap ver.",
        "target_is_admin": "⚠️ Bir yöneticiyi veya grup sahibini yönetemezsin.",
        "bot_lacks_rights": "⚠️ Bunu yapmak için bu grupta yönetici yetkilerine ihtiyacım var.",
        "bot_lacks_promote_rights": "⚠️ Bunu yapmak için üye terfi ettirme yetkisine ihtiyacım var.",
        "warn_done": "⚠️ {user} uyarıldı. Uyarılar: {count}/3.",
        "unwarn_done": "✅ {user} için uyarı kaldırıldı. Uyarılar: {count}/3.",
        "warnings_show": "📌 {user} için {count} uyarı var.",
        "warn_limit_hit": "🚨 {user} 3/3 uyarıya ulaştı ve 24 saat susturuldu.",
        "mute_done": "🔇 {user} susturuldu.",
        "unmute_done": "🔊 {user} tekrar konuşabilir.",
        "ban_done": "⛔ {user} yasaklandı.",
        "unban_done": "✅ {user} yasağı kaldırıldı.",
        "stats_title": "📊 <b>{bot} için bugünün istatistikleri</b>",
        "report_title": "📄 <b>Günlük rapor</b>",
        "yesterday_title": "📅 <b>Dünün raporu</b>",
        "growth_title": "📈 <b>Sunucu büyümesi</b>",
        "no_data": "Henüz istatistik yok.",
        "language_menu": "🌍 Bu sohbet için bir dil seç:",
        "language_set": "✅ Dil güncellendi.",
        "support_text": "⭐ <b>{bot} destekle</b>\n\nProjeyi <b>{stars} Telegram yıldızı</b> ile destekleyebilirsin.",
        "creator_text": "👨‍💻 <b>{bot}</b>\n\n{creator} tarafından oluşturuldu ve sürdürülüyor.\n\nTelegram toplulukları için hızlı, net ve güvenilir istatistikler sunar.\nHer güncelleme grup yönetimini daha kolay hale getirir.",
        "daily_prompt": "🌙 <b>Gece yarısı güncellemesi hazır.</b>\n\nDünün raporunu görmek ister misin?",
        "yes": "✅ Evet",
        "no": "❌ Hayır",
        "support_button": "⭐ {stars} yıldızla destekle",
        "gift_button": "🎁 Oluşturucunun hediye bağlantısı",
        "open_report": "📄 Raporu gör",
        "open_support": "💬 Desteği aç",
        "add_group": "➕ Gruba ekle"
    },
    "fa": {
        "welcome": "👋 به <b>{bot}</b> خوش آمدی!\n\nبرای شروع آمارگیری مرا به یک گروه تلگرام اضافه کن.",
        "help": "🤖 <b>Pulse Bot</b>\n\n━━━━━━━━━━━━━━\n\n📊 <b>آمار</b>\n• /stats — آمار امروز\n• /report — گزارش کامل امروز\n• /yesterday — گزارش دیروز\n• /growth — رشد نسبت به دیروز\n\n⚙️ <b>تنظیمات</b>\n• /language — تغییر زبان ربات\n\n🛡️ <b>مدیریت</b>\n• /warn — هشدار به عضو\n• /unwarn — حذف یک هشدار\n• /warnings — نمایش هشدارها\n• /mute — ساکت کردن عضو\n• /unmute — رفع سکوت\n• /ban — بن کردن عضو\n• /unban — رفع بن\n\nℹ️ <b>اطلاعات</b>\n• /start — شروع ربات\n• /help — نمایش راهنما\n• /support — ارتباط با پشتیبانی\n• /creator — سازنده ربات\n• /info — اطلاعات گروه و ربات\n\n━━━━━━━━━━━━━━\n\n✨ ممنون که از Pulse Bot استفاده می‌کنی!",
        "group_only": "⚠️ این دستور فقط در گروه‌های تلگرام کار می‌کند.",
        "admin_only": "⚠️ این تنظیم فقط توسط مدیران گروه قابل تغییر است.",
        "reply_required": "⚠️ برای استفاده از این دستور به پیام یک عضو پاسخ بده.",
        "target_is_admin": "⚠️ نمی‌توانی روی مدیر یا سازنده گروه این کار را انجام دهی.",
        "bot_lacks_rights": "⚠️ برای این کار به دسترسی مدیر در این گروه نیاز دارم.",
        "bot_lacks_promote_rights": "⚠️ برای این کار به اجازه ارتقای اعضا نیاز دارم.",
        "warn_done": "⚠️ به {user} هشدار داده شد. هشدارها: {count}/3.",
        "unwarn_done": "✅ یک هشدار از {user} حذف شد. هشدارها: {count}/3.",
        "warnings_show": "📌 {user} {count} هشدار دارد.",
        "warn_limit_hit": "🚨 {user} به 3/3 هشدار رسید و 24 ساعت ساکت شد.",
        "mute_done": "🔇 {user} ساکت شد.",
        "unmute_done": "🔊 {user} دوباره می‌تواند صحبت کند.",
        "ban_done": "⛔ {user} بن شد.",
        "unban_done": "✅ بن {user} برداشته شد.",
        "stats_title": "📊 <b>آمار امروز در {bot}</b>",
        "report_title": "📄 <b>گزارش روزانه</b>",
        "yesterday_title": "📅 <b>گزارش دیروز</b>",
        "growth_title": "📈 <b>رشد سرور</b>",
        "no_data": "هنوز آماری وجود ندارد.",
        "language_menu": "🌍 یک زبان برای این گفتگو انتخاب کن:",
        "language_set": "✅ زبان به‌روزرسانی شد.",
        "support_text": "⭐ <b>پشتیبانی از {bot}</b>\n\nمی‌توانی پروژه را با <b>{stars} ستاره تلگرام</b> پشتیبانی کنی.",
        "creator_text": "👨‍💻 <b>{bot}</b>\n\nساخته و نگهداری شده توسط {creator}.\n\nبرای جوامع تلگرامی که به آمار سریع، واضح و قابل اعتماد نیاز دارند ساخته شده است.\nهر به‌روزرسانی مدیریت گروه را بهتر می‌کند.",
        "daily_prompt": "🌙 <b>به‌روزرسانی نیمه‌شب آماده است.</b>\n\nآیا می‌خواهی گزارش دیروز را ببینی؟",
        "yes": "✅ بله",
        "no": "❌ نه",
        "support_button": "⭐ پشتیبانی با {stars} ستاره",
        "gift_button": "🎁 لینک هدیه سازنده",
        "open_report": "📄 مشاهده گزارش",
        "open_support": "💬 باز کردن پشتیبانی",
        "add_group": "➕ افزودن به گروه"
    },
    "id": {
        "welcome": "👋 Selamat datang di <b>{bot}</b>!\n\nTambahkan saya ke grup Telegram untuk mulai melacak statistik.",
        "help": "🤖 <b>Pulse Bot</b>\n\n━━━━━━━━━━━━━━\n\n📊 <b>Statistik</b>\n• /stats — Statistik hari ini\n• /report — Laporan lengkap hari ini\n• /yesterday — Laporan kemarin\n• /growth — Pertumbuhan dibanding kemarin\n\n⚙️ <b>Pengaturan</b>\n• /language — Ubah bahasa bot\n\n🛡️ <b>Moderasi</b>\n• /warn — Peringatkan anggota\n• /unwarn — Hapus peringatan\n• /warnings — Tampilkan peringatan\n• /mute — Bisukan anggota\n• /unmute — Buka bisu\n• /ban — Ban anggota\n• /unban — Buka ban\n\nℹ️ <b>Info</b>\n• /start — Mulai bot\n• /help — Tampilkan bantuan\n• /support — Hubungi dukungan\n• /creator — Lihat pembuat\n• /info — Info grup & bot\n\n━━━━━━━━━━━━━━\n\n✨ Terima kasih telah menggunakan Pulse Bot!",
        "group_only": "⚠️ Perintah ini hanya berfungsi di grup Telegram.",
        "admin_only": "⚠️ Pengaturan ini hanya bisa diubah oleh admin grup.",
        "reply_required": "⚠️ Balas pesan anggota untuk menggunakan perintah ini.",
        "target_is_admin": "⚠️ Kamu tidak bisa memoderasi admin atau pembuat grup.",
        "bot_lacks_rights": "⚠️ Saya butuh hak admin di grup ini untuk melakukan itu.",
        "bot_lacks_promote_rights": "⚠️ Saya butuh izin untuk mempromosikan anggota di grup ini.",
        "warn_done": "⚠️ {user} diperingatkan. Peringatan: {count}/3.",
        "unwarn_done": "✅ Peringatan untuk {user} dihapus. Peringatan: {count}/3.",
        "warnings_show": "📌 {user} memiliki {count} peringatan.",
        "warn_limit_hit": "🚨 {user} mencapai 3/3 peringatan dan dibisukan selama 24 jam.",
        "mute_done": "🔇 {user} dibisukan.",
        "unmute_done": "🔊 {user} bisa berbicara lagi.",
        "ban_done": "⛔ {user} diban.",
        "unban_done": "✅ Ban {user} dibuka.",
        "stats_title": "📊 <b>Statistik hari ini di {bot}</b>",
        "report_title": "📄 <b>Laporan harian</b>",
        "yesterday_title": "📅 <b>Laporan kemarin</b>",
        "growth_title": "📈 <b>Pertumbuhan server</b>",
        "no_data": "Belum ada statistik.",
        "language_menu": "🌍 Pilih bahasa untuk chat ini:",
        "language_set": "✅ Bahasa diperbarui.",
        "support_text": "⭐ <b>Dukung {bot}</b>\n\nKamu bisa mendukung proyek ini dengan <b>{stars} bintang Telegram</b>.",
        "creator_text": "👨‍💻 <b>{bot}</b>\n\nDibuat dan dikelola oleh {creator}.\n\nDibuat untuk komunitas Telegram yang membutuhkan statistik cepat, jelas, dan andal.\nSetiap pembaruan membuat pengelolaan grup lebih baik.",
        "daily_prompt": "🌙 <b>Pembaruan tengah malam siap.</b>\n\nIngin melihat laporan kemarin?",
        "yes": "✅ Ya",
        "no": "❌ Tidak",
        "support_button": "⭐ Dukung dengan {stars} bintang",
        "gift_button": "🎁 Tautan hadiah pembuat",
        "open_report": "📄 Lihat laporan",
        "open_support": "💬 Buka dukungan",
        "add_group": "➕ Tambahkan ke grup"
    }
,
    "ja": {
        "language_menu": "🌍 このチャットの言語を選んでください：",
        "language_set": "✅ 言語を更新しました。",
        "support_button": "⭐ {stars} スターでサポート",
        "gift_button": "🎁 作成者のギフトリンク",
        "open_report": "📄 レポートを見る",
        "open_support": "💬 サポートを開く",
        "add_group": "➕ グループに追加",
        "yes": "✅ はい",
        "no": "❌ いいえ",
    },
    "ko": {
        "language_menu": "🌍 이 채팅의 언어를 선택하세요:",
        "language_set": "✅ 언어가 업데이트되었습니다.",
        "support_button": "⭐ {stars}개 별로 지원",
        "gift_button": "🎁 제작자 선물 링크",
        "open_report": "📄 보고서 보기",
        "open_support": "💬 지원 열기",
        "add_group": "➕ 그룹에 추가",
        "yes": "✅ 예",
        "no": "❌ 아니요",
    },
    "zh": {
        "language_menu": "🌍 请选择此聊天的语言：",
        "language_set": "✅ 语言已更新。",
        "support_button": "⭐ 用 {stars} 颗星支持",
        "gift_button": "🎁 创建者礼物链接",
        "open_report": "📄 查看报告",
        "open_support": "💬 打开支持",
        "add_group": "➕ 添加到群组",
        "yes": "✅ 是",
        "no": "❌ 否",
    }
}

LANGUAGE_BUTTONS = [('🇬🇧 English', 'en'), ('🇸🇦 العربية', 'ar'), ('🇷🇺 Русский', 'ru'), ('🇫🇷 Français', 'fr'), ('🇪🇸 Español', 'es'), ('🇩🇪 Deutsch', 'de'), ('🇵🇹 Português', 'pt'), ('🇮🇹 Italiano', 'it'), ('🇹🇷 Türkçe', 'tr'), ('🇮🇷 فارسی', 'fa'), ('🇮🇩 Bahasa Indonesia', 'id'), ('🇯🇵 日本語', 'ja'), ('🇰🇷 한국어', 'ko'), ('🇨🇳 中文', 'zh')]
LANGUAGE_CODES = {code for _, code in LANGUAGE_BUTTONS}
LANGUAGE_ALIASES = {
    "en-us": "en",
    "en-gb": "en",
    "pt-br": "pt",
    "pt-pt": "pt",
    "zh-cn": "zh",
    "zh-hans": "zh",
    "zh-hant": "zh",
    "zh-tw": "zh",
    "fa-ir": "fa",
    "id-id": "id",
    "ja-jp": "ja",
    "ko-kr": "ko",
}

def normalize_lang(lang):
    lang = (lang or "en").strip().lower().replace("-", "_")
    if lang in LANGUAGE_CODES:
        return lang
    alias = LANGUAGE_ALIASES.get(lang.replace("_", "-"))
    if alias in LANGUAGE_CODES:
        return alias
    if "_" in lang:
        base = lang.split("_", 1)[0]
        if base in LANGUAGE_CODES:
            return base
    return "en"

def is_group(chat_type):
    return chat_type in ("group", "supergroup")

def t(lang, key, **kwargs):
    lang = normalize_lang(lang)
    base = TEXTS.get("en", {})
    text = (LANGUAGE_OVERRIDES.get(lang, {}).get(key)
            or TEXTS.get(lang, {}).get(key)
            or base.get(key, key))
    return text.format(bot=BOT_NAME, creator=CREATOR_NAME, support=SUPPORT_USERNAME, stars=STAR_SUPPORT_AMOUNT, version=BOT_VERSION, **kwargs)

def api_url(method):
    return f"https://api.telegram.org/bot{TOKEN}/{method}"

def tg(method, payload=None, files=None):
    try:
        if files:
            response = SESSION.post(api_url(method), data=payload or {}, files=files, timeout=20)
        else:
            response = SESSION.post(api_url(method), json=payload or {}, timeout=20)
        try:
            data = response.json()
        except ValueError:
            return {"ok": False, "description": response.text[:400], "status_code": response.status_code}
        if not response.ok and "ok" not in data:
            data["ok"] = False
            data["status_code"] = response.status_code
        return data
    except requests.RequestException as exc:
        LOG.exception("Telegram request failed (%s): %s", method, exc)
        return {"ok": False, "description": str(exc)}
def send_message(chat_id, text, reply_markup=None, parse_mode="HTML", disable_web_page_preview=True):
    payload = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": parse_mode,
        "disable_web_page_preview": disable_web_page_preview,
    }
    if reply_markup:
        payload["reply_markup"] = reply_markup
    return tg("sendMessage", payload)

def edit_message_text(chat_id, message_id, text, reply_markup=None, parse_mode="HTML"):
    payload = {
        "chat_id": chat_id,
        "message_id": message_id,
        "text": text,
        "parse_mode": parse_mode,
        "disable_web_page_preview": True,
    }
    if reply_markup:
        payload["reply_markup"] = reply_markup
    return tg("editMessageText", payload)

def answer_callback(callback_query_id, text=None, show_alert=False):
    payload = {"callback_query_id": callback_query_id}
    if text is not None:
        payload["text"] = text
    if show_alert:
        payload["show_alert"] = True
    return tg("answerCallbackQuery", payload)

def answer_pre_checkout(pre_checkout_query_id, ok=True, error_message=None):
    payload = {"pre_checkout_query_id": pre_checkout_query_id, "ok": ok}
    if error_message:
        payload["error_message"] = error_message
    return tg("answerPreCheckoutQuery", payload)

def send_invoice(chat_id, title, description, payload_value, stars_amount):
    payload = {
        "chat_id": chat_id,
        "title": title[:32],
        "description": description[:255],
        "payload": payload_value[:128],
        "currency": "XTR",
        "prices": [{"label": title[:32], "amount": stars_amount}],
    }
    return tg("sendInvoice", payload)

def make_button(text, callback_data=None, url=None):
    btn = {"text": text}
    if callback_data is not None:
        btn["callback_data"] = callback_data
    if url is not None:
        btn["url"] = url
    return btn

def inline_keyboard(rows):
    return {"inline_keyboard": rows}

def sanitize_text(value):
    return re.sub(r"\s+", " ", (value or "").strip())

def split_words(text):
    return re.findall(r"[\w\u0600-\u06FF']+", text.lower())

def extract_topics(texts, lang):
    stopwords = STOPWORDS_RU if lang == "ru" else (STOPWORDS_AR if lang == "ar" else STOPWORDS_EN)
    words = []
    for text in texts:
        for token in split_words(text):
            if len(token) < 3 or token in stopwords or token.isdigit():
                continue
            words.append(token)
    if not words:
        return []
    return [w for w, _ in Counter(words).most_common(5)]

def display_user_name(row):
    if row is None:
        return "Unknown"
    username = row["username"] if row["username"] else None
    first_name = row["first_name"] if row["first_name"] else None
    return f"@{username}" if username else (first_name or "Unknown")

def get_reply_target(message):
    reply = message.get("reply_to_message") or {}
    target = reply.get("from") or {}
    return target if target.get("id") else None

def get_command(text):
    if not text.startswith("/"):
        return ""
    return text.split()[0].split("@")[0].lower()

def bot_has_restrict_rights(chat_id):
    me = tg("getChatMember", {"chat_id": chat_id, "user_id": int(str(TOKEN).split(":")[0]) if False else None})
    return False

def bot_can_moderate(chat_id):
    result = tg("getChatMember", {"chat_id": chat_id, "user_id": int(tg("getMe", {}).get("result", {}).get("id", 0))})
    if not result or not result.get("ok"):
        return False
    status = result.get("result", {}).get("status")
    return status in ("creator", "administrator") and bool(result.get("result", {}).get("can_restrict_members", False))

def is_chat_admin(chat_id, user_id):
    if not chat_id or not user_id:
        return False
    result = tg("getChatMember", {"chat_id": chat_id, "user_id": user_id})
    if not result or not result.get("ok"):
        return False
    return result.get("result", {}).get("status") in ("creator", "administrator")

def get_member_object(chat_id, user_id):
    result = tg("getChatMember", {"chat_id": chat_id, "user_id": user_id})
    return result.get("result") if result and result.get("ok") else None

def bot_can_restrict(chat_id):
    me = tg("getMe", {})
    bot_id = me.get("result", {}).get("id")
    if not bot_id:
        return False
    result = tg("getChatMember", {"chat_id": chat_id, "user_id": bot_id})
    if not result or not result.get("ok"):
        return False
    obj = result.get("result", {})
    return obj.get("status") in ("creator", "administrator") and bool(obj.get("can_restrict_members", False))

def bot_can_promote(chat_id):
    me = tg("getMe", {})
    bot_id = me.get("result", {}).get("id")
    if not bot_id:
        return False
    result = tg("getChatMember", {"chat_id": chat_id, "user_id": bot_id})
    if not result or not result.get("ok"):
        return False
    obj = result.get("result", {})
    return obj.get("status") in ("creator", "administrator") and bool(obj.get("can_promote_members", False))


def build_main_keyboard(lang):
    group_url = f"https://t.me/{BOT_USERNAME}?startgroup=true" if BOT_USERNAME and not BOT_USERNAME.startswith("PUT_") else None
    rows = [
        [make_button(t(lang, "open_report"), callback_data="show:report"), make_button(t(lang, "open_support"), callback_data="show:support")],
        [make_button(menu_label(lang, "stats"), callback_data="show:stats"), make_button(menu_label(lang, "growth"), callback_data="show:growth")],
        [make_button(menu_label(lang, "info"), callback_data="show:info"), make_button(menu_label(lang, "language"), callback_data="show:language")],
        [make_button(menu_label(lang, "creator"), callback_data="show:creator")],
    ]
    if group_url:
        rows.insert(0, [make_button(t(lang, "add_group"), url=group_url)])
    return inline_keyboard(rows)


def build_language_keyboard():
    rows = []
    current = []
    for label, code in LANGUAGE_BUTTONS:
        current.append(make_button(label, callback_data=f"setlang:{code}"))
        if len(current) == 3:
            rows.append(current)
            current = []
    if current:
        rows.append(current)
    return inline_keyboard(rows)

def build_support_keyboard(lang):
    rows = [[make_button(t(lang, "support_button"), callback_data="support:stars")]]
    if CREATOR_GIFT_URL:
        rows.append([make_button(t(lang, "gift_button"), url=CREATOR_GIFT_URL)])
    return inline_keyboard(rows)

def build_daily_keyboard(lang, date_str):
    return inline_keyboard([[make_button(t(lang, "yes"), callback_data=f"daily:yes:{date_str}"), make_button(t(lang, "no"), callback_data=f"daily:no:{date_str}")]])

def build_moderation_keyboard(lang, action, chat_id, user_id, warning_count=None):
    rows = []
    if action == "warn":
        rows.append([
            make_button("↩️ Unwarn", callback_data=f"mod:unwarn:{chat_id}:{user_id}"),
            make_button("🔇 Mute 24h", callback_data=f"mod:mute:{chat_id}:{user_id}"),
        ])
        rows.append([
            make_button("📌 Warnings", callback_data=f"mod:warnings:{chat_id}:{user_id}"),
            make_button("⛔ Ban", callback_data=f"mod:ban:{chat_id}:{user_id}"),
        ])
    elif action == "mute":
        rows.append([
            make_button("🔊 Unmute", callback_data=f"mod:unmute:{chat_id}:{user_id}"),
            make_button("⛔ Ban", callback_data=f"mod:ban:{chat_id}:{user_id}"),
        ])
        rows.append([
            make_button("📌 Warnings", callback_data=f"mod:warnings:{chat_id}:{user_id}"),
            make_button("⚠️ Warn", callback_data=f"mod:warn:{chat_id}:{user_id}"),
        ])
    elif action == "ban":
        rows.append([
            make_button("✅ Unban", callback_data=f"mod:unban:{chat_id}:{user_id}"),
            make_button("⚠️ Warn", callback_data=f"mod:warn:{chat_id}:{user_id}"),
        ])
        rows.append([
            make_button("📌 Warnings", callback_data=f"mod:warnings:{chat_id}:{user_id}"),
            make_button("🔇 Mute", callback_data=f"mod:mute:{chat_id}:{user_id}"),
        ])
    elif action == "unwarn":
        rows.append([
            make_button("⚠️ Warn", callback_data=f"mod:warn:{chat_id}:{user_id}"),
            make_button("📌 Warnings", callback_data=f"mod:warnings:{chat_id}:{user_id}"),
        ])
    elif action == "unmute":
        rows.append([
            make_button("🔇 Mute", callback_data=f"mod:mute:{chat_id}:{user_id}"),
            make_button("⛔ Ban", callback_data=f"mod:ban:{chat_id}:{user_id}"),
        ])
    elif action == "unban":
        rows.append([
            make_button("⛔ Ban", callback_data=f"mod:ban:{chat_id}:{user_id}"),
            make_button("📌 Warnings", callback_data=f"mod:warnings:{chat_id}:{user_id}"),
        ])
    elif action == "warnings":
        rows.append([
            make_button("⚠️ Warn", callback_data=f"mod:warn:{chat_id}:{user_id}"),
            make_button("🔇 Mute", callback_data=f"mod:mute:{chat_id}:{user_id}"),
        ])
        rows.append([
            make_button("⛔ Ban", callback_data=f"mod:ban:{chat_id}:{user_id}"),
            make_button("📌 Warnings", callback_data=f"mod:warnings:{chat_id}:{user_id}"),
        ])
    return inline_keyboard(rows)

def action_result_text(lang, action, user, count=None):
    if action == "warn":
        return t(lang, "warn_done", user=user, count=count or 0)
    if action == "unwarn":
        return t(lang, "unwarn_done", user=user, count=count or 0)
    if action == "warnings":
        return t(lang, "warnings_show", user=user, count=count or 0)
    if action == "mute":
        return t(lang, "mute_done", user=user)
    if action == "unmute":
        return t(lang, "unmute_done", user=user)
    if action == "ban":
        return t(lang, "ban_done", user=user)
    if action == "unban":
        return t(lang, "unban_done", user=user)
    return ""

def activity_vibe(messages):
    if messages >= 2000:
        return "🔥 Explosive"
    if messages >= 800:
        return "🚀 Very active"
    if messages >= 250:
        return "✨ Active"
    if messages >= 80:
        return "🌙 Calm but alive"
    return "💤 Quiet"

def report_text(chat_id, date_str, lang):
    labels = TEXTS[normalize_lang(lang)]
    stats = get_daily_stats(chat_id, date_str)
    messages = stats["messages"] if stats else 0
    active_users = stats["active_users"] if stats else 0
    joined = stats["joined"] if stats else 0
    left = stats["left_count"] if stats else 0
    net = joined - left

    prev = get_previous_message_count(chat_id, date_str)
    delta = messages - prev
    trend = "N/A" if prev == 0 else f"{'+' if delta >= 0 else ''}{round((delta / prev) * 100)}%"

    avg_messages = round(messages / active_users, 1) if active_users else 0

    top = top_members(chat_id, date_str, 5)
    peak = peak_hour(chat_id, date_str)
    replied = most_replied_message(chat_id, date_str)
    reacted = most_reacted_message(chat_id, date_str)
    topics = extract_topics(topic_candidates(chat_id, date_str), lang)

    lines = [
        t(lang, "report_title") if date_str != utc_today() else t(lang, "stats_title"),
        "",
        f"🗓 <b>{date_str}</b>",
        "",
        f"💬 <b>{labels['messages']}:</b> {messages}",
        f"👥 <b>{labels['active_members']}:</b> {active_users}",
        f"🆕 <b>{labels['joined']}:</b> {joined}",
        f"🚪 <b>{labels['left']}:</b> {left}",
        f"📈 <b>{labels['net_growth']}:</b> {net:+d}",
        f"📊 <b>{labels['change_vs_prev']}:</b> {trend}",
        f"✨ <b>Average per active member:</b> {avg_messages}",
        f"🎭 <b>Vibe:</b> {activity_vibe(messages)}",
        "",
    ]

    if top:
        lines.append(f"🏆 <b>{labels['top_member']}:</b> {display_user_name(top[0])} ({top[0]['messages']})")
    else:
        lines.append(f"🏆 <b>{labels['top_member']}:</b> {t(lang,'no_data')}")

    if peak:
        lines.append(f"⏰ <b>{labels['peak_hour']}:</b> {peak['hour_bucket']}:00 UTC ({peak['count']} msgs)")
    else:
        lines.append(f"⏰ <b>{labels['peak_hour']}:</b> N/A")

    if replied:
        lines.append(f"💬 <b>{labels['most_replied']}:</b> {display_user_name(replied)} — {replied['replies_count']} replies")
    else:
        lines.append(f"💬 <b>{labels['most_replied']}:</b> N/A")

    if reacted:
        lines.append(f"❤️ <b>{labels['most_reacted']}:</b> {display_user_name(reacted)} — {reacted['reactions_count']} reactions")
    else:
        lines.append(f"❤️ <b>{labels['most_reacted']}:</b> N/A")

    if topics:
        lines.append("")
        lines.append("🔥 <b>Top topics:</b> " + ", ".join(f"#{x}" for x in topics))

    if top:
        lines.append("")
        lines.append("🥇 <b>Top members:</b>")
        for idx, row in enumerate(top[:5], start=1):
            lines.append(f"{idx}. {display_user_name(row)} — {row['messages']}")

    return "\n".join(lines)

def growth_text(chat_id, date_str, lang):

    labels = TEXTS[normalize_lang(lang)]
    # compare date_str vs previous day
    today_stats = get_daily_stats(chat_id, date_str)
    prev_date = (datetime.strptime(date_str, "%Y-%m-%d").date() - timedelta(days=1)).isoformat()
    prev_stats = get_daily_stats(chat_id, prev_date)

    joined = today_stats["joined"] if today_stats else 0
    left = today_stats["left_count"] if today_stats else 0
    net = joined - left
    prev_net = (prev_stats["joined"] - prev_stats["left_count"]) if prev_stats else 0
    delta = net - prev_net

    messages = today_stats["messages"] if today_stats else 0
    prev_messages = prev_stats["messages"] if prev_stats else 0

    lines = [
        t(lang, "growth_title"),
        "",
        f"🗓 <b>{date_str}</b>",
        "",
        f"🆕 <b>{labels['joined']}:</b> {joined}",
        f"🚪 <b>{labels['left']}:</b> {left}",
        f"📈 <b>{labels['net_growth']}:</b> {net:+d}",
        f"⚖️ <b>Vs previous day:</b> {delta:+d}",
        f"💬 <b>{labels['messages']}:</b> {messages} (previous: {prev_messages})",
    ]
    return "\n".join(lines)


def build_info_text(chat_id, lang):
    lang = normalize_lang(lang)
    labels = {
        "en": {
            "title": "ℹ️ <b>Server & Bot Info</b>",
            "bot": "Bot",
            "version": "Version",
            "current_chat": "Current chat",
            "chat_language": "Chat language",
            "tracked_users": "Tracked users",
            "daily_report": "Daily report",
            "today_messages": "Today's messages",
            "joined": "Joined today",
            "left": "Left today",
            "net_growth": "Net growth",
            "current_members": "Current members",
            "admins": "Admins",
            "bot_admins": "Bot admins",
            "peak_hour": "Peak hour",
            "top_member": "Top member",
            "support": "Support",
            "creator": "Creator",
            "enabled": "Enabled",
            "disabled": "Disabled",
            "vibe": "Vibe",
        },
        "ar": {
            "title": "ℹ️ <b>معلومات السيرفر والبوت</b>",
            "bot": "البوت",
            "version": "الإصدار",
            "current_chat": "المحادثة الحالية",
            "chat_language": "لغة المحادثة",
            "tracked_users": "المستخدمون المتتبعون",
            "daily_report": "التقرير اليومي",
            "today_messages": "رسائل اليوم",
            "joined": "المنضمين اليوم",
            "left": "المغادرين اليوم",
            "net_growth": "النمو الصافي",
            "current_members": "الأعضاء الحاليون",
            "admins": "المشرفون",
            "bot_admins": "أدمنية البوت",
            "peak_hour": "ساعة الذروة",
            "top_member": "أكثر عضو نشاطًا",
            "support": "الدعم",
            "creator": "الصانع",
            "enabled": "مفعّل",
            "disabled": "غير مفعّل",
            "vibe": "المزاج",
        },
        "ru": {
            "title": "ℹ️ <b>Информация о сервере и боте</b>",
            "bot": "Бот",
            "version": "Версия",
            "current_chat": "Текущий чат",
            "chat_language": "Язык чата",
            "tracked_users": "Отслеживаемые пользователи",
            "daily_report": "Ежедневный отчёт",
            "today_messages": "Сообщений сегодня",
            "joined": "Присоединились сегодня",
            "left": "Покинули сегодня",
            "net_growth": "Чистый рост",
            "current_members": "Текущие участники",
            "admins": "Администраторы",
            "bot_admins": "Админы-боты",
            "peak_hour": "Час пиковой активности",
            "top_member": "Самый активный участник",
            "support": "Поддержка",
            "creator": "Создатель",
            "enabled": "Включено",
            "disabled": "Выключено",
            "vibe": "Настроение",
        },
    }[lang]

    chat = get_chat(chat_id)
    stats = get_daily_stats(chat_id, utc_today())
    joined = get_joined_count(chat_id, utc_today())
    left = get_left_count(chat_id, utc_today())
    net = get_net_growth(chat_id, utc_today())
    messages = stats["messages"] if stats else 0

    chat_title = chat["title"] if chat and chat["title"] else "Private chat"
    chat_lang = normalize_lang(chat["language"]) if chat and chat["language"] else lang
    report_enabled = labels["enabled"] if (chat and int(chat["daily_report_enabled"])) else labels["disabled"]
    total_users = count_tracked_users()
    vibe = activity_vibe(messages)

    member_count = None
    admin_count = None
    bot_admin_count = None
    member_result = tg("getChatMemberCount", {"chat_id": chat_id})
    if member_result and member_result.get("ok"):
        try:
            member_count = int(member_result.get("result", 0))
        except (TypeError, ValueError):
            member_count = None

    admins_result = tg("getChatAdministrators", {"chat_id": chat_id})
    if admins_result and admins_result.get("ok"):
        admins = admins_result.get("result", [])
        admin_count = len(admins)
        bot_admin_count = sum(1 for a in admins if a.get("user", {}).get("is_bot"))

    peak = peak_hour(chat_id, utc_today())
    top = top_members(chat_id, utc_today(), 1)

    lines = [
        labels["title"],
        "",
        f"🤖 <b>{labels['bot']}:</b> {BOT_NAME}",
        f"🚀 <b>{labels['version']}:</b> {BOT_VERSION}",
        f"💬 <b>{labels['current_chat']}:</b> {escape(chat_title)}",
        f"🌍 <b>{labels['chat_language']}:</b> {chat_lang.upper()}",
        "",
        f"👥 <b>{labels['tracked_users']}:</b> {total_users}",
        f"📄 <b>{labels['daily_report']}:</b> {report_enabled}",
        f"💬 <b>{labels['today_messages']}:</b> {messages}",
        f"🆕 <b>{labels['joined']}:</b> {joined}",
        f"🚪 <b>{labels['left']}:</b> {left}",
        f"📈 <b>{labels['net_growth']}:</b> {net:+d}",
        f"👥 <b>{labels['current_members']}:</b> {member_count if member_count is not None else 'N/A'}",
        f"👑 <b>{labels['admins']}:</b> {admin_count if admin_count is not None else 'N/A'}",
        f"🤖 <b>{labels['bot_admins']}:</b> {bot_admin_count if bot_admin_count is not None else 'N/A'}",
        f"⏰ <b>{labels['peak_hour']}:</b> {peak['hour_bucket'] if peak else 'N/A'}",
        f"🎭 <b>{labels['vibe']}:</b> {vibe}",
        f"🏆 <b>{labels['top_member']}:</b> {display_user_name(top[0]) if top else 'N/A'}",
        "",
        f"⭐ <b>{labels['support']}:</b> @{SUPPORT_USERNAME.lstrip('@')}",
        f"👨‍💻 <b>{labels['creator']}:</b> {CREATOR_NAME}",
    ]
    return "\n".join(lines)

def send_support_invoice(chat_id):

    title = f"{BOT_NAME} Support"
    description = f"Support {BOT_NAME} with {STAR_SUPPORT_AMOUNT} Telegram Stars."
    payload_value = f"support:{chat_id}:{int(time.time())}"
    return send_invoice(chat_id, title, description, payload_value, STAR_SUPPORT_AMOUNT)

def handle_successful_payment(message):
    payment = message.get("successful_payment", {})
    user = message.get("from", {})
    chat = message["chat"]
    support_payment_log(
        user.get("id"),
        chat["id"],
        payment.get("total_amount", STAR_SUPPORT_AMOUNT),
        payment.get("provider_payment_charge_id", ""),
        payment.get("telegram_payment_charge_id", ""),
    )
    lang = get_chat_language(chat["id"])
    return send_message(chat["id"], "✅ Payment received. Thank you for supporting Pulse Bot!" if lang == "en" else "✅ تم استلام الدعم. شكرًا لدعمك Pulse Bot!")

def handle_pre_checkout(update):
    pre = update.get("pre_checkout_query")
    if pre:
        answer_pre_checkout(pre["id"], ok=True)

def record_group_activity(message):
    chat = message["chat"]
    user = message.get("from", {})
    ensure_chat(chat["id"], chat.get("title") or chat.get("first_name") or "Group", chat.get("type", "group"))
    add_user(chat["id"], user.get("id"), user.get("username"), user.get("first_name"))
    record_message(
        chat["id"],
        message["message_id"],
        user.get("id"),
        user.get("username"),
        user.get("first_name"),
        sanitize_text(message.get("text") or message.get("caption") or ""),
        "text" if message.get("text") else ("caption" if message.get("caption") else "other"),
        message.get("reply_to_message", {}).get("message_id") if message.get("reply_to_message") else None,
    )

def handle_membership_events(message):
    chat = message["chat"]
    for member in message.get("new_chat_members", []) or []:
        record_membership_event(chat["id"], member.get("id"), member.get("username"), member.get("first_name"), "joined")
    if message.get("left_chat_member"):
        member = message["left_chat_member"]
        record_membership_event(chat["id"], member.get("id"), member.get("username"), member.get("first_name"), "left")

def handle_reaction(update):
    reaction = update.get("message_reaction")
    if reaction:
        delta = len(reaction.get("new_reaction", [])) - len(reaction.get("old_reaction", []))
        if delta:
            record_reaction(reaction["chat"]["id"], reaction["message_id"], delta)

    reaction_count = update.get("message_reaction_count")
    if reaction_count:
        total = sum(int(item.get("total_count", 0)) for item in (reaction_count.get("reactions", []) or []))
        set_reaction_count(reaction_count["chat"]["id"], reaction_count["message_id"], total)

def moderate(chat_id, actor_user_id, target_user_id, lang, action, reason=""):
    if action in ("mute", "unmute", "ban", "unban") and not bot_can_restrict(chat_id):
        return False, t(lang, "bot_lacks_rights")
    if action in ("admin", "demote") and not bot_can_promote(chat_id):
        return False, t(lang, "bot_lacks_promote_rights")
    if action == "warn":
        count = add_warning(chat_id, target_user_id, 1)
        log_moderation_action(chat_id, actor_user_id, target_user_id, "warn", reason)
        return True, count
    if action == "unwarn":
        current = get_warning_count(chat_id, target_user_id)
        new_count = max(current - 1, 0)
        set_warning_count(chat_id, target_user_id, new_count)
        log_moderation_action(chat_id, actor_user_id, target_user_id, "unwarn", reason)
        return True, new_count
    if action == "warnings":
        return True, get_warning_count(chat_id, target_user_id)
    if action == "mute":
        result = tg("restrictChatMember", {
            "chat_id": chat_id,
            "user_id": target_user_id,
            "permissions": {
                "can_send_messages": False,
                "can_send_audios": False,
                "can_send_documents": False,
                "can_send_photos": False,
                "can_send_videos": False,
                "can_send_video_notes": False,
                "can_send_voice_notes": False,
                "can_send_polls": False,
                "can_send_other_messages": False,
                "can_add_web_page_previews": False,
                "can_change_info": False,
                "can_invite_users": False,
                "can_pin_messages": False,
                "can_manage_topics": False,
            },
            "until_date": int(time.time()) + 24 * 3600,
        })
        log_moderation_action(chat_id, actor_user_id, target_user_id, "mute", reason)
        return bool(result and result.get("ok")), None
    if action == "unmute":
        result = tg("restrictChatMember", {
            "chat_id": chat_id,
            "user_id": target_user_id,
            "permissions": {
                "can_send_messages": True,
                "can_send_audios": True,
                "can_send_documents": True,
                "can_send_photos": True,
                "can_send_videos": True,
                "can_send_video_notes": True,
                "can_send_voice_notes": True,
                "can_send_polls": True,
                "can_send_other_messages": True,
                "can_add_web_page_previews": True,
                "can_change_info": False,
                "can_invite_users": True,
                "can_pin_messages": False,
                "can_manage_topics": False,
            },
        })
        log_moderation_action(chat_id, actor_user_id, target_user_id, "unmute", reason)
        return bool(result and result.get("ok")), None
    if action == "ban":
        result = tg("banChatMember", {"chat_id": chat_id, "user_id": target_user_id})
        log_moderation_action(chat_id, actor_user_id, target_user_id, "ban", reason)
        return bool(result and result.get("ok")), None
    if action == "unban":
        result = tg("unbanChatMember", {"chat_id": chat_id, "user_id": target_user_id, "only_if_banned": False})
        log_moderation_action(chat_id, actor_user_id, target_user_id, "unban", reason)
        return bool(result and result.get("ok")), None
    return False, None

def handle_text_command(message):
    chat = message["chat"]
    chat_id = chat["id"]
    chat_type = chat["type"]
    lang = get_chat_language(chat_id)
    text = sanitize_text(message.get("text", ""))
    cmd = get_command(text)
    user_id = message.get("from", {}).get("id")

    group_only_cmds = {"/stats", "/report", "/yesterday", "/growth", "/language", "/warn", "/unwarn", "/warnings", "/mute", "/unmute", "/ban", "/unban"}
    if cmd in group_only_cmds and not is_group(chat_type):
        send_message(chat_id, t(lang, "group_only"))
        return

    if cmd == "/start":
        ensure_chat(chat_id, chat.get("title") or chat.get("first_name") or "Private", chat_type)
        send_message(chat_id, t(lang, "welcome"), reply_markup=build_main_keyboard(lang))
        return

    if cmd == "/help":
        send_message(chat_id, t(lang, "help"), reply_markup=build_main_keyboard(lang))
        return

    if cmd == "/stats":
        send_message(chat_id, report_text(chat_id, utc_today(), lang))
        return

    if cmd == "/report":
        send_message(chat_id, report_text(chat_id, utc_today(), lang))
        return

    if cmd == "/yesterday":
        send_message(chat_id, report_text(chat_id, utc_yesterday(), lang))
        return

    if cmd == "/growth":
        send_message(chat_id, growth_text(chat_id, utc_yesterday(), lang))
        return

    if cmd in {"/info", "/infos"}:
        send_message(chat_id, build_info_text(chat_id, lang), reply_markup=build_main_keyboard(lang))
        return

    if cmd == "/language":
        if is_group(chat_type) and not is_chat_admin(chat_id, user_id):
            send_message(chat_id, t(lang, "admin_only"))
            return
        send_message(chat_id, t(lang, "language_menu"), reply_markup=build_language_keyboard())
        return

    if cmd == "/support":
        send_message(chat_id, t(lang, "support_text"), reply_markup=build_support_keyboard(lang))
        return

    if cmd == "/creator":
        send_message(chat_id, t(lang, "creator_text"), reply_markup=build_support_keyboard(lang))
        return

    # moderation commands
    mod_cmds = {"/warn", "/unwarn", "/warnings", "/mute", "/unmute", "/ban", "/unban"}
    if cmd in mod_cmds:
        if not is_chat_admin(chat_id, user_id):
            send_message(chat_id, t(lang, "admin_only"))
            return
        target = get_reply_target(message)
        if not target:
            send_message(chat_id, t(lang, "reply_required"))
            return
        target_id = target.get("id")
        target_name = display_user_name(target)
        if is_chat_admin(chat_id, target_id):
            send_message(chat_id, t(lang, "target_is_admin"))
            return
        reason = ""
        parts = text.split(maxsplit=1)
        if len(parts) > 1:
            reason = sanitize_text(parts[1])

        if cmd == "/warn":
            ok, count = moderate(chat_id, user_id, target_id, lang, "warn", reason)
            if ok:
                send_message(chat_id, t(lang, "warn_done", user=target_name, count=count), reply_markup=build_moderation_keyboard(lang, "warn", chat_id, target_id))
                if count >= 3:
                    moderate(chat_id, user_id, target_id, lang, "mute", reason="Auto-mute at 3 warnings")
                    send_message(chat_id, t(lang, "warn_limit_hit", user=target_name))
            else:
                send_message(chat_id, t(lang, "bot_lacks_rights"))
            return

        if cmd == "/unwarn":
            ok, count = moderate(chat_id, user_id, target_id, lang, "unwarn", reason)
            if ok:
                send_message(chat_id, t(lang, "unwarn_done", user=target_name, count=count), reply_markup=build_moderation_keyboard(lang, "unwarn", chat_id, target_id))
            return

        if cmd == "/warnings":
            ok, count = moderate(chat_id, user_id, target_id, lang, "warnings", reason)
            if ok:
                send_message(chat_id, t(lang, "warnings_show", user=target_name, count=count), reply_markup=build_moderation_keyboard(lang, "warnings", chat_id, target_id))
            return

        if cmd == "/mute":
            ok, _ = moderate(chat_id, user_id, target_id, lang, "mute", reason)
            if ok:
                send_message(chat_id, t(lang, "mute_done", user=target_name), reply_markup=build_moderation_keyboard(lang, "mute", chat_id, target_id))
            else:
                send_message(chat_id, t(lang, "bot_lacks_rights"))
            return

        if cmd == "/unmute":
            ok, _ = moderate(chat_id, user_id, target_id, lang, "unmute", reason)
            if ok:
                send_message(chat_id, t(lang, "unmute_done", user=target_name), reply_markup=build_moderation_keyboard(lang, "unmute", chat_id, target_id))
            else:
                send_message(chat_id, t(lang, "bot_lacks_rights"))
            return

        if cmd == "/ban":
            ok, _ = moderate(chat_id, user_id, target_id, lang, "ban", reason)
            if ok:
                send_message(chat_id, t(lang, "ban_done", user=target_name), reply_markup=build_moderation_keyboard(lang, "ban", chat_id, target_id))
            else:
                send_message(chat_id, t(lang, "bot_lacks_rights"))
            return

        if cmd == "/unban":
            ok, _ = moderate(chat_id, user_id, target_id, lang, "unban", reason)
            if ok:
                send_message(chat_id, t(lang, "unban_done", user=target_name), reply_markup=build_moderation_keyboard(lang, "unban", chat_id, target_id))
            else:
                send_message(chat_id, t(lang, "bot_lacks_rights"))
            return

def process_callback_query(callback):
    cb_id = callback["id"]
    data = callback.get("data", "")
    message = callback.get("message")
    from_user = callback.get("from", {})
    if not message:
        answer_callback(cb_id)
        return

    chat_id = message["chat"]["id"]
    chat_type = message["chat"]["type"]
    lang = get_chat_language(chat_id)

    if data == "show:language":
        if is_group(chat_type) and not is_chat_admin(chat_id, from_user.get("id")):
            answer_callback(cb_id, t(lang, "admin_only"), show_alert=True)
            return
        answer_callback(cb_id)
        edit_message_text(chat_id, message["message_id"], t(lang, "language_menu"), reply_markup=build_language_keyboard())
        return

    if data == "show:support":
        answer_callback(cb_id)
        edit_message_text(chat_id, message["message_id"], t(lang, "support_text"), reply_markup=build_support_keyboard(lang))
        return

    if data == "show:creator":
        answer_callback(cb_id)
        edit_message_text(chat_id, message["message_id"], t(lang, "creator_text"), reply_markup=build_support_keyboard(lang))
        return

    if data == "show:info":
        answer_callback(cb_id)
        edit_message_text(chat_id, message["message_id"], build_info_text(chat_id, lang), reply_markup=build_main_keyboard(lang))
        return

    if data == "show:stats":
        answer_callback(cb_id)
        send_message(chat_id, report_text(chat_id, utc_today(), lang))
        return

    if data == "show:report":
        answer_callback(cb_id)
        send_message(chat_id, report_text(chat_id, utc_today(), lang))
        return

    if data == "show:growth":
        answer_callback(cb_id)
        send_message(chat_id, growth_text(chat_id, utc_yesterday(), lang))
        return

    if data == "support:stars":
        answer_callback(cb_id)
        send_support_invoice(chat_id)
        return

    if data.startswith("setlang:"):
        if is_group(chat_type) and not is_chat_admin(chat_id, from_user.get("id")):
            answer_callback(cb_id, t(lang, "admin_only"), show_alert=True)
            return
        lang_code = normalize_lang(data.split(":", 1)[1])
        set_chat_language(chat_id, lang_code)
        answer_callback(cb_id, t(lang_code, "language_set"))
        edit_message_text(chat_id, message["message_id"], t(lang_code, "language_set"), reply_markup=build_main_keyboard(lang_code))
        return

    if data.startswith("daily:"):
        _, choice, date_str = data.split(":", 2)
        answer_callback(cb_id)
        if choice == "yes":
            send_message(chat_id, report_text(chat_id, date_str, lang))
            edit_message_text(chat_id, message["message_id"], f"✅ {date_str} report sent.")
        else:
            edit_message_text(chat_id, message["message_id"], f"⏭ {date_str} report dismissed.")
        return

    if data.startswith("mod:"):
        parts = data.split(":")
        if len(parts) < 4:
            answer_callback(cb_id)
            return
        action, target_chat_id_s, target_user_id_s = parts[1], parts[2], parts[3]
        target_chat_id = int(target_chat_id_s)
        target_user_id = int(target_user_id_s)
        if target_chat_id != chat_id:
            answer_callback(cb_id, "Wrong chat", show_alert=True)
            return
        if not is_chat_admin(chat_id, from_user.get("id")):
            answer_callback(cb_id, t(lang, "admin_only"), show_alert=True)
            return

        user_row = get_user(chat_id, target_user_id)
        target_name = display_user_name(user_row or {"username": None, "first_name": None})
        if action == "warn":
            ok, count = moderate(chat_id, from_user.get("id"), target_user_id, lang, "warn")
            if ok:
                answer_callback(cb_id, t(lang, "warn_done", user=target_name, count=count))
                edit_message_text(chat_id, message["message_id"], t(lang, "warn_done", user=target_name, count=count), reply_markup=build_moderation_keyboard(lang, "warn", chat_id, target_user_id))
            return
        if action == "unwarn":
            ok, count = moderate(chat_id, from_user.get("id"), target_user_id, lang, "unwarn")
            if ok:
                answer_callback(cb_id, t(lang, "unwarn_done", user=target_name, count=count))
                edit_message_text(chat_id, message["message_id"], t(lang, "unwarn_done", user=target_name, count=count), reply_markup=build_moderation_keyboard(lang, "unwarn", chat_id, target_user_id))
            return
        if action == "warnings":
            count = get_warning_count(chat_id, target_user_id)
            answer_callback(cb_id, t(lang, "warnings_show", user=target_name, count=count))
            edit_message_text(chat_id, message["message_id"], t(lang, "warnings_show", user=target_name, count=count), reply_markup=build_moderation_keyboard(lang, "warnings", chat_id, target_user_id))
            return
        if action == "mute":
            ok, _ = moderate(chat_id, from_user.get("id"), target_user_id, lang, "mute")
            if ok:
                answer_callback(cb_id, t(lang, "mute_done", user=target_name))
                edit_message_text(chat_id, message["message_id"], t(lang, "mute_done", user=target_name), reply_markup=build_moderation_keyboard(lang, "mute", chat_id, target_user_id))
            return
        if action == "unmute":
            ok, _ = moderate(chat_id, from_user.get("id"), target_user_id, lang, "unmute")
            if ok:
                answer_callback(cb_id, t(lang, "unmute_done", user=target_name))
                edit_message_text(chat_id, message["message_id"], t(lang, "unmute_done", user=target_name), reply_markup=build_moderation_keyboard(lang, "unmute", chat_id, target_user_id))
            return
        if action == "ban":
            ok, _ = moderate(chat_id, from_user.get("id"), target_user_id, lang, "ban")
            if ok:
                answer_callback(cb_id, t(lang, "ban_done", user=target_name))
                edit_message_text(chat_id, message["message_id"], t(lang, "ban_done", user=target_name), reply_markup=build_moderation_keyboard(lang, "ban", chat_id, target_user_id))
            return
        if action == "unban":
            ok, _ = moderate(chat_id, from_user.get("id"), target_user_id, lang, "unban")
            if ok:
                answer_callback(cb_id, t(lang, "unban_done", user=target_name))
                edit_message_text(chat_id, message["message_id"], t(lang, "unban_done", user=target_name), reply_markup=build_moderation_keyboard(lang, "unban", chat_id, target_user_id))
            return
            ok, _ = moderate(chat_id, from_user.get("id"), target_user_id, lang, "demote")
            if ok:
                answer_callback(cb_id, t(lang, "demote_done", user=target_name))
                edit_message_text(chat_id, message["message_id"], t(lang, "demote_done", user=target_name), reply_markup=build_moderation_keyboard(lang, "demote", chat_id, target_user_id))
            return

    answer_callback(cb_id)

def maybe_send_daily_reports():
    today = utc_today()
    if not claim_daily_dispatch(today):
        return
    chats = chats_due_for_daily_prompt(today)
    for chat in chats:
        chat_id = chat["chat_id"]
        lang = normalize_lang(chat["language"])
        send_message(chat_id, t(lang, "daily_prompt"), reply_markup=build_daily_keyboard(lang, utc_yesterday()))
        mark_daily_prompt_sent(chat_id, today)

def scheduler_loop(stop_event):
    while not stop_event.is_set():
        try:
            maybe_send_daily_reports()
        except Exception:
            LOG.exception("scheduler loop failed")
        stop_event.wait(60)

def start_scheduler_once():
    if getattr(start_scheduler_once, "_started", False):
        return
    stop_event = threading.Event()
    thread = threading.Thread(target=scheduler_loop, args=(stop_event,), daemon=True)
    thread.start()
    start_scheduler_once._started = True
    start_scheduler_once._stop_event = stop_event

def handle_update(update):
    try:
        maybe_send_daily_reports()

        if "pre_checkout_query" in update:
            handle_pre_checkout(update)
            return

        if "message_reaction" in update:
            handle_reaction(update)
            return

        if "callback_query" in update:
            process_callback_query(update["callback_query"])
            return

        message = update.get("message")
        if not message:
            return

        chat = message["chat"]
        chat_id = chat["id"]
        chat_type = chat["type"]
        ensure_chat(chat_id, chat.get("title") or chat.get("first_name") or "Private", chat_type)

        user = message.get("from", {})
        add_user(chat_id, user.get("id"), user.get("username"), user.get("first_name"))

        # record join/leave service messages
        if message.get("new_chat_members") or message.get("left_chat_member"):
            handle_membership_events(message)

        if is_group(chat_type):
            record_group_activity(message)

        if message.get("successful_payment"):
            handle_successful_payment(message)
            return

        if sanitize_text(message.get("text", "")).startswith("/"):
            handle_text_command(message)
    except Exception:
        LOG.exception("handle_update failed")
        return
