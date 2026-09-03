"""Time-entry flow: a plain "HH:MM" / "D/M[/Y]" / "D/M[/Y] HH:MM" reply
typed after a due-date prompt (new-task or punt), instead of pressing a
keyboard button."""

from __future__ import annotations

import re
from datetime import date, datetime
from typing import Optional

import requests

from . import config
from . import vikunja as vk
from .punt_flow import _apply_punt
from .state import _load_json, _save_json, _state_lock
from .telegram_api import _telegram_call

_TIME_RE = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)$")
_DATE_TIME_RE = re.compile(r"^(\d{1,2})/(\d{1,2})(?:/(\d{4}|\d{2}))?(?:\s+([01]?\d|2[0-3]):([0-5]\d))?$")


def _resolve_due_reply(text: str) -> Optional[tuple]:
    """Parses a plain-text due-date reply typed after a due-date prompt.
    Accepts "HH:MM" (next occurrence of that time), "D/M[/Y] HH:MM" (that
    date at that time), and "D/M[/Y]" (that date, with no specific time —
    the 23:59 "sin hora" sentinel). Y may be 2 or 4 digits; a 2-digit year
    is taken as 2000+YY. Without an explicit year, "D/M[/Y]" picks the next
    occurrence of that day/month (this year, or next year if it's already
    passed); with a year, it's used as given. Returns (due_date_iso,
    due_label), or None if `text` matches neither format."""
    text = text.strip()

    m = _TIME_RE.match(text)
    if m:
        hour, minute = int(m.group(1)), int(m.group(2))
        due_date_iso = vk._next_occurrence_iso(hour, minute)
        due_label = vk._parse_vikunja_ts(due_date_iso).astimezone().strftime("%Y-%m-%d %H:%M")
        return due_date_iso, due_label

    m = _DATE_TIME_RE.match(text)
    if m:
        day, month, year_s, hour_s, minute_s = m.group(1, 2, 3, 4, 5)
        today = date.today()
        if year_s is None:
            year = today.year
        elif len(year_s) == 2:
            year = 2000 + int(year_s)
        else:
            year = int(year_s)
        try:
            candidate = date(year, int(month), int(day))
        except ValueError:
            return None

        if hour_s is not None:
            hour, minute = int(hour_s), int(minute_s)
            if year_s is None and datetime(candidate.year, candidate.month, candidate.day, hour, minute) <= datetime.now():
                candidate = candidate.replace(year=candidate.year + 1)
            due_date_iso = vk._local_time_to_iso(candidate, hour, minute)
            due_label = vk._parse_vikunja_ts(due_date_iso).astimezone().strftime("%Y-%m-%d %H:%M")
        else:
            if year_s is None and candidate < today:
                candidate = candidate.replace(year=candidate.year + 1)
            due_date_iso = vk._day_to_due_iso(candidate)
            due_label = candidate.strftime("%Y-%m-%d") + ", sin hora"
        return due_date_iso, due_label

    return None


def _reply_to_pending(chat_id, message_id: Optional[int], text: str, reply_markup: Optional[dict] = None) -> None:
    """Edits the message that showed the due-date keyboard to reflect the
    typed-in time, falling back to a new message if it can no longer be
    edited (e.g. too old, or already replaced)."""
    if message_id is not None:
        try:
            _telegram_call(
                "editMessageText", chat_id=chat_id, message_id=message_id, text=text, reply_markup=reply_markup
            )
            return
        except (requests.RequestException, RuntimeError) as e:
            config.log.warning("Could not edit pending message %s, sending new one: %s", message_id, e)
    _telegram_call("sendMessage", chat_id=chat_id, text=text, reply_markup=reply_markup)


def _handle_time_entry(chat_id, text: str, pending: dict) -> None:
    message_id = pending.get("message_id")

    parsed = _resolve_due_reply(text)
    if parsed is None:
        _reply_to_pending(
            chat_id, message_id,
            "Formato inválido. Escribí la hora (HH:MM), una fecha (D/M o D/M/Y) o ambas "
            "(D/M[/Y] HH:MM); ej: 14:30, 5/9, 5/9/2027 o 5/9 13:00.",
        )
        return
    due_date_iso, due_label = parsed

    with _state_lock:
        state = _load_json(config.PENDING_TIME_STATE_FILE, {})
        state.pop(str(chat_id), None)
        _save_json(config.PENDING_TIME_STATE_FILE, state)

    if pending["kind"] == "newtask":
        try:
            created = vk._vk_put(
                f"/projects/{pending['project_id']}/tasks",
                {"title": pending["title"], "due_date": due_date_iso},
            )
        except requests.RequestException as e:
            config.log.error("Could not create task: %s", e)
            _reply_to_pending(chat_id, message_id, f"Error: {e}")
            return

        # See new_task_flow._handle_new_task_due: a title without a
        # "[15m]"-style prefix still needs an estimate before we're done.
        if vk._parse_estimate_minutes(pending["title"]) is None:
            _reply_to_pending(
                chat_id, message_id,
                (
                    f"✅ Creada: {pending['title']}\nProyecto: {pending['project_title']}\n"
                    f"Vencimiento: {due_label}\n⏱ ¿Cuánto estimás que dura?"
                ),
                reply_markup=vk._estimate_duration_keyboard(created["id"]),
            )
            with _state_lock:
                estimate_state = _load_json(config.PENDING_ESTIMATE_STATE_FILE, {})
                estimate_state[str(chat_id)] = {"task_id": created["id"], "message_id": message_id}
                _save_json(config.PENDING_ESTIMATE_STATE_FILE, estimate_state)
            config.log.info("Created task '%s' for chat %s, prompting for estimate", pending["title"], chat_id)
            return

        _reply_to_pending(
            chat_id, message_id,
            f"✅ Creada: {pending['title']}\nProyecto: {pending['project_title']}\nVencimiento: {due_label}",
        )
        config.log.info("Created task '%s' for chat %s", pending["title"], chat_id)
        return

    # kind == "punt"
    task_id = pending["task_id"]
    try:
        task = vk._find_task(task_id)
    except requests.RequestException as e:
        config.log.error("Could not fetch task %s: %s", task_id, e)
        _reply_to_pending(chat_id, message_id, f"Error: {e}")
        return
    if task is None:
        _reply_to_pending(chat_id, message_id, "Esa tarea ya no existe")
        return

    try:
        result_text = _apply_punt(task, task_id, due_date_iso, due_label)
    except requests.RequestException as e:
        config.log.error("Could not punt task %s: %s", task_id, e)
        _reply_to_pending(chat_id, message_id, f"Error: {e}")
        return

    _reply_to_pending(
        chat_id, message_id, result_text,
        reply_markup={"inline_keyboard": [[{"text": "↩️ Deshacer", "callback_data": f"undo:{task_id}"}]]},
    )
