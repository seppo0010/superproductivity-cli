#!/usr/bin/env python3
"""Super Productivity CLI — wraps the Local REST API at http://127.0.0.1:3876

Prerequisites:
  • Super Productivity desktop app must be running
  • Enable the Local REST API: Settings → Misc → Enable local REST API

Tip: alias sp='python /path/to/sp.py'
"""

from __future__ import annotations

import re
import sys
from datetime import date, datetime, timedelta
from typing import List, Optional

import questionary
import requests
import typer
from rich import box
from rich.console import Console
from rich.table import Table

app = typer.Typer(
    help="Super Productivity CLI",
    add_completion=False,
    no_args_is_help=True,
)
console = Console()

BASE_URL = "http://127.0.0.1:3876"
INBOX_LABEL = "Inbox (no project)"


# ─── HTTP helpers ─────────────────────────────────────────────────────────────

def _connection_error() -> None:
    console.print(
        "[bold red]Cannot reach Super Productivity.[/bold red]\n"
        "  • Is the desktop app running?\n"
        "  • Enable via: [bold]Settings → Misc → Enable local REST API[/bold]"
    )
    raise typer.Exit(1)


def _unwrap(r: requests.Response) -> object:
    body = r.json()
    if not body.get("ok"):
        err = body.get("error", {})
        console.print(f"[red]API error:[/red] {err.get('message', str(body))}")
        raise typer.Exit(1)
    return body["data"]


def _get(path: str, params: Optional[dict] = None) -> object:
    try:
        return _unwrap(requests.get(f"{BASE_URL}{path}", params=params, timeout=10))
    except requests.ConnectionError:
        _connection_error()
    except requests.Timeout:
        console.print("[red]Request timed out.[/red]")
        raise typer.Exit(1)


def _post(path: str, body: dict) -> object:
    try:
        return _unwrap(requests.post(f"{BASE_URL}{path}", json=body, timeout=10))
    except requests.ConnectionError:
        _connection_error()


def _patch(path: str, body: dict) -> object:
    try:
        return _unwrap(requests.patch(f"{BASE_URL}{path}", json=body, timeout=10))
    except requests.ConnectionError:
        _connection_error()


def _delete(path: str) -> object:
    try:
        return _unwrap(requests.delete(f"{BASE_URL}{path}", timeout=10))
    except requests.ConnectionError:
        _connection_error()


# ─── Project helpers ──────────────────────────────────────────────────────────

def _project_name(task: dict, all_projects: list) -> str:
    pid = task.get("projectId")
    if not pid:
        return "Inbox"
    return next((p["title"] for p in all_projects if p["id"] == pid), "?")


def _hex_color_from(obj: dict) -> Optional[str]:
    """Extract a hex color from a tag or project dict (color field, then theme.primary)."""
    direct = obj.get("color")
    if direct and re.match(r"#[0-9a-fA-F]{6}$", direct):
        return direct
    primary = obj.get("theme", {}).get("primary", "")
    m = re.match(r"rgb\(\s*(\d+),\s*(\d+),\s*(\d+)\s*\)", primary)
    if m:
        r, g, b = int(m.group(1)), int(m.group(2)), int(m.group(3))
        return f"#{r:02x}{g:02x}{b:02x}"
    if re.match(r"#[0-9a-fA-F]{6}$", primary):
        return primary
    return None


def _project_hex_color(project: dict) -> Optional[str]:
    return _hex_color_from(project)


def _rich_badge(name: str, color: Optional[str]) -> str:
    if not color:
        return name
    r, g, b = int(color[1:3], 16), int(color[3:5], 16), int(color[5:7], 16)
    text = "black" if (0.299 * r + 0.587 * g + 0.114 * b) / 255 > 0.5 else "white"
    return f"[{text} on {color}] {name} [/]"


def _project_rich_label(project: dict) -> str:
    return _rich_badge(project["title"], _project_hex_color(project))


def _fmt_duration(ms: int) -> str:
    if not ms:
        return ""
    total_min = ms // 60_000
    h, m = divmod(total_min, 60)
    return f"{h}h {m:02d}m" if h else f"{m}m"


def _tags_rich_text(task: dict, all_tags: list) -> str:
    tag_ids = task.get("tagIds") or []
    parts = []
    for tid in tag_ids:
        tag = next((x for x in all_tags if x["id"] == tid), None)
        if tag:
            parts.append(_rich_badge(tag["title"], _hex_color_from(tag)))
    return " ".join(parts)


def _project_rich_name(task: dict, all_projects: list) -> str:
    pid = task.get("projectId")
    proj = next((p for p in all_projects if p["id"] == (pid or "INBOX_PROJECT")), None)
    if proj:
        return _project_rich_label(proj)
    return "Inbox" if not pid else "?"


def _parse_duration(value: str) -> int:
    """Parse a duration string like '1h30m', '90m', '2h' into milliseconds."""
    value = value.strip()
    m = re.fullmatch(r"(?:(\d+)h)?(?:(\d+)m)?", value)
    if not m or not m.group(0):
        console.print(f"[red]Invalid duration '[bold]{value}[/bold]'. Use formats like 1h30m, 90m, or 2h.[/red]")
        raise typer.Exit(1)
    hours = int(m.group(1) or 0)
    minutes = int(m.group(2) or 0)
    if hours == 0 and minutes == 0:
        console.print(f"[red]Invalid duration '[bold]{value}[/bold]'. Use formats like 1h30m, 90m, or 2h.[/red]")
        raise typer.Exit(1)
    return (hours * 60 + minutes) * 60_000


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


def _format_due(task: dict) -> str:
    ts = task.get("dueWithTime")
    if ts:
        return datetime.fromtimestamp(ts / 1000).strftime("%Y-%m-%d %H:%M")
    return task.get("dueDay") or ""


def _match_project(name: str, all_projects: list) -> str:
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


def _pick_project(all_projects: list) -> Optional[str]:
    """Interactive project picker. Returns project ID, or None for Inbox."""
    chosen = questionary.select(
        "Choose a project:",
        choices=[INBOX_LABEL] + [p["title"] for p in all_projects],
    ).ask()
    if chosen is None:
        raise typer.Exit(0)
    if chosen == INBOX_LABEL:
        return None
    return next(p["id"] for p in all_projects if p["title"] == chosen)


# ─── Task resolution ──────────────────────────────────────────────────────────

def _resolve_task(query: Optional[str], include_done: bool = False) -> dict:
    """Find a single task via search, with an interactive picker when needed."""
    is_tty = sys.stdin.isatty()
    params: dict = {}
    if query:
        params["query"] = query
    if include_done:
        params["includeDone"] = True

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
        return f"{t['title']}{suffix}"

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
        None, "--tag", "-t", help="Tag name (repeat for multiple: -t foo -t bar)"
    ),
    notes: Optional[str] = typer.Option(None, "--notes", "-n", help="Task notes"),
    due: Optional[str] = typer.Option(None, "--due", "-d", help="Due date (today, tomorrow, or YYYY-MM-DD)"),
    estimate: Optional[str] = typer.Option(None, "--estimate", "-e", help="Time estimate (e.g. 1h30m, 90m, 2h)"),
):
    """Create a new task.

    When --project is omitted in an interactive terminal, you will be prompted
    to choose one. Pass --project to skip the prompt.
    """
    is_tty = sys.stdin.isatty()
    all_projects: list = _get("/projects")

    project_id: Optional[str] = None
    if project:
        project_id = _match_project(project, all_projects)
    elif is_tty:
        project_id = _pick_project(all_projects)

    body: dict = {"title": title}
    if project_id:
        body["projectId"] = project_id
    if notes:
        body["notes"] = notes
    if due:
        body["dueDay"] = _parse_due(due)
    if estimate:
        body["timeEstimate"] = _parse_duration(estimate)

    tag_ids: List[str] = []
    if tag:
        all_tags: list = _get("/tags")
        for t in tag:
            hits = [x for x in all_tags if t.lower() in x["title"].lower()]
            if not hits:
                console.print(f"[yellow]Warning:[/yellow] no tag matching '{t}' — skipped.")
            elif len(hits) == 1:
                tag_ids.append(hits[0]["id"])
            elif is_tty:
                chosen = questionary.select(
                    f"Multiple tags match '{t}', pick one:",
                    choices=[x["title"] for x in hits],
                ).ask()
                if chosen is None:
                    raise typer.Exit(0)
                tag_ids.append(next(x["id"] for x in hits if x["title"] == chosen))
            else:
                console.print(f"[yellow]Warning:[/yellow] multiple tags match '{t}' — skipped.")

    if tag_ids:
        body["tagIds"] = tag_ids

    result: dict = _post("/tasks", body)
    console.print(
        f"[green]✓[/green] Created: [bold]{result['title']}[/bold]  ({_project_rich_name(result, all_projects)})"
    )


@app.command()
def edit(
    query: Optional[str] = typer.Argument(None, help="Part of the task title to search for"),
    title: Optional[str] = typer.Option(None, "--title", help="New title"),
    notes: Optional[str] = typer.Option(None, "--notes", "-n", help="New notes"),
    due: Optional[str] = typer.Option(None, "--due", "-d", help="Due date (today, tomorrow, or YYYY-MM-DD)"),
    estimate: Optional[str] = typer.Option(None, "--estimate", "-e", help="Time estimate (e.g. 1h30m, 90m, 2h)"),
    project: Optional[str] = typer.Option(None, "--project", "-p", help="Move to project (name substring)"),
):
    """Edit an existing task."""
    task = _resolve_task(query)

    body: dict = {}
    if title:
        body["title"] = title
    if notes:
        body["notes"] = notes
    if due:
        body["dueDay"] = _parse_due(due)
    if estimate:
        body["timeEstimate"] = _parse_duration(estimate)
    if project:
        all_projects: list = _get("/projects")
        body["projectId"] = _match_project(project, all_projects)

    if not body:
        console.print("[yellow]Nothing to update. Use --title, --notes, --due, --estimate, or --project.[/yellow]")
        raise typer.Exit(0)

    _patch(f"/tasks/{task['id']}", body)
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
    _patch(f"/tasks/{task['id']}", {"isDone": True})
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
    archived: bool = typer.Option(False, "--archived", "-a", help="Show archived tasks instead of active"),
):
    """List tasks."""
    all_projects: list = _get("/projects")

    params: dict = {}
    if query:
        params["query"] = query
    if archived:
        params["source"] = "archived"
        params["includeDone"] = True
    elif done_flag:
        params["includeDone"] = True
    if project:
        params["projectId"] = _match_project(project, all_projects)

    tasks: list = _get("/tasks", params)
    all_tags: list = _get("/tags")

    if due:
        due_day = _parse_due(due)
        if archived:
            tasks = [
                t for t in tasks
                if t.get("doneOn")
                and datetime.fromtimestamp(t["doneOn"] / 1000).strftime("%Y-%m-%d") == due_day
            ]
        else:
            tasks = [
                t for t in tasks
                if t.get("dueDay") == due_day
                or (
                    t.get("dueWithTime")
                    and datetime.fromtimestamp(t["dueWithTime"] / 1000).strftime("%Y-%m-%d") == due_day
                )
            ]
    tasks.sort(key=lambda t: (
        t["dueWithTime"]
        if t.get("dueWithTime")
        else datetime.strptime(t["dueDay"], "%Y-%m-%d").timestamp() * 1000
        if t.get("dueDay")
        else float("inf")
    ))

    if not tasks:
        console.print("[yellow]No tasks found.[/yellow]")
        return

    table = Table(box=box.SIMPLE, show_header=True, header_style="bold")
    table.add_column("Title", ratio=5)
    table.add_column("Project", ratio=2)
    table.add_column("Tags", ratio=2)
    table.add_column("Est.", style="cyan", ratio=1, no_wrap=True)
    table.add_column("Spent", style="magenta", ratio=1, no_wrap=True)
    table.add_column("Due", style="yellow", ratio=2, no_wrap=True)
    table.add_column("", ratio=1, justify="center")

    for t in tasks:
        done_mark = "[green]✓[/green]" if t.get("isDone") else ""
        table.add_row(
            t["title"],
            _project_rich_name(t, all_projects),
            _tags_rich_text(t, all_tags),
            _fmt_duration(t.get("timeEstimate", 0)),
            _fmt_duration(t.get("timeSpent", 0)),
            _format_due(t),
            done_mark,
            style="dim" if t.get("isDone") else "",
        )

    console.print(table)
    console.print(f"[dim]{len(tasks)} task(s)[/dim]")


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

    if task.get("dueWithTime"):
        new_ts = task["dueWithTime"] + 86_400_000
        _patch(f"/tasks/{task['id']}", {"dueWithTime": new_ts})
        label = datetime.fromtimestamp(new_ts / 1000).strftime("%Y-%m-%d %H:%M")
    elif task.get("dueDay"):
        current = datetime.strptime(task["dueDay"], "%Y-%m-%d").date()
        label = (current + timedelta(days=1)).strftime("%Y-%m-%d")
        _patch(f"/tasks/{task['id']}", {"dueDay": label})
    else:
        label = (date.today() + timedelta(days=1)).strftime("%Y-%m-%d")
        _patch(f"/tasks/{task['id']}", {"dueDay": label})

    console.print(f"[green]✓[/green] Punted: [bold]{task['title']}[/bold]  → {label}")


if __name__ == "__main__":
    app()
