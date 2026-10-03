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
    json_output: Annotated[bool, typer.Option("--json", help="Output JSON for scripting.")] = False,
) -> None:
    """List tasks and their current owners."""
    params: dict[str, Any] = {"today_only": today}
    if status is not None:
        params["status"] = status.value
    tasks = asyncio.run(_request("GET", "/api/tasks", params=params))
    if json_output:
        console.print_json(data=tasks)
        return
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
def done(task_id: Annotated[UUID, typer.Argument(help="Task UUID.")]) -> None:
    """Complete a task and clear its lease."""
    task = asyncio.run(_request("POST", f"/api/tasks/{task_id}/complete"))
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
