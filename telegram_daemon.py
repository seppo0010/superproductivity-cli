#!/usr/bin/env python3
"""Telegram daemon for sp-cli.

Runs two loops in one process:
  • Main thread: checks Super Productivity for tasks that just became
    overdue and sends a Telegram message per task with action buttons (mark
    done, snooze). Polls at most every 5 minutes, but wakes up sooner when a
    task is already scheduled to become due before then.
  • Background thread: long-polls Telegram for button presses and applies
    them to Super Productivity via the Local REST API.

Prerequisites:
  • Super Productivity desktop app running with the Local REST API enabled
  • TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID set in the environment
"""

from __future__ import annotations

import html
import json
import logging
import os
import sys
import threading
import time
from datetime import datetime, timedelta, time as dtime
from pathlib import Path
from typing import Optional

import requests

BASE_URL = "http://127.0.0.1:3876"
TELEGRAM_API = "https://api.telegram.org/bot{token}/{method}"
CHECK_INTERVAL_SECONDS = 300

STATE_DIR = Path(os.environ.get("SP_CLI_STATE_DIR", Path.home() / ".config" / "sp-cli"))
NOTIFY_STATE_FILE = STATE_DIR / "notify_state.json"
BOT_STATE_FILE = STATE_DIR / "telegram_bot_state.json"
DAILY_DIGEST_STATE_FILE = STATE_DIR / "daily_digest_state.json"
PENDING_TASK_STATE_FILE = STATE_DIR / "pending_task_state.json"
DAILY_DIGEST_HOUR = 7

TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", stream=sys.stdout
)
log = logging.getLogger("sp-cli-telegram")


# ─── State ──────────────────────────────────────────────────────────────────

def _load_json(path: Path, default: dict) -> dict:
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        log.warning("Could not read state file %s, starting fresh", path)
        return default


def _save_json(path: Path, data: dict) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data))


# ─── Super Productivity API ─────────────────────────────────────────────────

def _sp_get(path: str, params: Optional[dict] = None) -> object:
    r = requests.get(f"{BASE_URL}{path}", params=params, timeout=10)
    r.raise_for_status()
    body = r.json()
    if not body.get("ok"):
        raise RuntimeError(body.get("error", {}).get("message", str(body)))
    return body["data"]


def _sp_patch(path: str, body: dict) -> object:
    r = requests.patch(f"{BASE_URL}{path}", json=body, timeout=10)
    r.raise_for_status()
    resp = r.json()
    if not resp.get("ok"):
        raise RuntimeError(resp.get("error", {}).get("message", str(resp)))
    return resp["data"]


def _sp_post(path: str, body: dict) -> object:
    r = requests.post(f"{BASE_URL}{path}", json=body, timeout=10)
    r.raise_for_status()
    resp = r.json()
    if not resp.get("ok"):
        raise RuntimeError(resp.get("error", {}).get("message", str(resp)))
    return resp["data"]


def _find_task(task_id: str) -> Optional[dict]:
    tasks: list = _sp_get("/tasks", {"includeDone": True})
    return next((t for t in tasks if t["id"] == task_id), None)


def _task_due_ms(task: dict) -> Optional[int]:
    # dueDay-only tasks (no time of day) are never considered overdue here —
    # only tasks with an explicit dueWithTime can trigger a notification.
    return task.get("dueWithTime")


def _format_due(task: dict) -> str:
    return datetime.fromtimestamp(task["dueWithTime"] / 1000).strftime("%Y-%m-%d %H:%M")


def _next_9am(now: datetime) -> datetime:
    target_date = now.date() if now.hour < 9 else now.date() + timedelta(days=1)
    return datetime.combine(target_date, dtime(9, 0))


def _today_tasks() -> list:
    """Active tasks due today, either via dueDay or dueWithTime, sorted by time."""
    tasks: list = _sp_get("/tasks")
    today = datetime.now().strftime("%Y-%m-%d")
    result = []
    for t in tasks:
        if t.get("isDone"):
            continue
        due_ms = t.get("dueWithTime")
        if t.get("dueDay") == today:
            result.append(t)
        elif due_ms and datetime.fromtimestamp(due_ms / 1000).strftime("%Y-%m-%d") == today:
            result.append(t)
    result.sort(key=lambda t: t.get("dueWithTime") or 0)
    return result


def _project_title_map() -> dict:
    try:
        projects: list = _sp_get("/projects")
    except (requests.RequestException, RuntimeError) as e:
        log.warning("Could not fetch projects for task list formatting: %s", e)
        return {}
    return {p["id"]: p["title"] for p in projects}


_INBOX_LABEL = "📥 Inbox"


def _format_today_message(tasks: list) -> str:
    if not tasks:
        return "🎉 No hay tareas para hoy."

    project_map = _project_title_map()
    groups: dict[str, list] = {}
    for t in tasks:
        project_title = project_map.get(t.get("projectId"), _INBOX_LABEL) if t.get("projectId") else _INBOX_LABEL
        groups.setdefault(project_title, []).append(t)

    lines = [f"📋 <b>Tareas de hoy</b> ({len(tasks)})"]
    for project_title in sorted(groups, key=lambda p: (p == _INBOX_LABEL, p)):
        lines.append("")
        lines.append(f"<b>{html.escape(project_title)}</b>")
        for t in groups[project_title]:
            due_ms = t.get("dueWithTime")
            time_str = datetime.fromtimestamp(due_ms / 1000).strftime("%H:%M") if due_ms else "──"
            lines.append(f"🕐 {time_str}  {html.escape(t['title'])}")
    return "\n".join(lines)


def _hecho_keyboard(tasks: list) -> dict:
    return {
        "inline_keyboard": [
            [{"text": t["title"], "callback_data": f"done:{t['id']}"}] for t in tasks
        ]
    }


# ─── Telegram API ───────────────────────────────────────────────────────────

def _telegram_call(method: str, **params) -> object:
    url = TELEGRAM_API.format(token=TOKEN, method=method)
    r = requests.post(url, json=params, timeout=35)
    r.raise_for_status()
    body = r.json()
    if not body.get("ok"):
        raise RuntimeError(f"Telegram API error on {method}: {body}")
    return body["result"]


def _send_due_notification(task: dict) -> bool:
    keyboard = {
        "inline_keyboard": [
            [{"text": "✅ Hecha", "callback_data": f"done:{task['id']}"}],
            [
                {"text": "+10 min", "callback_data": f"snooze10:{task['id']}"},
                {"text": "+1 hora", "callback_data": f"snooze60:{task['id']}"},
                {"text": "🌅 9am", "callback_data": f"snooze9am:{task['id']}"},
            ],
        ]
    }
    text = f"⏰ Vencida: {task['title']}\n{_format_due(task)}"
    try:
        _telegram_call("sendMessage", chat_id=CHAT_ID, text=text, reply_markup=keyboard)
        return True
    except (requests.RequestException, RuntimeError) as e:
        log.error("Failed to send Telegram notification for task %s: %s", task["id"], e)
        return False


# ─── Due-task check (main thread, every 5 min) ──────────────────────────────

def check_due_tasks() -> Optional[int]:
    """Sends notifications for newly-overdue tasks. Returns the number of
    seconds until the next task with a known due time becomes due (so the
    caller can wake up sooner instead of waiting the full poll interval),
    or None if there's no upcoming due time to wait for."""
    try:
        tasks: list = _sp_get("/tasks")
    except (requests.RequestException, RuntimeError) as e:
        log.error("Could not fetch tasks: %s", e)
        return None

    now_ms = int(time.time() * 1000)
    overdue = {}
    next_due_ms = None
    for t in tasks:
        if t.get("isDone"):
            continue
        due = _task_due_ms(t)
        if due is None:
            continue
        if due <= now_ms:
            overdue[t["id"]] = t
        elif next_due_ms is None or due < next_due_ms:
            next_due_ms = due

    state = _load_json(NOTIFY_STATE_FILE, {"notified_ids": []})
    notified = set(state.get("notified_ids", []))

    new_ids = [tid for tid in overdue if tid not in notified]
    for tid in new_ids:
        if _send_due_notification(overdue[tid]):
            notified.add(tid)

    notified &= overdue.keys()
    _save_json(NOTIFY_STATE_FILE, {"notified_ids": sorted(notified)})

    if new_ids:
        log.info("Notified %d newly overdue task(s)", len(new_ids))
    else:
        log.info("Checked %d active task(s), none newly overdue", len(tasks))

    if next_due_ms is None:
        return None
    return (next_due_ms - now_ms) // 1000 + 1


# ─── Daily digest (main thread, checked alongside due-task check) ──────────

def check_daily_digest() -> None:
    """Sends the day's task list once per day, the first time the loop runs
    at or after DAILY_DIGEST_HOUR local time."""
    now = datetime.now()
    if now.hour < DAILY_DIGEST_HOUR:
        return

    today = now.strftime("%Y-%m-%d")
    state = _load_json(DAILY_DIGEST_STATE_FILE, {"last_sent_date": None})
    if state.get("last_sent_date") == today:
        return

    try:
        tasks = _today_tasks()
    except (requests.RequestException, RuntimeError) as e:
        log.error("Could not fetch today's tasks for daily digest: %s", e)
        return

    try:
        _telegram_call(
            "sendMessage", chat_id=CHAT_ID, text=_format_today_message(tasks), parse_mode="HTML"
        )
    except (requests.RequestException, RuntimeError) as e:
        log.error("Failed to send daily digest: %s", e)
        return

    _save_json(DAILY_DIGEST_STATE_FILE, {"last_sent_date": today})
    log.info("Sent daily digest (%d task(s))", len(tasks))


# ─── New-task flow (plain messages start a project/due-date prompt) ────────

_DUE_DATE_KEYBOARD = {
    "inline_keyboard": [
        [
            {"text": "Hoy", "callback_data": "ntdue:today"},
            {"text": "Mañana", "callback_data": "ntdue:tomorrow"},
        ],
        [{"text": "Sin fecha", "callback_data": "ntdue:none"}],
    ]
}


def _start_new_task(chat_id, title: str) -> None:
    try:
        projects: list = _sp_get("/projects")
    except (requests.RequestException, RuntimeError) as e:
        log.error("Could not fetch projects: %s", e)
        _telegram_call("sendMessage", chat_id=chat_id, text=f"Error: {e}")
        return
    projects = [p for p in projects if not p.get("isHiddenFromMenu") and not p.get("isArchived")]

    state = _load_json(PENDING_TASK_STATE_FILE, {})
    state[str(chat_id)] = {
        "title": title,
        "project_ids": [p["id"] for p in projects],
        "project_titles": [p["title"] for p in projects],
    }
    _save_json(PENDING_TASK_STATE_FILE, state)

    keyboard = {
        "inline_keyboard": [[{"text": "📥 Inbox", "callback_data": "ntproj:0"}]] + [
            [{"text": p["title"], "callback_data": f"ntproj:{i + 1}"}]
            for i, p in enumerate(projects)
        ]
    }
    _telegram_call(
        "sendMessage", chat_id=chat_id, text=f"📝 {title}\n¿En qué proyecto?", reply_markup=keyboard
    )
    log.info("Started new-task flow for chat %s: %s", chat_id, title)


def _handle_new_task_project(callback_id: str, chat_id, message_id, payload: str) -> None:
    state = _load_json(PENDING_TASK_STATE_FILE, {})
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

    if idx == 0:
        project_id, project_title = None, "Inbox"
    elif 1 <= idx <= len(project_ids):
        project_id = project_ids[idx - 1]
        project_title = pending["project_titles"][idx - 1]
    else:
        _telegram_call(
            "answerCallbackQuery", callback_query_id=callback_id,
            text="Opción inválida", show_alert=True,
        )
        return

    pending["project_id"] = project_id
    pending["project_title"] = project_title
    state[str(chat_id)] = pending
    _save_json(PENDING_TASK_STATE_FILE, state)

    _telegram_call(
        "editMessageText", chat_id=chat_id, message_id=message_id,
        text=f"📝 {pending['title']}\nProyecto: {project_title}\n¿Vencimiento?",
        reply_markup=_DUE_DATE_KEYBOARD,
    )
    _telegram_call("answerCallbackQuery", callback_query_id=callback_id)


def _handle_new_task_due(callback_id: str, chat_id, message_id, payload: str) -> None:
    state = _load_json(PENDING_TASK_STATE_FILE, {})
    pending = state.pop(str(chat_id), None)
    _save_json(PENDING_TASK_STATE_FILE, state)

    if not pending or "project_title" not in pending:
        _telegram_call(
            "answerCallbackQuery", callback_query_id=callback_id,
            text="No hay ninguna tarea pendiente", show_alert=True,
        )
        return

    due_day = None
    due_label = "Sin fecha"
    if payload == "today":
        due_day = datetime.now().strftime("%Y-%m-%d")
        due_label = "Hoy"
    elif payload == "tomorrow":
        due_day = (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%d")
        due_label = "Mañana"

    body: dict = {"title": pending["title"]}
    if pending.get("project_id"):
        body["projectId"] = pending["project_id"]
    if due_day:
        body["dueDay"] = due_day

    try:
        _sp_post("/tasks", body)
    except (requests.RequestException, RuntimeError) as e:
        log.error("Could not create task: %s", e)
        _telegram_call("editMessageText", chat_id=chat_id, message_id=message_id, text=f"Error: {e}")
        _telegram_call(
            "answerCallbackQuery", callback_query_id=callback_id, text="Error", show_alert=True
        )
        return

    _telegram_call(
        "editMessageText", chat_id=chat_id, message_id=message_id,
        text=f"✅ Creada: {pending['title']}\nProyecto: {pending['project_title']}\nVencimiento: {due_label}",
    )
    _telegram_call("answerCallbackQuery", callback_query_id=callback_id, text="Creada")
    log.info("Created task '%s' for chat %s", pending["title"], chat_id)


# ─── Button handling (background thread, continuous) ───────────────────────

def _handle_callback(callback: dict) -> None:
    callback_id = callback["id"]
    data = callback.get("data", "")
    message = callback.get("message", {})
    chat_id = message.get("chat", {}).get("id")
    message_id = message.get("message_id")

    try:
        action, payload = data.split(":", 1)
    except ValueError:
        log.warning("Malformed callback_data: %s", data)
        _telegram_call("answerCallbackQuery", callback_query_id=callback_id)
        return

    if action == "ntproj":
        _handle_new_task_project(callback_id, chat_id, message_id, payload)
        return
    if action == "ntdue":
        _handle_new_task_due(callback_id, chat_id, message_id, payload)
        return
    task_id = payload

    try:
        task = _find_task(task_id)
    except (requests.RequestException, RuntimeError) as e:
        log.error("Could not fetch task %s: %s", task_id, e)
        _telegram_call(
            "answerCallbackQuery", callback_query_id=callback_id,
            text=f"Error: {e}", show_alert=True,
        )
        return

    if task is None:
        log.warning("Task %s no longer exists", task_id)
        _telegram_call(
            "answerCallbackQuery", callback_query_id=callback_id,
            text="Esa tarea ya no existe", show_alert=True,
        )
        return

    try:
        if action == "done":
            _sp_patch(f"/tasks/{task_id}", {"isDone": True})
            result_text = f"✅ {task['title']} — hecha"
        elif action in ("snooze10", "snooze60"):
            delta_ms = 10 * 60_000 if action == "snooze10" else 60 * 60_000
            new_ts = int(time.time() * 1000) + delta_ms
            _sp_patch(f"/tasks/{task_id}", {"dueWithTime": new_ts})
            result_text = f"⏰ {task['title']} — pospuesta a las {datetime.fromtimestamp(new_ts / 1000).strftime('%H:%M')}"
        elif action == "snooze9am":
            target = _next_9am(datetime.now())
            new_ts = int(target.timestamp() * 1000)
            _sp_patch(f"/tasks/{task_id}", {"dueWithTime": new_ts})
            result_text = f"⏰ {task['title']} — pospuesta a {target.strftime('%Y-%m-%d %H:%M')}"
        else:
            log.warning("Unknown action: %s", action)
            _telegram_call("answerCallbackQuery", callback_query_id=callback_id)
            return
    except (requests.RequestException, RuntimeError) as e:
        log.error("Could not apply action %s to task %s: %s", action, task_id, e)
        _telegram_call(
            "answerCallbackQuery", callback_query_id=callback_id,
            text=f"Error: {e}", show_alert=True,
        )
        return

    try:
        _telegram_call("editMessageText", chat_id=chat_id, message_id=message_id, text=result_text)
    except (requests.RequestException, RuntimeError) as e:
        log.error("Could not edit Telegram message: %s", e)
    _telegram_call("answerCallbackQuery", callback_query_id=callback_id, text="Listo")
    log.info("Handled '%s' for task %s", action, task_id)


def _handle_message(message: dict) -> None:
    chat_id = message.get("chat", {}).get("id")
    if str(chat_id) != str(CHAT_ID):
        return
    text = (message.get("text") or "").strip()
    if not text:
        return

    command = text.split("@", 1)[0]

    if command in ("/hoy", "/today"):
        try:
            tasks = _today_tasks()
        except (requests.RequestException, RuntimeError) as e:
            log.error("Could not fetch today's tasks: %s", e)
            _telegram_call("sendMessage", chat_id=chat_id, text=f"Error: {e}")
            return

        _telegram_call(
            "sendMessage", chat_id=chat_id, text=_format_today_message(tasks), parse_mode="HTML"
        )
        log.info("Sent today's task list (%d task(s)) to chat %s", len(tasks), chat_id)
        return

    if command == "/hecho":
        try:
            tasks = _today_tasks()
        except (requests.RequestException, RuntimeError) as e:
            log.error("Could not fetch today's tasks: %s", e)
            _telegram_call("sendMessage", chat_id=chat_id, text=f"Error: {e}")
            return

        if not tasks:
            _telegram_call("sendMessage", chat_id=chat_id, text="🎉 No hay tareas pendientes hoy.")
            return

        _telegram_call(
            "sendMessage", chat_id=chat_id, text="¿Qué tarea marcamos como hecha?",
            reply_markup=_hecho_keyboard(tasks),
        )
        log.info("Sent /hecho task picker (%d task(s)) to chat %s", len(tasks), chat_id)
        return

    if text.startswith("/"):
        return
    _start_new_task(chat_id, text)


def poll_telegram_updates() -> None:
    state = _load_json(BOT_STATE_FILE, {"offset": 0})
    offset = state.get("offset", 0)
    log.info("Starting Telegram button listener (offset=%d)", offset)
    while True:
        try:
            updates = _telegram_call(
                "getUpdates", offset=offset, timeout=30,
                allowed_updates=["callback_query", "message"],
            )
        except (requests.RequestException, RuntimeError) as e:
            log.error("getUpdates failed, retrying: %s", e)
            time.sleep(5)
            continue

        for update in updates:
            offset = update["update_id"] + 1
            callback = update.get("callback_query")
            message = update.get("message")
            if callback:
                try:
                    _handle_callback(callback)
                except (requests.RequestException, RuntimeError):
                    log.exception("Unhandled error processing callback")
            elif message:
                try:
                    _handle_message(message)
                except (requests.RequestException, RuntimeError):
                    log.exception("Unhandled error processing message")
            _save_json(BOT_STATE_FILE, {"offset": offset})


def main() -> None:
    if not TOKEN or not CHAT_ID:
        log.error("TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID must be set in the environment")
        sys.exit(1)

    try:
        _telegram_call(
            "setMyCommands",
            commands=[
                {"command": "hoy", "description": "Tareas de hoy"},
                {"command": "hecho", "description": "Marcar una tarea de hoy como hecha"},
            ],
        )
    except (requests.RequestException, RuntimeError) as e:
        log.warning("Could not register bot commands: %s", e)

    threading.Thread(target=poll_telegram_updates, daemon=True).start()

    while True:
        next_due_in = check_due_tasks()
        check_daily_digest()
        sleep_seconds = CHECK_INTERVAL_SECONDS
        if next_due_in is not None:
            sleep_seconds = min(CHECK_INTERVAL_SECONDS, next_due_in)
        time.sleep(sleep_seconds)


if __name__ == "__main__":
    main()
