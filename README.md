# meldX — Personal Todo that Agents Can Claim and Complete

One Python process serves a shared todo queue through MCP, REST, SSE, and a live Today view.
Agents claim exclusive leases so they can work without colliding.

**Stack:** FastAPI, MCP Python SDK, SQLModel, Postgres, Alembic, sse-starlette,
Jinja2, HTMX. Dependencies are managed with uv; no frontend build step.

## Status

P1–P4 are merged. P5 implements the REST mirror and Today view; its acceptance checklist
is in [docs/plan.md](docs/plan.md#p5-web--api). P6 (CLI, Render deployment, and
ChatGPT Connector verification) is next. Docker and Render configuration exist;
production deployment and Connector round-trip are not yet verified.

## Run locally

Use Python 3.12+ and a local or development Postgres database.

```sh
uv sync
cp .env.example .env
# Set DATABASE_URL in your shell; .env is a reference file, not loaded automatically.
export DATABASE_URL='postgresql+asyncpg://meldx:meldx@localhost:5432/meldx'
uv run alembic upgrade head
uv run uvicorn meldx.app.main:app --reload
```

Open `http://localhost:8000/`. Add a task, choose the agent ID to claim as, then Claim
or Complete. Inbox, Today, and Doing are grouped; done and snoozed tasks are hidden.
Agent badges, priorities, descriptions, and remaining leases appear on each card.
HTMX, its SSE extension, and Tailwind load from CDNs.

## Interfaces

- `POST /mcp`: seven MCP tools via Streamable HTTP.
- `GET /health`: readiness via database `SELECT 1`.
- `GET /api/tasks?status=doing&today_only=true`: list/filter tasks.
- `POST /api/tasks`: create with `title`, optional `description`, `priority`, `due_at`, `delegate_to`.
- `POST /api/tasks/{id}/claim`: JSON `{"agent_id":"codex-1"}`.
- `POST /api/tasks/{id}/complete`: complete and clear ownership/lease.
- `PATCH /api/tasks/{id}`: update title, description, or priority.
- `POST /api/tasks/{id}/handoff`: JSON `{"to_agent":"hermes-1","note":"take over"}`.
- `POST /api/tasks/{id}/snooze`: JSON `{"until":"tomorrow 9am"}`.
- `GET /api/agents`, `POST /api/agents`: list/upsert (`id`, `type`, optional `status`).
- `GET /api/events`: SSE task events (`created`, `claimed`, `completed`, `updated`).
- `/docs`: interactive OpenAPI documentation.

REST uses the same task services as MCP. Missing tasks return 404, locked/leased tasks
409, invalid due strings 400, and schema validation errors 422. Task error responses
retain the MCP message strings under `detail`.

Each SSE connection subscribes to its own bounded in-memory queue. Events invalidate
and refresh the HTML board through HTMX. Reconnecting clients refresh current state;
there is no durable event replay. `take=N` streams N future task events then closes.
Slow subscribers retain up to 128 events; the latest refresh recovers current state.
Run one uvicorn worker so all mutations and subscribers share the same bus.

## Validation

```sh
# Use a separate local test database or a separate Neon branch. Never point this at prod.
export DATABASE_URL_TEST='postgresql+asyncpg://meldx:meldx@localhost:5432/meldx_test'
uv run ruff check .
uv run mypy --strict meldx
uv run pytest -q
```

Postgres integration tests prove exclusive claims, expired-lease reclaim, REST 409
mapping for held locks, and concurrent REST claims. They skip if the dedicated test
database is unreachable; SQLite tests alone do not prove Postgres locking.

P5 validation: 23 tests passed with a dedicated local Postgres database, Ruff and strict
mypy passed, and `alembic check` reported no schema changes. Two real browser tabs
verified create → claim → complete updates, lease countdowns, and lease conflict feedback.
