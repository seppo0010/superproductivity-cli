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
from datetime import date, datetime, timedelta, timezone
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
CHECK_INTERVAL_SECONDS = 300  # safety-net reconciliation cadence

STATE_DIR = Path(os.environ.get("SP_CLI_STATE_DIR", Path.home() / ".config" / "sp-cli"))
NOTIFY_STATE_FILE = STATE_DIR / "notify_state.json"
BOT_STATE_FILE = STATE_DIR / "telegram_bot_state.json"
DAILY_DIGEST_STATE_FILE = STATE_DIR / "daily_digest_state.json"
PENDING_TASK_STATE_FILE = STATE_DIR / "pending_task_state.json"
PENDING_TIME_STATE_FILE = STATE_DIR / "pending_time_state.json"
UNDO_STATE_FILE = STATE_DIR / "undo_state.json"
AVAILABILITY_STATE_FILE = STATE_DIR / "availability.json"
DAILY_DIGEST_HOUR = 6

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
    """GET, following pagination when the response is a list. Vikunja caps
    per_page server-side (observed: 50) regardless of what's requested, so
    a single request silently drops later items — fetch every page."""
    params = dict(params or {})
    params.setdefault("per_page", 250)
    page = 1
    results = None
    while True:
        params["page"] = page
        r = requests.get(f"{API_BASE}{path}", params=params, headers=_vk_headers(), timeout=10)
        body = _vk_unwrap(r)
        if not isinstance(body, list):
            return body
        if results is None:
            results = body
        else:
            results.extend(body)
        total_pages = int(r.headers.get("x-pagination-total-pages", 1))
        if page >= total_pages or not body:
            return results
        page += 1


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


def _local_time_to_iso(day: date, hour: int, minute: int) -> str:
    """Convert a local wall-clock time on `day` to a UTC ISO timestamp."""
    local_dt = datetime(day.year, day.month, day.day, hour, minute, 0).astimezone()
    return local_dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _day_to_due_iso(day: date) -> str:
    """A day-only due date is stored as 23:59 local time converted to UTC —
    Vikunja's own convention for date-only due dates (it has no native
    date-only field). Using literal UTC midnight instead (the convention for
    already-migrated SP data) displays as the previous evening in Vikunja's
    own UI for negative-UTC-offset timezones."""
    return _local_time_to_iso(day, 23, 59)


def _task_due_dt(task: dict) -> Optional[datetime]:
    """The due datetime for tasks with an explicit time-of-day. Never
    considered "overdue" (or shown with a time) for 23:59 local time — the
    sole "no specific time" convention (used for tasks with no native
    date-only due date, as a "due sometime today" marker). A literal-UTC-
    midnight sentinel used to be treated the same way for migrated data, but
    that collided with any genuine local due time that happens to convert to
    UTC midnight (e.g. 21:00 local for UTC-3), silently swallowing real
    reminders — retired in favor of this single convention."""
    dt = _parse_vikunja_ts(task.get("due_date", ""))
    if dt is None:
        return None
    local = dt.astimezone()
    if (local.hour, local.minute) == (23, 59):
        return None
    return dt


def _task_local_date(task: dict) -> Optional[date]:
    dt = _parse_vikunja_ts(task.get("due_date", ""))
    if dt is None:
        return None
    return dt.astimezone().date()


def _format_due(task: dict) -> str:
    dt = _task_due_dt(task)
    if dt is None:
        return ""
    return dt.astimezone().strftime("%Y-%m-%d %H:%M")


def _next_occurrence_iso(hour: int, minute: int) -> str:
    """UTC ISO timestamp for the next local wall-clock HH:MM — today if that
    time hasn't passed yet, tomorrow otherwise."""
    now_local = datetime.now()
    candidate = now_local.replace(hour=hour, minute=minute, second=0, microsecond=0)
    target_date = candidate.date() if candidate > now_local else candidate.date() + timedelta(days=1)
    return _local_time_to_iso(target_date, hour, minute)


def _tasks_for_date(day: date) -> list:
    """Active tasks due on `day`, sorted by time (day-only tasks first)."""
    tasks: list = _vk_get("/tasks", {"filter": "done = false"})
    result = [t for t in tasks if _task_local_date(t) == day]
    result.sort(key=lambda t: (_task_due_dt(t) or datetime.min.replace(tzinfo=timezone.utc)))
    return result


def _tasks_by_date(start: date, days: int) -> dict:
    """Active tasks due within [start, start+days-1], grouped by local
    calendar day. A single /tasks fetch, unlike calling _tasks_for_date once
    per day."""
    end = start + timedelta(days=days - 1)
    tasks: list = _vk_get("/tasks", {"filter": "done = false"})
    by_date: dict = {}
    for t in tasks:
        d = _task_local_date(t)
        if d is not None and start <= d <= end:
            by_date.setdefault(d, []).append(t)
    return by_date


def _parse_day_arg(arg: str) -> Optional[date]:
    """Parse a /day argument: 'hoy'/'today', 'mañana'/'manana'/'tomorrow', or
    an explicit YYYY-MM-DD. None if unparseable."""
    arg = arg.strip().lower()
    if arg in ("hoy", "today"):
        return date.today()
    if arg in ("mañana", "manana", "tomorrow"):
        return date.today() + timedelta(days=1)
    try:
        return datetime.strptime(arg, "%Y-%m-%d").date()
    except ValueError:
        return None


def _today_tasks() -> list:
    return _tasks_for_date(date.today())


def _tomorrow_tasks() -> list:
    return _tasks_for_date(date.today() + timedelta(days=1))


def _overdue_tasks() -> list:
    """Active tasks whose due date's local calendar day is before today.
    Distinct from check_due_tasks's notion of overdue (which fires the
    instant a due *time* passes): date-only tasks (23:59 "sin hora"
    convention) only show up here once their whole day has elapsed."""
    tasks: list = _vk_get("/tasks", {"filter": "done = false"})
    today = date.today()
    result = [t for t in tasks if (d := _task_local_date(t)) is not None and d < today]
    result.sort(key=lambda t: _task_local_date(t))
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


_ESTIMATE_RE = re.compile(r"^\[([^\]]*)\]")


def _parse_estimate_minutes(title: str) -> Optional[int]:
    """Parse a leading '[5m]' / '[1h30m]' / '[0]' estimate prefix into minutes,
    or None if the title has no such prefix (or it doesn't parse)."""
    m = _ESTIMATE_RE.match(title.strip())
    if not m:
        return None
    content = m.group(1)
    if content == "0":
        return 0
    hm = re.fullmatch(r"(?:(\d+)h)?(?:(\d+)m)?", content)
    if not hm or not (hm.group(1) or hm.group(2)):
        return None
    return int(hm.group(1) or 0) * 60 + int(hm.group(2) or 0)


def _format_minutes(total: int) -> str:
    h, m = divmod(total, 60)
    if h and m:
        return f"{h}h{m}m"
    if h:
        return f"{h}h"
    return f"{m}m"


_TARJETA_LABEL_TITLE = "Para tarjeta"


def _find_label_by_title(title: str) -> Optional[dict]:
    labels: list = _vk_get("/labels")
    return next((l for l in labels if l["title"] == title), None)


def _zero_out_estimate(title: str) -> str:
    """Replace the leading '[...]' estimate prefix with '[0]' (or add one if
    there's none)."""
    stripped = title.strip()
    m = _ESTIMATE_RE.match(stripped)
    if m:
        rest = stripped[m.end():].lstrip()
    else:
        rest = stripped
    return f"[0] {rest}" if rest else "[0]"


_WEEKDAY_NAMES = {
    "lunes": 0, "martes": 1, "miercoles": 2, "miércoles": 2, "jueves": 3,
    "viernes": 4, "sabado": 5, "sábado": 5, "domingo": 6,
}
_WEEKDAY_DISPLAY = ["Lunes", "Martes", "Miércoles", "Jueves", "Viernes", "Sábado", "Domingo"]
_WEEKDAY_SHORT = ["Lun", "Mar", "Mié", "Jue", "Vie", "Sáb", "Dom"]


def _parse_duration_minutes(s: str) -> Optional[int]:
    """Parse a duration like '3h', '7h30m', '0', or a bare number (hours)."""
    s = s.strip().lower()
    if not s:
        return None
    if s == "0":
        return 0
    hm = re.fullmatch(r"(?:(\d+)h)?(?:(\d+)m)?", s)
    if hm and (hm.group(1) or hm.group(2)):
        return int(hm.group(1) or 0) * 60 + int(hm.group(2) or 0)
    if re.fullmatch(r"\d+(\.\d+)?", s):
        return round(float(s) * 60)
    return None


def _parse_availability_target(s: str) -> Optional[tuple]:
    """Returns ('weekday', '1') or ('date', '2026-08-21'), or None."""
    s = s.strip().lower()
    if s in _WEEKDAY_NAMES:
        return ("weekday", str(_WEEKDAY_NAMES[s]))
    try:
        datetime.strptime(s, "%Y-%m-%d")
        return ("date", s)
    except ValueError:
        return None


def _availability_minutes_for(day: date) -> Optional[int]:
    """A specific date overrides the weekday default. None means no limit
    has been configured for that day."""
    config = _load_json(AVAILABILITY_STATE_FILE, {"weekday": {}, "date": {}})
    date_key = day.strftime("%Y-%m-%d")
    if date_key in config.get("date", {}):
        return config["date"][date_key]
    weekday_key = str(day.weekday())
    if weekday_key in config.get("weekday", {}):
        return config["weekday"][weekday_key]
    return None


def _format_availability_config() -> str:
    config = _load_json(AVAILABILITY_STATE_FILE, {"weekday": {}, "date": {}})
    lines = ["<b>Disponibilidad configurada</b>", ""]

    weekday_cfg = config.get("weekday", {})
    if weekday_cfg:
        for idx in sorted(weekday_cfg, key=int):
            lines.append(f"{_WEEKDAY_DISPLAY[int(idx)]}: {_format_minutes(weekday_cfg[idx])}")
    else:
        lines.append("(sin días de semana configurados)")

    date_cfg = config.get("date", {})
    if date_cfg:
        lines.append("")
        for d in sorted(date_cfg):
            lines.append(f"{d}: {_format_minutes(date_cfg[d])}")

    lines.append("")
    lines.append(
        "Usá /disponibilidad &lt;día|fecha&gt; &lt;horas&gt; para configurar "
        "(ej: <code>/disponibilidad martes 3h</code>, <code>/disponibilidad 2026-08-21 7h</code>), "
        "o /disponibilidad borrar &lt;día|fecha&gt; para quitar."
    )
    return "\n".join(lines)


def _labels_text(task: dict) -> str:
    titles = [l["title"] for l in (task.get("labels") or [])]
    return f" · 🏷 {html.escape(', '.join(titles))}" if titles else ""


def _task_title_link(task: dict) -> str:
    url = f"{VIKUNJA_URL}/tasks/{task['id']}"
    return f'<a href="{html.escape(url)}">{html.escape(task["title"])}</a>'


def _format_day_message(tasks: list, label: str, overdue: Optional[list] = None, day: Optional[date] = None) -> str:
    overdue = overdue or []
    if not tasks and not overdue:
        return f"🎉 No hay tareas para {label}."

    project_map = _project_title_map()
    lines = []

    if overdue:
        lines.append(f"⚠️ <b>Tareas vencidas</b> ({len(overdue)})")
        lines.append("")
        for t in overdue:
            due_date = _task_local_date(t)
            date_str = due_date.strftime("%Y-%m-%d") if due_date else "──"
            project_title = project_map.get(t.get("project_id"), _INBOX_LABEL)
            lines.append(f"⚠️ {date_str}  {_task_title_link(t)} · {html.escape(project_title)}{_labels_text(t)}")
        lines.append("")

    if tasks:
        lines.append(f"📋 <b>Tareas de {label}</b> ({len(tasks)})")
        lines.append("")
        for t in tasks:
            due_dt = _task_due_dt(t)
            time_str = due_dt.astimezone().strftime("%H:%M") if due_dt else "──"
            project_title = project_map.get(t.get("project_id"), _INBOX_LABEL)
            lines.append(f"🕐 {time_str}  {_task_title_link(t)} · {html.escape(project_title)}{_labels_text(t)}")

        estimates = [_parse_estimate_minutes(t["title"]) for t in tasks]
        missing = estimates.count(None)
        total = sum(e for e in estimates if e is not None)
        lines.append("")
        capacity = _availability_minutes_for(day) if day is not None else None
        if capacity is None:
            lines.append(f"⏱ Total estimado: {_format_minutes(total)}")
        else:
            lines.append(f"⏱ Total estimado: {_format_minutes(total)} (disponible: {_format_minutes(capacity)})")
            if total > capacity:
                lines.append(f"🚨 Te pasaste por {_format_minutes(total - capacity)}")
        if missing:
            lines.append(f"⚠️ {missing} tarea(s) sin estimación")
    elif overdue:
        lines.append(f"🎉 No hay tareas para {label} (aparte de las vencidas).")

    return "\n".join(lines)


def _format_load_message(by_date: dict, start: date, days: int) -> str:
    """Per-day estimated load vs configured availability, for spotting
    overloaded days ahead of time (and free ones worth pulling tasks into)."""
    lines = [f"📊 <b>Carga de los próximos {days} día(s)</b>", ""]
    for i in range(days):
        day = start + timedelta(days=i)
        day_tasks = by_date.get(day, [])
        day_label = f"{_WEEKDAY_SHORT[day.weekday()]} {day.strftime('%m-%d')}"

        if not day_tasks:
            lines.append(f"{day_label}: sin tareas")
            continue

        estimates = [_parse_estimate_minutes(t["title"]) for t in day_tasks]
        missing = estimates.count(None)
        total = sum(e for e in estimates if e is not None)
        capacity = _availability_minutes_for(day)

        summary = f"{len(day_tasks)} tarea(s) · {_format_minutes(total)}"
        if capacity is not None:
            summary += f" / {_format_minutes(capacity)}"
            if total > capacity:
                summary += f" 🚨 +{_format_minutes(total - capacity)}"
        if missing:
            summary += f" ⚠️{missing}"
        lines.append(f"{day_label}: {summary}")

    return "\n".join(lines)


def _hecho_button_label(task: dict, project_map: dict) -> str:
    due_dt = _task_due_dt(task)
    time_str = due_dt.astimezone().strftime("%H:%M") if due_dt else "──"
    project_title = project_map.get(task.get("project_id"), _INBOX_LABEL)
    return f"{time_str} · {project_title} · {task['title']}"


def _task_picker_keyboard(tasks: list, action: str, label_fn=_hecho_button_label) -> dict:
    project_map = _project_title_map()
    return {
        "inline_keyboard": [
            [{"text": label_fn(t, project_map), "callback_data": f"{action}:{t['id']}"}] for t in tasks
        ] + [[{"text": "❌ Cancelar", "callback_data": "hcancel:0"}]]
    }


def _punt_button_label(task: dict, project_map: dict, today: date) -> str:
    """Like _hecho_button_label, but overdue tasks show their (past) due
    date instead of a time, since "──" for every overdue row would make
    them indistinguishable from each other in the picker."""
    task_date = _task_local_date(task)
    if task_date is not None and task_date < today:
        prefix = task_date.strftime("%Y-%m-%d")
    else:
        due_dt = _task_due_dt(task)
        prefix = due_dt.astimezone().strftime("%H:%M") if due_dt else "──"
    project_title = project_map.get(task.get("project_id"), _INBOX_LABEL)
    return f"{prefix} · {project_title} · {task['title']}"


def _punt_due_keyboard(task_id: int) -> dict:
    return {
        "inline_keyboard": [
            [
                {"text": "+10 min", "callback_data": f"puntdue:{task_id}:snooze10"},
                {"text": "+1 hora", "callback_data": f"puntdue:{task_id}:snooze60"},
            ],
            [
                {"text": "+24 horas", "callback_data": f"puntdue:{task_id}:snooze1440"},
                {"text": "🌆 Hoy, sin hora", "callback_data": f"puntdue:{task_id}:today"},
            ],
            [{"text": "⏰ Elegir hora", "callback_data": f"puntdue:{task_id}:pick"}],
            [{"text": "❌ Cancelar", "callback_data": "hcancel:0"}],
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
                {"text": "+24 horas", "callback_data": f"snooze1440:{task['id']}"},
            ],
            [{"text": "🌆 Más tarde (hoy, sin hora)", "callback_data": f"snoozeday:{task['id']}"}],
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
            "sendMessage", chat_id=CHAT_ID, text=_format_day_message(tasks, "hoy", day=date.today()), parse_mode="HTML"
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
        [{"text": "⏰ Elegir hora", "callback_data": "ntdue:pick"}],
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
    pending = state.get(str(chat_id))

    if not pending or "project_title" not in pending:
        _telegram_call(
            "answerCallbackQuery", callback_query_id=callback_id,
            text="No hay ninguna tarea pendiente", show_alert=True,
        )
        return

    state.pop(str(chat_id), None)
    _save_json(PENDING_TASK_STATE_FILE, state)

    if payload == "pick":
        time_state = _load_json(PENDING_TIME_STATE_FILE, {})
        time_state[str(chat_id)] = {
            "kind": "newtask", "title": pending["title"],
            "project_id": pending["project_id"], "project_title": pending["project_title"],
        }
        _save_json(PENDING_TIME_STATE_FILE, time_state)
        _telegram_call(
            "editMessageText", chat_id=chat_id, message_id=message_id,
            text=f"📝 {pending['title']}\nProyecto: {pending['project_title']}\n¿A qué hora? (HH:MM)",
        )
        _telegram_call("answerCallbackQuery", callback_query_id=callback_id)
        return

    if payload == "today":
        due_date_iso, due_label = _day_to_due_iso(date.today()), "Hoy"
    elif payload == "tomorrow":
        due_date_iso, due_label = _day_to_due_iso(date.today() + timedelta(days=1)), "Mañana"
    else:
        log.warning("Unknown ntdue payload: %s", payload)
        _telegram_call("answerCallbackQuery", callback_query_id=callback_id)
        return

    try:
        _vk_put(f"/projects/{pending['project_id']}/tasks", {"title": pending["title"], "due_date": due_date_iso})
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


# ─── Punt flow (postpone an existing task, two-step callback) ──────────────

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

    _telegram_call(
        "editMessageText", chat_id=chat_id, message_id=message_id,
        text=f"📅 {task['title']}\n¿Nuevo vencimiento?", reply_markup=_punt_due_keyboard(task_id),
    )
    _telegram_call("answerCallbackQuery", callback_query_id=callback_id)


def _apply_punt(task: dict, task_id: int, due_date_iso: str, due_label: str) -> str:
    """Applies a new due date to `task` (fetched pre-change, for the undo
    snapshot) and returns the confirmation text. Raises RequestException."""
    _vk_task_update(task_id, {"due_date": due_date_iso})
    with _state_lock:
        state = _load_json(UNDO_STATE_FILE, {})
        state[str(task_id)] = {"action": "punt", "task": task}
        _save_json(UNDO_STATE_FILE, state)
    log.info("Punted task %s to %s", task_id, due_label)
    return f"📅 {task['title']} — pospuesta a {due_label}"


def _handle_punt_due(callback_id: str, chat_id, message_id, payload: str) -> None:
    try:
        task_id_str, option = payload.split(":", 1)
        task_id = int(task_id_str)
    except ValueError:
        log.warning("Malformed puntdue payload: %s", payload)
        _telegram_call("answerCallbackQuery", callback_query_id=callback_id)
        return

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

    if option == "pick":
        with _state_lock:
            time_state = _load_json(PENDING_TIME_STATE_FILE, {})
            time_state[str(chat_id)] = {"kind": "punt", "task_id": task_id, "task_title": task["title"]}
            _save_json(PENDING_TIME_STATE_FILE, time_state)
        _telegram_call(
            "editMessageText", chat_id=chat_id, message_id=message_id,
            text=f"📅 {task['title']}\n¿A qué hora? (HH:MM)",
        )
        _telegram_call("answerCallbackQuery", callback_query_id=callback_id)
        return

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
        base = _parse_vikunja_ts(task.get("due_date", "")) or datetime.now(timezone.utc)
        due_date_iso = (base + timedelta(hours=24)).strftime("%Y-%m-%dT%H:%M:%SZ")
        due_label = "24 horas más tarde"
    elif option == "today":
        due_date_iso, due_label = _day_to_due_iso(date.today()), "hoy, sin hora"
    else:
        log.warning("Unknown puntdue option: %s", option)
        _telegram_call("answerCallbackQuery", callback_query_id=callback_id)
        return

    try:
        result_text = _apply_punt(task, task_id, due_date_iso, due_label)
    except requests.RequestException as e:
        log.error("Could not punt task %s: %s", task_id, e)
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
        log.error("Could not edit Telegram message: %s", e)
    _telegram_call("answerCallbackQuery", callback_query_id=callback_id, text="Pospuesta")


# ─── Time-entry flow (plain "HH:MM" reply after "⏰ Elegir hora") ───────────

_TIME_RE = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)$")


def _handle_time_entry(chat_id, text: str, pending: dict) -> None:
    m = _TIME_RE.match(text.strip())
    if not m:
        _telegram_call(
            "sendMessage", chat_id=chat_id,
            text="Formato inválido. Escribí la hora como HH:MM (ej: 14:30).",
        )
        return

    hour, minute = int(m.group(1)), int(m.group(2))
    due_date_iso = _next_occurrence_iso(hour, minute)
    due_label = _parse_vikunja_ts(due_date_iso).astimezone().strftime("%Y-%m-%d %H:%M")

    with _state_lock:
        state = _load_json(PENDING_TIME_STATE_FILE, {})
        state.pop(str(chat_id), None)
        _save_json(PENDING_TIME_STATE_FILE, state)

    if pending["kind"] == "newtask":
        try:
            _vk_put(
                f"/projects/{pending['project_id']}/tasks",
                {"title": pending["title"], "due_date": due_date_iso},
            )
        except requests.RequestException as e:
            log.error("Could not create task: %s", e)
            _telegram_call("sendMessage", chat_id=chat_id, text=f"Error: {e}")
            return
        _telegram_call(
            "sendMessage", chat_id=chat_id,
            text=f"✅ Creada: {pending['title']}\nProyecto: {pending['project_title']}\nVencimiento: {due_label}",
        )
        log.info("Created task '%s' for chat %s", pending["title"], chat_id)
        return

    # kind == "punt"
    task_id = pending["task_id"]
    try:
        task = _find_task(task_id)
    except requests.RequestException as e:
        log.error("Could not fetch task %s: %s", task_id, e)
        _telegram_call("sendMessage", chat_id=chat_id, text=f"Error: {e}")
        return
    if task is None:
        _telegram_call("sendMessage", chat_id=chat_id, text="Esa tarea ya no existe")
        return

    try:
        result_text = _apply_punt(task, task_id, due_date_iso, due_label)
    except requests.RequestException as e:
        log.error("Could not punt task %s: %s", task_id, e)
        _telegram_call("sendMessage", chat_id=chat_id, text=f"Error: {e}")
        return

    _telegram_call(
        "sendMessage", chat_id=chat_id, text=result_text,
        reply_markup={"inline_keyboard": [[{"text": "↩️ Deshacer", "callback_data": f"undo:{task_id}"}]]},
    )


# ─── Button handling (background thread, continuous) ───────────────────────

def _handle_undo(callback_id: str, chat_id, message_id, payload: str) -> None:
    with _state_lock:
        state = _load_json(UNDO_STATE_FILE, {})
        entry = state.pop(payload, None)
        _save_json(UNDO_STATE_FILE, state)

    if not entry:
        _telegram_call(
            "answerCallbackQuery", callback_query_id=callback_id,
            text="Ya no se puede deshacer", show_alert=True,
        )
        return

    prev_task = entry["task"]
    try:
        if entry["action"] == "delete":
            body = {
                "title": prev_task["title"],
                "description": prev_task.get("description") or "",
                "due_date": prev_task.get("due_date") or "",
                "priority": prev_task.get("priority") or 0,
            }
            created = _vk_put(f"/projects/{prev_task['project_id']}/tasks", body)
            for label in prev_task.get("labels") or []:
                _vk_put(f"/tasks/{created['id']}/labels", {"label_id": label["id"]})
            result_text = f"↩️ {prev_task['title']} — restaurada"
        elif entry["action"] == "tarjeta":
            _vk_task_update(prev_task["id"], {"title": prev_task["title"]})
            prev_label_ids = {l["id"] for l in prev_task.get("labels") or []}
            label_obj = _find_label_by_title(_TARJETA_LABEL_TITLE)
            if label_obj is not None and label_obj["id"] not in prev_label_ids:
                _vk_delete(f"/tasks/{prev_task['id']}/labels/{label_obj['id']}")
            result_text = f"↩️ {prev_task['title']} — deshecho"
        else:
            # done/snooze* only ever touch these two fields, so restoring
            # both from the pre-action snapshot reverses any of them.
            _vk_task_update(prev_task["id"], {
                "done": prev_task.get("done", False),
                "due_date": prev_task.get("due_date") or "",
            })
            result_text = f"↩️ {prev_task['title']} — deshecho"
    except (requests.RequestException, RuntimeError) as e:
        log.error("Could not undo action for task %s: %s", payload, e)
        _telegram_call(
            "answerCallbackQuery", callback_query_id=callback_id,
            text=f"Error: {e}", show_alert=True,
        )
        return

    try:
        _telegram_call("editMessageText", chat_id=chat_id, message_id=message_id, text=result_text)
    except (requests.RequestException, RuntimeError) as e:
        log.error("Could not edit Telegram message: %s", e)
    _telegram_call("answerCallbackQuery", callback_query_id=callback_id, text="Deshecho")
    log.info("Undid '%s' for task %s", entry["action"], payload)


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
    if action == "undo":
        _handle_undo(callback_id, chat_id, message_id, payload)
        return
    if action == "punt":
        _handle_punt_pick(callback_id, chat_id, message_id, payload)
        return
    if action == "puntdue":
        _handle_punt_due(callback_id, chat_id, message_id, payload)
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
        elif action in ("snooze10", "snooze60", "snooze1440"):
            delta = {
                "snooze10": timedelta(minutes=10),
                "snooze60": timedelta(hours=1),
                "snooze1440": timedelta(hours=24),
            }[action]
            new_dt = datetime.now(timezone.utc) + delta
            _vk_task_update(task_id, {"due_date": new_dt.strftime("%Y-%m-%dT%H:%M:%SZ")})
            result_text = f"⏰ {task['title']} — pospuesta a las {new_dt.astimezone().strftime('%Y-%m-%d %H:%M')}"
        elif action == "snoozeday":
            _vk_task_update(task_id, {"due_date": _day_to_due_iso(date.today())})
            result_text = f"⏰ {task['title']} — pospuesta a hoy, sin hora fija"
        elif action == "tarjeta":
            label_obj = _find_label_by_title(_TARJETA_LABEL_TITLE)
            if label_obj is None:
                raise RuntimeError(f'No existe el label "{_TARJETA_LABEL_TITLE}" en Vikunja')
            if not any(l["id"] == label_obj["id"] for l in task.get("labels") or []):
                _vk_put(f"/tasks/{task_id}/labels", {"label_id": label_obj["id"]})
            new_title = _zero_out_estimate(task["title"])
            if new_title != task["title"]:
                _vk_task_update(task_id, {"title": new_title})
            result_text = f'💳 {new_title} — etiquetada "{_TARJETA_LABEL_TITLE}", estimado a 0'
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

    with _state_lock:
        state = _load_json(UNDO_STATE_FILE, {})
        state[str(task_id)] = {"action": action, "task": task}
        _save_json(UNDO_STATE_FILE, state)

    try:
        _telegram_call(
            "editMessageText", chat_id=chat_id, message_id=message_id, text=result_text,
            reply_markup={"inline_keyboard": [[{"text": "↩️ Deshacer", "callback_data": f"undo:{task_id}"}]]},
        )
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

    parts = text.split(maxsplit=1)
    command = parts[0].split("@", 1)[0]
    arg = parts[1].strip() if len(parts) > 1 else ""

    if command in ("/hoy", "/today"):
        try:
            tasks = _today_tasks()
            overdue = _overdue_tasks()
        except requests.RequestException as e:
            log.error("Could not fetch today's tasks: %s", e)
            _telegram_call("sendMessage", chat_id=chat_id, text=f"Error: {e}")
            return

        _telegram_call(
            "sendMessage", chat_id=chat_id, text=_format_day_message(tasks, "hoy", overdue=overdue, day=date.today()),
            parse_mode="HTML",
        )
        log.info(
            "Sent today's task list (%d task(s), %d overdue) to chat %s",
            len(tasks), len(overdue), chat_id,
        )
        return

    if command in ("/mañana", "/tomorrow"):
        try:
            tasks = _tomorrow_tasks()
        except requests.RequestException as e:
            log.error("Could not fetch tomorrow's tasks: %s", e)
            _telegram_call("sendMessage", chat_id=chat_id, text=f"Error: {e}")
            return

        _telegram_call(
            "sendMessage", chat_id=chat_id,
            text=_format_day_message(tasks, "mañana", day=date.today() + timedelta(days=1)),
            parse_mode="HTML",
        )
        log.info("Sent tomorrow's task list (%d task(s)) to chat %s", len(tasks), chat_id)
        return

    if command in ("/day", "/dia"):
        target = _parse_day_arg(arg)
        if target is None:
            _telegram_call(
                "sendMessage", chat_id=chat_id,
                text="Usá /day YYYY-MM-DD (o 'hoy' / 'mañana'). Ej: /day 2026-08-20",
            )
            return

        try:
            tasks = _tasks_for_date(target)
        except requests.RequestException as e:
            log.error("Could not fetch tasks for %s: %s", target, e)
            _telegram_call("sendMessage", chat_id=chat_id, text=f"Error: {e}")
            return

        label = target.strftime("%Y-%m-%d")
        _telegram_call(
            "sendMessage", chat_id=chat_id, text=_format_day_message(tasks, label, day=target), parse_mode="HTML"
        )
        log.info("Sent task list for %s (%d task(s)) to chat %s", label, len(tasks), chat_id)
        return

    if command in ("/carga", "/semana"):
        days = 14
        if arg:
            try:
                days = int(arg)
            except ValueError:
                _telegram_call(
                    "sendMessage", chat_id=chat_id,
                    text="Usá /carga [días]. Ej: /carga o /carga 21",
                )
                return
        days = max(1, min(days, 30))

        start = date.today()
        try:
            by_date = _tasks_by_date(start, days)
        except requests.RequestException as e:
            log.error("Could not fetch tasks for /carga: %s", e)
            _telegram_call("sendMessage", chat_id=chat_id, text=f"Error: {e}")
            return

        _telegram_call(
            "sendMessage", chat_id=chat_id, text=_format_load_message(by_date, start, days), parse_mode="HTML"
        )
        log.info("Sent %d-day load overview to chat %s", days, chat_id)
        return

    if command in ("/disponibilidad", "/disp"):
        if not arg:
            _telegram_call(
                "sendMessage", chat_id=chat_id, text=_format_availability_config(), parse_mode="HTML"
            )
            return

        args = arg.split(maxsplit=1)

        if args[0] == "borrar":
            if len(args) < 2:
                _telegram_call(
                    "sendMessage", chat_id=chat_id,
                    text="Usá /disponibilidad borrar <día|fecha>. Ej: /disponibilidad borrar martes",
                )
                return
            target = _parse_availability_target(args[1])
            if target is None:
                _telegram_call(
                    "sendMessage", chat_id=chat_id,
                    text="No entendí el día/fecha. Usá un día de la semana o YYYY-MM-DD.",
                )
                return
            kind, key = target
            with _state_lock:
                config = _load_json(AVAILABILITY_STATE_FILE, {"weekday": {}, "date": {}})
                config.setdefault(kind, {}).pop(key, None)
                _save_json(AVAILABILITY_STATE_FILE, config)
            _telegram_call("sendMessage", chat_id=chat_id, text=f"✓ Disponibilidad de {args[1]} eliminada.")
            log.info("Cleared availability override %s=%s for chat %s", kind, key, chat_id)
            return

        if len(args) != 2:
            _telegram_call(
                "sendMessage", chat_id=chat_id,
                text=(
                    "Usá /disponibilidad <día|fecha> <horas>. "
                    "Ej: /disponibilidad martes 3h, /disponibilidad 2026-08-21 7h"
                ),
            )
            return

        target = _parse_availability_target(args[0])
        minutes = _parse_duration_minutes(args[1])
        if target is None or minutes is None:
            _telegram_call(
                "sendMessage", chat_id=chat_id,
                text=(
                    "No entendí. Usá /disponibilidad <día|fecha> <horas>. "
                    "Ej: /disponibilidad martes 3h, /disponibilidad 2026-08-21 7h"
                ),
            )
            return

        kind, key = target
        with _state_lock:
            config = _load_json(AVAILABILITY_STATE_FILE, {"weekday": {}, "date": {}})
            config.setdefault(kind, {})[key] = minutes
            _save_json(AVAILABILITY_STATE_FILE, config)

        label = _WEEKDAY_DISPLAY[int(key)] if kind == "weekday" else key
        _telegram_call(
            "sendMessage", chat_id=chat_id, text=f"✓ Disponibilidad de {label}: {_format_minutes(minutes)}"
        )
        log.info("Set availability %s=%s to %d min for chat %s", kind, key, minutes, chat_id)
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

    if command == "/tarjeta":
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
            "sendMessage", chat_id=chat_id, text=f'¿Qué tarea marcamos "{_TARJETA_LABEL_TITLE}"?',
            reply_markup=_task_picker_keyboard(tasks, "tarjeta"),
        )
        log.info("Sent /tarjeta task picker (%d task(s)) to chat %s", len(tasks), chat_id)
        return

    if command in ("/punt", "/postergar"):
        try:
            tasks = _overdue_tasks() + _today_tasks()
        except requests.RequestException as e:
            log.error("Could not fetch tasks for /punt: %s", e)
            _telegram_call("sendMessage", chat_id=chat_id, text=f"Error: {e}")
            return

        if not tasks:
            _telegram_call("sendMessage", chat_id=chat_id, text="🎉 No hay tareas vencidas ni de hoy para posponer.")
            return

        _telegram_call(
            "sendMessage", chat_id=chat_id, text="¿Qué tarea posponemos?",
            reply_markup=_task_picker_keyboard(
                tasks, "punt", label_fn=lambda t, pm: _punt_button_label(t, pm, date.today())
            ),
        )
        log.info("Sent /punt task picker (%d task(s)) to chat %s", len(tasks), chat_id)
        return

    if text.startswith("/"):
        return

    pending_time_state = _load_json(PENDING_TIME_STATE_FILE, {})
    pending_time = pending_time_state.get(str(chat_id))
    if pending_time:
        _handle_time_entry(chat_id, text, pending_time)
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
                {"command": "tomorrow", "description": "Tareas de mañana"},
                {"command": "day", "description": "Tareas de una fecha (YYYY-MM-DD)"},
                {"command": "carga", "description": "Carga estimada de los próximos días"},
                {"command": "disponibilidad", "description": "Ver o configurar horas disponibles por día"},
                {"command": "hecho", "description": "Marcar una tarea de hoy como hecha"},
                {"command": "borrar", "description": "Borrar una tarea de hoy"},
                {"command": "tarjeta", "description": 'Etiquetar "Para tarjeta" y poner estimado en 0'},
                {"command": "punt", "description": "Posponer una tarea vencida o de hoy"},
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
