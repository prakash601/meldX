# meldX — Personal Todo that Agents Can Claim and Complete

One Python process serves a shared todo queue through MCP, REST, SSE, and a live Today view.
Agents claim exclusive leases so they can work without colliding.

**Stack:** FastAPI, MCP Python SDK, SQLModel, Postgres, Alembic, sse-starlette,
Jinja2, HTMX. Dependencies are managed with uv; no frontend build step.

## Status

P1–P5 are merged. P6 adds the CLI and tested Docker/Render deployment configuration.
Production deployment and the ChatGPT Connector round-trip are deferred, so
[issue #6](https://github.com/prakash601/meldX/issues/6) remains open.
See [the phase checklist](docs/plan.md#p6-cli--deploy) and [deployment guide](docs/deploy.md).

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

## CLI

The installed `meldx` command talks to the REST API. `API` defaults to
`http://localhost:8000`; set it to another local port or the deployed HTTPS URL.

```sh
uv run meldx add "write report" --description "Q3" --due tomorrow --priority high
uv run meldx add "review report" --delegate hermes-1
uv run meldx list --today
uv run meldx list --status doing --json
uv run meldx claim <task-uuid> --agent codex-1
uv run meldx done <task-uuid>
API=http://localhost:8015 uv run meldx list
```

Add, claim, and done print the returned task as JSON. List uses a Rich table;
`--json` gives IDs and fields for scripting. API, validation, and network failures
exit nonzero with an error on stderr. Requests use a 60-second timeout.

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

## Docker, Render, and ChatGPT

```sh
docker build -t meldx .
# Use your development Postgres URL; host.docker.internal reaches the Mac host.
docker run --rm -p 8000:8000 \
  -e DATABASE_URL='postgresql+asyncpg://meldx:meldx@host.docker.internal:5432/meldx' meldx
```

The container installs locked dependencies, includes Alembic migrations, runs
`alembic upgrade head` before serving, and binds one uvicorn worker to `PORT`
(default 8000). A migration failure stops startup. Secrets and Git data are excluded
from the image build context.

Render uses [render.yaml](render.yaml) and the Dockerfile's command, with `/health`
as its readiness check. Configure `DATABASE_URL` directly in Render. Render's
`RENDER_EXTERNAL_HOSTNAME` is accepted by MCP; set `MCP_HOSTNAME` for a custom domain.
The MCP transport still validates Host and Origin headers.

The ChatGPT connection will use `https://<actual-render-host>/mcp`. Follow the
[deployment guide](docs/deploy.md) and [official connection instructions](https://developers.openai.com/plugins/deploy/connect-chatgpt).

P6 local validation: 40 tests passed with dedicated Postgres; Ruff and strict mypy
passed. The Docker image applied migrations, served health/web on `PORT=10000`,
and passed a CLI add/list/claim/conflict/done round-trip. MCP initialization, seven-tool
listing, and create/list/complete passed using a Render Host header. These checks do
not establish a production deployment or a real ChatGPT connection.
