# Render deployment and ChatGPT connection

Status: CLI and deployment preparation verified locally. Production deployment
and a real ChatGPT connection are deferred. Keep issue #6 open until the checklist
at the end is complete.

## Deploy on Render

1. Sign in to your existing Render account and select the intended workspace.
2. Create a Blueprint from `prakash601/meldX` on `main`, using `render.yaml`, or
   configure a Docker web service with `Dockerfile` and health path `/health`.
3. Configure `DATABASE_URL` directly in Render for the intended production
   Postgres database, for example `postgresql+asyncpg://...?...ssl=require`.
   Do not paste credentials into an issue, PR, or chat. The `.env.example` file is
   a reference; it is not automatically loaded by uvicorn.
4. Keep the Docker Command unset so the Dockerfile CMD runs `alembic upgrade head`
   before starting uvicorn. It binds to `0.0.0.0:$PORT` with one worker. Render's
   Docker runtime uses `dockerCommand` for overrides, rather than `startCommand`.
   The configured free plan does not require a paid pre-deploy migration command.
5. Deploy. Check that migrations succeed, uvicorn starts, and `/health` returns
   `{"ok":true}`. Successful health queries emit a JSON `db.select_1` event with
   status `ok`. Record the actual Render URL and deployed commit.
6. Open `/` and verify task groups, countdowns, and live updates. The SSE bus is
   process-local, so use one worker/instance.

Render automatically supplies `RENDER_EXTERNAL_HOSTNAME`; MCP accepts that exact
hostname and its HTTPS origin. For a custom domain set `MCP_HOSTNAME` to the
hostname, without a scheme, path, or port. Other MCP hosts/origins are rejected.
No authentication is implemented in v1: the deployed endpoint allows anyone who
can reach it to list and mutate tasks. Keep that deployment boundary in mind when
choosing the database and task contents.

References: [Docker on Render](https://render.com/docs/docker),
[Blueprint fields](https://render.com/docs/blueprint-spec),
[PORT binding](https://render.com/docs/web-services#port-binding).

## Verify CLI against production

Set API to the actual HTTPS URL obtained from Render, then use a disposable task:

```sh
export API='https://<actual-render-host>'
uv run meldx list --today
uv run meldx add 'Production CLI smoke' --due today --priority high
# Copy the UUID from the returned JSON.
uv run meldx claim <task-uuid> --agent codex-1
uv run meldx list --status doing --json
uv run meldx done <task-uuid>
```

Confirm errors exit nonzero and a second agent cannot claim an active lease.

## Connect in ChatGPT

The [official connection guide](https://developers.openai.com/plugins/deploy/connect-chatgpt)
requires an accessible MCP endpoint and describes the current developer-mode flow.
Availability depends on the account/workspace.

1. Open ChatGPT Settings → Security and login → Developer mode.
2. Open ChatGPT Plugins, select the plus button, enter the meldX name/description,
   and enter the actual public HTTPS URL ending in `/mcp` under Connection.
3. Create the connection and confirm all seven tools appear. v1 has no MCP auth.
4. In a new conversation, enable the connection and ask it to call `todo_list`.
5. Ask it to create a disposable task through `todo_create`, then complete that
   exact returned task UUID through `todo_complete`.
6. Confirm the task is done through CLI and that an open Today view updated live.
   Record the tool calls/results and any client confirmations or errors.

A generic MCP client round-trip proves the server transport, not that a ChatGPT
account can discover, connect, select tools, and complete this workflow.

## Issue #6 closure checklist

- [ ] Render URL and deployed commit recorded; `/health` 200 with DB query confirmed.
- [ ] CLI production list/add/claim/done round-trip passes.
- [ ] Real ChatGPT `todo_list` → `todo_create` → `todo_complete` round-trip passes.
- [ ] PROJECT_SPEC v1 acceptance verified, including live web countdown/SSE.
- [ ] Phase checklist updated with actual production evidence; issue #6 closed.
