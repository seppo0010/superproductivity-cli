"""Completion history and `/stats`.

Vikunja keeps no history of recurring-task completions (marking one done
just advances its due date), but it does set `done_at` on every completion,
one-off or recurring. So a completion is recorded whenever a task's
`done_at` moves past the last value seen for that task id — fed from
`task.updated` webhooks (push path, covers completions made in the Vikunja
web UI too) and from the reconciliation loop (safety net). Only the *latest*
`done_at` is visible via the API, so a completion is lost only if its
webhook is missed and the task is completed again before reconciliation.

State (STATS_STATE_FILE):
    last_done_at: {str(task_id): iso}   newest done_at seen per task
    completions:  [{task_id, title, project_id, done_at, estimate_min, recurring, partial}]
    snapshots:    {"YYYY-MM-DD": {"pending": N, "overdue": N}}  one per day
    progress_ids: [task_id]  tasks created by /avance (avance_flow.py) to log
                  partial progress — counted as minutes, not as completed tasks
"""

from __future__ import annotations

import io
import time
from collections import Counter
from datetime import date, datetime, timedelta, timezone
from typing import Optional

import requests

from . import config
from . import vikunja as vk
from .state import _load_json, _save_json, _state_lock

_RECENT_DONE_INTERVAL_SECONDS = 3600
_last_recent_done_check = 0.0


def _empty_state() -> dict:
    return {"last_done_at": {}, "completions": [], "snapshots": {}, "progress_ids": []}


def _load_state() -> dict:
    state = _load_json(config.STATS_STATE_FILE, _empty_state())
    for key, default in _empty_state().items():
        state.setdefault(key, default)
    return state


# ─── Recording ──────────────────────────────────────────────────────────────

def _record_completion_if_new(task: dict, state: dict) -> bool:
    """Appends a completion for `task` if its done_at is newer than the last
    one seen for that id. Returns whether anything was recorded."""
    if "id" not in task:
        return False
    done_at = vk._parse_vikunja_ts(task.get("done_at") or "")
    if done_at is None:
        return False
    key = str(task["id"])
    last = vk._parse_vikunja_ts(state["last_done_at"].get(key) or "")
    if last is not None and done_at <= last:
        return False
    iso = done_at.strftime("%Y-%m-%dT%H:%M:%SZ")
    state["last_done_at"][key] = iso
    title = task.get("title") or ""
    state["completions"].append({
        "task_id": task["id"],
        "title": title,
        "project_id": task.get("project_id"),
        "done_at": iso,
        "estimate_min": vk._parse_estimate_minutes(title),
        "recurring": vk._is_recurring(task),
        "partial": task["id"] in state["progress_ids"],
    })
    return True


def record_completions(tasks: list) -> int:
    """Records any new completions among `tasks`; returns how many."""
    if not tasks:
        return 0
    with _state_lock:
        state = _load_state()
        added = sum(_record_completion_if_new(t, state) for t in tasks)
        if added:
            _save_json(config.STATS_STATE_FILE, state)
    if added:
        config.log.info("Recorded %d task completion(s)", added)
    return added


def mark_progress_task(task_id: int) -> None:
    """Flags `task_id` as a partial-progress entry; call before marking it
    done so its completion is recorded as partial."""
    with _state_lock:
        state = _load_state()
        if task_id not in state["progress_ids"]:
            state["progress_ids"].append(task_id)
            _save_json(config.STATS_STATE_FILE, state)


def _remove_latest_completion(task_id: int, state: dict) -> bool:
    """Drops the newest completion for `task_id`. `last_done_at` is left
    alone on purpose: undoing a recurring task keeps its done_at, so the
    undo's own task.updated webhook must not re-record it."""
    for i in range(len(state["completions"]) - 1, -1, -1):
        if state["completions"][i]["task_id"] == task_id:
            del state["completions"][i]
            return True
    return False


def remove_latest_completion(task_id: int) -> None:
    with _state_lock:
        state = _load_state()
        if _remove_latest_completion(task_id, state):
            _save_json(config.STATS_STATE_FILE, state)


def backfill() -> None:
    """First run only (no state file yet): imports every one-off task's
    done_at plus each recurring task's latest one, so there's history from
    day one."""
    if config.STATS_STATE_FILE.exists():
        return
    try:
        tasks = vk._vk_get("/tasks", {"filter": "done = true"})
        tasks += vk._vk_get("/tasks", {"filter": "done = false"})
    except requests.RequestException as e:
        config.log.error("Could not backfill completion history: %s", e)
        return
    with _state_lock:
        state = _load_state()
        added = sum(_record_completion_if_new(t, state) for t in tasks)
        state["completions"].sort(key=lambda c: c["done_at"])
        _save_json(config.STATS_STATE_FILE, state)
    config.log.info("Backfilled %d completion(s) from %d task(s)", added, len(tasks))


def check_recent_done() -> None:
    """Safety net for one-off tasks whose webhook was missed (reconciliation
    only fetches active tasks): at most hourly, re-reads tasks completed in
    the last two days."""
    global _last_recent_done_check
    if time.monotonic() - _last_recent_done_check < _RECENT_DONE_INTERVAL_SECONDS:
        return
    since = (datetime.now(timezone.utc) - timedelta(days=2)).strftime("%Y-%m-%d")
    try:
        tasks = vk._vk_get("/tasks", {"filter": f"done = true && done_at > {since}"})
    except requests.RequestException as e:
        config.log.error("Could not fetch recently completed tasks: %s", e)
        return
    _last_recent_done_check = time.monotonic()
    record_completions(tasks)


def _snapshot_counts(active_tasks: list, today: date) -> dict:
    pending = overdue = 0
    for t in active_tasks:
        d = vk._task_local_date(t)
        if d is None or d > today:
            continue
        pending += 1
        if d < today:
            overdue += 1
    return {"pending": pending, "overdue": overdue}


def record_daily_snapshot(active_tasks: list) -> None:
    """Once per local day (first call at/after DAILY_DIGEST_HOUR), stores how
    many active tasks were due today-or-earlier and how many of those were
    overdue. `active_tasks` is the reconciliation pass's `done = false` list."""
    now = datetime.now()
    if now.hour < config.DAILY_DIGEST_HOUR:
        return
    key = now.date().isoformat()
    with _state_lock:
        state = _load_state()
        if key in state["snapshots"]:
            return
        state["snapshots"][key] = _snapshot_counts(active_tasks, now.date())
        _save_json(config.STATS_STATE_FILE, state)


# ─── Aggregation (pure) ─────────────────────────────────────────────────────

def _completion_local_date(c: dict) -> Optional[date]:
    dt = vk._parse_vikunja_ts(c.get("done_at") or "")
    return dt.astimezone().date() if dt else None


def completions_in_range(completions: list, start: date, end: date) -> list:
    """Completions whose local completion day is within [start, end]."""
    return [c for c in completions if (d := _completion_local_date(c)) is not None and start <= d <= end]


def finished(completions: list) -> list:
    """Real completions — without /avance partial-progress entries."""
    return [c for c in completions if not c.get("partial")]


def completions_per_day(completions: list, start: date, end: date) -> dict:
    counts = {start + timedelta(days=i): 0 for i in range((end - start).days + 1)}
    for c in completions:
        d = _completion_local_date(c)
        if d in counts:
            counts[d] += 1
    return counts


def by_project(completions: list) -> list:
    """[(project_id, count)] most-completed first."""
    return Counter(c.get("project_id") for c in completions).most_common()


def estimated_minutes(completions: list) -> int:
    return sum(c.get("estimate_min") or 0 for c in completions)


# ─── Rendering ──────────────────────────────────────────────────────────────

def render_chart(per_day: dict, snapshots: dict) -> bytes:
    """PNG: bars = completions per day, line = overdue count from the daily
    snapshots (where available)."""
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.dates as mdates
    import matplotlib.pyplot as plt

    days = sorted(per_day)
    fig, ax = plt.subplots(figsize=(8, 4), dpi=120)
    try:
        ax.bar(days, [per_day[d] for d in days], color="#4C8BF5", label="Completadas")
        ax.set_ylabel("Completadas")
        ax.yaxis.get_major_locator().set_params(integer=True)
        ax.xaxis.set_major_formatter(mdates.DateFormatter("%d/%m"))
        ax.spines[["top", "right"]].set_visible(False)

        snap_days = [d for d in days if d.isoformat() in snapshots]
        if snap_days:
            ax2 = ax.twinx()
            ax2.plot(
                snap_days, [snapshots[d.isoformat()]["overdue"] for d in snap_days],
                color="#E8710A", marker="o", markersize=3, label="Vencidas",
            )
            ax2.set_ylabel("Vencidas")
            ax2.set_ylim(bottom=0)
            ax2.yaxis.get_major_locator().set_params(integer=True)
            ax2.spines[["top"]].set_visible(False)
            lines = ax.get_legend_handles_labels()[0] + ax2.get_legend_handles_labels()[0]
            ax.legend(lines, [h.get_label() for h in lines], loc="upper left", frameon=False)

        fig.autofmt_xdate()
        fig.tight_layout()
        buf = io.BytesIO()
        fig.savefig(buf, format="png")
        return buf.getvalue()
    finally:
        plt.close(fig)


def format_summary(completions: list, start: date, end: date, project_titles: dict,
                   active_tasks: list, today: date) -> str:
    """HTML caption for /stats."""
    import html

    with_partial = completions_in_range(completions, start, end)
    in_range = finished(with_partial)
    week_start = today - timedelta(days=today.weekday())
    this_week = finished(completions_in_range(completions, week_start, today))
    last_week = finished(completions_in_range(completions, week_start - timedelta(days=7), week_start - timedelta(days=1)))
    days = (end - start).days + 1

    lines = [f"📊 <b>Estadísticas — últimos {days} días</b>", ""]
    lines.append(f"✅ Completadas: <b>{len(in_range)}</b> ({len(in_range) / days:.1f}/día)")
    recurring = sum(1 for c in in_range if c.get("recurring"))
    if recurring:
        lines.append(f"   🔁 {recurring} recurrentes · {len(in_range) - recurring} únicas")
    partial = len(with_partial) - len(in_range)
    if partial:
        lines.append(f"⏳ Avances parciales: {partial}")
    minutes = estimated_minutes(with_partial)
    if minutes:
        lines.append(f"⏱ Tiempo estimado completado: {vk._format_minutes(minutes)}")
    lines.append(f"📅 Esta semana: {len(this_week)} · semana pasada: {len(last_week)}")

    top = by_project(in_range)[:5]
    if top:
        lines += ["", "<b>Por proyecto</b>"]
        for pid, n in top:
            name = project_titles.get(pid) or f"Proyecto {pid}"
            lines.append(f"• {html.escape(name)}: {n}")

    counts = _snapshot_counts(active_tasks, today)
    lines += ["", f"📌 Pendientes hoy o antes: {counts['pending']} · ⚠️ vencidas: {counts['overdue']}"]
    return "\n".join(lines)
