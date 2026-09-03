"""Vikunja REST API client plus the task/date/project/estimate/priority/
availability domain logic built on top of it."""

from __future__ import annotations

import re
from datetime import date, datetime, time as dtime, timedelta, timezone
from typing import Optional

import requests

from . import config
from .state import _load_json


# ─── Vikunja API ─────────────────────────────────────────────────────────────

def _vk_headers() -> dict:
    return {"Authorization": f"Bearer {config.VIKUNJA_TOKEN}"}


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
        r = requests.get(f"{config.API_BASE}{path}", params=params, headers=_vk_headers(), timeout=10)
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
    r = requests.put(f"{config.API_BASE}{path}", json=body, headers=_vk_headers(), timeout=10)
    return _vk_unwrap(r)


def _vk_post(path: str, body: dict) -> object:
    r = requests.post(f"{config.API_BASE}{path}", json=body, headers=_vk_headers(), timeout=10)
    return _vk_unwrap(r)


def _vk_delete(path: str) -> object:
    r = requests.delete(f"{config.API_BASE}{path}", headers=_vk_headers(), timeout=10)
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
    """Active tasks due on `day`, sorted by time (day-only tasks first),
    then by project title to keep same-time ties stable and grouped."""
    tasks: list = _vk_get("/tasks", {"filter": "done = false"})
    result = [t for t in tasks if _task_local_date(t) == day]
    project_map = _project_title_map()
    result.sort(key=lambda t: (
        _task_due_dt(t) or datetime.min.replace(tzinfo=timezone.utc),
        project_map.get(t.get("project_id"), _INBOX_LABEL),
    ))
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
    """Parse a /day argument: 'hoy'/'today', 'mañana'/'manana'/'tomorrow',
    an explicit YYYY-MM-DD, or the shorthand DD/MM (current year). None if
    unparseable."""
    arg = arg.strip().lower()
    if arg in ("hoy", "today"):
        return date.today()
    if arg in ("mañana", "manana", "tomorrow"):
        return date.today() + timedelta(days=1)
    try:
        return datetime.strptime(arg, "%Y-%m-%d").date()
    except ValueError:
        pass
    m = re.fullmatch(r"(\d{1,2})/(\d{1,2})", arg)
    if m:
        day, month = int(m.group(1)), int(m.group(2))
        try:
            return date(date.today().year, month, day)
        except ValueError:
            return None
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
        config.log.warning("Could not fetch projects: %s", e)
        return []
    return [p for p in projects if p["id"] > 0 and not p.get("is_archived")]


def _project_title_map() -> dict:
    return {p["id"]: _project_display(p) for p in _real_projects()}


_ESTIMATE_RE = re.compile(r"^\[([^\]]*)\]")
_DURATION_RE = re.compile(r"(?:(\d+)h)?(?:(\d+)m)?")


def _parse_duration_minutes(text: str) -> Optional[int]:
    """Parse a bare duration like '5m' / '1h30m' / '1h' / '0' (no brackets)
    into minutes, or None if it doesn't parse. Shared by the '[...]'
    title-prefix estimate convention and free-text duration replies."""
    content = text.strip()
    if content == "0":
        return 0
    hm = _DURATION_RE.fullmatch(content)
    if not hm or not (hm.group(1) or hm.group(2)):
        return None
    return int(hm.group(1) or 0) * 60 + int(hm.group(2) or 0)


def _parse_estimate_minutes(title: str) -> Optional[int]:
    """Parse a leading '[5m]' / '[1h30m]' / '[0]' estimate prefix into minutes,
    or None if the title has no such prefix (or it doesn't parse)."""
    m = _ESTIMATE_RE.match(title.strip())
    if not m:
        return None
    return _parse_duration_minutes(m.group(1))


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


def _set_estimate(title: str, minutes: int) -> str:
    """Replace the leading '[...]' estimate prefix with one for `minutes`
    (or add one if there's none)."""
    stripped = title.strip()
    m = _ESTIMATE_RE.match(stripped)
    rest = stripped[m.end():].lstrip() if m else stripped
    prefix = "[0]" if minutes == 0 else f"[{_format_minutes(minutes)}]"
    return f"{prefix} {rest}" if rest else prefix


_ESTIMATE_OPTIONS = [(0, "0"), (5, "5m"), (10, "10m"), (15, "15m"), (60, "1h"), (120, "2h")]


def _estimate_duration_keyboard(task_id: int) -> dict:
    rows = [
        [
            {"text": label, "callback_data": f"estimdur:{task_id}:{minutes}"}
            for minutes, label in _ESTIMATE_OPTIONS[i:i + 3]
        ]
        for i in range(0, len(_ESTIMATE_OPTIONS), 3)
    ]
    return {"inline_keyboard": rows + [[{"text": "❌ Cancelar", "callback_data": "hcancel:0"}]]}


def _next_unestimated_task() -> Optional[dict]:
    """The active task missing an estimate that's due soonest (undated
    tasks, and date-only "sin hora" tasks relative to timed ones the same
    day, sort last), for the /sinestimar catch-up command."""
    tasks: list = _vk_get("/tasks", {"filter": "done = false"})
    unestimated = [t for t in tasks if _parse_estimate_minutes(t["title"]) is None]
    if not unestimated:
        return None
    unestimated.sort(key=lambda t: (
        _task_local_date(t) or date.max,
        _task_due_dt(t) or datetime.max.replace(tzinfo=timezone.utc),
    ))
    return unestimated[0]


# Vikunja priority levels: 0 Unset, 1 Low, 2 Medium, 3 High, 4 Urgent, 5 DO NOW.
_PRIORITY_OPTIONS = [(0, "Ninguna"), (1, "Baja"), (2, "Media"), (3, "Alta"), (4, "Urgente"), (5, "Ya mismo")]
_PRIORITY_EMOJI = {3: "🔸", 4: "🔺", 5: "🚨"}


def _priority_prefix(task: dict) -> str:
    """A short emoji flag for High/Urgent/DO NOW tasks in list views — Low
    and Medium aren't distinctive enough to be worth the visual noise."""
    emoji = _PRIORITY_EMOJI.get(task.get("priority") or 0)
    return f"{emoji} " if emoji else ""


def _priority_keyboard(task_id: int) -> dict:
    rows = [
        [
            {"text": label, "callback_data": f"priolvl:{task_id}:{level}"}
            for level, label in _PRIORITY_OPTIONS[i:i + 3]
        ]
        for i in range(0, len(_PRIORITY_OPTIONS), 3)
    ]
    return {"inline_keyboard": rows + [[{"text": "❌ Cancelar", "callback_data": "hcancel:0"}]]}


_WEEKDAY_NAMES = {
    "lunes": 0, "martes": 1, "miercoles": 2, "miércoles": 2, "jueves": 3,
    "viernes": 4, "sabado": 5, "sábado": 5, "domingo": 6,
}
_WEEKDAY_DISPLAY = ["Lunes", "Martes", "Miércoles", "Jueves", "Viernes", "Sábado", "Domingo"]
_WEEKDAY_SHORT = ["Lun", "Mar", "Mié", "Jue", "Vie", "Sáb", "Dom"]


def _parse_time_range(s: str) -> Optional[tuple]:
    """Parse 'HH:MM-HH:MM' into (dtime, dtime), or None."""
    m = re.fullmatch(r"(\d{1,2}):(\d{2})-(\d{1,2}):(\d{2})", s.strip())
    if not m:
        return None
    h1, m1, h2, m2 = (int(g) for g in m.groups())
    try:
        start, end = dtime(h1, m1), dtime(h2, m2)
    except ValueError:
        return None
    if end <= start:
        return None
    return (start, end)


def _format_time_range(window: tuple) -> str:
    start, end = window
    return f"{start.strftime('%H:%M')}–{end.strftime('%H:%M')}"


def _parse_availability_target(s: str) -> Optional[tuple]:
    """Returns ('weekday', '1'), ('date', '2026-08-21'), or ('general',
    'general'), or None."""
    s = s.strip().lower()
    if s == "general":
        return ("general", "general")
    if s in _WEEKDAY_NAMES:
        return ("weekday", str(_WEEKDAY_NAMES[s]))
    try:
        datetime.strptime(s, "%Y-%m-%d")
        return ("date", s)
    except ValueError:
        return None


def _parse_stored_window(value) -> Optional[tuple]:
    """Stored windows are ["HH:MM", "HH:MM"]. Older state (from before
    availability switched from a total-minutes-per-day int to a window) is
    tolerated as unset rather than raising, so a leftover legacy entry can't
    crash the bot."""
    try:
        return tuple(dtime.fromisoformat(t) for t in value)
    except (TypeError, ValueError):
        config.log.warning("Ignoring unparseable availability window: %r", value)
        return None


def _availability_window_for(day: date) -> Optional[tuple]:
    """A specific date overrides the weekday default, which overrides the
    general default. None means no window has been configured for that
    day at any level."""
    cfg = _load_json(config.AVAILABILITY_STATE_FILE, {"weekday": {}, "date": {}, "general": None})
    date_key = day.strftime("%Y-%m-%d")
    if date_key in cfg.get("date", {}):
        window = _parse_stored_window(cfg["date"][date_key])
        if window is not None:
            return window
    weekday_key = str(day.weekday())
    if weekday_key in cfg.get("weekday", {}):
        window = _parse_stored_window(cfg["weekday"][weekday_key])
        if window is not None:
            return window
    general = cfg.get("general")
    if general:
        window = _parse_stored_window(general)
        if window is not None:
            return window
    return None


def _format_availability_config() -> str:
    cfg = _load_json(config.AVAILABILITY_STATE_FILE, {"weekday": {}, "date": {}, "general": None})
    lines = ["<b>Disponibilidad configurada</b>", ""]

    def _window_text(stored) -> str:
        window = _parse_stored_window(stored)
        return _format_time_range(window) if window is not None else "(sin parsear, volvé a configurarlo)"

    general = cfg.get("general")
    lines.append(f"General: {_window_text(general)}" if general else "General: (sin default configurado)")
    lines.append("")

    weekday_cfg = cfg.get("weekday", {})
    if weekday_cfg:
        for idx in sorted(weekday_cfg, key=int):
            lines.append(f"{_WEEKDAY_DISPLAY[int(idx)]}: {_window_text(weekday_cfg[idx])}")
    else:
        lines.append("(sin días de semana configurados)")

    date_cfg = cfg.get("date", {})
    if date_cfg:
        lines.append("")
        for d in sorted(date_cfg):
            lines.append(f"{d}: {_window_text(date_cfg[d])}")

    lines.append("")
    lines.append(
        "Usá /disponibilidad &lt;general|día|fecha&gt; &lt;HH:MM-HH:MM&gt; para configurar "
        "(ej: <code>/disponibilidad general 09:00-18:00</code>, <code>/disponibilidad martes 09:00-13:00</code>, "
        "<code>/disponibilidad 2026-08-21 10:00-16:00</code>), "
        "o /disponibilidad borrar &lt;general|día|fecha&gt; para quitar."
    )
    return "\n".join(lines)


def _merge_intervals(intervals: list) -> list:
    """Sorts and merges overlapping (start, end) datetime intervals, so
    overlapping calendar events don't get double-subtracted from a day's
    free time."""
    merged: list = []
    for start, end in sorted(intervals):
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return merged


def _free_windows_for(day: date, events: list) -> tuple:
    """Free (start, end) clock-time gaps within `day`'s configured occupancy
    window after subtracting Busy, non-all-day calendar events (merging any
    overlaps). Returns (None, None, False) if no window is configured for
    the day. For today, the window is clamped to start at the current time
    — time that's already elapsed isn't free (and if the window's already
    over, there's simply nothing free left); the third element is True
    whenever that clamping actually cut into the window, so callers can
    flag that the free-time figure isn't the full configured window."""
    window = _availability_window_for(day)
    if window is None:
        return (None, None, False)
    win_start = datetime.combine(day, window[0])
    win_end = datetime.combine(day, window[1])
    elapsed = False
    if day == date.today():
        now = datetime.now()
        if now > win_start:
            win_start = now
            elapsed = True
    if win_start >= win_end:
        return ([], 0, elapsed)

    busy = []
    for e in events:
        if e["all_day"] or not e["busy"]:
            continue
        s, en = e["start"], e["end"]
        if en <= win_start or s >= win_end:
            continue
        busy.append((max(s, win_start), min(en, win_end)))

    free = []
    cursor = win_start
    for s, en in _merge_intervals(busy):
        if s > cursor:
            free.append((cursor.time(), s.time()))
        cursor = max(cursor, en)
    if cursor < win_end:
        free.append((cursor.time(), win_end.time()))

    free_minutes = sum(
        (datetime.combine(day, en) - datetime.combine(day, s)).seconds // 60
        for s, en in free
    )
    return (free, free_minutes, elapsed)


def _task_in_availability_window(task: dict, window: Optional[tuple]) -> bool:
    """Whether the task's due time falls within the day's availability
    window. Date-only tasks (the 23:59 "sin hora" sentinel, or any task
    with no explicit due time) always count as inside — there's no
    specific time to judge them against, and they're the default case an
    availability window is meant to cover. A task with an explicit due
    time outside the window doesn't compete for that window's free time,
    so it's tracked separately."""
    if window is None:
        return True
    due_dt = _task_due_dt(task)
    if due_dt is None:
        return True
    t = due_dt.astimezone().time()
    return window[0] <= t <= window[1]


def _split_estimates_by_window(tasks: list, window: Optional[tuple]) -> tuple:
    """Splits tasks' estimated minutes into (inside, outside, missing)
    buckets against the day's availability window — see
    _task_in_availability_window. `missing` counts tasks with no
    estimate at all."""
    inside = outside = missing = 0
    for t in tasks:
        e = _parse_estimate_minutes(t["title"])
        if e is None:
            missing += 1
        elif _task_in_availability_window(t, window):
            inside += e
        else:
            outside += e
    return inside, outside, missing
