"""Environment-derived configuration, state file paths, and logging setup
shared by every other module in this package."""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

VIKUNJA_URL = os.environ.get("VIKUNJA_URL", "http://192.168.0.9:3456").rstrip("/")
API_BASE = f"{VIKUNJA_URL}/api/v1"
VIKUNJA_TOKEN = os.environ.get("VIKUNJA_TOKEN")
WEBHOOK_SECRET = os.environ.get("VIKUNJA_WEBHOOK_SECRET")
WEBHOOK_HOST = os.environ.get("VIKUNJA_WEBHOOK_HOST", "0.0.0.0")
WEBHOOK_PORT = int(os.environ.get("VIKUNJA_WEBHOOK_PORT", "8765"))

TELEGRAM_API = "https://api.telegram.org/bot{token}/{method}"
CHECK_INTERVAL_SECONDS = 300  # safety-net reconciliation cadence
RESEND_AFTER_SECONDS = 600  # re-post an unacknowledged due notification after this long

STATE_DIR = Path(os.environ.get("SP_CLI_STATE_DIR", Path.home() / ".config" / "sp-cli"))
NOTIFY_STATE_FILE = STATE_DIR / "notify_state.json"
BOT_STATE_FILE = STATE_DIR / "telegram_bot_state.json"
DAILY_DIGEST_STATE_FILE = STATE_DIR / "daily_digest_state.json"
PENDING_TASK_STATE_FILE = STATE_DIR / "pending_task_state.json"
PENDING_TIME_STATE_FILE = STATE_DIR / "pending_time_state.json"
UNDO_STATE_FILE = STATE_DIR / "undo_state.json"
AVAILABILITY_STATE_FILE = STATE_DIR / "availability.json"
GOOGLE_CALENDAR_STATE_FILE = STATE_DIR / "calendars.json"
GOOGLE_CALENDAR_CACHE_SECONDS = int(os.environ.get("GOOGLE_CALENDAR_CACHE_SECONDS", str(24 * 60 * 60)))
DAILY_DIGEST_HOUR = 6

TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", stream=sys.stdout
)
log = logging.getLogger("sp-cli-telegram")
