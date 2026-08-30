"""Exact cold-successor launch/readback coverage for the operator CLI."""

import hashlib
import json

import blueprint
import blueprint_api


PROJECT_ID = "37d445e1-0775-4403-bacf-1cd7ed129336"
RUNNER_ID = "11111111-1111-4111-8111-111111111111"
PREDECESSOR_TASK_ID = "22222222-2222-4222-8222-222222222222"
SUCCESSOR_TASK_ID = "33333333-3333-4333-8333-333333333333"
IDEMPOTENCY_KEY = "v57-cold-successor-proof"


def _request():
    return {
        "runner_id": RUNNER_ID,
        "idempotency_key": IDEMPOTENCY_KEY,
        "expected_predecessor_session_id": "ss-v57-predecessor",
        "expected_predecessor_task_id": PREDECESSOR_TASK_ID,
        "expected_predecessor_worker_generation": 7,
    }


def _status(request=None, **overrides):
    request = request or _request()
    row = {
        "launch_mode": "cold_successor",
        "proof_state": "pending",
        "session_id": "ss-v57-successor",
        "task_id": SUCCESSOR_TASK_ID,
        "worker_generation": 1,
        "runner_id": request["runner_id"],
        "idempotency_fingerprint": hashlib.sha256(
            request["idempotency_key"].encode("utf-8")
        ).hexdigest(),
        "launch_effect_sha256": blueprint._cold_successor_effect(
            PROJECT_ID, request
        ),
        "core_sha256": "a" * 64,
        "task_status": "PROVISIONING",
    }
    row.update(overrides)
    return row


def _argv():
    return [
        "--json",
        "strategic",
        "cold-successor-start",
        "--project-id",
        PROJECT_ID,
        "--runner-id",
        RUNNER_ID,
        "--idempotency-key",
        IDEMPOTENCY_KEY,
        "--expected-predecessor-session-id",
        "ss-v57-predecessor",
        "--expected-predecessor-task-id",
        PREDECESSOR_TASK_ID,
        "--expected-predecessor-worker-generation",
        "7",
        "--yes",
    ]


def _wire(monkeypatch, fake_api, *, read_only=False):
    config = {
        "api_url": "https://example.invalid",
        "api_key": "test-admin-key",
        "read_only": read_only,
    }
    monkeypatch.setattr(blueprint, "api", fake_api)
    monkeypatch.setattr(blueprint_api, "api", fake_api)
    monkeypatch.setattr(blueprint, "load_config", lambda: config)
    monkeypatch.setattr(blueprint_api, "load_config", lambda: config)
    monkeypatch.setattr(
        blueprint, "get_project_id", lambda value=None: value or PROJECT_ID
    )
    monkeypatch.setattr(blueprint, "validate_project_id", lambda _value: None)


def test_fresh_launch_posts_one_frozen_body_then_reads_exact_receipt(
    monkeypatch, capsys
):
    calls = []
    sent = {}

    def fake_api(method, path, body=None, timeout=30, *, raw_body=None):
        calls.append((method, path, body, raw_body))
        if method == "GET" and len(calls) == 1:
            return {"launch_mode": "cold_successor", "proof_state": "absent"}
        if method == "POST":
            sent["request"] = json.loads(raw_body)
            return _status(sent["request"])
        if method == "GET":
            return _status(sent["request"])
        raise AssertionError((method, path))

    _wire(monkeypatch, fake_api)
    assert blueprint.main(_argv()) == 0
    output = json.loads(capsys.readouterr().out)
    assert output["task_id"] == SUCCESSOR_TASK_ID
    assert [call[0] for call in calls] == ["GET", "POST", "GET"]
    assert calls[1][2] is None
    assert calls[1][3] == json.dumps(
        _request(), sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def test_lost_post_response_reconciles_without_replay(monkeypatch):
    posts = []

    def fake_api(method, path, body=None, timeout=30, *, raw_body=None):
        if method == "GET" and not posts:
            return {"launch_mode": "cold_successor", "proof_state": "absent"}
        if method == "POST":
            posts.append(raw_body)
            raise blueprint.APIError(
                "connection lost", request_may_have_been_sent=True
            )
        if method == "GET":
            return _status(json.loads(posts[0]))
        raise AssertionError((method, path))

    _wire(monkeypatch, fake_api)
    assert blueprint.main(_argv()) == 0
    assert len(posts) == 1


def test_exact_existing_receipt_is_read_before_any_retry(monkeypatch):
    calls = []

    def fake_api(method, path, body=None, timeout=30, *, raw_body=None):
        calls.append(method)
        if method == "GET":
            return _status()
        raise AssertionError("POST must not be replayed")

    _wire(monkeypatch, fake_api)
    assert blueprint.main(_argv()) == 0
    assert calls == ["GET"]


def test_different_canonical_proof_fails_closed_without_post(monkeypatch):
    calls = []

    def fake_api(method, path, body=None, timeout=30, *, raw_body=None):
        calls.append(method)
        if method == "GET":
            return _status(launch_effect_sha256="b" * 64)
        raise AssertionError("POST must not be sent")

    _wire(monkeypatch, fake_api)
    assert blueprint.main(_argv()) == 1
    assert calls == ["GET"]


def test_invalid_authority_fails_before_api(monkeypatch):
    calls = []

    def fake_api(*args, **kwargs):
        calls.append((args, kwargs))
        raise AssertionError("API must not be called")

    _wire(monkeypatch, fake_api)
    argv = _argv()
    argv[argv.index(RUNNER_ID)] = "not-a-uuid"
    assert blueprint.main(argv) == 1
    assert calls == []


def test_status_is_read_only_and_uses_the_canonical_owner_route(
    monkeypatch, capsys
):
    calls = []

    def fake_api(method, path, body=None, timeout=30, *, raw_body=None):
        calls.append((method, path))
        return _status(proof_state="succeeded", task_status="COMPLETED")

    _wire(monkeypatch, fake_api, read_only=True)
    assert blueprint.main([
        "--json",
        "strategic",
        "cold-successor-status",
        "--project-id",
        PROJECT_ID,
    ]) == 0
    assert json.loads(capsys.readouterr().out)["proof_state"] == "succeeded"
    assert calls == [(
        "GET",
        f"/api/admin/projects/{PROJECT_ID}/strategic-session/cold-successor",
    )]
