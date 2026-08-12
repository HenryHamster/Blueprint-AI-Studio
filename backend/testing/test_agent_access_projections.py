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


def test_coding_agent_projection_redacts_secret_containers_and_preserves_domain_keys():
    secrets = {
        "passphrase": "passphrase-secret",
        "passwd": "passwd-secret",
        "pwd": "pwd-secret",
        "creds": "creds-secret",
        "awsCreds": "aws-creds-secret",
        "envVars": "env-vars-secret",
        "environmentVariables": "environment-vars-secret",
        "databaseUrl": "mysql://alice:database-secret@example.test/db",
        "connectionString": "connection-string-secret",
        "redisUrl": "redis://default:redis-secret@example.test/0",
        "mongodbUri": "mongodb://alice:mongodb-secret@example.test/db",
        "signedUrls": "https://example.test/signed-path-secret",
        "uploadUrls": "https://example.test/upload-path-secret",
        "downloadUrls": "https://example.test/download-path-secret",
        "presignedUrls": "https://example.test/presigned-path-secret",
        "authConfig": "auth-config-secret",
        "authData": "auth-data-secret",
        "authMap": "auth-map-secret",
        "authList": "auth-list-secret",
        "authParams": "auth-params-secret",
        "authentication": "authentication-secret",
        "authenticationConfig": "authentication-config-secret",
        "httpHeaders": "http-headers-secret",
        "requestHeaders": "request-headers-secret",
        "defaultHeaders": "default-headers-secret",
        "headerMap": "header-map-secret",
        "webhookUrl": "https://example.test/webhook-path-secret",
        "databaseDsn": "database-dsn-secret",
        "connectionUri": "mysql://alice:connection-secret@example.test/db",
        "proxyUrl": "http://alice:proxy-secret@example.test",
        "repositoryUrl": "https://alice:repository-secret@example.test/repo.git",
        "keyMaterial": "key-material-secret",
        "accountKey": "account-key-secret",
        "storageKey": "storage-key-secret",
        "storageAccountKey": "storage-account-key-secret",
        "subscriptionKey": "subscription-key-secret",
        "masterKey": "master-key-secret",
        "sharedKey": "shared-key-secret",
        "oauthConfig": "oauth-config-secret",
        "oauthSettings": "oauth-settings-secret",
        "oidcConfig": "oidc-config-secret",
    }
    domain_fields = {
        "criterionKey": "criterion-1",
        "directionKey": "direction-1",
        "categoryKey": "category-1",
        "findingKey": "finding-1",
        "configKey": "config-1",
    }
    projected = _project_agent_task_detail(
        {
            "id": "00000000-0000-0000-0000-000000000511",
            "project_id": "00000000-0000-0000-0000-000000000512",
            "status": "FAILED",
            "task_type": "fix",
            "plan_output": {
                "nested": {
                    **secrets,
                    **domain_fields,
                    "endpoint": "mysql://alice:generic-secret@example.test/db",
                    "cacheEndpoint": "redis://:empty-user-secret@example.test/0",
                    "sourceEndpoint": "https://token-only-secret@example.test/repo",
                }
            },
        }
    )
    serialized = json.dumps(projected, ensure_ascii=False)

    assert projected["redaction"]["applied"] is True
    for key, secret in secrets.items():
        assert key not in serialized
        assert secret not in serialized
    for key, value in domain_fields.items():
        assert projected["plan_output"]["nested"][key] == value
    assert projected["plan_output"]["nested"]["endpoint"] == (
        "mysql://***@example.test/db"
    )
    assert projected["plan_output"]["nested"]["cacheEndpoint"] == (
        "redis://***@example.test/0"
    )
    assert projected["plan_output"]["nested"]["sourceEndpoint"] == (
        "https://***@example.test/repo"
    )
    assert "generic-secret" not in serialized
    assert "empty-user-secret" not in serialized
    assert "token-only-secret" not in serialized


def test_coding_agent_projection_redacts_env_wrappers_and_preserves_token_metrics():
    secrets = {
        "envValue": "env-value-secret",
        "envValues": "env-values-secret",
        "envList": "env-list-secret",
        "environmentList": "environment-list-secret",
        "envOptions": "env-options-secret",
        "envSettings": "env-settings-secret",
        "envFile": "env-file-secret",
        "envFiles": "env-files-secret",
        "dotenv": "dotenv-secret",
    }
    metrics = {
        "cacheCreationInputTokens": 10,
        "cacheReadInputTokens": 11,
        "totalCacheReadInputTokens": 12,
        "llmInputTokens": 13,
        "providerReportedInputTokens": 14,
        "agentAccumulatedOutputTokens": 15,
        "tokensPerSecond": 16,
        "tokensPerMinute": 17,
        "tokenRate": 18,
        "tokenThroughput": 19,
        "billableTokens": 20,
        "generatedTokens": 21,
        "consumedTokens": 22,
        "acceptedPredictionTokens": 23,
        "rejectedPredictionTokens": 24,
        "audioTokens": 25,
    }
    projected = _project_agent_task_detail(
        {
            "id": "00000000-0000-0000-0000-000000000521",
            "project_id": "00000000-0000-0000-0000-000000000522",
            "status": "FAILED",
            "task_type": "fix",
            "plan_output": {"nested": {**secrets, **metrics}},
        }
    )
    serialized = json.dumps(projected, ensure_ascii=False)

    assert projected["redaction"]["applied"] is True
    for key, secret in secrets.items():
        assert key not in serialized
        assert secret not in serialized
    for key, value in metrics.items():
        assert projected["plan_output"]["nested"][key] == value


def test_coding_agent_projection_redacts_secrets_embedded_in_diagnostics():
    diagnostic_secrets = (
        "opaque-access-token",
        "opaque-client-secret",
        "opaque-api-key",
        "QWxhZGRpbjpvcGVuIHNlc2FtZQ==",
        "opaque-header-key",
    )
    projected = _project_agent_task_detail(
        {
            "id": "00000000-0000-0000-0000-000000000531",
            "project_id": "00000000-0000-0000-0000-000000000532",
            "status": "FAILED",
            "task_type": "fix",
            "error_logs": "\n".join(
                (
                    '{"accessToken":"opaque-access-token"}',
                    '{"clientSecret":"opaque-client-secret"}',
                    '{"api-key":"opaque-api-key"}',
                    "Authorization: Basic QWxhZGRpbjpvcGVuIHNlc2FtZQ==",
                    "X-API-Key: opaque-header-key",
                    '{"safeField":"still-visible"}',
                )
            ),
        }
    )
    serialized = json.dumps(projected, ensure_ascii=False)

    assert projected["redaction"]["applied"] is True
    for secret in diagnostic_secrets:
        assert secret not in serialized
    assert "[redacted]" in serialized
    assert "still-visible" in serialized
