"""Estimate and priority flows: pick a task, then pick a value from an
inline keyboard. Structurally identical two-step callback flows, kept
together for that reason."""

from __future__ import annotations

import requests

from . import config
from . import vikunja as vk
from .state import _load_json, _save_json, _state_lock
from .telegram_api import _telegram_call


# ─── Estimate flow (set/change a task's duration estimate) ────────────────

def _handle_estimate_pick(callback_id: str, chat_id, message_id, payload: str) -> None:
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

    _telegram_call(
        "editMessageText", chat_id=chat_id, message_id=message_id,
        text=f"⏱ {task['title']}\n¿Cuánto estimás que dura?",
        reply_markup=vk._estimate_duration_keyboard(task_id),
    )
    _telegram_call("answerCallbackQuery", callback_query_id=callback_id)


def _handle_estimate_duration(callback_id: str, chat_id, message_id, payload: str) -> None:
    try:
        task_id_str, minutes_str = payload.split(":", 1)
        task_id, minutes = int(task_id_str), int(minutes_str)
    except ValueError:
        config.log.warning("Malformed estimdur payload: %s", payload)
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

    new_title = vk._set_estimate(task["title"], minutes)
    try:
        if new_title != task["title"]:
            vk._vk_task_update(task_id, {"title": new_title})
    except requests.RequestException as e:
        config.log.error("Could not update estimate for task %s: %s", task_id, e)
        _telegram_call(
            "answerCallbackQuery", callback_query_id=callback_id,
            text=f"Error: {e}", show_alert=True,
        )
        return

    with _state_lock:
        state = _load_json(config.UNDO_STATE_FILE, {})
        state[str(task_id)] = {"action": "estimate", "task": task}
        _save_json(config.UNDO_STATE_FILE, state)

    result_text = f"⏱ {new_title} — estimado en {'0' if minutes == 0 else vk._format_minutes(minutes)}"
    try:
        _telegram_call(
            "editMessageText", chat_id=chat_id, message_id=message_id, text=result_text,
            reply_markup={"inline_keyboard": [[{"text": "↩️ Deshacer", "callback_data": f"undo:{task_id}"}]]},
        )
    except (requests.RequestException, RuntimeError) as e:
        config.log.error("Could not edit Telegram message: %s", e)
    _telegram_call("answerCallbackQuery", callback_query_id=callback_id, text="Estimado")
    config.log.info("Set estimate for task %s to %d min", task_id, minutes)


# ─── Priority flow (set/change a task's priority) ──────────────────────────

def _handle_priority_pick(callback_id: str, chat_id, message_id, payload: str) -> None:
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

    _telegram_call(
        "editMessageText", chat_id=chat_id, message_id=message_id,
        text=f"🚩 {task['title']}\n¿Qué prioridad le ponemos?",
        reply_markup=vk._priority_keyboard(task_id),
    )
    _telegram_call("answerCallbackQuery", callback_query_id=callback_id)


def _handle_priority_set(callback_id: str, chat_id, message_id, payload: str) -> None:
    try:
        task_id_str, level_str = payload.split(":", 1)
        task_id, level = int(task_id_str), int(level_str)
    except ValueError:
        config.log.warning("Malformed priolvl payload: %s", payload)
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

    label = dict(vk._PRIORITY_OPTIONS)[level]
    try:
        vk._vk_task_update(task_id, {"priority": level})
    except requests.RequestException as e:
        config.log.error("Could not update priority for task %s: %s", task_id, e)
        _telegram_call(
            "answerCallbackQuery", callback_query_id=callback_id,
            text=f"Error: {e}", show_alert=True,
        )
        return

    with _state_lock:
        state = _load_json(config.UNDO_STATE_FILE, {})
        state[str(task_id)] = {"action": "priority", "task": task}
        _save_json(config.UNDO_STATE_FILE, state)

    result_text = f"🚩 {task['title']} — prioridad: {label}"
    try:
        _telegram_call(
            "editMessageText", chat_id=chat_id, message_id=message_id, text=result_text,
            reply_markup={"inline_keyboard": [[{"text": "↩️ Deshacer", "callback_data": f"undo:{task_id}"}]]},
        )
    except (requests.RequestException, RuntimeError) as e:
        config.log.error("Could not edit Telegram message: %s", e)
    _telegram_call("answerCallbackQuery", callback_query_id=callback_id, text="Prioridad actualizada")
    config.log.info("Set priority for task %s to %d", task_id, level)
