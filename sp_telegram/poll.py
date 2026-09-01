"""Long-polls Telegram for updates (button presses, messages) and applies
them via the callback/commands dispatchers, forever."""

from __future__ import annotations

import time

import requests

from . import config
from .callback import _handle_callback
from .commands import _handle_message
from .state import _load_json, _save_json
from .telegram_api import _telegram_call


def poll_telegram_updates() -> None:
    state = _load_json(config.BOT_STATE_FILE, {"offset": 0})
    offset = state.get("offset", 0)
    config.log.info("Starting Telegram button listener (offset=%d)", offset)
    while True:
        try:
            updates = _telegram_call(
                "getUpdates", offset=offset, timeout=30,
                allowed_updates=["callback_query", "message"],
            )
        except (requests.RequestException, RuntimeError) as e:
            config.log.error("getUpdates failed, retrying: %s", e)
            time.sleep(5)
            continue

        for update in updates:
            offset = update["update_id"] + 1
            callback = update.get("callback_query")
            message = update.get("message")
            if callback:
                try:
                    _handle_callback(callback)
                except Exception:
                    # A bug in one command must not take down this whole
                    # listener thread (as an uncaught TypeError from a
                    # malformed availability.json once did) — the bot would
                    # then stop responding to everything until manually
                    # restarted.
                    config.log.exception("Unhandled error processing callback")
            elif message:
                try:
                    _handle_message(message)
                except Exception:
                    config.log.exception("Unhandled error processing message")
            _save_json(config.BOT_STATE_FILE, {"offset": offset})
