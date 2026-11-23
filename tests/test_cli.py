"""CLI request contracts and actionable failures, without contacting a real server."""

import json
from collections.abc import Callable
from functools import partial

import httpx
import pytest
from typer.testing import CliRunner

from meldx.app.cli import main as cli

runner = CliRunner()
TID = "00000000-0000-0000-0000-000000000001"


@pytest.fixture
def mock_api(monkeypatch):
    original = httpx.AsyncClient
    monkeypatch.setenv("API", "https://meldx.example.com/")

    def install(handler: Callable[[httpx.Request], httpx.Response]) -> None:
        monkeypatch.setattr(
            cli.httpx,
            "AsyncClient",
            partial(
                original,
                transport=httpx.MockTransport(handler),
            ),
        )

    return install


def test_add_maps_flags_to_rest(mock_api) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == "https://meldx.example.com/api/tasks"
        assert request.method == "POST"
        assert json.loads(request.content) == {
            "title": "write report",
            "description": "Q3",
            "due_at": "tomorrow",
            "priority": "high",
            "delegate_to": "hermes-1",
        }
        return httpx.Response(201, json={"id": TID, "title": "write report", "status": "today"})

    mock_api(handler)
    result = runner.invoke(
        cli.app,
        [
            "add",
            "write report",
            "--description",
            "Q3",
            "--due",
            "tomorrow",
            "--priority",
            "high",
            "--delegate",
            "hermes-1",
        ],
    )
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["id"] == TID


def test_list_filters_and_json(mock_api) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        assert dict(request.url.params) == {"today_only": "true", "status": "doing"}
        return httpx.Response(
            200, json=[{"id": TID, "title": "literal [red]task", "status": "doing"}]
        )

    mock_api(handler)
    result = runner.invoke(cli.app, ["list", "--today", "--status", "doing", "--json"])
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)[0]["title"] == "literal [red]task"


def test_list_view_param(mock_api) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert dict(request.url.params) == {"today_only": "false", "view": "overdue"}
        return httpx.Response(200, json=[])

    mock_api(handler)
    result = runner.invoke(cli.app, ["list", "--view", "overdue"])
    assert result.exit_code == 0, result.output


def test_today_prints_summary_sections(mock_api) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/tasks/summary"
        return httpx.Response(
            200,
            json={
                "date": "2026-11-24",
                "today": [{"id": TID, "title": "write plan"}],
                "overdue": [],
                "missed_plans": [],
                "completed_today": [{"id": TID, "title": "shipped v0.0.1"}],
            },
        )

    mock_api(handler)
    result = runner.invoke(cli.app, ["today"])
    assert result.exit_code == 0, result.output
    assert "2026-11-24" in result.stdout
    assert "Remaining today (1)" in result.stdout and "write plan" in result.stdout
    assert "Completed today (1)" in result.stdout and "shipped v0.0.1" in result.stdout
    assert "Overdue (0)" in result.stdout


def test_completed_uses_view_and_window(mock_api) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert dict(request.url.params) == {"view": "completed", "completed_days": "7"}
        return httpx.Response(
            200, json=[{"id": TID, "title": "older work", "completed_at": "2026-11-20T10:00:00Z"}]
        )

    mock_api(handler)
    result = runner.invoke(cli.app, ["completed", "--days", "7"])
    assert result.exit_code == 0, result.output
    assert "older work" in result.stdout


@pytest.mark.parametrize(
    "command,payload,path",
    [
        (["done", TID], None, f"/api/tasks/{TID}/complete"),
        (["start", TID], None, f"/api/tasks/{TID}/start"),
        (["reopen", TID], None, f"/api/tasks/{TID}/reopen"),
        (["claim", TID, "--agent", "codex-1"], {"agent_id": "codex-1"}, f"/api/tasks/{TID}/claim"),
        (["plan", TID, "tomorrow"], {"date": "tomorrow"}, f"/api/tasks/{TID}/plan"),
        (["unplan", TID], {"date": None}, f"/api/tasks/{TID}/plan"),
        (["snooze", TID, "tomorrow 9am"], {"until": "tomorrow 9am"}, f"/api/tasks/{TID}/snooze"),
        (["unsnooze", TID], None, f"/api/tasks/{TID}/unsnooze"),
    ],
)
def test_mutation_commands(mock_api, command, payload, path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert request.url.path == path
        if payload is not None:
            assert json.loads(request.content) == payload
        return httpx.Response(200, json={"id": TID})

    mock_api(handler)
    result = runner.invoke(cli.app, command)
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["id"] == TID


@pytest.mark.parametrize(
    "status,detail",
    [
        (404, "Task locked or not found"),
        (409, "Task leased to codex-1 until 2026-10-03T13:00:00+00:00"),
        (422, [{"msg": "Invalid title"}]),
    ],
)
def test_api_errors_exit_nonzero(mock_api, status, detail) -> None:
    mock_api(lambda request: httpx.Response(status, json={"detail": detail}))
    result = runner.invoke(cli.app, ["done", TID])
    assert result.exit_code == 1
    assert f"HTTP {status}" in result.stderr
    assert str(detail) in result.stderr.replace("\n", " ")
    assert result.stdout == ""


def test_show_fetches_detail(mock_api) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        assert request.url.path == f"/api/tasks/{TID}"
        return httpx.Response(200, json={"id": TID, "title": "shown", "revision": 2})

    mock_api(handler)
    result = runner.invoke(cli.app, ["show", TID])
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["revision"] == 2


def test_edit_sends_only_provided_options_and_clears_description(mock_api) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "PATCH"
        assert request.url.path == f"/api/tasks/{TID}"
        assert json.loads(request.content) == {"title": "renamed", "description": None}
        return httpx.Response(200, json={"id": TID})

    mock_api(handler)
    result = runner.invoke(cli.app, ["edit", TID, "--title", "renamed", "--description", ""])
    assert result.exit_code == 0, result.output


def test_edit_conflict_exits_nonzero(mock_api) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            409,
            json={
                "detail": "Stale revision: task is at revision 5",
                "current_task": {"id": TID, "revision": 5},
            },
        )

    mock_api(handler)
    result = runner.invoke(cli.app, ["edit", TID, "--title", "renamed"])
    assert result.exit_code == 1
    assert "Stale revision" in result.stderr


def test_unreachable_and_invalid_responses(mock_api) -> None:
    def unavailable(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("unreachable", request=request)

    mock_api(unavailable)
    result = runner.invoke(cli.app, ["list"])
    assert result.exit_code == 1 and "Could not reach meldX" in result.stderr
    mock_api(lambda request: httpx.Response(200, text="not JSON"))
    result = runner.invoke(cli.app, ["list"])
    assert result.exit_code == 1 and "invalid JSON" in result.stderr
    mock_api(lambda request: httpx.Response(503, text="unavailable"))
    result = runner.invoke(cli.app, ["list"])
    assert result.exit_code == 1 and "HTTP 503" in result.stderr


def test_invalid_cli_input_never_calls_api(mock_api, monkeypatch) -> None:
    def unexpected(request: httpx.Request) -> httpx.Response:
        raise AssertionError("invalid input must not send a request")

    mock_api(unexpected)
    for command in [
        ["done", "bad-id"],
        ["claim", TID],
        ["list", "--status", "bad"],
        ["add", "task", "--priority", "bad"],
    ]:
        assert runner.invoke(cli.app, command).exit_code == 2
    monkeypatch.setenv("API", "https://user:secret@meldx.example.com")
    result = runner.invoke(cli.app, ["list"])
    assert result.exit_code == 1 and "Invalid API URL" in result.stderr
    assert "secret" not in result.output


def test_default_api_and_table(mock_api, monkeypatch) -> None:
    monkeypatch.delenv("API", raising=False)

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.host == "localhost" and request.url.port == 8000
        return httpx.Response(200, json=[{"id": TID, "title": "Review work", "status": "inbox"}])

    mock_api(handler)
    result = runner.invoke(cli.app, ["list"])
    assert result.exit_code == 0, result.output
    assert "meldX tasks" in result.stdout and "Review work" in result.stdout
