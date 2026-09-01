"""Thin wrapper around the Telegram Bot API, plus the due-notification
message builder built directly on top of it."""

from __future__ import annotations

from typing import Optional

import requests

from . import config
from . import vikunja as vk


def _telegram_call(method: str, **params) -> object:
    # Telegram's API rejects some optional fields (e.g. reply_markup) sent as
    # JSON null with a 400 — callers that pass through a possibly-None value
    # (like _reply_to_pending) must have that treated as "omitted".
    params = {k: v for k, v in params.items() if v is not None}
    url = config.TELEGRAM_API.format(token=config.TOKEN, method=method)
    r = requests.post(url, json=params, timeout=35)
    r.raise_for_status()
    body = r.json()
    if not body.get("ok"):
        raise RuntimeError(f"Telegram API error on {method}: {body}")
    return body["result"]


def _send_due_notification(task: dict) -> Optional[int]:
    keyboard = {
        "inline_keyboard": [
            [{"text": "✅ Hecha", "callback_data": f"done:{task['id']}"}],
            [
                {"text": "+10 min", "callback_data": f"snooze10:{task['id']}"},
                {"text": "+1 hora", "callback_data": f"snooze60:{task['id']}"},
                {"text": "+24 horas", "callback_data": f"snooze1440:{task['id']}"},
            ],
            [{"text": "🌆 Más tarde (hoy, sin hora)", "callback_data": f"snoozeday:{task['id']}"}],
        ]
    }
    text = f"⏰ Vencida: {task['title']}\n{vk._format_due(task)}"
    try:
        result = _telegram_call("sendMessage", chat_id=config.CHAT_ID, text=text, reply_markup=keyboard)
        return result["message_id"]
    except (requests.RequestException, RuntimeError) as e:
        config.log.error("Failed to send Telegram notification for task %s: %s", task["id"], e)
        return None
