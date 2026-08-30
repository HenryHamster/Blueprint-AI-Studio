"""Focused exact version-cut and transport ambiguity coverage for the operator CLI."""

import http.client
import io
import json
import urllib.error
import uuid

import blueprint
import blueprint_api
import pytest


PROJECT_ID = "37d445e1-0775-4403-bacf-1cd7ed129336"
VERSION_ID = "11111111-1111-4111-8111-111111111111"
KEY = "22222222-2222-4222-8222-222222222222"
SHA = "a" * 40
IDENTITY = "b" * 64


def _ready(*, version_id=VERSION_ID, version_number=7, sha=SHA, identity=IDENTITY):
    return {
        "status": "ready",
        "replayed": False,
        "version": {"id": version_id, "version_number": version_number},
        "integration": {
            "owner": "HenryHamster",
            "repo": "BlueprintAIStudio",
            "branch": "automain",
            "head_sha": sha,
            "identity_token": identity,
        },
    }


def _request(**overrides):
    value = {
        "reason": None,
        "idempotency_key": KEY,
        "expected_version_id": VERSION_ID,
        "expected_version_number": 7,
        "expected_integration_sha": SHA,
        "expected_integration_identity": IDENTITY,
    }
    value.update(overrides)
    return value


def _committed(request, *, followup_status="complete", **receipt_overrides):
    receipt = {
        "schema_version": 1,
        "status": "committed",
        "project_id": PROJECT_ID,
        "idempotency_key": request["idempotency_key"],
        "reason": request["reason"],
        "expected_version_id": request["expected_version_id"],
        "expected_version_number": request["expected_version_number"],
        "expected_integration_sha": request["expected_integration_sha"],
        "expected_integration_identity": request["expected_integration_identity"],
    }
    receipt.update(receipt_overrides)
    return {
        "status": "committed",
        "replayed": True,
        "receipt": receipt,
        "followups": {"status": followup_status},
    }


def _wire_cli(monkeypatch, fake_api):
    monkeypatch.setattr(blueprint, "api", fake_api)
    monkeypatch.setattr(blueprint_api, "api", fake_api)
    monkeypatch.setattr(
        blueprint,
        "load_config",
        lambda: {"api_url": "https://example.invalid", "api_key": "test"},
    )
    monkeypatch.setattr(
        blueprint_api,
        "load_config",
        lambda: {"api_url": "https://example.invalid", "api_key": "test"},
    )
    monkeypatch.setattr(blueprint, "get_project_id", lambda value=None: value or PROJECT_ID)
    monkeypatch.setattr(blueprint, "validate_project_id", lambda _value: None)
    monkeypatch.setattr(blueprint.uuid, "uuid4", lambda: uuid.UUID(KEY))


def _fresh_argv():
    return ["--json", "promote-recover", "cut", "--project-id", PROJECT_ID, "--yes"]


def _retry_argv(request=None):
    request = request or _request()
    argv = [
        "--json",
        "promote-recover",
        "cut",
        "--project-id",
        PROJECT_ID,
        "--idempotency-key",
        request["idempotency_key"],
        "--expected-version-id",
        request["expected_version_id"],
        "--expected-version-number",
        str(request["expected_version_number"]),
        "--expected-integration-sha",
        request["expected_integration_sha"],
        "--expected-integration-identity",
        request["expected_integration_identity"],
        "--yes",
    ]
    if request["reason"] is not None:
        argv.extend(["--reason", request["reason"]])
    return argv


def _lifecycle():
    return {"current_version": {"version_number": 7, "in_flight_task_count": 0}, "staged_directions": []}


def test_fresh_cut_freezes_one_raw_post_then_reads_committed_receipt(monkeypatch, capsys):
    calls = []
    sent = {}

    def fake_api(method, path, body=None, timeout=30, *, raw_body=None):
        calls.append((method, path, body, raw_body))
        if method == "GET" and "version-lifecycle" in path:
            return _lifecycle()
        if method == "GET" and "version-cut-context" in path and "?" not in path:
            return _ready()
        if method == "POST":
            sent["request"] = json.loads(raw_body)
            return {"status": "committed"}
        if method == "GET" and "version-cut-context?" in path:
            return _committed(sent["request"])
        raise AssertionError((method, path))

    _wire_cli(monkeypatch, fake_api)
    assert blueprint.main(_fresh_argv()) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["result"]["status"] == "committed"
    posts = [call for call in calls if call[0] == "POST"]
    assert len(posts) == 1
    assert posts[0][2] is None
    assert posts[0][3] == json.dumps(_request(), sort_keys=True, separators=(",", ":")).encode()
    assert [call[0] for call in calls] == ["GET", "GET", "POST", "GET"]


def test_lost_post_response_reconciles_committed_without_replay(monkeypatch):
    posts = []

    def fake_api(method, path, body=None, timeout=30, *, raw_body=None):
        if method == "GET" and "version-lifecycle" in path:
            return _lifecycle()
        if method == "GET" and "version-cut-context" in path and "?" not in path:
            return _ready()
        if method == "POST":
            posts.append(raw_body)
            raise blueprint.APIError("connection lost", request_may_have_been_sent=True)
        if method == "GET" and "version-cut-context?" in path:
            return _committed(json.loads(posts[0]))
        raise AssertionError((method, path))

    _wire_cli(monkeypatch, fake_api)
    assert blueprint.main(_fresh_argv()) == 0
    assert len(posts) == 1


def test_unchanged_ready_retry_reuses_identical_frozen_bytes(monkeypatch, capsys):
    posts = []

    def fake_api(method, path, body=None, timeout=30, *, raw_body=None):
        if method == "GET" and "version-lifecycle" in path:
            return _lifecycle()
        if method == "GET" and "version-cut-context" in path and "?" not in path:
            return _ready()
        if method == "POST":
            posts.append(raw_body)
            if len(posts) == 1:
                raise blueprint.APIError("transport failed", request_may_have_been_sent=True)
            return {"status": "committed"}
        if method == "GET" and "version-cut-context?" in path:
            return _committed(json.loads(posts[-1])) if len(posts) == 2 else _ready()
        raise AssertionError((method, path))

    _wire_cli(monkeypatch, fake_api)
    assert blueprint.main(_fresh_argv()) == 1
    failure = json.loads(capsys.readouterr().out)
    assert failure["status"] == "not_committed_or_ambiguous"
    assert blueprint.main(["--json", *failure["retry_argv"], "--yes"]) == 0
    capsys.readouterr()
    assert len(posts) == 2
    assert posts[0] == posts[1]


def test_explicit_retry_returns_existing_commit_without_post(monkeypatch):
    request = _request()
    posts = []

    def fake_api(method, path, body=None, timeout=30, *, raw_body=None):
        if method == "GET" and "version-cut-context?" in path:
            return _committed(request)
        if method == "POST":
            posts.append(raw_body)
        else:
            raise AssertionError((method, path))

    _wire_cli(monkeypatch, fake_api)
    assert blueprint.main(_retry_argv(request)) == 0
    assert posts == []


@pytest.mark.parametrize(
    "context",
    [
        _ready(version_number=8),
        _committed(_request(), expected_integration_sha="c" * 40),
        _committed(_request(), followup_status="invalid"),
    ],
)
def test_explicit_retry_refuses_changed_or_corrupt_authority(monkeypatch, context):
    calls = []

    def fake_api(method, path, body=None, timeout=30, *, raw_body=None):
        calls.append(method)
        if method == "GET":
            return context
        raise AssertionError("POST must not be sent")

    _wire_cli(monkeypatch, fake_api)
    assert blueprint.main(_retry_argv()) == 1
    assert calls == ["GET"]


@pytest.mark.parametrize(
    "extra",
    [
        ["--idempotency-key", KEY],
        [
            "--idempotency-key", KEY,
            "--expected-version-id", "{11111111-1111-4111-8111-111111111111}",
            "--expected-version-number", "7",
            "--expected-integration-sha", SHA,
            "--expected-integration-identity", IDENTITY,
        ],
    ],
)
def test_partial_or_noncanonical_retry_authority_fails_before_api(monkeypatch, extra):
    calls = []

    def fake_api(*args, **kwargs):
        calls.append((args, kwargs))
        raise AssertionError("API must not be called")

    _wire_cli(monkeypatch, fake_api)
    argv = ["--json", "promote-recover", "cut", "--project-id", PROJECT_ID, *extra, "--yes"]
    assert blueprint.main(argv) == 1
    assert calls == []


def test_failed_keyed_readback_does_not_repeat_post(monkeypatch):
    posts = []

    def fake_api(method, path, body=None, timeout=30, *, raw_body=None):
        if method == "GET" and "version-lifecycle" in path:
            return _lifecycle()
        if method == "GET" and "version-cut-context" in path and "?" not in path:
            return _ready()
        if method == "POST":
            posts.append(raw_body)
            raise blueprint.APIError("lost response", request_may_have_been_sent=True)
        if method == "GET" and "version-cut-context?" in path:
            raise blueprint.APIError("readback unavailable")
        raise AssertionError((method, path))

    _wire_cli(monkeypatch, fake_api)
    assert blueprint.main(_fresh_argv()) == 1
    assert len(posts) == 1


class _Response:
    def __init__(self, data):
        self.data = data

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self):
        return self.data


def _wire_transport(monkeypatch, result):
    captured = []
    monkeypatch.setattr(
        blueprint_api,
        "load_config",
        lambda: {"api_url": "https://example.invalid", "api_key": "test"},
    )

    def urlopen(request, timeout=30):
        captured.append(request)
        if isinstance(result, Exception):
            raise result
        return _Response(result)

    monkeypatch.setattr(blueprint_api.urllib.request, "urlopen", urlopen)
    return captured


def test_api_raw_body_and_empty_object_are_sent_exactly(monkeypatch):
    raw = b'{"z":1,"a":2}'
    captured = _wire_transport(monkeypatch, b'{"ok":true}')
    assert blueprint_api.api("POST", "/cut", raw_body=raw) == {"ok": True}
    assert captured[0].data == raw

    assert blueprint_api.api("POST", "/cut", body={}) == {"ok": True}
    assert captured[1].data == b"{}"
    with pytest.raises(blueprint_api.APIError, match="mutually exclusive"):
        blueprint_api.api("POST", "/cut", body={}, raw_body=raw)


@pytest.mark.parametrize(
    ("body", "code", "message"),
    [
        ({"detail": {"code": "stale", "message": "authority changed"}}, "stale", "authority changed"),
        ({"error": {"message": {"code": "wrapped", "message": "wrapped error"}}}, "wrapped", "wrapped error"),
        ({"error": {"code": "direct", "message": "direct error"}}, "direct", "direct error"),
    ],
)
def test_api_structured_errors_preserve_code_and_payload(monkeypatch, body, code, message):
    error = urllib.error.HTTPError(
        "https://example.invalid/cut",
        409,
        "Conflict",
        {},
        io.BytesIO(json.dumps(body).encode()),
    )
    _wire_transport(monkeypatch, error)
    with pytest.raises(blueprint_api.APIError) as exc_info:
        blueprint_api.api("POST", "/cut", raw_body=b"{}")
    exc = exc_info.value
    assert exc.status_code == 409
    assert exc.error_code == code
    assert message in str(exc)
    assert exc.payload == body
    assert exc.response_received is True
    assert exc.request_may_have_been_sent is True


@pytest.mark.parametrize(
    "error",
    [urllib.error.URLError("offline"), http.client.IncompleteRead(b"", 1)],
)
def test_api_post_transport_failures_are_marked_ambiguous(monkeypatch, error):
    _wire_transport(monkeypatch, error)
    with pytest.raises(blueprint_api.APIError) as exc_info:
        blueprint_api.api("POST", "/cut", raw_body=b"{}")
    assert exc_info.value.response_received is False
    assert exc_info.value.request_may_have_been_sent is True


def test_api_malformed_success_is_a_received_ambiguous_mutation(monkeypatch):
    _wire_transport(monkeypatch, b"not-json")
    with pytest.raises(blueprint_api.APIError) as exc_info:
        blueprint_api.api("POST", "/cut", raw_body=b"{}")
    assert exc_info.value.response_received is True
    assert exc_info.value.request_may_have_been_sent is True


class _IncompleteErrorBody:
    def read(self, _limit):
        raise http.client.IncompleteRead(b'{"detail":{"code":"partial"', 8)

    def close(self):
        pass


def test_api_incomplete_http_error_body_is_wrapped(monkeypatch):
    error = urllib.error.HTTPError(
        "https://example.invalid/cut",
        503,
        "Unavailable",
        {},
        _IncompleteErrorBody(),
    )
    _wire_transport(monkeypatch, error)
    with pytest.raises(blueprint_api.APIError, match="incomplete error response") as exc_info:
        blueprint_api.api("POST", "/cut", raw_body=b"{}")
    assert exc_info.value.status_code == 503
    assert exc_info.value.response_received is True
    assert exc_info.value.request_may_have_been_sent is True
