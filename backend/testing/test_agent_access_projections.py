"""Adversarial bounds and redaction tests for coding-agent read projections."""

import json

from app.routers.agent_access import _project_agent_snapshot


def test_coding_agent_snapshot_caps_escaped_controls_lists_and_secrets():
    control_text = "\x01" * 8_000
    secret = "snapshot-secret-must-not-escape"
    project = {
        "id": "00000000-0000-0000-0000-000000000401",
        "name": control_text,
        "github_owner": "owner",
        "github_repo": "repo",
    }
    attention = {
        "summary": "Authorization: Bearer " + secret,
        "items": [
            {"id": index, "content": control_text, "client_secret": secret}
            for index in range(90)
        ],
    }
    diagnostics = {
        "direction_remediation": [
            {
                "direction_id": str(index),
                "direction_text": control_text,
                "nested_bearer": secret,
            }
            for index in range(90)
        ]
    }
    resources = {
        "runners": [
            {"id": index, "status": control_text, "jwt": secret}
            for index in range(90)
        ]
    }

    projected = _project_agent_snapshot(
        project,
        attention=attention,
        diagnostics=diagnostics,
        resources=resources,
        message_counts={"open": 3},
    )
    serialized = json.dumps(projected, ensure_ascii=False)

    assert projected["schema_version"] == "blueprint.project-snapshot.v1"
    assert len(serialized.encode("utf-8")) <= projected["limits"]["response_bytes"]
    assert projected["complete"] is False
    assert projected["project"]["id"] == project["id"]
    assert projected["truncation"]["attention"] is True
    assert projected["truncation"]["diagnostics"] is True
    assert projected["truncation"]["resources"] is True
    assert projected["limits"]["nested_collection_entries"] == 50
    assert set(projected["limits"]["section_value_budget_bytes"]) == {
        "project",
        "attention",
        "diagnostics",
        "resources",
        "message_counts",
        "server",
    }
    assert projected["redaction"]["applied"] is True
    assert secret not in serialized
    for sensitive_key in ("client_secret", "nested_bearer", "jwt"):
        assert sensitive_key not in serialized
