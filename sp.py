#!/usr/bin/env python3
"""Super Productivity CLI — wraps the Local REST API at http://127.0.0.1:3876

Prerequisites:
  • Super Productivity desktop app must be running
  • Enable the Local REST API: Settings → Misc → Enable local REST API

Tip: alias sp='python /path/to/sp.py'
"""

from __future__ import annotations

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
    labels = [
        f"{t['title']}  [{_project_name(t, all_projects)}]" for t in tasks
    ]
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
    today: bool = typer.Option(False, "--today", help="Schedule for today"),
    due: Optional[str] = typer.Option(None, "--due", "-d", help="Due date (YYYY-MM-DD)"),
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
        body["dueDay"] = due

    tag_ids: List[str] = []
    if today:
        tag_ids.append("TODAY")
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
    proj_name = _project_name(result, all_projects)
    console.print(
        f"[green]✓[/green] Created: [bold]{result['title']}[/bold]  [dim]({proj_name})[/dim]"
    )


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
    today: bool = typer.Option(False, "--today", help="Only today's tasks"),
    done_flag: bool = typer.Option(
        False, "--done", help="Include completed tasks"
    ),
    query: Optional[str] = typer.Option(None, "--query", "-q", help="Filter by title"),
):
    """List tasks."""
    all_projects: list = _get("/projects")

    params: dict = {}
    if query:
        params["query"] = query
    if done_flag:
        params["includeDone"] = True
    if today:
        params["tagId"] = "TODAY"
    elif project:
        params["projectId"] = _match_project(project, all_projects)

    tasks: list = _get("/tasks", params)
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
    table.add_column("Project", style="cyan", ratio=2)
    table.add_column("Due", style="yellow", ratio=2, no_wrap=True)
    table.add_column("", ratio=1, justify="center")

    for t in tasks:
        done_mark = "[green]✓[/green]" if t.get("isDone") else ""
        table.add_row(
            t["title"],
            _project_name(t, all_projects),
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
    table.add_column("Project", style="cyan")
    for p in all_projects:
        table.add_row(p["title"])
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
