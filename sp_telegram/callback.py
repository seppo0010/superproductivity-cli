"""Dispatches inline-keyboard button presses to the right flow handler.
The simple one-shot actions (done/delete/snooze/tarjeta) that don't need
their own flow module are handled inline here."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import requests

from . import config
from . import vikunja as vk
from .estimate_priority_flow import (
    _handle_estimate_duration,
    _handle_estimate_pick,
    _handle_priority_pick,
    _handle_priority_set,
)
from .new_task_flow import _handle_new_task_due, _handle_new_task_project
from .punt_flow import _handle_punt_due, _handle_punt_pick
from .state import _load_json, _save_json, _state_lock
from .telegram_api import _telegram_call
from .undo import _handle_undo


def _handle_callback(callback: dict) -> None:
    callback_id = callback["id"]
    data = callback.get("data", "")
    message = callback.get("message", {})
    chat_id = message.get("chat", {}).get("id")
    message_id = message.get("message_id")

    try:
        action, payload = data.split(":", 1)
    except ValueError:
        config.log.warning("Malformed callback_data: %s", data)
        _telegram_call("answerCallbackQuery", callback_query_id=callback_id)
        return

    if action == "ntproj":
        _handle_new_task_project(callback_id, chat_id, message_id, payload)
        return
    if action == "ntdue":
        _handle_new_task_due(callback_id, chat_id, message_id, payload)
        return
    if action == "hcancel":
        with _state_lock:
            time_state = _load_json(config.PENDING_TIME_STATE_FILE, {})
            time_state.pop(str(chat_id), None)
            _save_json(config.PENDING_TIME_STATE_FILE, time_state)
            task_state = _load_json(config.PENDING_TASK_STATE_FILE, {})
            task_state.pop(str(chat_id), None)
            _save_json(config.PENDING_TASK_STATE_FILE, task_state)
            estimate_state = _load_json(config.PENDING_ESTIMATE_STATE_FILE, {})
            estimate_state.pop(str(chat_id), None)
            _save_json(config.PENDING_ESTIMATE_STATE_FILE, estimate_state)
        _telegram_call("editMessageText", chat_id=chat_id, message_id=message_id, text="Cancelado")
        _telegram_call("answerCallbackQuery", callback_query_id=callback_id)
        return
    if action == "undo":
        _handle_undo(callback_id, chat_id, message_id, payload)
        return
    if action == "punt":
        _handle_punt_pick(callback_id, chat_id, message_id, payload)
        return
    if action == "puntdue":
        _handle_punt_due(callback_id, chat_id, message_id, payload)
        return
    if action == "estim":
        _handle_estimate_pick(callback_id, chat_id, message_id, payload)
        return
    if action == "estimdur":
        _handle_estimate_duration(callback_id, chat_id, message_id, payload)
        return
    if action == "prio":
        _handle_priority_pick(callback_id, chat_id, message_id, payload)
        return
    if action == "priolvl":
        _handle_priority_set(callback_id, chat_id, message_id, payload)
        return
    task_id = int(payload)

    try:
        task = vk._find_task(task_id)
    except requests.RequestException as e:
        config.log.error("Could not fetch task %s: %s", task_id, e)
        _telegram_call(
            "answerCallbackQuery", callback_query_id=callback_id,
            text=f"Error: {e}", show_alert=True,
        )
        return

    if task is None:
        config.log.warning("Task %s no longer exists", task_id)
        _telegram_call(
            "answerCallbackQuery", callback_query_id=callback_id,
            text="Esa tarea ya no existe", show_alert=True,
        )
        return

    try:
        if action == "done":
            vk._vk_task_update(task_id, {"done": True})
            result_text = f"✅ {task['title']} — hecha"
        elif action == "delete":
            vk._vk_delete(f"/tasks/{task_id}")
            result_text = f"🗑️ {task['title']} — borrada"
        elif action in ("snooze10", "snooze60", "snooze1440"):
            delta = {
                "snooze10": timedelta(minutes=10),
                "snooze60": timedelta(hours=1),
                "snooze1440": timedelta(hours=24),
            }[action]
            new_dt = datetime.now(timezone.utc) + delta
            vk._vk_task_update(task_id, {"due_date": new_dt.strftime("%Y-%m-%dT%H:%M:%SZ")})
            result_text = f"⏰ {task['title']} — pospuesta a las {new_dt.astimezone().strftime('%Y-%m-%d %H:%M')}"
        elif action == "snoozeday":
            vk._vk_task_update(task_id, {"due_date": vk._day_to_due_iso(date.today())})
            result_text = f"⏰ {task['title']} — pospuesta a hoy, sin hora fija"
        elif action == "tarjeta":
            label_obj = vk._find_label_by_title(vk._TARJETA_LABEL_TITLE)
            if label_obj is None:
                raise RuntimeError(f'No existe el label "{vk._TARJETA_LABEL_TITLE}" en Vikunja')
            if not any(l["id"] == label_obj["id"] for l in task.get("labels") or []):
                vk._vk_put(f"/tasks/{task_id}/labels", {"label_id": label_obj["id"]})
            new_title = vk._set_estimate(task["title"], 0)
            if new_title != task["title"]:
                vk._vk_task_update(task_id, {"title": new_title})
            result_text = f'💳 {new_title} — etiquetada "{vk._TARJETA_LABEL_TITLE}", estimado a 0'
        else:
            config.log.warning("Unknown action: %s", action)
            _telegram_call("answerCallbackQuery", callback_query_id=callback_id)
            return
    except (requests.RequestException, RuntimeError) as e:
        config.log.error("Could not apply action %s to task %s: %s", action, task_id, e)
        _telegram_call(
            "answerCallbackQuery", callback_query_id=callback_id,
            text=f"Error: {e}", show_alert=True,
        )
        return

    with _state_lock:
        state = _load_json(config.UNDO_STATE_FILE, {})
        state[str(task_id)] = {"action": action, "task": task}
        _save_json(config.UNDO_STATE_FILE, state)

    try:
        _telegram_call(
            "editMessageText", chat_id=chat_id, message_id=message_id, text=result_text,
            reply_markup={"inline_keyboard": [[{"text": "↩️ Deshacer", "callback_data": f"undo:{task_id}"}]]},
        )
    except (requests.RequestException, RuntimeError) as e:
        config.log.error("Could not edit Telegram message: %s", e)
    _telegram_call("answerCallbackQuery", callback_query_id=callback_id, text="Listo")
    config.log.info("Handled '%s' for task %s", action, task_id)
