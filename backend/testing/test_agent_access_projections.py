"""Adversarial bounds and redaction tests for coding-agent read projections."""

import json

from app.routers.agent_access import (
    _project_agent_snapshot,
    _project_agent_task_detail,
)


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


def test_coding_agent_projection_redacts_camel_case_secret_fields():
    secrets = {
        "accessToken": "access-token-secret",
        "accessTokens": "access-tokens-secret",
        "refreshTokens": "refresh-tokens-secret",
        "clientSecret": "client-secret-value",
        "clientSecrets": "client-secrets-value",
        "privateKey": "private-key-value",
        "privateKeys": "private-keys-value",
        "accessKeyId": "access-key-id-value",
        "authorizationHeader": "authorization-header-value",
        "authHeader": "auth-header-value",
        "APIKey": "api-key-value",
        "apiKeys": "api-keys-value",
        "accessTokenValue": "access-token-wrapper-secret",
        "refreshTokenValue": "refresh-token-wrapper-secret",
        "oauthTokenValue": "oauth-token-wrapper-secret",
        "tokenValue": "generic-token-wrapper-secret",
        "sessionTokenValue": "session-token-wrapper-secret",
        "accessTokenList": "access-token-list-secret",
        "refreshTokenList": "refresh-token-list-secret",
    }
    accounting = {
        "inputTokens": 10,
        "outputTokens": 20,
        "tokenCount": 30,
        "tokenBudget": 40,
        "maxTokens": 50,
    }
    projected = _project_agent_task_detail(
        {
            "id": "00000000-0000-0000-0000-000000000501",
            "project_id": "00000000-0000-0000-0000-000000000502",
            "status": "FAILED",
            "task_type": "fix",
            "plan_output": {"nested": {**secrets, **accounting}},
        }
    )
    serialized = json.dumps(projected, ensure_ascii=False)

    assert projected["redaction"]["applied"] is True
    for key, secret in secrets.items():
        assert key not in serialized
        assert secret not in serialized
    for key, value in accounting.items():
        assert projected["plan_output"]["nested"][key] == value
