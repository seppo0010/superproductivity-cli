"""New-task flow: a plain message with no pending state starts an (optional
emoji-pick then) project-then-due-date prompt that ends in task creation."""

from __future__ import annotations

from datetime import date, timedelta

import requests

from . import config
from . import emoji_suggest
from . import vikunja as vk
from .formatting import _DUE_DATE_HINT, _DUE_DATE_KEYBOARD
from .state import _load_json, _save_json, _state_lock
from .telegram_api import _telegram_call


def _show_project_picker(chat_id, title: str, message_id=None) -> None:
    """Show the project-choice keyboard for `title`, either as a new message
    (`message_id=None`) or by editing an existing one (e.g. the emoji-pick
    message, once a choice has been made there)."""
    projects = vk._real_projects()
    if not projects:
        _telegram_call("sendMessage", chat_id=chat_id, text="Error: no se pudieron obtener los proyectos")
        return

    state = _load_json(config.PENDING_TASK_STATE_FILE, {})
    state[str(chat_id)] = {
        "title": title,
        "project_ids": [p["id"] for p in projects],
        "project_titles": [vk._project_display(p) for p in projects],
    }
    _save_json(config.PENDING_TASK_STATE_FILE, state)

    keyboard = {
        "inline_keyboard": [
            [{"text": vk._project_display(p), "callback_data": f"ntproj:{i}"}]
            for i, p in enumerate(projects)
        ]
    }
    text = f"📝 {title}\n¿En qué proyecto?"
    if message_id is None:
        _telegram_call("sendMessage", chat_id=chat_id, text=text, reply_markup=keyboard)
    else:
        _telegram_call("editMessageText", chat_id=chat_id, message_id=message_id, text=text, reply_markup=keyboard)
    config.log.info("Started new-task flow for chat %s: %s", chat_id, title)


def _start_new_task(chat_id, title: str) -> None:
    suggestions = emoji_suggest.suggest_emojis(title)
    if not suggestions:
        _show_project_picker(chat_id, title)
        return

    keyboard = {
        "inline_keyboard": [
            [{"text": e, "callback_data": f"ntemoji:{e}"} for e in suggestions],
            [{"text": "⏭ Sin emoji", "callback_data": "ntemoji:skip"}, {"text": "❌ Cancelar", "callback_data": "hcancel:0"}],
        ]
    }
    sent = _telegram_call("sendMessage", chat_id=chat_id, text=f"📝 {title}\n¿Emoji?", reply_markup=keyboard)

    state = _load_json(config.PENDING_TASK_STATE_FILE, {})
    state[str(chat_id)] = {"title": title, "message_id": sent["message_id"]}
    _save_json(config.PENDING_TASK_STATE_FILE, state)


def _handle_new_task_emoji(callback_id: str, chat_id, message_id, payload: str) -> None:
    state = _load_json(config.PENDING_TASK_STATE_FILE, {})
    pending = state.get(str(chat_id))
    if not pending:
        _telegram_call(
            "answerCallbackQuery", callback_query_id=callback_id,
            text="No hay ninguna tarea pendiente", show_alert=True,
        )
        return

    title = pending["title"] if payload == "skip" else f"{payload} {pending['title']}"
    _telegram_call("answerCallbackQuery", callback_query_id=callback_id)
    _show_project_picker(chat_id, title, message_id=message_id)


def _handle_new_task_emoji_text(chat_id, text: str, pending: dict) -> None:
    """A plain-text reply typed while the emoji prompt is showing, instead of
    pressing one of the suggested-emoji buttons — read for emoji characters
    of the user's own choosing rather than restarting the new-task flow."""
    found = emoji_suggest.extract_emojis(text)
    if not found:
        _telegram_call(
            "sendMessage", chat_id=chat_id,
            text="No encontré ningún emoji en ese mensaje. Elegí uno del teclado o escribí uno.",
        )
        return

    title = f"{' '.join(found)} {pending['title']}"
    _show_project_picker(chat_id, title, message_id=pending.get("message_id"))


def _handle_new_task_project(callback_id: str, chat_id, message_id, payload: str) -> None:
    state = _load_json(config.PENDING_TASK_STATE_FILE, {})
    pending = state.get(str(chat_id))
    if not pending:
        _telegram_call(
            "answerCallbackQuery", callback_query_id=callback_id,
            text="No hay ninguna tarea pendiente", show_alert=True,
        )
        return

    project_ids = pending.get("project_ids", [])
    try:
        idx = int(payload)
    except ValueError:
        idx = -1

    if 0 <= idx < len(project_ids):
        project_id = project_ids[idx]
        project_title = pending["project_titles"][idx]
    else:
        _telegram_call(
            "answerCallbackQuery", callback_query_id=callback_id,
            text="Opción inválida", show_alert=True,
        )
        return

    pending["project_id"] = project_id
    pending["project_title"] = project_title
    state[str(chat_id)] = pending
    _save_json(config.PENDING_TASK_STATE_FILE, state)

    # Set the time-entry state as soon as the due-date keyboard is shown, so
    # a plain "HH:MM"/"D/M"/"D/M HH:MM" reply works immediately without
    # touching a button.
    with _state_lock:
        time_state = _load_json(config.PENDING_TIME_STATE_FILE, {})
        time_state[str(chat_id)] = {
            "kind": "newtask", "title": pending["title"],
            "project_id": project_id, "project_title": project_title,
            "message_id": message_id,
        }
        _save_json(config.PENDING_TIME_STATE_FILE, time_state)

    _telegram_call(
        "editMessageText", chat_id=chat_id, message_id=message_id,
        text=f"📝 {pending['title']}\nProyecto: {project_title}\n¿Vencimiento? ({_DUE_DATE_HINT})",
        reply_markup=_DUE_DATE_KEYBOARD,
    )
    _telegram_call("answerCallbackQuery", callback_query_id=callback_id)


def _handle_new_task_due(callback_id: str, chat_id, message_id, payload: str) -> None:
    state = _load_json(config.PENDING_TASK_STATE_FILE, {})
    pending = state.get(str(chat_id))

    if not pending or "project_title" not in pending:
        _telegram_call(
            "answerCallbackQuery", callback_query_id=callback_id,
            text="No hay ninguna tarea pendiente", show_alert=True,
        )
        return

    state.pop(str(chat_id), None)
    _save_json(config.PENDING_TASK_STATE_FILE, state)

    # A due-date button was pressed instead of a plain-text time/date
    # reply — clear the pending time-entry state so a later message isn't
    # mistaken for a leftover time entry for this already-resolved task.
    with _state_lock:
        time_state = _load_json(config.PENDING_TIME_STATE_FILE, {})
        time_state.pop(str(chat_id), None)
        _save_json(config.PENDING_TIME_STATE_FILE, time_state)

    if payload == "today":
        due_date_iso, due_label = vk._day_to_due_iso(date.today()), "Hoy"
    elif payload == "tomorrow":
        due_date_iso, due_label = vk._day_to_due_iso(date.today() + timedelta(days=1)), "Mañana"
    else:
        config.log.warning("Unknown ntdue payload: %s", payload)
        _telegram_call("answerCallbackQuery", callback_query_id=callback_id)
        return

    try:
        created = vk._vk_put(f"/projects/{pending['project_id']}/tasks", {"title": pending["title"], "due_date": due_date_iso})
    except requests.RequestException as e:
        config.log.error("Could not create task: %s", e)
        _telegram_call("editMessageText", chat_id=chat_id, message_id=message_id, text=f"Error: {e}")
        _telegram_call(
            "answerCallbackQuery", callback_query_id=callback_id, text="Error", show_alert=True
        )
        return

    # A title typed without a "[15m]"-style prefix has no estimate yet —
    # every task must get one, so fall straight into the same duration
    # picker /estimar uses instead of finishing here.
    if vk._parse_estimate_minutes(pending["title"]) is None:
        _telegram_call(
            "editMessageText", chat_id=chat_id, message_id=message_id,
            text=(
                f"✅ Creada: {pending['title']}\nProyecto: {pending['project_title']}\n"
                f"Vencimiento: {due_label}\n⏱ ¿Cuánto estimás que dura?"
            ),
            reply_markup=vk._estimate_duration_keyboard(created["id"]),
        )
        with _state_lock:
            estimate_state = _load_json(config.PENDING_ESTIMATE_STATE_FILE, {})
            estimate_state[str(chat_id)] = {"task_id": created["id"], "message_id": message_id}
            _save_json(config.PENDING_ESTIMATE_STATE_FILE, estimate_state)
        _telegram_call("answerCallbackQuery", callback_query_id=callback_id, text="Creada")
        config.log.info("Created task '%s' for chat %s, prompting for estimate", pending["title"], chat_id)
        return

    _telegram_call(
        "editMessageText", chat_id=chat_id, message_id=message_id,
        text=f"✅ Creada: {pending['title']}\nProyecto: {pending['project_title']}\nVencimiento: {due_label}",
    )
    _telegram_call("answerCallbackQuery", callback_query_id=callback_id, text="Creada")
    config.log.info("Created task '%s' for chat %s", pending["title"], chat_id)
