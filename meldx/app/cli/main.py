"""Typer commands backed by the shared REST API, locally or on Render."""

import asyncio
import os
from typing import Annotated, Any
from uuid import UUID

import httpx
import typer
from rich.console import Console
from rich.table import Table
from rich.text import Text

from meldx.app.models import Priority, TaskStatus

app = typer.Typer(no_args_is_help=True, help="Manage the todo queue shared with your agents.")
console = Console()
error_console = Console(stderr=True)


async def _request(
    method: str,
    path: str,
    *,
    payload: dict[str, Any] | None = None,
    params: dict[str, Any] | None = None,
) -> Any:
    try:
        url = httpx.URL(os.environ.get("API", "http://localhost:8000"))
        if url.scheme not in {"http", "https"} or not url.host or url.userinfo:
            raise ValueError("API must be an absolute HTTP(S) URL without credentials.")
        async with httpx.AsyncClient(base_url=str(url).rstrip("/"), timeout=60) as client:
            response = await client.request(method, path, json=payload, params=params)
    except (httpx.InvalidURL, ValueError):
        error_console.print(Text("Invalid API URL. Set API to the meldX server's HTTP(S) URL."))
        raise typer.Exit(1) from None
    except httpx.RequestError:
        error_console.print(Text("Could not reach meldX. Check API and the server's availability."))
        raise typer.Exit(1) from None
    if response.is_error:
        try:
            detail = response.json().get("detail", response.reason_phrase)
        except (ValueError, AttributeError):
            detail = response.reason_phrase
        error_console.print(Text(f"HTTP {response.status_code}: {detail}"))
        raise typer.Exit(1)
    try:
        return response.json()
    except ValueError:
        error_console.print(Text("meldX returned an invalid JSON response."))
        raise typer.Exit(1) from None


@app.command()
def add(
    title: Annotated[str, typer.Argument(help="Task title.")],
    description: Annotated[str | None, typer.Option(help="Task details.")] = None,
    due: Annotated[str | None, typer.Option(help="ISO date/time or e.g. tomorrow 9am.")] = None,
    priority: Annotated[Priority, typer.Option(help="low, med, or high.")] = Priority.med,
    delegate: Annotated[str | None, typer.Option(help="Assign a task to this agent.")] = None,
) -> None:
    """Add a task to Inbox, or Today when delegated."""
    task = asyncio.run(
        _request(
            "POST",
            "/api/tasks",
            payload={
                "title": title,
                "description": description,
                "due_at": due,
                "priority": priority.value,
                "delegate_to": delegate,
            },
        )
    )
    console.print_json(data=task)


@app.command("list")
def list_tasks(
    today: Annotated[bool, typer.Option(help="Only tasks due today.")] = False,
    status: Annotated[TaskStatus | None, typer.Option(help="Filter by task status.")] = None,
    view: Annotated[
        str | None,
        typer.Option(help="inbox|today|upcoming|open|overdue|missed|completed (owner timezone)."),
    ] = None,
    json_output: Annotated[bool, typer.Option("--json", help="Output JSON for scripting.")] = False,
) -> None:
    """List tasks and their current owners."""
    params: dict[str, Any] = {"today_only": today}
    if status is not None:
        params["status"] = status.value
    if view is not None:
        params["view"] = view
    tasks = asyncio.run(_request("GET", "/api/tasks", params=params))
    if json_output:
        console.print_json(data=tasks)
        return
    # Kept narrow on purpose: an 80-column terminal still shows the title.
    # Planning/deadline details live in --json, `show`, and `today`.
    table = Table(title="meldX tasks")
    for heading in ("ID", "Title", "Status", "Priority", "Agent", "Lease until"):
        table.add_column(heading)
    for task in tasks:
        table.add_row(
            *(
                Text(str(task.get(key) or "—"))
                for key in ("id", "title", "status", "priority", "agent_id", "lease_expires_at")
            )
        )
    console.print(table)


@app.command()
def today() -> None:
    """What remains today, and what was finished today (owner timezone)."""
    summary = asyncio.run(_request("GET", "/api/tasks/summary"))
    console.print(Text(summary["date"], style="bold teal"))
    for key, label in (
        ("today", "Remaining today"),
        ("overdue", "Overdue"),
        ("missed_plans", "Missed plans"),
        ("completed_today", "Completed today"),
    ):
        tasks = summary[key]
        console.print(Text(f"{label} ({len(tasks)})", style="bold"))
        for task in tasks:
            console.print(f"  {task['title']}  [dim]({task['id']})[/dim]")


@app.command()
def completed(
    days: Annotated[int, typer.Option(help="Window in owner-local days; 1 = today.")] = 1,
) -> None:
    """Done tasks filtered by completion time, newest first."""
    tasks = asyncio.run(
        _request("GET", "/api/tasks", params={"view": "completed", "completed_days": days})
    )
    if not tasks:
        console.print(Text(f"Nothing completed in the last {days} day(s)."))
        return
    table = Table(title=f"Completed (last {days} day(s))")
    for heading in ("ID", "Title", "Completed at"):
        table.add_column(heading)
    for task in tasks:
        table.add_row(str(task["id"]), Text(task["title"]), Text(str(task.get("completed_at") or "—")))
    console.print(table)


@app.command()
def plan(
    task_id: Annotated[UUID, typer.Argument(help="Task UUID.")],
    date: Annotated[str, typer.Argument(help="today, tomorrow, friday, or YYYY-MM-DD.")],
) -> None:
    """Plan a task for an owner-local date. Planning never sets a deadline."""
    task = asyncio.run(_request("POST", f"/api/tasks/{task_id}/plan", payload={"date": date}))
    console.print_json(data=task)


@app.command()
def unplan(task_id: Annotated[UUID, typer.Argument(help="Task UUID.")]) -> None:
    """Clear a task's planned date."""
    task = asyncio.run(_request("POST", f"/api/tasks/{task_id}/plan", payload={"date": None}))
    console.print_json(data=task)


@app.command()
def snooze(
    task_id: Annotated[UUID, typer.Argument(help="Task UUID.")],
    until: Annotated[
        str, typer.Argument(help="ISO datetime or e.g. 'tomorrow 9am'; naive means owner-local.")
    ],
) -> None:
    """Hide a task until an instant. The deadline is preserved."""
    task = asyncio.run(_request("POST", f"/api/tasks/{task_id}/snooze", payload={"until": until}))
    console.print_json(data=task)


@app.command()
def unsnooze(task_id: Annotated[UUID, typer.Argument(help="Task UUID.")]) -> None:
    """Show a snoozed task again."""
    task = asyncio.run(_request("POST", f"/api/tasks/{task_id}/unsnooze"))
    console.print_json(data=task)


@app.command()
def done(task_id: Annotated[UUID, typer.Argument(help="Task UUID.")]) -> None:
    """Complete a task (idempotent) and clear its lease."""
    task = asyncio.run(_request("POST", f"/api/tasks/{task_id}/complete"))
    console.print_json(data=task)


@app.command()
def start(task_id: Annotated[UUID, typer.Argument(help="Task UUID.")]) -> None:
    """Mark a task as personally doing — no agent identity, no lease."""
    task = asyncio.run(_request("POST", f"/api/tasks/{task_id}/start"))
    console.print_json(data=task)


@app.command()
def reopen(task_id: Annotated[UUID, typer.Argument(help="Task UUID.")]) -> None:
    """Reopen a completed task; completion history is preserved."""
    task = asyncio.run(_request("POST", f"/api/tasks/{task_id}/reopen"))
    console.print_json(data=task)


@app.command()
def show(task_id: Annotated[UUID, typer.Argument(help="Task UUID.")]) -> None:
    """Fetch one task with its revision and lifecycle fields."""
    task = asyncio.run(_request("GET", f"/api/tasks/{task_id}"))
    console.print_json(data=task)


@app.command()
def edit(
    task_id: Annotated[UUID, typer.Argument(help="Task UUID.")],
    title: Annotated[str | None, typer.Option(help="New title.")] = None,
    description: Annotated[
        str | None, typer.Option(help="New description; an empty string clears it.")
    ] = None,
    priority: Annotated[Priority | None, typer.Option(help="low, med, or high.")] = None,
) -> None:
    """Edit a task. Only provided options are sent; conflict exits nonzero."""
    payload: dict[str, Any] = {}
    if title is not None:
        payload["title"] = title
    if description is not None:
        payload["description"] = description or None  # empty string clears
    if priority is not None:
        payload["priority"] = priority.value
    task = asyncio.run(_request("PATCH", f"/api/tasks/{task_id}", payload=payload))
    console.print_json(data=task)


@app.command()
def claim(
    task_id: Annotated[UUID, typer.Argument(help="Task UUID.")],
    agent: Annotated[str, typer.Option(help="Agent claiming exclusive work.")],
) -> None:
    """Claim a task for an agent's exclusive lease."""
    task = asyncio.run(_request("POST", f"/api/tasks/{task_id}/claim", payload={"agent_id": agent}))
    console.print_json(data=task)


if __name__ == "__main__":
    app()
