# Pulse Bot v4 configuration
# BOT_USERNAME must be written WITHOUT @.

from pathlib import Path

TOKEN = "8850274461:AAGDe8JVpjJ2HbnofX0kSNaUwwHdbUHrqLk"
WEBHOOK_URL = "https://pulse-db.onrender.com/webhook"

BOT_NAME = "Pulse Bot"
BOT_USERNAME = "Groupmind_bot"
BOT_VERSION = "v4.6"
CREATOR_NAME = "@kaafzlll"
SUPPORT_USERNAME = "@kaafzlll"

STAR_SUPPORT_AMOUNT = 5
CREATOR_GIFT_URL = ""

BASE_DIR = Path(__file__).resolve().parent
DATABASE_NAME = str(BASE_DIR / "pulse.db")
