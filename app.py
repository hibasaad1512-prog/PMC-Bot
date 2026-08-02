from flask import Flask, request
import logging
import os

from database import initialize
from bot_core import handle_update, start_scheduler_once

logging.basicConfig(level=logging.INFO)

app = Flask(__name__)

initialize()
start_scheduler_once()

@app.route("/")
def home():
    return "Pulse Bot is running!"

@app.route("/health")
def health():
    return {"ok": True}

@app.route("/webhook", methods=["POST"])
def webhook():
    update = request.get_json(force=True, silent=True) or {}
    handle_update(update)
    return "OK"

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8080)), debug=False)
