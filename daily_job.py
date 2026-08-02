from database import initialize
from bot_core import maybe_send_daily_reports

if __name__ == "__main__":
    initialize()
    maybe_send_daily_reports()
