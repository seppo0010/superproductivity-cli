"""Partial-progress flow (/avance): pick a task, then pick (or type) how
long you worked on it.

The progress is logged as its own one-off task in the same project —
"[20m] Title (avance)", created and immediately marked done — so it shows up
in the completion history with its minutes (stats.py counts it as minutes
only, not as a completed task). The original's `[1h]` estimate drops to what's
left (`[40m]`). A recurring task is forked like postponing: the remainder
becomes a one-off copy with the lowered estimate and the recurring task
moves to its next occurrence, so its schedule and estimate stay untouched.
The confirmation offers the punt buttons for the remaining work."""

from __future__ import annotations

from typing import Optional

import requests

from . import config
from . import stats
from . import vikunja as vk
from .formatting import _punt_due_keyboard
from .punt_flow import _FORK_NOTE
from .state import _load_json, _save_json, _state_lock
from .telegram_api import _telegram_call
from .time_entry_flow import _reply_to_pending

PENDING_KIND = "avance"  # marks an avance prompt in PENDING_ESTIMATE_STATE_FILE
_PROGRESS_SUFFIX = " (avance)"
_PROGRESS_OPTIONS = [(10, "10m"), (15, "15m"), (20, "20m"), (30, "30m"), (45, "45m"), (60, "1h")]


def _progress_keyboard(task_id: int) -> dict:
    rows = [
        [
            {"text": label, "callback_data": f"avancedur:{task_id}:{minutes}"}
            for minutes, label in _PROGRESS_OPTIONS[i:i + 3]
        ]
        for i in range(0, len(_PROGRESS_OPTIONS), 3)
    ]
    return {"inline_keyboard": rows + [[{"text": "❌ Cancelar", "callback_data": "hcancel:0"}]]}


def _covers_estimate_keyboard(task_id: int, minutes: int) -> dict:
    return {"inline_keyboard": [
        [{"text": "✅ Sí, marcar hecha", "callback_data": f"done:{task_id}"}],
        [{"text": "⏳ No, registrar el avance", "callback_data": f"avancedur:{task_id}:{minutes}:f"}],
        [{"text": "❌ Cancelar", "callback_data": "hcancel:0"}],
    ]}


def _progress_title(title: str, minutes: int) -> str:
    """"[1h] 📞 Llamar" + 20 → "[20m] 📞 Llamar (avance)"."""
    return vk._set_estimate(f"{title.strip()}{_PROGRESS_SUFFIX}", minutes)


def _clear_pending(chat_id) -> None:
    with _state_lock:
        state = _load_json(config.PENDING_ESTIMATE_STATE_FILE, {})
        state.pop(str(chat_id), None)
        _save_json(config.PENDING_ESTIMATE_STATE_FILE, state)


def _fetch_or_alert(callback_id: str, task_id: int) -> Optional[dict]:
    try:
        task = vk._find_task(task_id)
    except requests.RequestException as e:
        config.log.error("Could not fetch task %s: %s", task_id, e)
        _telegram_call("answerCallbackQuery", callback_query_id=callback_id, text=f"Error: {e}", show_alert=True)
        return None
    if task is None:
        _telegram_call("answerCallbackQuery", callback_query_id=callback_id, text="Esa tarea ya no existe", show_alert=True)
    return task


def _apply_progress(task: dict, minutes: int) -> tuple:
    """Logs `minutes` of progress on `task` (fetched pre-change, for the
    undo snapshot). Returns (confirmation text, id of the task holding the
    remaining work). Raises RequestException."""
    progress = vk._vk_put(f"/projects/{task['project_id']}/tasks", {
        "title": _progress_title(task["title"], minutes),
        "description": f"Avance parcial de {config.VIKUNJA_URL}/tasks/{task['id']}",
        "hex_color": task.get("hex_color") or "",
    })
    # Flag it before it's marked done, so the task.updated webhook already
    # sees it as partial progress.
    stats.mark_progress_task(progress["id"])
    stats.record_completions([vk._vk_task_update(progress["id"], {"done": True})])

    entry = {"action": "avance", "task": task, "progress_id": progress["id"]}
    remaining_id = task["id"]
    estimate = vk._parse_estimate_minutes(task["title"])
    lines = [f"⏳ {task['title']} — avance de {vk._format_minutes(minutes)} registrado"]
    fork = None

    if estimate is None:
        lines.append("Sin estimado, así que no lo cambié.")
    elif minutes < estimate:
        remaining = estimate - minutes
        new_title = vk._set_estimate(task["title"], remaining)
        if vk._is_recurring(task):
            # Same fork as postponing: the remainder is a one-off copy (at
            # the same due date), the recurring task moves on unchanged.
            fork = vk._postpone_task({**task, "title": new_title}, task.get("due_date") or "")
            entry["fork_id"] = fork["id"]
            remaining_id = fork["id"]
        else:
            vk._vk_task_update(task["id"], {"title": new_title})
        lines.append(f"Quedan {vk._format_minutes(remaining)}: {new_title}")
    else:
        lines.append("El estimado quedó igual.")

    with _state_lock:
        state = _load_json(config.UNDO_STATE_FILE, {})
        state[str(task["id"])] = entry
        _save_json(config.UNDO_STATE_FILE, state)

    text = "\n".join(lines) + (_FORK_NOTE if fork else "")
    if estimate is not None and minutes < estimate:
        text += "\n¿Lo que queda lo posponés?"
    config.log.info("Logged %d min of progress on task %s (progress task %s%s)",
                    minutes, task["id"], progress["id"], f", fork {fork['id']}" if fork else "")
    return text, remaining_id


def _result_keyboard(task: dict, minutes: int, remaining_id: int) -> dict:
    """Punt buttons for the remaining work (when there is any) + undo."""
    undo_row = [{"text": "↩️ Deshacer", "callback_data": f"undo:{task['id']}"}]
    estimate = vk._parse_estimate_minutes(task["title"])
    if estimate is None or minutes >= estimate:
        return {"inline_keyboard": [undo_row]}
    punt_rows = _punt_due_keyboard(remaining_id)["inline_keyboard"][:-1]  # drop its Cancelar
    return {"inline_keyboard": punt_rows + [undo_row]}


def _prompt_text(task: dict) -> str:
    estimate = vk._parse_estimate_minutes(task["title"])
    hint = f" (estimado: {vk._format_minutes(estimate)})" if estimate else ""
    return f"⏳ {task['title']}\n¿Cuánto avanzaste?{hint} Elegí o escribí la duración (ej: 25m, 1h10m)."


def _handle_avance_pick(callback_id: str, chat_id, message_id, payload: str) -> None:
    try:
        task_id = int(payload)
    except ValueError:
        _telegram_call("answerCallbackQuery", callback_query_id=callback_id, text="Opción inválida", show_alert=True)
        return
    task = _fetch_or_alert(callback_id, task_id)
    if task is None:
        return

    _telegram_call(
        "editMessageText", chat_id=chat_id, message_id=message_id,
        text=_prompt_text(task), reply_markup=_progress_keyboard(task_id),
    )
    with _state_lock:
        state = _load_json(config.PENDING_ESTIMATE_STATE_FILE, {})
        state[str(chat_id)] = {"kind": PENDING_KIND, "task_id": task_id, "message_id": message_id}
        _save_json(config.PENDING_ESTIMATE_STATE_FILE, state)
    _telegram_call("answerCallbackQuery", callback_query_id=callback_id)


def _needs_confirmation(task: dict, minutes: int) -> bool:
    estimate = vk._parse_estimate_minutes(task["title"])
    return estimate is not None and minutes >= estimate


def _confirmation_text(task: dict, minutes: int) -> str:
    estimate = vk._parse_estimate_minutes(task["title"])
    return (f"⏳ {task['title']}\n{vk._format_minutes(minutes)} cubre todo el estimado "
            f"({'0' if not estimate else vk._format_minutes(estimate)}). ¿La terminaste?")


def _handle_avance_duration(callback_id: str, chat_id, message_id, payload: str) -> None:
    """`avancedur:<task_id>:<minutes>[:f]` — `f` = confirmed it's progress
    even though it covers the whole estimate."""
    parts = payload.split(":")
    try:
        task_id, minutes = int(parts[0]), int(parts[1])
    except (ValueError, IndexError):
        config.log.warning("Malformed avancedur payload: %s", payload)
        _telegram_call("answerCallbackQuery", callback_query_id=callback_id)
        return
    forced = len(parts) > 2 and parts[2] == "f"

    task = _fetch_or_alert(callback_id, task_id)
    if task is None:
        return
    _clear_pending(chat_id)

    if not forced and _needs_confirmation(task, minutes):
        _telegram_call(
            "editMessageText", chat_id=chat_id, message_id=message_id,
            text=_confirmation_text(task, minutes), reply_markup=_covers_estimate_keyboard(task_id, minutes),
        )
        _telegram_call("answerCallbackQuery", callback_query_id=callback_id)
        return

    try:
        text, remaining_id = _apply_progress(task, minutes)
    except requests.RequestException as e:
        config.log.error("Could not log progress on task %s: %s", task_id, e)
        _telegram_call("answerCallbackQuery", callback_query_id=callback_id, text=f"Error: {e}", show_alert=True)
        return

    try:
        _telegram_call(
            "editMessageText", chat_id=chat_id, message_id=message_id, text=text,
            reply_markup=_result_keyboard(task, minutes, remaining_id),
        )
    except (requests.RequestException, RuntimeError) as e:
        config.log.error("Could not edit Telegram message: %s", e)
    _telegram_call("answerCallbackQuery", callback_query_id=callback_id, text="Avance registrado")


def _handle_avance_text(chat_id, text: str, pending: dict) -> None:
    """A typed duration ("25m", "1h10m") instead of a button."""
    message_id = pending.get("message_id")
    task_id = pending["task_id"]

    minutes = vk._parse_duration_minutes(text)
    if not minutes:
        _reply_to_pending(chat_id, message_id, "Formato inválido. Escribí cuánto avanzaste (ej: 25m, 1h10m, 1h).")
        return

    try:
        task = vk._find_task(task_id)
    except requests.RequestException as e:
        config.log.error("Could not fetch task %s: %s", task_id, e)
        _reply_to_pending(chat_id, message_id, f"Error: {e}")
        return
    _clear_pending(chat_id)
    if task is None:
        _reply_to_pending(chat_id, message_id, "Esa tarea ya no existe")
        return

    if _needs_confirmation(task, minutes):
        _reply_to_pending(chat_id, message_id, _confirmation_text(task, minutes),
                          reply_markup=_covers_estimate_keyboard(task_id, minutes))
        return

    try:
        result, remaining_id = _apply_progress(task, minutes)
    except requests.RequestException as e:
        config.log.error("Could not log progress on task %s: %s", task_id, e)
        _reply_to_pending(chat_id, message_id, f"Error: {e}")
        return
    _reply_to_pending(chat_id, message_id, result, reply_markup=_result_keyboard(task, minutes, remaining_id))
