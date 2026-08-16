#!/usr/bin/env python3
"""Vikunja CLI — wraps the Vikunja REST API

Prerequisites:
  • VIKUNJA_URL set to the base URL of your Vikunja instance (e.g. http://192.168.0.9:3456)
  • VIKUNJA_TOKEN set to an API token (Vikunja → Settings → API Tokens)

Both can also live in ~/.config/sp-cli/telegram.env (the same file the
Telegram daemon reads via systemd's EnvironmentFile=) — it's loaded as a
fallback for any of these vars not already set in the environment, so an
interactive shell alias doesn't need to export them itself.

Tip: alias sp='python /path/to/sp.py'
"""

from __future__ import annotations

import os
import re
import sys
import textwrap
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import List, Optional

import questionary
import requests
import typer
from rich import box
from rich.console import Console
from rich.progress import track
from rich.table import Table
from rich.text import Text

app = typer.Typer(
    help="Vikunja CLI",
    add_completion=False,
    no_args_is_help=True,
)
console = Console()


def _load_env_file(path: Path) -> None:
    """Fill in os.environ from a KEY=VALUE file for any key not already set,
    so vars already exported in the shell always take precedence."""
    try:
        lines = path.read_text().splitlines()
    except OSError:
        return
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip())


_load_env_file(Path.home() / ".config" / "sp-cli" / "telegram.env")

VIKUNJA_URL = os.environ.get("VIKUNJA_URL", "http://192.168.0.9:3456").rstrip("/")
API_BASE = f"{VIKUNJA_URL}/api/v1"
TOKEN = os.environ.get("VIKUNJA_TOKEN")
INBOX_PROJECT_TITLE = "Inbox"
MIN_TABLE_WIDTH = 80


# ─── HTTP helpers ─────────────────────────────────────────────────────────────

def _require_token() -> None:
    if not TOKEN:
        console.print(
            "[bold red]VIKUNJA_TOKEN is not set.[/bold red]\n"
            "  • Create an API token in Vikunja → Settings → API Tokens\n"
            "  • export VIKUNJA_TOKEN=... (and VIKUNJA_URL if not http://192.168.0.9:3456)"
        )
        raise typer.Exit(1)


def _headers() -> dict:
    return {"Authorization": f"Bearer {TOKEN}"}


def _connection_error() -> None:
    console.print(
        "[bold red]Cannot reach Vikunja.[/bold red]\n"
        f"  • Is it running at [bold]{VIKUNJA_URL}[/bold]?\n"
        "  • Check VIKUNJA_URL and VIKUNJA_TOKEN."
    )
    raise typer.Exit(1)


def _unwrap(r: requests.Response) -> object:
    if r.status_code >= 400:
        try:
            err = r.json()
            message = err.get("message", str(err))
        except ValueError:
            message = r.text
        console.print(f"[red]API error:[/red] {message}")
        raise typer.Exit(1)
    return r.json()


def _get(path: str, params: Optional[dict] = None) -> object:
    """GET, following pagination when the response is a list. Vikunja caps
    per_page server-side (observed: 50) regardless of what's requested, so
    a single request silently drops later items — fetch every page."""
    _require_token()
    params = dict(params or {})
    params.setdefault("per_page", 250)
    page = 1
    results = None
    while True:
        params["page"] = page
        try:
            r = requests.get(f"{API_BASE}{path}", params=params, headers=_headers(), timeout=10)
        except requests.ConnectionError:
            _connection_error()
        except requests.Timeout:
            console.print("[red]Request timed out.[/red]")
            raise typer.Exit(1)
        body = _unwrap(r)
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


def _put(path: str, body: dict) -> object:
    _require_token()
    try:
        return _unwrap(requests.put(f"{API_BASE}{path}", json=body, headers=_headers(), timeout=10))
    except requests.ConnectionError:
        _connection_error()


def _api_post(path: str, body: dict) -> object:
    _require_token()
    try:
        return _unwrap(requests.post(f"{API_BASE}{path}", json=body, headers=_headers(), timeout=10))
    except requests.ConnectionError:
        _connection_error()


def _delete(path: str) -> object:
    _require_token()
    try:
        return _unwrap(requests.delete(f"{API_BASE}{path}", headers=_headers(), timeout=10))
    except requests.ConnectionError:
        _connection_error()


def _task_update(task_id: int, changes: dict) -> dict:
    """Fetch the full task, apply `changes` on top, and write the whole object
    back. Vikunja's task update endpoint resets fields that are omitted from
    the request body rather than leaving them untouched, so a naive partial
    PATCH silently destroys data (e.g. priority)."""
    current: dict = _get(f"/tasks/{task_id}")
    current.update(changes)
    return _api_post(f"/tasks/{task_id}", current)


# ─── Project helpers ──────────────────────────────────────────────────────────

def _project_name(task: dict, all_projects: list) -> str:
    pid = task.get("project_id")
    return next((p["title"] for p in all_projects if p["id"] == pid), "?")


def _hex_color_from(obj: dict) -> Optional[str]:
    color = (obj.get("hex_color") or "").lstrip("#")
    if re.fullmatch(r"[0-9a-fA-F]{6}", color):
        return f"#{color}"
    return None


def _rich_badge(name: str, color: Optional[str]) -> str:
    if not color:
        return name
    r, g, b = int(color[1:3], 16), int(color[3:5], 16), int(color[5:7], 16)
    text = "black" if (0.299 * r + 0.587 * g + 0.114 * b) / 255 > 0.5 else "white"
    return f"[{text} on {color}] {name} [/]"


def _project_rich_label(project: dict) -> str:
    return _rich_badge(project["title"], _hex_color_from(project))


def _labels_rich_text(task: dict) -> str:
    labels = task.get("labels") or []
    return " ".join(_rich_badge(l["title"], _hex_color_from(l)) for l in labels)


def _project_rich_name(task: dict, all_projects: list) -> str:
    pid = task.get("project_id")
    proj = next((p for p in all_projects if p["id"] == pid), None)
    return _project_rich_label(proj) if proj else "?"


def _parse_due(value: str) -> str:
    """Resolve 'today', 'tomorrow', or passthrough a YYYY-MM-DD string."""
    if value == "today":
        return date.today().strftime("%Y-%m-%d")
    if value == "tomorrow":
        return (date.today() + timedelta(days=1)).strftime("%Y-%m-%d")
    if not re.match(r"\d{4}-\d{2}-\d{2}$", value):
        console.print(f"[red]Invalid due date '[bold]{value}[/bold]'. Use today, tomorrow, or YYYY-MM-DD.[/red]")
        raise typer.Exit(1)
    return value


def _local_time_to_iso(day: str, hour: int, minute: int) -> str:
    """Convert a local wall-clock time on `day` to a UTC ISO timestamp."""
    y, mo, d = (int(p) for p in day.split("-"))
    local_dt = datetime(y, mo, d, hour, minute, 0).astimezone()
    return local_dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _due_day_to_iso(day: str) -> str:
    """A day-only due date is stored as 23:59 local time converted to UTC —
    Vikunja's own convention for date-only due dates (it has no native
    date-only field). Using literal UTC midnight instead (the convention for
    already-migrated SP data) displays as the previous evening in Vikunja's
    own UI for negative-UTC-offset timezones."""
    return _local_time_to_iso(day, 23, 59)


def _due_value_to_iso(value: str) -> str:
    """'tomorrow' schedules at 9am local (a concrete time worth a
    notification); 'today' and explicit dates stay day-only (23:59 local
    marker, no specific time)."""
    if value == "tomorrow":
        return _local_time_to_iso((date.today() + timedelta(days=1)).strftime("%Y-%m-%d"), 9, 0)
    return _due_day_to_iso(_parse_due(value))


def _parse_vikunja_ts(ds: str) -> Optional[datetime]:
    if not ds or ds.startswith("0001-01-01"):
        return None
    m = re.match(r"(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2}):(\d{2})", ds)
    if not m:
        return None
    y, mo, d, h, mi, s = (int(g) for g in m.groups())
    return datetime(y, mo, d, h, mi, s, tzinfo=timezone.utc)


def _wrap_title(title: str, width: int) -> str:
    """Wrap a title to `width`, indenting continuation lines so they read as
    part of the same task rather than a new row."""
    flat = title.replace("\r\n", " ").replace("\n", " ")
    lines = textwrap.wrap(flat, width=max(width, 10), subsequent_indent="  ") or [""]
    return "\n".join(lines)


def _format_dt_due(dt: Optional[datetime]) -> str:
    """Format a due datetime for display, hiding the time-of-day for the two
    "no specific time" conventions in use: the literal-UTC-midnight sentinel
    from migrated data, and 23:59 local time — used for tasks created
    directly in Vikunja, which has no native date-only due date, as a
    "due sometime today" marker."""
    if dt is None:
        return ""
    if (dt.hour, dt.minute, dt.second) == (0, 0, 0):
        return dt.strftime("%Y-%m-%d")
    local = dt.astimezone()
    if (local.hour, local.minute) == (23, 59):
        return local.strftime("%Y-%m-%d")
    return local.strftime("%Y-%m-%d %H:%M")


def _format_due(task: dict) -> str:
    return _format_dt_due(_parse_vikunja_ts(task.get("due_date", "")))


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


def _match_project(name: str, all_projects: list) -> int:
    """Return a project ID from a name substring. Interactive picker if ambiguous."""
    hits = [p for p in all_projects if name.lower() in p["title"].lower()]
    if not hits:
        console.print(
            f"[red]No project matching '{name}'.[/red]  "
            "Run [bold]sp projects[/bold] to see all options."
        )
        raise typer.Exit(1)
    if len(hits) == 1:
        return hits[0]["id"]
    chosen = questionary.select(
        f"Multiple projects match '{name}', pick one:",
        choices=[p["title"] for p in hits],
    ).ask()
    if chosen is None:
        raise typer.Exit(0)
    return next(p["id"] for p in hits if p["title"] == chosen)


def _pick_project(all_projects: list) -> int:
    """Interactive project picker. Returns project ID."""
    chosen = questionary.select(
        "Choose a project:",
        choices=[p["title"] for p in all_projects],
    ).ask()
    if chosen is None:
        raise typer.Exit(0)
    return next(p["id"] for p in all_projects if p["title"] == chosen)


def _default_project_id(all_projects: list) -> int:
    return next(
        (p["id"] for p in all_projects if p["title"] == INBOX_PROJECT_TITLE),
        all_projects[0]["id"],
    )


# ─── Task resolution ──────────────────────────────────────────────────────────

def _resolve_task(query: Optional[str], include_done: bool = False) -> dict:
    """Find a single task via search, with an interactive picker when needed."""
    is_tty = sys.stdin.isatty()
    params: dict = {}
    if query:
        params["s"] = query
    if not include_done:
        params["filter"] = "done = false"

    tasks: list = _get("/tasks", params)

    if not tasks:
        msg = f"No tasks matching '{query}'." if query else "No active tasks found."
        console.print(f"[yellow]{msg}[/yellow]")
        raise typer.Exit(1)

    if len(tasks) == 1:
        return tasks[0]

    if not is_tty:
        tip = f" for '{query}'" if query else ""
        console.print(
            f"[red]Multiple tasks found{tip}.[/red]  Run interactively to choose."
        )
        raise typer.Exit(1)

    all_projects: list = _get("/projects")
    def _task_label(t: dict) -> str:
        project = _project_name(t, all_projects)
        due = _format_due(t)
        suffix = f"  [{project}]" if project else ""
        if due:
            suffix += f"  {due}"
        title = t["title"].replace("\r\n", "\n").replace("\n", " ↵ ")
        return f"{title}{suffix}"

    labels = [_task_label(t) for t in tasks]
    prompt = (
        f"Multiple matches for '{query}', pick one:" if query else "Select a task:"
    )
    chosen = questionary.select(prompt, choices=labels).ask()
    if chosen is None:
        raise typer.Exit(0)
    return tasks[labels.index(chosen)]


# ─── Commands ─────────────────────────────────────────────────────────────────

@app.command()
def add(
    title: str = typer.Argument(..., help="Task title"),
    project: Optional[str] = typer.Option(
        None, "--project", "-p", help="Project name (substring match)"
    ),
    tag: Optional[List[str]] = typer.Option(
        None, "--tag", "-t", help="Label name (repeat for multiple: -t foo -t bar)"
    ),
    notes: Optional[str] = typer.Option(None, "--notes", "-n", help="Task description"),
    due: Optional[str] = typer.Option(None, "--due", "-d", help="Due date (today, tomorrow, or YYYY-MM-DD)"),
):
    """Create a new task.

    When --project is omitted in an interactive terminal, you will be prompted
    to choose one. Pass --project to skip the prompt.
    """
    is_tty = sys.stdin.isatty()
    all_projects: list = _get("/projects")

    if project:
        project_id = _match_project(project, all_projects)
    elif is_tty:
        project_id = _pick_project(all_projects)
    else:
        project_id = _default_project_id(all_projects)

    body: dict = {"title": title}
    if notes:
        body["description"] = notes
    if due:
        body["due_date"] = _due_value_to_iso(due)

    result: dict = _put(f"/projects/{project_id}/tasks", body)

    if tag:
        all_labels: list = _get("/labels")
        for t in tag:
            hits = [x for x in all_labels if t.lower() in x["title"].lower()]
            if not hits:
                console.print(f"[yellow]Warning:[/yellow] no label matching '{t}' — skipped.")
            elif len(hits) == 1:
                _put(f"/tasks/{result['id']}/labels", {"label_id": hits[0]["id"]})
            elif is_tty:
                chosen = questionary.select(
                    f"Multiple labels match '{t}', pick one:",
                    choices=[x["title"] for x in hits],
                ).ask()
                if chosen is None:
                    raise typer.Exit(0)
                label_id = next(x["id"] for x in hits if x["title"] == chosen)
                _put(f"/tasks/{result['id']}/labels", {"label_id": label_id})
            else:
                console.print(f"[yellow]Warning:[/yellow] multiple labels match '{t}' — skipped.")

    project_title = next((p["title"] for p in all_projects if p["id"] == project_id), "?")
    console.print(
        f"[green]✓[/green] Created: [bold]{result['title']}[/bold]  ({project_title})"
    )


@app.command()
def edit(
    query: Optional[str] = typer.Argument(None, help="Part of the task title to search for"),
    title: Optional[str] = typer.Option(None, "--title", help="New title"),
    notes: Optional[str] = typer.Option(None, "--notes", "-n", help="New description"),
    due: Optional[str] = typer.Option(None, "--due", "-d", help="Due date (today, tomorrow, or YYYY-MM-DD)"),
    project: Optional[str] = typer.Option(None, "--project", "-p", help="Move to project (name substring)"),
):
    """Edit an existing task."""
    task = _resolve_task(query)

    changes: dict = {}
    if title:
        changes["title"] = title
    if notes:
        changes["description"] = notes
    if due:
        changes["due_date"] = _due_value_to_iso(due)
    if project:
        all_projects: list = _get("/projects")
        changes["project_id"] = _match_project(project, all_projects)

    if not changes:
        console.print("[yellow]Nothing to update. Use --title, --notes, --due, or --project.[/yellow]")
        raise typer.Exit(0)

    _task_update(task["id"], changes)
    console.print(f"[green]✓[/green] Updated: [bold]{task['title']}[/bold]")


@app.command()
def done(
    query: Optional[str] = typer.Argument(
        None, help="Part of the task title to search for"
    ),
):
    """Mark a task as done.

    Omit QUERY to pick interactively from all active tasks.
    """
    task = _resolve_task(query)
    _task_update(task["id"], {"done": True})
    console.print(f"[green]✓[/green] Done: [bold]{task['title']}[/bold]")


@app.command(name="ls")
def list_tasks(
    project: Optional[str] = typer.Option(
        None, "--project", "-p", help="Filter by project name"
    ),
    due: Optional[str] = typer.Option(
        None, "--due", "-d", help="Filter by due date (today, tomorrow, or YYYY-MM-DD)"
    ),
    done_flag: bool = typer.Option(
        False, "--done", help="Include completed tasks"
    ),
    query: Optional[str] = typer.Option(None, "--query", "-q", help="Filter by title"),
):
    """List tasks."""
    all_projects: list = _get("/projects")

    params: dict = {}
    if query:
        params["s"] = query
    filter_parts: List[str] = []
    if not done_flag:
        filter_parts.append("done = false")
    if project:
        filter_parts.append(f"project_id = {_match_project(project, all_projects)}")
    if filter_parts:
        params["filter"] = " && ".join(filter_parts)

    tasks: list = _get("/tasks", params)

    if due:
        due_day = _parse_due(due)
        tasks = [t for t in tasks if _format_due(t)[:10] == due_day]

    def _sort_key(t: dict):
        """Chronological by due instant, but the 23:59-local "no specific
        time" marker sorts as if due at the start of that local day (like
        the UTC-midnight day-only convention), not at its literal time."""
        dt = _parse_vikunja_ts(t.get("due_date", ""))
        if dt is None:
            return float("inf")
        if (dt.hour, dt.minute, dt.second) == (0, 0, 0):
            return dt.timestamp()
        local = dt.astimezone()
        if (local.hour, local.minute) == (23, 59):
            return datetime(local.year, local.month, local.day, tzinfo=timezone.utc).timestamp()
        return dt.timestamp()
    tasks.sort(key=_sort_key)

    if not tasks:
        console.print("[yellow]No tasks found.[/yellow]")
        return

    # Precompute the fixed-content columns so the Title column can be sized
    # against whatever space they actually need, and pre-wrapped to match.
    project_cells = [_project_rich_name(t, all_projects) for t in tasks]
    labels_cells = [_labels_rich_text(t) for t in tasks]
    due_cells = [_format_due(t) for t in tasks]
    recurring_cells = ["[blue]↻[/blue]" if t.get("repeat_after") else "" for t in tasks]
    done_cells = ["[green]✓[/green]" if t.get("done") else "" for t in tasks]

    def _col_w(header: str, cells: List[str]) -> int:
        return max([len(header)] + [Text.from_markup(c).cell_len for c in cells])

    w_project = _col_w("Project", project_cells)
    w_labels = _col_w("Labels", labels_cells)
    w_due = _col_w("Due", due_cells)
    w_recurring = 1
    w_done = 1

    num_columns = 6
    overhead = 3 * num_columns + 1  # empirical: per-column padding + table margins
    other_width = w_project + w_labels + w_due + w_recurring + w_done
    # Below MIN_TABLE_WIDTH there isn't enough room to lay out all columns
    # sensibly; render at MIN_TABLE_WIDTH instead and let the terminal soft-wrap.
    render_width = max(console.size.width, MIN_TABLE_WIDTH)
    title_width = max(render_width - other_width - overhead, 1)
    render_console = Console(width=render_width)

    table = Table(box=box.SIMPLE, show_header=True, header_style="bold")
    table.add_column("Title", width=title_width)
    table.add_column("Project", width=w_project)
    table.add_column("Labels", width=w_labels)
    table.add_column("Due", style="yellow", width=w_due, no_wrap=True)
    table.add_column("", width=w_recurring, justify="center", no_wrap=True)
    table.add_column("", width=w_done, justify="center")

    for i, t in enumerate(tasks):
        table.add_row(
            _wrap_title(t["title"], title_width),
            project_cells[i],
            labels_cells[i],
            due_cells[i],
            recurring_cells[i],
            done_cells[i],
            style="dim" if t.get("done") else "",
        )

    render_console.print(table)
    console.print(f"[dim]{len(tasks)} task(s)[/dim]")

    if due:
        estimates = [_parse_estimate_minutes(t["title"]) for t in tasks if not t.get("done")]
        missing = estimates.count(None)
        total = sum(e for e in estimates if e is not None)
        console.print(f"[bold]Estimado:[/bold] {_format_minutes(total)}")
        if missing:
            console.print(f"[yellow]⚠ {missing} tarea(s) sin estimación[/yellow]")


@app.command()
def projects():
    """List all projects."""
    all_projects: list = _get("/projects")
    if not all_projects:
        console.print("[yellow]No projects found.[/yellow]")
        return
    table = Table(box=box.SIMPLE, show_header=True, header_style="bold")
    table.add_column("Project")
    for p in all_projects:
        table.add_row(_project_rich_label(p))
    console.print(table)


@app.command()
def delete(
    query: Optional[str] = typer.Argument(
        None, help="Part of the task title to search for"
    ),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip confirmation prompt"),
):
    """Delete a task."""
    task = _resolve_task(query, include_done=True)
    if not yes:
        answer = questionary.confirm(
            f'Delete "{task["title"]}"?', default=False
        ).ask()
        if answer is None or not answer:
            console.print("[dim]Cancelled.[/dim]")
            raise typer.Exit(0)
    _delete(f"/tasks/{task['id']}")
    console.print(f"[red]✗[/red] Deleted: [bold]{task['title']}[/bold]")


@app.command()
def punt(
    query: Optional[str] = typer.Argument(
        None, help="Part of the task title to search for"
    ),
):
    """Push a task's due date to the next day."""
    task = _resolve_task(query)

    dt = _parse_vikunja_ts(task.get("due_date", ""))
    if dt is None:
        new_dt = datetime.combine(date.today() + timedelta(days=1), datetime.min.time(), tzinfo=timezone.utc)
    else:
        new_dt = dt + timedelta(days=1)

    new_iso = new_dt.strftime("%Y-%m-%dT%H:%M:%SZ")
    _task_update(task["id"], {"due_date": new_iso})

    label = _format_dt_due(new_dt)
    console.print(f"[green]✓[/green] Punted: [bold]{task['title']}[/bold]  → {label}")


if __name__ == "__main__":
    app()
