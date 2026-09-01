"""Punt flow: postpone an existing task's due date, either via the
snooze/today quick-reply keyboard or a typed date/time (see
time_entry_flow._handle_time_entry, which calls back into _apply_punt)."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import requests

from . import config
from . import vikunja as vk
from .formatting import _DUE_DATE_HINT, _punt_due_keyboard
from .state import _load_json, _save_json, _state_lock
from .telegram_api import _telegram_call


def _handle_punt_pick(callback_id: str, chat_id, message_id, payload: str) -> None:
    try:
        task_id = int(payload)
    except ValueError:
        _telegram_call(
            "answerCallbackQuery", callback_query_id=callback_id,
            text="Opción inválida", show_alert=True,
        )
        return

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

    # Set the time-entry state as soon as the due-date keyboard is shown, so
    # a plain "HH:MM"/"D/M"/"D/M HH:MM" reply works immediately without
    # touching a button.
    with _state_lock:
        time_state = _load_json(config.PENDING_TIME_STATE_FILE, {})
        time_state[str(chat_id)] = {
            "kind": "punt", "task_id": task_id, "task_title": task["title"], "message_id": message_id,
        }
        _save_json(config.PENDING_TIME_STATE_FILE, time_state)

    _telegram_call(
        "editMessageText", chat_id=chat_id, message_id=message_id,
        text=f"📅 {task['title']}\n¿Nuevo vencimiento? ({_DUE_DATE_HINT})",
        reply_markup=_punt_due_keyboard(task_id),
    )
    _telegram_call("answerCallbackQuery", callback_query_id=callback_id)


def _apply_punt(task: dict, task_id: int, due_date_iso: str, due_label: str) -> str:
    """Applies a new due date to `task` (fetched pre-change, for the undo
    snapshot) and returns the confirmation text. Raises RequestException."""
    vk._vk_task_update(task_id, {"due_date": due_date_iso})
    with _state_lock:
        state = _load_json(config.UNDO_STATE_FILE, {})
        state[str(task_id)] = {"action": "punt", "task": task}
        _save_json(config.UNDO_STATE_FILE, state)
    config.log.info("Punted task %s to %s", task_id, due_label)
    return f"📅 {task['title']} — pospuesta a {due_label}"


def _handle_punt_due(callback_id: str, chat_id, message_id, payload: str) -> None:
    try:
        task_id_str, option = payload.split(":", 1)
        task_id = int(task_id_str)
    except ValueError:
        config.log.warning("Malformed puntdue payload: %s", payload)
        _telegram_call("answerCallbackQuery", callback_query_id=callback_id)
        return

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

    # A due-date button was pressed instead of a plain-text time/date
    # reply — clear the pending time-entry state so a later message isn't
    # mistaken for a leftover time entry for this already-resolved task.
    with _state_lock:
        time_state = _load_json(config.PENDING_TIME_STATE_FILE, {})
        time_state.pop(str(chat_id), None)
        _save_json(config.PENDING_TIME_STATE_FILE, time_state)

    if option == "snooze10":
        due_date_iso = (datetime.now(timezone.utc) + timedelta(minutes=10)).strftime("%Y-%m-%dT%H:%M:%SZ")
        due_label = "en 10 minutos"
    elif option == "snooze60":
        due_date_iso = (datetime.now(timezone.utc) + timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
        due_label = "en 1 hora"
    elif option == "snooze1440":
        # Adds a day to the task's own due date (preserving its time-of-day,
        # including the 23:59 "no specific time" sentinel), not to "now" —
        # otherwise a "sin hora" task would pick up whatever time it happens
        # to be right now, defeating the point of that convention.
        base = vk._parse_vikunja_ts(task.get("due_date", "")) or datetime.now(timezone.utc)
        due_date_iso = (base + timedelta(hours=24)).strftime("%Y-%m-%dT%H:%M:%SZ")
        due_label = "24 horas más tarde"
    elif option == "today":
        due_date_iso, due_label = vk._day_to_due_iso(date.today()), "hoy, sin hora"
    else:
        config.log.warning("Unknown puntdue option: %s", option)
        _telegram_call("answerCallbackQuery", callback_query_id=callback_id)
        return

    try:
        result_text = _apply_punt(task, task_id, due_date_iso, due_label)
    except requests.RequestException as e:
        config.log.error("Could not punt task %s: %s", task_id, e)
        _telegram_call(
            "answerCallbackQuery", callback_query_id=callback_id,
            text=f"Error: {e}", show_alert=True,
        )
        return

    try:
        _telegram_call(
            "editMessageText", chat_id=chat_id, message_id=message_id, text=result_text,
            reply_markup={"inline_keyboard": [[{"text": "↩️ Deshacer", "callback_data": f"undo:{task_id}"}]]},
        )
    except (requests.RequestException, RuntimeError) as e:
        config.log.error("Could not edit Telegram message: %s", e)
    _telegram_call("answerCallbackQuery", callback_query_id=callback_id, text="Pospuesta")
