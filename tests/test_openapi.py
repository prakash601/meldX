"""The generated OpenAPI document is a published contract: keep it documented."""

import pytest

from meldx.app.main import create_app

SPEC = create_app().openapi()


def test_info_has_description_and_version() -> None:
    info = SPEC["info"]
    assert info["title"] == "meldX"
    assert info["version"] == "0.1.0"
    assert "FOR UPDATE SKIP LOCKED" in info["description"]


def test_all_documented_tags_are_declared() -> None:
    declared = {tag["name"] for tag in SPEC["tags"]}
    assert declared == {"tasks", "agents", "events", "health", "web"}

    used = {
        tag
        for path in SPEC["paths"].values()
        for operation in path.values()
        for tag in operation.get("tags", [])
    }
    assert used <= declared, f"undeclared tags: {used - declared}"


@pytest.mark.parametrize(
    "path,method",
    [
        ("/api/tasks", "post"),
        ("/api/tasks/{task_id}/claim", "post"),
        ("/api/tasks/{task_id}/complete", "post"),
        ("/api/tasks/{task_id}", "patch"),
        ("/api/tasks/{task_id}/handoff", "post"),
        ("/api/tasks/{task_id}/snooze", "post"),
    ],
)
def test_task_routes_document_the_error_contract(path: str, method: str) -> None:
    operation = SPEC["paths"][path][method]
    assert operation["summary"]
    assert operation["description"]
    assert {"400", "404", "409"} <= set(operation["responses"])
    blob = " ".join(str(operation["responses"][code]["description"]) for code in ("404", "409"))
    assert "Task locked or not found" in blob
    assert "Task leased to {agent} until {ts}" in blob


def test_every_operation_is_documented() -> None:
    for path, operations in SPEC["paths"].items():
        for method, operation in operations.items():
            assert operation.get("summary"), f"{method.upper()} {path} has no summary"
            assert operation.get("tags"), f"{method.upper()} {path} has no tag"


def test_health_documents_unreachable_database() -> None:
    health = SPEC["paths"]["/health"]["get"]
    assert health["tags"] == ["health"]
    assert "503" in health["responses"]
