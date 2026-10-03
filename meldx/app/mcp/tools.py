"""7 MCP tools. Docstrings are the ChatGPT-visible descriptions.

Spec: docs/mcp-tools.md (local-only for now). Thin wrappers over
meldx.app.services.tasks so REST shares the same SQL.
"""
from typing import Any

from mcp.server.mcpserver import MCPServer

from meldx.app.db import session_scope
from meldx.app.services import tasks as svc

mcp = MCPServer("meldx")


@mcp.tool()
async def todo_create(
    title: str,
    description: str | None = None,
    due_at: str | None = None,
    priority: str = "med",
    delegate_to: str | None = None,
) -> dict[str, Any]:
    """Create a new todo task. Use for any task the user mentions. Returns the created task."""
    async with session_scope() as session:
        task = await svc.create_task(
            session,
            title=title,
            description=description,
            due_at=due_at,
            priority=priority,
            delegate_to=delegate_to,
        )
        return task.model_dump(mode="json")


@mcp.tool()
async def todo_list(status: str | None = None, today_only: bool = False) -> list[dict[str, Any]]:
    """List tasks. Filter by status (inbox/today/doing/done/snoozed) or today_only for due today."""
    async with session_scope() as session:
        tasks = await svc.list_tasks(session, status=status, today_only=today_only)
        return [t.model_dump(mode="json") for t in tasks]


@mcp.tool()
async def todo_claim(task_id: str, agent_id: str) -> dict[str, Any]:
    """Claim a task for exclusive work. Uses FOR UPDATE SKIP LOCKED to avoid races. Sets 30m lease."""
    async with session_scope() as session:
        task = await svc.claim_task(session, task_id=task_id, agent_id=agent_id)
        return task.model_dump(mode="json")


@mcp.tool()
async def todo_complete(task_id: str) -> dict[str, Any]:
    """Mark task as done. Clears lease and agent."""
    async with session_scope() as session:
        task = await svc.complete_task(session, task_id=task_id)
        return task.model_dump(mode="json")


@mcp.tool()
async def todo_update(
    task_id: str,
    title: str | None = None,
    description: str | None = None,
    priority: str | None = None,
) -> dict[str, Any]:
    """Update title, description, or priority of a task."""
    async with session_scope() as session:
        task = await svc.update_task(
            session, task_id=task_id, title=title, description=description, priority=priority
        )
        return task.model_dump(mode="json")


@mcp.tool()
async def todo_handoff(task_id: str, to_agent: str, note: str | None = None) -> dict[str, Any]:
    """Handoff task to another agent (e.g., hermes -> codex) with optional note appended."""
    async with session_scope() as session:
        task = await svc.handoff_task(session, task_id=task_id, to_agent=to_agent, note=note)
        return task.model_dump(mode="json")


@mcp.tool()
async def todo_snooze(task_id: str, until: str) -> dict[str, Any]:
    """Snooze task until ISO datetime or natural language like 'tomorrow 9am'."""
    async with session_scope() as session:
        task = await svc.snooze_task(session, task_id=task_id, until=until)
        return task.model_dump(mode="json")
