"""MCP transport host validation for Render and custom domains."""

import pytest
from starlette.testclient import TestClient

from meldx.app.main import mcp_transport_security
from meldx.app.mcp.tools import mcp


@pytest.mark.parametrize(
    "host,origin,expected",
    [
        ("meldx.onrender.com", None, 200),
        ("meldx.onrender.com", "https://meldx.onrender.com", 200),
        ("todos.example.com", "https://todos.example.com", 200),
        ("localhost:8000", None, 200),
        ("evil.example.com", None, 421),
        ("meldx.onrender.com", "https://evil.example.com", 403),
    ],
)
def test_mcp_public_hosts(monkeypatch, host, origin, expected) -> None:
    monkeypatch.setenv("RENDER_EXTERNAL_HOSTNAME", "meldx.onrender.com")
    monkeypatch.setenv("MCP_HOSTNAME", "todos.example.com")
    app = mcp.streamable_http_app(transport_security=mcp_transport_security())
    with TestClient(app, base_url=f"https://{host}") as client:
        headers = {"Accept": "application/json, text/event-stream"}
        if origin is not None:
            headers["Origin"] = origin
        response = client.post(
            "/mcp",
            headers=headers,
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-06-18",
                    "capabilities": {},
                    "clientInfo": {"name": "deploy-test", "version": "1"},
                },
            },
        )
    assert response.status_code == expected


def test_invalid_public_hostname_rejected(monkeypatch) -> None:
    monkeypatch.setenv("MCP_HOSTNAME", "https://todos.example.com/path")
    with pytest.raises(ValueError, match="hostname without a scheme or port"):
        mcp_transport_security()
