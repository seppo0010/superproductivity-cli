#!/usr/bin/env python3
"""Telegram daemon for sp-cli, backed by Vikunja.

Runs three things in one process:
  • A webhook receiver (background thread) that Vikunja POSTs to on
    task.overdue / tasks.overdue events (a user-level webhook you register
    yourself in Vikunja — see README). This is the fast path for "a task
    just became overdue" notifications.
  • Main thread: a safety-net reconciliation pass that re-checks for any
    overdue task not yet notified, in case a webhook delivery was missed
    (daemon downtime, network blip). Runs at most every
    CHECK_INTERVAL_SECONDS, but wakes up sooner when a task is already
    scheduled to become due before then.
  • Background thread: long-polls Telegram for button presses and commands
    and applies them to Vikunja via its REST API.

Prerequisites:
  • VIKUNJA_URL, VIKUNJA_TOKEN set in the environment (API token from
    Vikunja → Settings → API Tokens)
  • VIKUNJA_WEBHOOK_SECRET set to the secret used when you registered the
    user-level webhook (Vikunja → user settings → Webhooks) pointing at
    http://<this-host>:VIKUNJA_WEBHOOK_PORT/webhook, events task.overdue
    and tasks.overdue
  • TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID set in the environment
"""

from __future__ import annotations

import hashlib
import hmac
import html
import http.server
import json
import logging
import os
import re
import sys
import threading
import time
from datetime import date, datetime, timedelta, timezone, time as dtime
from pathlib import Path
from typing import Optional

import requests

VIKUNJA_URL = os.environ.get("VIKUNJA_URL", "http://192.168.0.9:3456").rstrip("/")
API_BASE = f"{VIKUNJA_URL}/api/v1"
VIKUNJA_TOKEN = os.environ.get("VIKUNJA_TOKEN")
WEBHOOK_SECRET = os.environ.get("VIKUNJA_WEBHOOK_SECRET")
WEBHOOK_HOST = os.environ.get("VIKUNJA_WEBHOOK_HOST", "0.0.0.0")
WEBHOOK_PORT = int(os.environ.get("VIKUNJA_WEBHOOK_PORT", "8765"))

TELEGRAM_API = "https://api.telegram.org/bot{token}/{method}"
CHECK_INTERVAL_SECONDS = 1800  # safety-net reconciliation cadence

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

_state_lock = threading.Lock()


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


# ─── Vikunja API ─────────────────────────────────────────────────────────────

def _vk_headers() -> dict:
    return {"Authorization": f"Bearer {VIKUNJA_TOKEN}"}


def _vk_unwrap(r: requests.Response) -> object:
    r.raise_for_status()
    return r.json()


def _vk_get(path: str, params: Optional[dict] = None) -> object:
    params = dict(params or {})
    params.setdefault("per_page", 250)
    r = requests.get(f"{API_BASE}{path}", params=params, headers=_vk_headers(), timeout=10)
    return _vk_unwrap(r)


def _vk_put(path: str, body: dict) -> object:
    r = requests.put(f"{API_BASE}{path}", json=body, headers=_vk_headers(), timeout=10)
    return _vk_unwrap(r)


def _vk_post(path: str, body: dict) -> object:
    r = requests.post(f"{API_BASE}{path}", json=body, headers=_vk_headers(), timeout=10)
    return _vk_unwrap(r)


def _vk_delete(path: str) -> object:
    r = requests.delete(f"{API_BASE}{path}", headers=_vk_headers(), timeout=10)
    return _vk_unwrap(r)


def _vk_task_update(task_id: int, changes: dict) -> dict:
    """Fetch-merge-write: Vikunja's task update resets fields omitted from
    the request body, so a naive partial PATCH silently destroys data."""
    current: dict = _vk_get(f"/tasks/{task_id}")
    current.update(changes)
    return _vk_post(f"/tasks/{task_id}", current)


def _find_task(task_id) -> Optional[dict]:
    try:
        return _vk_get(f"/tasks/{task_id}")
    except requests.HTTPError as e:
        if e.response is not None and e.response.status_code == 404:
            return None
        raise


def _parse_vikunja_ts(ds: str) -> Optional[datetime]:
    if not ds or ds.startswith("0001-01-01"):
        return None
    m = re.match(r"(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2}):(\d{2})", ds)
    if not m:
        return None
    y, mo, d, h, mi, s = (int(g) for g in m.groups())
    return datetime(y, mo, d, h, mi, s, tzinfo=timezone.utc)


def _task_due_dt(task: dict) -> Optional[datetime]:
    """The due datetime for tasks with an explicit time-of-day. Never
    considered "overdue" (or shown with a time) for either of the two
    "no specific time" conventions in use: the literal-UTC-midnight sentinel
    from migrated data, and 23:59 local time — used for tasks created
    directly in Vikunja, which has no native date-only due date, as a
    "due sometime today" marker."""
    dt = _parse_vikunja_ts(task.get("due_date", ""))
    if dt is None or (dt.hour, dt.minute, dt.second) == (0, 0, 0):
        return None
    local = dt.astimezone()
    if (local.hour, local.minute) == (23, 59):
        return None
    return dt


def _task_local_date(task: dict) -> Optional[date]:
    dt = _parse_vikunja_ts(task.get("due_date", ""))
    if dt is None:
        return None
    if (dt.hour, dt.minute, dt.second) == (0, 0, 0):
        return dt.date()
    return dt.astimezone().date()


def _format_due(task: dict) -> str:
    dt = _task_due_dt(task)
    if dt is None:
        return ""
    return dt.astimezone().strftime("%Y-%m-%d %H:%M")


def _next_9am(now: datetime) -> datetime:
    target_date = now.date() if now.hour < 9 else now.date() + timedelta(days=1)
    return datetime.combine(target_date, dtime(9, 0))


def _today_tasks() -> list:
    """Active tasks due today, sorted by time (day-only tasks first)."""
    tasks: list = _vk_get("/tasks", {"filter": "done = false"})
    today = date.today()
    result = [t for t in tasks if _task_local_date(t) == today]
    result.sort(key=lambda t: (_task_due_dt(t) or datetime.min.replace(tzinfo=timezone.utc)))
    return result


_INBOX_LABEL = "📥 Inbox"


def _project_display(project: dict) -> str:
    """Vikunja has no dedicated emoji field for projects, so emoji are
    embedded directly in the title (e.g. "🌱Vivir") except for the built-in
    Inbox project, which stays plain and gets one prepended here."""
    if project["title"] == "Inbox":
        return f"📥 {project['title']}"
    return project["title"]


def _real_projects() -> list:
    """Projects tasks can actually be created in — excludes archived
    projects and Vikunja's virtual saved-filter pseudo-projects (negative
    ids, e.g. "My Open Tasks")."""
    try:
        projects: list = _vk_get("/projects")
    except requests.RequestException as e:
        log.warning("Could not fetch projects: %s", e)
        return []
    return [p for p in projects if p["id"] > 0 and not p.get("is_archived")]


def _project_title_map() -> dict:
    return {p["id"]: _project_display(p) for p in _real_projects()}


def _format_today_message(tasks: list) -> str:
    if not tasks:
        return "🎉 No hay tareas para hoy."

    project_map = _project_title_map()
    groups: dict[str, list] = {}
    for t in tasks:
        project_title = project_map.get(t.get("project_id"), _INBOX_LABEL)
        groups.setdefault(project_title, []).append(t)

    lines = [f"📋 <b>Tareas de hoy</b> ({len(tasks)})"]
    for project_title in sorted(groups, key=lambda p: (p == _INBOX_LABEL, p)):
        lines.append("")
        lines.append(f"<b>{html.escape(project_title)}</b>")
        for t in groups[project_title]:
            due_dt = _task_due_dt(t)
            time_str = due_dt.astimezone().strftime("%H:%M") if due_dt else "──"
            lines.append(f"🕐 {time_str}  {html.escape(t['title'])}")
    return "\n".join(lines)


def _hecho_button_label(task: dict, project_map: dict) -> str:
    due_dt = _task_due_dt(task)
    time_str = due_dt.astimezone().strftime("%H:%M") if due_dt else "──"
    project_title = project_map.get(task.get("project_id"), _INBOX_LABEL)
    return f"{time_str} · {project_title} · {task['title']}"


def _task_picker_keyboard(tasks: list, action: str) -> dict:
    project_map = _project_title_map()
    return {
        "inline_keyboard": [
            [{"text": _hecho_button_label(t, project_map), "callback_data": f"{action}:{t['id']}"}] for t in tasks
        ] + [[{"text": "❌ Cancelar", "callback_data": "hcancel:0"}]]
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


# ─── Overdue notification state (shared between webhook + reconciliation) ──

def _notify_if_new(task: dict, notified: set) -> None:
    tid = task["id"]
    if tid in notified or task.get("done"):
        return
    if _send_due_notification(task):
        notified.add(tid)


# ─── Webhook receiver (background thread, push path) ────────────────────────

def _verify_signature(raw_body: bytes, signature: str) -> bool:
    if not WEBHOOK_SECRET:
        return True
    mac = hmac.new(WEBHOOK_SECRET.encode(), raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(mac, signature or "")


def _extract_tasks_from_webhook_data(data: dict) -> list:
    if isinstance(data.get("task"), dict):
        return [data["task"]]
    if isinstance(data.get("tasks"), list):
        return data["tasks"]
    return []


def _handle_webhook_payload(payload: dict) -> None:
    event = payload.get("event_name", "")
    data = payload.get("data") or {}
    log.info("Webhook event received: %s", event)

    if event not in ("task.overdue", "tasks.overdue"):
        return

    tasks = _extract_tasks_from_webhook_data(data)
    if not tasks:
        log.warning("Overdue webhook event %s had no recognizable task(s) in payload: %s", event, json.dumps(data)[:2000])
        return

    with _state_lock:
        state = _load_json(NOTIFY_STATE_FILE, {"notified_ids": []})
        notified = set(state.get("notified_ids", []))
        for t in tasks:
            _notify_if_new(t, notified)
        _save_json(NOTIFY_STATE_FILE, {"notified_ids": sorted(notified)})


class _WebhookHandler(http.server.BaseHTTPRequestHandler):
    def do_POST(self):
        if self.path != "/webhook":
            self.send_response(404)
            self.end_headers()
            return
        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length)
        signature = self.headers.get("X-Vikunja-Signature", "")
        if not _verify_signature(raw, signature):
            log.warning("Webhook signature mismatch, ignoring request")
            self.send_response(401)
            self.end_headers()
            return
        self.send_response(200)
        self.end_headers()
        try:
            _handle_webhook_payload(json.loads(raw))
        except (json.JSONDecodeError, KeyError, TypeError):
            log.exception("Could not parse webhook payload")

    def log_message(self, format, *args):
        log.info("webhook: " + format, *args)


def _run_webhook_server() -> None:
    server = http.server.ThreadingHTTPServer((WEBHOOK_HOST, WEBHOOK_PORT), _WebhookHandler)
    log.info("Webhook receiver listening on %s:%d", WEBHOOK_HOST, WEBHOOK_PORT)
    server.serve_forever()


# ─── Reconciliation pass (main thread, safety net) ──────────────────────────

def check_due_tasks() -> Optional[int]:
    """Safety net for missed webhook deliveries: sends notifications for any
    newly-overdue task not already notified. Returns the number of seconds
    until the next task with a known due time becomes due (so the caller can
    wake up sooner instead of waiting the full poll interval), or None if
    there's no upcoming due time to wait for."""
    try:
        tasks: list = _vk_get("/tasks", {"filter": "done = false"})
    except requests.RequestException as e:
        log.error("Could not fetch tasks: %s", e)
        return None

    now = datetime.now(timezone.utc)
    overdue = {}
    next_due_in = None
    for t in tasks:
        due_dt = _task_due_dt(t)
        if due_dt is None:
            continue
        if due_dt <= now:
            overdue[t["id"]] = t
        else:
            seconds = (due_dt - now).total_seconds()
            if next_due_in is None or seconds < next_due_in:
                next_due_in = seconds

    with _state_lock:
        state = _load_json(NOTIFY_STATE_FILE, {"notified_ids": []})
        notified = set(state.get("notified_ids", []))

        new_ids = [tid for tid in overdue if tid not in notified]
        for tid in new_ids:
            _notify_if_new(overdue[tid], notified)

        notified &= overdue.keys()
        _save_json(NOTIFY_STATE_FILE, {"notified_ids": sorted(notified)})

    if new_ids:
        log.info("Reconciliation notified %d newly overdue task(s)", len(new_ids))
    else:
        log.info("Reconciliation checked %d active task(s), none newly overdue", len(tasks))

    if next_due_in is None:
        return None
    return int(next_due_in) + 1


# ─── Daily digest (main thread, checked alongside reconciliation) ──────────

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
    except requests.RequestException as e:
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
    projects = _real_projects()
    if not projects:
        _telegram_call("sendMessage", chat_id=chat_id, text="Error: no se pudieron obtener los proyectos")
        return

    state = _load_json(PENDING_TASK_STATE_FILE, {})
    state[str(chat_id)] = {
        "title": title,
        "project_ids": [p["id"] for p in projects],
        "project_titles": [_project_display(p) for p in projects],
    }
    _save_json(PENDING_TASK_STATE_FILE, state)

    keyboard = {
        "inline_keyboard": [
            [{"text": _project_display(p), "callback_data": f"ntproj:{i}"}]
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

    due_date_iso = None
    due_label = "Sin fecha"
    if payload == "today":
        due_date_iso = date.today().strftime("%Y-%m-%dT00:00:00Z")
        due_label = "Hoy"
    elif payload == "tomorrow":
        due_date_iso = (date.today() + timedelta(days=1)).strftime("%Y-%m-%dT00:00:00Z")
        due_label = "Mañana"

    body: dict = {"title": pending["title"]}
    if due_date_iso:
        body["due_date"] = due_date_iso

    try:
        _vk_put(f"/projects/{pending['project_id']}/tasks", body)
    except requests.RequestException as e:
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
    if action == "hcancel":
        _telegram_call("editMessageText", chat_id=chat_id, message_id=message_id, text="Cancelado")
        _telegram_call("answerCallbackQuery", callback_query_id=callback_id)
        return
    task_id = int(payload)

    try:
        task = _find_task(task_id)
    except requests.RequestException as e:
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
            _vk_task_update(task_id, {"done": True})
            result_text = f"✅ {task['title']} — hecha"
        elif action == "delete":
            _vk_delete(f"/tasks/{task_id}")
            result_text = f"🗑️ {task['title']} — borrada"
        elif action in ("snooze10", "snooze60"):
            delta = timedelta(minutes=10) if action == "snooze10" else timedelta(hours=1)
            new_dt = datetime.now(timezone.utc) + delta
            _vk_task_update(task_id, {"due_date": new_dt.strftime("%Y-%m-%dT%H:%M:%SZ")})
            result_text = f"⏰ {task['title']} — pospuesta a las {new_dt.astimezone().strftime('%H:%M')}"
        elif action == "snooze9am":
            target = _next_9am(datetime.now())
            new_dt = target.astimezone(timezone.utc)
            _vk_task_update(task_id, {"due_date": new_dt.strftime("%Y-%m-%dT%H:%M:%SZ")})
            result_text = f"⏰ {task['title']} — pospuesta a {target.strftime('%Y-%m-%d %H:%M')}"
        else:
            log.warning("Unknown action: %s", action)
            _telegram_call("answerCallbackQuery", callback_query_id=callback_id)
            return
    except requests.RequestException as e:
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
        except requests.RequestException as e:
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
        except requests.RequestException as e:
            log.error("Could not fetch today's tasks: %s", e)
            _telegram_call("sendMessage", chat_id=chat_id, text=f"Error: {e}")
            return

        if not tasks:
            _telegram_call("sendMessage", chat_id=chat_id, text="🎉 No hay tareas pendientes hoy.")
            return

        _telegram_call(
            "sendMessage", chat_id=chat_id, text="¿Qué tarea marcamos como hecha?",
            reply_markup=_task_picker_keyboard(tasks, "done"),
        )
        log.info("Sent /hecho task picker (%d task(s)) to chat %s", len(tasks), chat_id)
        return

    if command == "/borrar":
        try:
            tasks = _today_tasks()
        except requests.RequestException as e:
            log.error("Could not fetch today's tasks: %s", e)
            _telegram_call("sendMessage", chat_id=chat_id, text=f"Error: {e}")
            return

        if not tasks:
            _telegram_call("sendMessage", chat_id=chat_id, text="🎉 No hay tareas pendientes hoy.")
            return

        _telegram_call(
            "sendMessage", chat_id=chat_id, text="¿Qué tarea borramos?",
            reply_markup=_task_picker_keyboard(tasks, "delete"),
        )
        log.info("Sent /borrar task picker (%d task(s)) to chat %s", len(tasks), chat_id)
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
    if not VIKUNJA_TOKEN:
        log.error("VIKUNJA_TOKEN must be set in the environment")
        sys.exit(1)
    if not WEBHOOK_SECRET:
        log.warning("VIKUNJA_WEBHOOK_SECRET not set — incoming webhooks will not be signature-verified")

    try:
        _telegram_call(
            "setMyCommands",
            commands=[
                {"command": "hoy", "description": "Tareas de hoy"},
                {"command": "hecho", "description": "Marcar una tarea de hoy como hecha"},
                {"command": "borrar", "description": "Borrar una tarea de hoy"},
            ],
        )
    except (requests.RequestException, RuntimeError) as e:
        log.warning("Could not register bot commands: %s", e)

    threading.Thread(target=poll_telegram_updates, daemon=True).start()
    threading.Thread(target=_run_webhook_server, daemon=True).start()

    while True:
        next_due_in = check_due_tasks()
        check_daily_digest()
        sleep_seconds = CHECK_INTERVAL_SECONDS
        if next_due_in is not None:
            sleep_seconds = min(CHECK_INTERVAL_SECONDS, next_due_in)
        time.sleep(sleep_seconds)


if __name__ == "__main__":
    main()
