# Build plan — 6 phases (agent-runnable)

> Status: P1-P5 merged; P6 CLI and deployment preparation implemented and locally verified. Production deployment and ChatGPT connection deferred by the user.

> Each phase: Files / Commands / Acceptance. Copy Prompt 1 → OpenCode (P1-P3), Prompt 2 → Hermes (P4-P5), Prompt 3 → Codex (P6).

## P1 Scaffold (30m)
**Files:** `pyproject.toml` (uv), `.env.example`, `.gitignore`, `meldx/__init__.py`, `Dockerfile`, `render.yaml` skeleton
**Commands:**
- [ ] `uv init --name meldx && uv add fastapi "mcp[cli]" sqlmodel asyncpg alembic sse-starlette jinja2 python-multipart typer httpx rich dateparser structlog`
- [ ] `uv add --dev pytest pytest-asyncio httpx ruff mypy`
- [ ] `cp .env.example .env` with `DATABASE_URL=postgresql+asyncpg://... (Neon)`
**Acceptance:**
- [ ] `uv run uvicorn meldx.app.main:app --reload` boots (even with empty routers)
- [ ] `ruff check . && mypy meldx` clean

## P2 DB (45m)
**Files:** `meldx/app/db.py`, `meldx/app/models.py`, `alembic.ini`, `alembic/env.py`, `alembic/versions/*`
**Commands:**
- [ ] `alembic revision --autogenerate -m "tasks agents"`
- [ ] `alembic upgrade head`
- [ ] `uv run python -c "from meldx.app.models import Task; print(Task.__table__.columns.keys())"`
**Acceptance:**
- [ ] Tables `tasks`, `agents` + indexes exist on empty Neon DB
- [ ] `GET /health` returns `{"ok": true}` after `SELECT 1`

## P3 MCP Core (2h)
**Files:** `meldx/app/main.py` (mount `/mcp`), `meldx/app/mcp/tools.py` (7 tools), `tests/test_mcp_tools.py`
**Commands:**
- [ ] `uv run uvicorn meldx.app.main:app --reload`
- [ ] Open MCP Inspector at `http://localhost:8000/mcp`: create → list → claim → complete
- [ ] `pytest tests/test_mcp_tools.py -q`
**Acceptance:**
- [ ] 7 tools visible in Inspector with docstring descriptions
- [ ] create/list/claim/complete round-trip green
- [ ] Missing task → `Task locked or not found`

## P4 Leases + Events (1.5h)
**Files:** `meldx/app/services/tasks.py` (shared claim fn), `meldx/app/api/events.py`, `tests/test_claim_race.py`
**Commands:**
- [ ] `pytest tests/test_claim_race.py -q` (2 concurrent `todo_claim`, one wins)
- [ ] `curl -N http://localhost:8000/api/events` shows `task.claimed` after claim
**Acceptance:**
- [ ] Concurrent claim: winner `doing+lease`, loser gets `Task leased to {agent} until {ts}` or locked
- [ ] Expired lease is claimable again
- [ ] `todo_complete` clears lease/agent, `todo_handoff`/`todo_snooze` behave per spec

## P5 Web + API

[Issue #5](https://github.com/prakash601/meldX/issues/5)

- [x] REST task routes mirror MCP through shared task services.
- [x] Minimal agent list/upsert routes.
- [x] Today groups inbox/today/doing; done and snoozed tasks hidden.
- [x] Cards show description, priority, agent badge, and a ticking lease countdown.
- [x] HTMX create/claim/complete actions and visible error feedback.
- [x] SSE broadcasts created/claimed/completed/updated to every active client.
- [x] Reconnection refreshes current board; slow subscriber queues are bounded.
- [x] REST preserves MCP messages: missing 404, locked/leased 409.
- [x] Web/API routes registered before root MCP mount; MCP round-trip retained.
- [x] `pytest -q`: 23 passed with dedicated local Postgres, including REST race/lock tests.
- [x] `ruff check .` and `mypy --strict meldx` pass.
- [x] Empty test DB migrations applied; `alembic check` found no schema changes.
- [x] Two browser tabs verified add/claim/complete, countdown, and conflict feedback.

P1-P4 historical command checkboxes below/above are not a fresh claim of Neon or
manual MCP Inspector verification. P5 validation used local dedicated Postgres.

## P6 CLI + Deploy

[Issue #6](https://github.com/prakash601/meldX/issues/6)

- [x] Typer `meldx add/list/done/claim` entry point installed through pyproject.
- [x] CLI uses async httpx, API environment variable, Rich output, nonzero errors.
- [x] Local CLI add/list/claim/conflict/done round-trip against the Docker server.
- [x] Locked Docker build includes source and migrations; excludes secrets and Git.
- [x] Container runs migrations, binds to PORT, and serves one worker.
- [x] Render blueprint uses Docker startup, `/health`, and DATABASE_URL secret configuration.
- [x] MCP Host/Origin validation supports Render and custom hostnames.
- [x] Container `/health` returns 200 after SELECT 1; web and MCP round-trips pass.
- [x] `pytest -q`: 40 passed including Postgres race tests, CLI and deployed-host contracts.
- [x] Ruff and strict mypy pass; Docker/installed CLI smoke checks added to CI.
- [x] [Deployment and ChatGPT connection guide](deploy.md) documents remaining checks.
- [ ] Render production deployment healthy, `/health` 200, migration/health logs verified.
- [ ] CLI `API=https://<actual-render-host>` list/add/claim/done verified in production.
- [ ] Real ChatGPT connection can list → create → complete through `/mcp`.
- [ ] PROJECT_SPEC v1 acceptance verified against production.

Production and ChatGPT verification were explicitly deferred. Keep #6 open until
these checks pass; local container evidence alone does not complete v1 deployment.


---
**Prompts to copy:**
- *Prompt 1 — OpenCode P1-P3:* "In /meldX, follow AGENTS.md + docs/plan.md P1-P3 + docs/data-model.md + docs/mcp-tools.md. Scaffold, DB, MCP core. Stop when Inspector round-trip is green. Do not touch web/SSE/CLI."
- *Prompt 2 — Hermes P4-P5:* "In /meldX, assume P3 green. Implement P4-P5 per docs/plan.md. Shared claim fn with FOR UPDATE SKIP LOCKED, SSE, REST+HTMX Today. Prove race test."
- *Prompt 3 — Codex P6:* "In /meldX, assume P5 green. Implement Typer CLI + Dockerfile + render.yaml + Render deploy + Connector verification."
