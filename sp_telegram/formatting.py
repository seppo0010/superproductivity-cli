"""Renders Vikunja tasks and calendar events into Telegram message text and
inline keyboards."""

from __future__ import annotations

import html
from datetime import date, datetime, timedelta
from typing import Optional

from . import config
from . import vikunja as vk

_DUE_DATE_KEYBOARD = {
    "inline_keyboard": [
        [
            {"text": "Hoy", "callback_data": "ntdue:today"},
            {"text": "Mañana", "callback_data": "ntdue:tomorrow"},
        ],
    ]
}

_DUE_DATE_HINT = "o escribí HH:MM, D/M[/Y] o D/M[/Y] HH:MM"


def _labels_text(task: dict) -> str:
    titles = [l["title"] for l in (task.get("labels") or [])]
    return f" · 🏷 {html.escape(', '.join(titles))}" if titles else ""


def _task_title_link(task: dict) -> str:
    url = f"{config.VIKUNJA_URL}/tasks/{task['id']}"
    return f'<a href="{html.escape(url)}">{html.escape(task["title"])}</a>'


def _format_day_message(
    tasks: list, label: str, overdue: Optional[list] = None, day: Optional[date] = None,
    events: Optional[list] = None,
) -> tuple:
    """Returns (text, keyboard) — keyboard is a one-button "estimate this"
    shortcut when at least one of `tasks` has no estimate yet, else None."""
    overdue = overdue or []
    events = events or []
    if day == date.today():
        now = datetime.now()
        events = [e for e in events if e["all_day"] or e["end"] > now]
    if not tasks and not overdue and not events:
        return f"🎉 No hay tareas para {label}.", None

    project_map = vk._project_title_map()
    lines = []

    if overdue:
        lines.append(f"⚠️ <b>Tareas vencidas</b> ({len(overdue)})")
        lines.append("")
        for t in overdue:
            due_date = vk._task_local_date(t)
            date_str = due_date.strftime("%Y-%m-%d") if due_date else "──"
            project_title = project_map.get(t.get("project_id"), vk._INBOX_LABEL)
            lines.append(f"⚠️ {date_str}  {vk._priority_prefix(t)}{_task_title_link(t)} · {html.escape(project_title)}{_labels_text(t)}")
        lines.append("")

    _, free_minutes, elapsed = vk._free_windows_for(day, events) if day is not None else (None, None, False)

    if tasks or events:
        if tasks and events:
            lines.append(f"📋 <b>Agenda de {label}</b> ({len(tasks)} tarea(s), {len(events)} evento(s))")
        elif tasks:
            lines.append(f"📋 <b>Tareas de {label}</b> ({len(tasks)})")
        else:
            lines.append(f"📅 <b>Eventos de {label}</b> ({len(events)})")
        lines.append("")

        # Events and tasks share one chronological list (sorted by local
        # clock time, timeless items first) so overlaps between them are
        # visible at a glance.
        entries = []
        for e in events:
            if e["all_day"]:
                sort_key = datetime.min
                time_str = "(todo el día)"
            else:
                sort_key = e["start"]
                time_str = f'{e["start"].strftime("%H:%M")}–{e["end"].strftime("%H:%M")}'
            marker = "📅" if e["busy"] else "🟢"
            suffix = "" if e["busy"] else " (libre)"
            entries.append((sort_key, f'{marker} {time_str}  {html.escape(e["title"])}{suffix}'))

        for t in tasks:
            due_dt = vk._task_due_dt(t)
            local_dt = due_dt.astimezone().replace(tzinfo=None) if due_dt else None
            sort_key = local_dt or datetime.min
            time_str = local_dt.strftime("%H:%M") if local_dt else "──"
            project_title = project_map.get(t.get("project_id"), vk._INBOX_LABEL)
            entries.append((
                sort_key,
                f"🕐 {time_str}  {vk._priority_prefix(t)}{_task_title_link(t)} · {html.escape(project_title)}{_labels_text(t)}",
            ))

        entries.sort(key=lambda entry: entry[0])
        for _, line in entries:
            lines.append(line)

        if tasks:
            window = vk._availability_window_for(day) if day is not None else None
            inside_total, outside_total, missing = vk._split_estimates_by_window(tasks, window)
            lines.append("")
            if free_minutes is None:
                lines.append(f"⏱ Total estimado: {vk._format_minutes(inside_total + outside_total)}")
            else:
                elapsed_note = " ⏳ desde ahora" if elapsed else ""
                lines.append(
                    f"⏱ Total estimado: {vk._format_minutes(inside_total)} (disponible: {vk._format_minutes(free_minutes)}{elapsed_note})"
                )
                if inside_total > free_minutes:
                    lines.append(f"🚨 Te pasaste por {vk._format_minutes(inside_total - free_minutes)}")
                if outside_total:
                    lines.append(f"🌙 {vk._format_minutes(outside_total)} fuera del horario de disponibilidad")
            if missing:
                lines.append(f"⚠️ {missing} tarea(s) sin estimación")
    elif overdue:
        lines.append(f"🎉 No hay tareas para {label} (aparte de las vencidas).")

    keyboard = None
    first_unestimated = next((t for t in tasks if vk._parse_estimate_minutes(t["title"]) is None), None)
    if first_unestimated is not None:
        keyboard = {
            "inline_keyboard": [[
                {"text": "⏱ Estimar tarea sin estimación", "callback_data": f"estim:{first_unestimated['id']}"}
            ]]
        }

    return "\n".join(lines), keyboard


def _load_heat_emoji(total: int, free_minutes: Optional[int]) -> str:
    """A quick visual read of how packed a day is: how much of its free
    time (window minus calendar events) the estimated task load fills."""
    if free_minutes is None:
        return "⬜"
    if total <= 0:
        return "🟩"
    if free_minutes <= 0:
        return "🟥"
    ratio = total / free_minutes
    if ratio <= 0.5:
        return "🟩"
    if ratio <= 0.85:
        return "🟨"
    if ratio <= 1.0:
        return "🟧"
    return "🟥"


def _format_load_message(by_date: dict, start: date, days: int, events_by_date: Optional[dict] = None) -> str:
    """Per-day estimated load vs free time left after calendar events, for
    spotting overloaded days ahead of time (and free ones worth pulling
    tasks into)."""
    events_by_date = events_by_date or {}
    lines = [f"📊 <b>Carga de los próximos {days} día(s)</b>", ""]
    for i in range(days):
        day = start + timedelta(days=i)
        day_tasks = by_date.get(day, [])
        day_events = events_by_date.get(day, [])
        day_label = f"{vk._WEEKDAY_SHORT[day.weekday()]} {day.strftime('%d/%m')}"

        _, free_minutes, elapsed = vk._free_windows_for(day, day_events)

        if not day_tasks:
            heat = _load_heat_emoji(0, free_minutes)
            lines.append(f"{heat} {day_label}: sin tareas")
            continue

        window = vk._availability_window_for(day)
        inside_total, outside_total, missing = vk._split_estimates_by_window(day_tasks, window)
        heat = _load_heat_emoji(inside_total, free_minutes)

        summary = vk._format_minutes(inside_total)
        if free_minutes is not None:
            summary += f" / {vk._format_minutes(free_minutes)}"
            if elapsed:
                summary += " ⏳"
            if inside_total > free_minutes:
                summary += f" 🚨 +{vk._format_minutes(inside_total - free_minutes)}"
        if outside_total:
            summary += f" (+{vk._format_minutes(outside_total)} fuera)"
        if missing:
            summary += f" ⚠️{missing}"
        lines.append(f"{heat} {day_label}: {summary}")

    return "\n".join(lines)


def _hecho_button_label(task: dict, project_map: dict) -> str:
    due_dt = vk._task_due_dt(task)
    time_str = due_dt.astimezone().strftime("%H:%M") if due_dt else "──"
    project_title = project_map.get(task.get("project_id"), vk._INBOX_LABEL)
    return f"{time_str} · {vk._priority_prefix(task)}{project_title} · {task['title']}"


def _task_picker_keyboard(tasks: list, action: str, label_fn=_hecho_button_label) -> dict:
    project_map = vk._project_title_map()
    return {
        "inline_keyboard": [
            [{"text": label_fn(t, project_map), "callback_data": f"{action}:{t['id']}"}] for t in tasks
        ] + [[{"text": "❌ Cancelar", "callback_data": "hcancel:0"}]]
    }


def _punt_button_label(task: dict, project_map: dict, today: date) -> str:
    """Like _hecho_button_label, but overdue tasks show their (past) due
    date instead of a time, since "──" for every overdue row would make
    them indistinguishable from each other in the picker."""
    task_date = vk._task_local_date(task)
    if task_date is not None and task_date < today:
        prefix = task_date.strftime("%Y-%m-%d")
    else:
        due_dt = vk._task_due_dt(task)
        prefix = due_dt.astimezone().strftime("%H:%M") if due_dt else "──"
    project_title = project_map.get(task.get("project_id"), vk._INBOX_LABEL)
    return f"{prefix} · {vk._priority_prefix(task)}{project_title} · {task['title']}"


def _day_picker_keyboard() -> dict:
    """Two-column picker of the next 14 days, for /day with no argument."""
    today = date.today()
    days = [today + timedelta(days=i) for i in range(14)]
    rows = []
    for i in range(0, len(days), 2):
        row = []
        for day in days[i:i + 2]:
            label = f"{vk._WEEKDAY_SHORT[day.weekday()]} {day.strftime('%d/%m')}"
            row.append({"text": label, "callback_data": f"daypick:{day.strftime('%Y-%m-%d')}"})
        rows.append(row)
    return {"inline_keyboard": rows}


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
            [{"text": "❌ Cancelar", "callback_data": "hcancel:0"}],
        ]
    }
