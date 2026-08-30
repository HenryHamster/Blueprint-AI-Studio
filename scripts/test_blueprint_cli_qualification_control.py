"""Exact qualification-control CLI authority and ambiguity coverage."""

import json

import blueprint
import blueprint_api
import pytest


PROJECT_ID = "37d445e1-0775-4403-bacf-1cd7ed129336"
KEY = "hold-v57-exact"
RECEIPT_ID = "11111111-1111-4111-8111-111111111111"
REASON = "Bound v57 Resident qualification"


def _request(*, enabled=False, revision=0, key=KEY, reason=REASON):
    return {
        "automatic_starts_enabled": enabled,
        "expected_revision": revision,
        "idempotency_key": key,
        "reason": reason,
    }


def _context(
    *, current_revision=0, current_enabled=True, receipt=None,
    requester_actor="owner-user", requester_can_mutate=True,
):
    current = {
        "schema_version": 1,
        "project_id": PROJECT_ID,
        "id": None,
        "revision": 0,
        "automatic_starts_enabled": True,
        "idempotency_key": None,
        "specification_hash": None,
        "actor_user_id": None,
        "reason": None,
        "created_at": None,
    }
    if current_revision > 0:
        current_request = _request(
            enabled=current_enabled,
            revision=current_revision - 1,
            key=f"current-revision-{current_revision}",
            reason="Later control",
        )
        current = {
            "schema_version": 1,
            "project_id": PROJECT_ID,
            "id": "22222222-2222-4222-8222-222222222222",
            "revision": current_revision,
            "automatic_starts_enabled": current_enabled,
            "idempotency_key": f"current-revision-{current_revision}",
            "specification_hash": (
                blueprint._qualification_control_specification_hash(
                    current_request, PROJECT_ID, "owner-user"
                )
            ),
            "actor_user_id": "owner-user",
            "reason": "Later control",
            "created_at": "2026-08-30T20:01:00",
        }
    if receipt is not None and receipt.get("revision") == current_revision:
        current = dict(receipt)
    return {
        "schema_version": 1,
        "project_id": PROJECT_ID,
        "current": current,
        "receipt": receipt,
        "requested_idempotency_key": KEY,
        "requester_actor_user_id": requester_actor,
        "requester_can_mutate": requester_can_mutate,
    }


def _receipt(request):
    actor = "owner-user"
    return {
        "schema_version": 1,
        "project_id": PROJECT_ID,
        "id": RECEIPT_ID,
        "revision": request["expected_revision"] + 1,
        "automatic_starts_enabled": request["automatic_starts_enabled"],
        "idempotency_key": request["idempotency_key"],
        "specification_hash": blueprint._qualification_control_specification_hash(
            request, PROJECT_ID, actor
        ),
        "actor_user_id": actor,
        "reason": request["reason"],
        "created_at": "2026-08-30T20:00:00",
    }


def _committed(request, *, current_enabled=None, current_revision=None):
    enabled = (
        request["automatic_starts_enabled"]
        if current_enabled is None
        else current_enabled
    )
    revision = (
        request["expected_revision"] + 1
        if current_revision is None
        else current_revision
    )
    return _context(
        current_revision=revision,
        current_enabled=enabled,
        receipt=_receipt(request),
    )


def _wire(monkeypatch, fake_api, *, key="admin-test"):
    blueprint._SCOPED_PROFILE_CACHE = None
    monkeypatch.setattr(blueprint, "api", fake_api)
    monkeypatch.setattr(blueprint_api, "api", fake_api)
    config = {"api_url": "https://example.invalid", "api_key": key}
    monkeypatch.setattr(blueprint, "load_config", lambda: config)
    monkeypatch.setattr(blueprint_api, "load_config", lambda: config)
    monkeypatch.setattr(blueprint, "get_project_id", lambda value=None: value)
    monkeypatch.setattr(blueprint, "validate_project_id", lambda _value: None)


def _argv(action="hold", *, revision=0, key=KEY, reason=REASON):
    return [
        "--json",
        "qualification-control",
        action,
        "--project-id",
        PROJECT_ID,
        "--expected-revision",
        str(revision),
        "--idempotency-key",
        key,
        "--reason",
        reason,
        "--yes",
    ]


def test_hold_freezes_one_raw_post_and_reconciles_exact_receipt(
    monkeypatch, capsys
):
    calls = []
    request = _request()

    def fake_api(method, path, body=None, timeout=30, *, raw_body=None):
        calls.append((method, path, body, raw_body))
        if method == "GET" and len(calls) == 1:
            return _context()
        if method == "POST":
            return {"status": "acknowledged"}
        if method == "GET":
            return _committed(request)
        raise AssertionError((method, path))

    _wire(monkeypatch, fake_api)
    assert blueprint.main(_argv()) == 0
    assert json.loads(capsys.readouterr().out)["result"]["receipt"]["revision"] == 1
    assert [call[0] for call in calls] == ["GET", "POST", "GET"]
    assert calls[1][2] is None
    assert calls[1][3] == json.dumps(
        request, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def test_canonical_hash_matches_the_independent_golden_vector():
    assert blueprint._qualification_control_specification_hash(
        _request(), PROJECT_ID, "owner-user"
    ) == "1d87078494e2123042dc01c82caf7a85fb7088fd1ee055d76897b68c057d6e78"


def test_arm_freezes_the_enabled_raw_request(monkeypatch):
    posts = []
    request = _request(enabled=True)

    def fake_api(method, path, body=None, timeout=30, *, raw_body=None):
        if method == "GET" and not posts:
            return _context()
        if method == "POST":
            posts.append(raw_body)
            return {"status": "acknowledged"}
        if method == "GET":
            return _committed(request)
        raise AssertionError((method, path))

    _wire(monkeypatch, fake_api)
    assert blueprint.main(_argv("arm")) == 0
    assert posts == [
        json.dumps(request, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ]


def test_lost_post_response_reconciles_without_replay(monkeypatch):
    posts = []
    request = _request()

    def fake_api(method, path, body=None, timeout=30, *, raw_body=None):
        if method == "GET" and not posts:
            return _context()
        if method == "POST":
            posts.append(raw_body)
            raise blueprint.APIError("lost response", request_may_have_been_sent=True)
        if method == "GET":
            return _committed(request)
        raise AssertionError((method, path))

    _wire(monkeypatch, fake_api)
    assert blueprint.main(_argv()) == 0
    assert len(posts) == 1


def test_authoritative_conflict_reconciles_once_without_replay(monkeypatch):
    posts = []
    request = _request()

    def fake_api(method, path, body=None, timeout=30, *, raw_body=None):
        if method == "GET" and not posts:
            return _context()
        if method == "POST":
            posts.append(raw_body)
            raise blueprint.APIError("HTTP 409: conflict")
        if method == "GET":
            return _committed(request)
        raise AssertionError((method, path))

    _wire(monkeypatch, fake_api)
    assert blueprint.main(_argv()) == 0
    assert len(posts) == 1


def test_existing_receipt_returns_without_post_and_superseded_objective_fails(
    monkeypatch
):
    request = _request()
    posts = []
    context = _committed(request)

    def fake_api(method, path, body=None, timeout=30, *, raw_body=None):
        if method == "GET":
            return context
        posts.append(raw_body)
        raise AssertionError("existing receipt must not be posted again")

    _wire(monkeypatch, fake_api)
    assert blueprint.main(_argv()) == 0
    assert posts == []

    context = _committed(request, current_enabled=True, current_revision=2)
    assert blueprint.main(_argv()) == 1
    assert posts == []


def test_stale_revision_or_conflicting_key_never_posts(monkeypatch):
    request = _request()
    contexts = [
        _context(current_revision=1, current_enabled=False),
        _committed({**request, "reason": "Different frozen request"}),
    ]
    posts = []

    def fake_api(method, path, body=None, timeout=30, *, raw_body=None):
        if method == "GET":
            return contexts.pop(0)
        posts.append(raw_body)
        raise AssertionError("authority mismatch must not post")

    _wire(monkeypatch, fake_api)
    assert blueprint.main(_argv()) == 1
    assert blueprint.main(_argv()) == 1
    assert posts == []


def test_failed_or_malformed_readback_sends_only_once(monkeypatch):
    posts = []
    reads = 0

    def fake_api(method, path, body=None, timeout=30, *, raw_body=None):
        nonlocal reads
        if method == "GET":
            reads += 1
            if reads == 1:
                return _context()
            return _context(receipt={"schema_version": 1})
        if method == "POST":
            posts.append(raw_body)
            raise blueprint.APIError("ambiguous", request_may_have_been_sent=True)
        raise AssertionError((method, path))

    _wire(monkeypatch, fake_api)
    assert blueprint.main(_argv()) == 1
    assert len(posts) == 1


@pytest.mark.parametrize(
    ("target", "value"),
    [
        (("receipt", "specification_hash"), "f" * 64),
        (("project_id",), "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"),
        (("current", "project_id"), "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"),
        (("current", "id"), "33333333-3333-4333-8333-333333333333"),
    ],
)
def test_corrupt_receipt_envelope_or_current_identity_never_posts(
    monkeypatch, target, value
):
    context = _committed(_request())
    cursor = context
    for key in target[:-1]:
        cursor = cursor[key]
    cursor[target[-1]] = value
    posts = []

    def fake_api(method, path, body=None, timeout=30, *, raw_body=None):
        if method == "GET":
            return context
        posts.append(raw_body)
        raise AssertionError("corrupt proof must not post")

    _wire(monkeypatch, fake_api)
    assert blueprint.main(_argv()) == 1
    assert posts == []


@pytest.mark.parametrize(
    "extra",
    [
        ["--expected-revision", "-1"],
        ["--idempotency-key", " padded "],
        ["--reason", " padded "],
    ],
)
def test_invalid_authority_fails_before_api(monkeypatch, extra):
    calls = []

    def fake_api(*args, **kwargs):
        calls.append((args, kwargs))
        raise AssertionError("invalid input must not call the API")

    _wire(monkeypatch, fake_api)
    argv = _argv()
    option = extra[0]
    argv[argv.index(option) + 1] = extra[1]
    assert blueprint.main(argv) == 1
    assert calls == []


@pytest.mark.parametrize("action", ["show", "hold", "arm"])
def test_scoped_credentials_cannot_use_control_surface(monkeypatch, action):
    calls = []

    def fake_api(method, path, body=None, timeout=30, *, raw_body=None):
        calls.append((method, path))
        if path == "/api/agent-access/v1/me":
            return {"credential": {"scopes": ["project-operator"]}}
        raise AssertionError("scoped credential reached qualification control")

    _wire(monkeypatch, fake_api, key="bpsc_test")
    argv = (
        ["qualification-control", "show", "--project-id", PROJECT_ID]
        if action == "show"
        else _argv(action)
    )
    assert blueprint.main(argv) == 1
    assert calls == [("GET", "/api/agent-access/v1/me")]


def test_show_validates_the_canonical_readback(monkeypatch, capsys):
    context = _context()
    context["requested_idempotency_key"] = None

    def fake_api(method, path, body=None, timeout=30, *, raw_body=None):
        assert (method, path) == (
            "GET",
            f"/api/admin/projects/{PROJECT_ID}/qualification-control",
        )
        return context

    _wire(monkeypatch, fake_api)
    argv = [
        "--json", "qualification-control", "show", "--project-id", PROJECT_ID,
    ]
    assert blueprint.main(argv) == 0
    assert json.loads(capsys.readouterr().out)["result"] == context

    context["current"]["revision"] = "0"
    assert blueprint.main(argv) == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "not_committed_or_ambiguous"
    assert payload["retry_argv"] == [
        "qualification-control", "show", "--project-id", PROJECT_ID,
    ]

    context = _committed(_request())
    context["requested_idempotency_key"] = None
    assert blueprint.main(argv) == 1


@pytest.mark.parametrize(
    ("context"),
    [
        _committed(_request(), current_revision=1) | {
            "requester_actor_user_id": "different-owner",
        },
        _committed(_request(), current_revision=1) | {
            "requester_can_mutate": False,
        },
    ],
)
def test_mutation_preflight_requires_same_authorized_actor(
    monkeypatch, context
):
    posts = []

    def fake_api(method, path, body=None, timeout=30, *, raw_body=None):
        if method == "GET":
            return context
        posts.append(raw_body)
        raise AssertionError("unauthorized or different actor must not post")

    _wire(monkeypatch, fake_api)
    assert blueprint.main(_argv()) == 1
    assert posts == []


def test_malformed_keyed_show_preserves_the_receipt_key(monkeypatch, capsys):
    context = _context(current_revision=1, current_enabled=False)
    context["current"]["specification_hash"] = "f" * 64
    _wire(monkeypatch, lambda *_args, **_kwargs: context)
    argv = [
        "--json", "qualification-control", "show", "--project-id", PROJECT_ID,
        "--idempotency-key", KEY,
    ]
    assert blueprint.main(argv) == 1
    assert json.loads(capsys.readouterr().out)["retry_argv"] == [
        "qualification-control", "show", "--project-id", PROJECT_ID,
        "--idempotency-key", KEY,
    ]


def test_keyed_show_rejects_a_self_consistent_receipt_for_another_key(
    monkeypatch
):
    other_request = _request(key="different-key")
    context = _context(current_revision=1, receipt=_receipt(other_request))
    _wire(monkeypatch, lambda *_args, **_kwargs: context)
    argv = [
        "--json", "qualification-control", "show", "--project-id", PROJECT_ID,
        "--idempotency-key", KEY,
    ]
    assert blueprint.main(argv) == 1
