"""Focused transport-safety tests for ``blueprint deploy``.

The main CLI test module is intentionally close to its live-tree size cap.
Keep new deployment transport cases here while reusing its hermetic manifest,
API, promotion-binding, and config fixtures.
"""

import json
from types import SimpleNamespace

import blueprint
import pytest
import release_socket_submit

from test_blueprint_cli import _DEPLOY_REV, _deploy_api_fake, _deploy_env


def _write_parked_activation(tmp_path, phase):
    revision = "c" * 40
    digest = "sha256:" + "d" * 64
    approved = {
        "version_number": 17,
        "status": "promoted",
        "promoted_sha": revision,
        "serving_status": "unconfirmed",
        "activation_requested_at": "2026-08-30T00:00:00Z",
    }
    operation_id = blueprint._activation_operation_id("project-1", approved)
    state_path = tmp_path / "deploy-agent.state"
    state_path.write_text(json.dumps({
        "version_number": approved["version_number"],
        "promoted_sha": revision,
        "revision": revision,
        "digest": digest,
        "operation_id": operation_id,
        "activation_requested_at": approved["activation_requested_at"],
        "phase": phase,
    }), encoding="utf-8")
    return approved, state_path, digest, operation_id


def _parked_agent_env(monkeypatch, approved, state_path):
    monkeypatch.setattr(blueprint, "_require_write_config", lambda *_args: None)
    monkeypatch.setattr(
        blueprint, "_read_live_approved_activation", lambda _args: approved
    )
    monkeypatch.setattr(blueprint, "get_project_id", lambda _args: "project-1")
    return SimpleNamespace(project_id=None, state_path=str(state_path))


def test_deploy_local_submits_directly_to_release_socket(monkeypatch):
    """A host-resident operator can deploy without an SSH credential."""
    calls = []
    api_fake = _deploy_api_fake(calls)
    _deploy_env(monkeypatch, api_fake)
    socket_calls = []

    monkeypatch.setattr(
        blueprint,
        "_request_release_socket",
        lambda *args, **kwargs: socket_calls.append((args, kwargs)) or True,
    )
    monkeypatch.setattr(blueprint, "_local_served_sha", lambda: _DEPLOY_REV)
    monkeypatch.setattr(
        blueprint.subprocess,
        "run",
        lambda *_args, **_kwargs: pytest.fail("--local must not invoke SSH"),
    )

    rc = blueprint.main(["deploy", "--local", "--host", "hamsterwheel", "--yes"])

    assert rc == 0
    digest = "sha256:" + "0" * 64
    operation_id = blueprint._manual_release_operation_id(
        "proj-1",
        digest,
        _DEPLOY_REV,
        {
            "id": "uuid-promo",
            "version_number": 7,
            "status": "promoted",
            "promoted_sha": "a" * 40,
            "serving_status": "unconfirmed",
        },
    )
    assert socket_calls == [((digest, _DEPLOY_REV, operation_id), {
        "receipt_only": False,
    })]
    receipts = [
        body for method, path, body in calls
        if method == "POST" and path.endswith("/activation-receipt")
    ]
    assert receipts == [{
        "version_number": 7,
        "revision": _DEPLOY_REV,
        "host": "hamsterwheel",
    }]


def test_deploy_ambiguous_local_handoff_is_nonzero_and_never_retried(
    monkeypatch, capsys
):
    """A lost socket verdict is an evidence-reconciliation boundary."""
    calls = []
    api_fake = _deploy_api_fake(calls)
    _deploy_env(monkeypatch, api_fake)
    socket_calls = []
    monkeypatch.setattr(
        blueprint,
        "_request_release_socket",
        lambda *args, **kwargs: socket_calls.append((args, kwargs)) or False,
    )

    rc = blueprint.main(["deploy", "--local", "--host", "hamsterwheel", "--yes"])

    assert rc == 1
    assert len(socket_calls) == 1
    assert not any(
        method == "POST" and path.endswith("/activation-receipt")
        for method, path, _body in calls
    )
    err = capsys.readouterr().err
    assert "acceptance is ambiguous" in err
    assert "do not retry activation" in err
    assert f"blueprint deploy --receipt-only --sha {_DEPLOY_REV}" in err


def test_receipt_query_transport_failure_preserves_started_fence(monkeypatch, tmp_path):
    approved, state_path, _digest, _operation_id = _write_parked_activation(
        tmp_path, "started"
    )
    args = _parked_agent_env(monkeypatch, approved, state_path)
    calls = []

    def unavailable(*call_args, **call_kwargs):
        calls.append((call_args, call_kwargs))
        raise blueprint._ReleaseRequestNotSent("socket unavailable")

    monkeypatch.setattr(blueprint, "_invoke_release_shim", unavailable)
    monkeypatch.setattr(
        blueprint, "_fetch_deploy_manifest", lambda *_args: pytest.fail("fresh activation")
    )

    assert blueprint._deploy_agent_once(args) is False
    assert json.loads(state_path.read_text(encoding="utf-8"))["phase"] == "started"
    assert len(calls) == 1 and calls[0][1]["receipt_only"] is True


def test_exact_not_accepted_rearms_same_operation_once(monkeypatch, tmp_path):
    approved, state_path, digest, operation_id = _write_parked_activation(
        tmp_path, "started"
    )
    args = _parked_agent_env(monkeypatch, approved, state_path)
    calls = []
    receipts = []

    def release(*call_args, **call_kwargs):
        calls.append((call_args, call_kwargs))
        if len(calls) == 1:
            raise blueprint._ReleaseOperationNotAccepted("no immutable claim")
        return True

    monkeypatch.setattr(blueprint, "_invoke_release_shim", release)
    monkeypatch.setattr(blueprint, "_local_served_sha", lambda: approved["promoted_sha"])
    monkeypatch.setattr(
        blueprint, "_post_activation_receipt", lambda *items: receipts.append(items)
    )

    assert blueprint._deploy_agent_once(args) is False
    parked = json.loads(state_path.read_text(encoding="utf-8"))
    assert parked["phase"] == parked["root_release"]["status"] == "not_sent"
    assert parked["failure_reason"] == parked["root_release"]["failure_reason"]
    assert blueprint._deploy_agent_once(args) is True
    activation_calls = [item for item in calls if not item[1].get("receipt_only")]
    assert activation_calls == [((digest, approved["promoted_sha"], operation_id), {"host": None})]
    assert len(receipts) == 1
    assert not state_path.exists()


def test_unknown_parked_phase_refuses_fresh_activation(monkeypatch, tmp_path):
    approved, state_path, _digest, _operation_id = _write_parked_activation(
        tmp_path, "permission_mode_bypass"
    )
    args = _parked_agent_env(monkeypatch, approved, state_path)
    monkeypatch.setattr(
        blueprint, "_invoke_release_shim", lambda *_args, **_kwargs: pytest.fail("release")
    )
    monkeypatch.setattr(
        blueprint, "_fetch_deploy_manifest", lambda *_args: pytest.fail("manifest")
    )

    assert blueprint._deploy_agent_once(args) is False
    assert json.loads(state_path.read_text(encoding="utf-8"))["phase"] == (
        "permission_mode_bypass"
    )


def test_completed_success_fence_never_rearms_if_root_receipt_disappears(
    monkeypatch, tmp_path
):
    approved, state_path, _digest, _operation_id = _write_parked_activation(
        tmp_path, "completed"
    )
    args = _parked_agent_env(monkeypatch, approved, state_path)
    calls = []

    def no_claim(*call_args, **call_kwargs):
        calls.append((call_args, call_kwargs))
        raise blueprint._ReleaseOperationNotAccepted("no immutable claim")

    monkeypatch.setattr(blueprint, "_invoke_release_shim", no_claim)

    assert blueprint._deploy_agent_once(args) is False
    assert json.loads(state_path.read_text(encoding="utf-8"))["phase"] == "completed"
    assert len(calls) == 1 and calls[0][1]["receipt_only"] is True


def test_exact_terminal_receipt_latches_failed_without_replay(monkeypatch, tmp_path):
    approved, state_path, _digest, _operation_id = _write_parked_activation(
        tmp_path, "accepted"
    )
    args = _parked_agent_env(monkeypatch, approved, state_path)
    calls = []

    def terminal(*call_args, **call_kwargs):
        calls.append((call_args, call_kwargs))
        raise blueprint._ReleaseTerminalFailure("root terminal outcome rollback_failed")

    monkeypatch.setattr(blueprint, "_invoke_release_shim", terminal)

    assert blueprint._deploy_agent_once(args) is False
    state = json.loads(state_path.read_text(encoding="utf-8"))
    assert state["phase"] == "failed"
    assert state["root_release"]["status"] == "failed"
    assert "rollback_failed" in state["failure_reason"]
    assert blueprint._deploy_agent_once(args) is False
    assert len(calls) == 1 and calls[0][1]["receipt_only"] is True


def test_raw_not_accepted_verdict_is_authoritative_no_claim():
    digest = "sha256:" + "7" * 64
    revision = "8" * 40
    operation_id = "9" * 64

    with pytest.raises(blueprint._ReleaseOperationNotAccepted):
        blueprint._parse_release_socket_response(
            f"NOT_ACCEPTED {operation_id} {digest} {revision}",
            digest,
            revision,
            operation_id,
        )


def test_mismatched_root_receipt_fails_closed():
    digest = "sha256:" + "7" * 64
    revision = "8" * 40
    operation_id = "9" * 64

    with pytest.raises(blueprint.APIError, match="mismatched operation receipt"):
        blueprint._parse_release_socket_response(
            f"OK {'a' * 64} {digest} {revision} success",
            digest,
            revision,
            operation_id,
        )


@pytest.mark.parametrize(("response", "connect_error", "expected"), [
    ("NOT_ACCEPTED {op} {digest} {revision}\n", False, 5),
    ("", True, 4),
])
def test_remote_socket_client_separates_no_claim_from_unsent(
    monkeypatch, response, connect_error, expected
):
    digest = "sha256:" + "a" * 64
    revision = "b" * 40
    operation_id = "c" * 64

    class FakeSocket:
        def settimeout(self, _timeout):
            pass

        def connect(self, _path):
            if connect_error:
                raise OSError("unavailable")

        def sendall(self, _payload):
            pass

        def shutdown(self, _direction):
            pass

        def recv(self, _limit):
            return response.format(
                op=operation_id, digest=digest, revision=revision
            ).encode("ascii")

        def close(self):
            pass

    monkeypatch.setattr(release_socket_submit.socket, "socket", lambda *_args: FakeSocket())
    monkeypatch.setattr(release_socket_submit.sys, "argv", [
        "release_socket_submit.py", "--receipt-only", digest, revision, operation_id
    ])
    assert release_socket_submit.main() == expected


def test_receipt_only_deploy_requires_write_mode_before_root_reconciliation(
    monkeypatch
):
    revision = "e" * 40
    manifest = {
        "run": {"createdAt": "now", "databaseId": 1},
        "digest": "sha256:" + "f" * 64,
        "revision": revision,
    }
    approved = {
        "version_number": 18,
        "promoted_sha": revision,
        "activation_requested_at": "2026-08-30T00:00:00Z",
    }
    monkeypatch.setattr(blueprint, "_fetch_deploy_manifest", lambda _sha: manifest)
    monkeypatch.setattr(blueprint, "_resolve_deploy_binding", lambda *_args: approved)
    monkeypatch.setattr(
        blueprint,
        "_require_write_config",
        lambda _action: (_ for _ in ()).throw(blueprint.APIError("read-only config")),
    )
    monkeypatch.setattr(
        blueprint, "_execute_deploy_shim", lambda *_args, **_kwargs: pytest.fail("root")
    )
    args = SimpleNamespace(
        sha=revision, host="control-plane", dry_run=False, receipt_only=True,
        local=True, yes=True,
    )

    with pytest.raises(blueprint.APIError, match="read-only config"):
        blueprint.cmd_deploy(args)


def test_execute_deploy_derives_operation_from_explicit_project_id(monkeypatch):
    revision = "1" * 40
    digest = "sha256:" + "2" * 64
    approved = {
        "version_number": 19,
        "promoted_sha": revision,
        "activation_requested_at": "2026-08-30T01:02:03Z",
    }
    calls = []
    monkeypatch.setattr(
        blueprint,
        "_execute_deploy_shim",
        lambda *args, **kwargs: calls.append((args, kwargs)) or True,
    )
    monkeypatch.setattr(blueprint, "_post_activation_receipt", lambda *_args: None)
    monkeypatch.setattr(blueprint, "_local_served_sha", lambda: revision)
    args = SimpleNamespace(project_id="project-explicit", host="control-plane")

    blueprint._execute_deploy(args, revision, digest, approved, inplace=True)

    expected = blueprint._activation_operation_id("project-explicit", approved)
    assert calls == [
        ((args, revision, digest), {"inplace": True, "operation_id": expected})
    ]


def test_receipt_only_manual_binding_queries_exact_artifact_without_activation(
    monkeypatch, capsys,
):
    revision = "3" * 40
    digest = "sha256:" + "0" * 64
    promoted = {
        "id": "version-20",
        "version_number": 20,
        "status": "promoted",
        "promoted_sha": "2" * 40,
        "served_sha": "2" * 40,
        "served_at": "2026-08-30T00:00:00Z",
        "activation_requested_at": None,
    }
    api_calls = []
    _deploy_env(
        monkeypatch,
        _deploy_api_fake(api_calls, promoted=promoted),
        revision=revision,
    )
    root_calls = []
    monkeypatch.setattr(
        blueprint,
        "_request_release_socket",
        lambda *args, **kwargs: root_calls.append((args, kwargs)) or True,
    )
    monkeypatch.setattr(blueprint, "_local_served_sha", lambda: revision)

    rc = blueprint.main([
        "deploy", "--local", "--receipt-only", "--sha", revision,
    ])

    operation_id = blueprint._manual_release_operation_id(
        "proj-1", digest, revision, promoted
    )
    assert rc == 0
    assert root_calls == [((digest, revision, operation_id), {
        "receipt_only": True,
    })]
    assert all(call[1]["receipt_only"] is True for call in root_calls)
    assert not any(
        method == "POST" and path.endswith("/activation-receipt")
        for method, path, _body in api_calls
    )
    err = capsys.readouterr().err
    assert "v20 immutable historical receipt remains unchanged" in err
    assert "No version receipt was posted" in err
    assert "re-run deploy" not in err


def test_manual_operation_identity_changes_after_serving_predecessor_advances():
    revision = "3" * 40
    digest = "sha256:" + "4" * 64
    predecessor = {
        "id": "version-20",
        "served_sha": "2" * 40,
        "served_at": "2026-08-30T00:00:00Z",
    }
    reconciled = {
        **predecessor,
        "served_sha": revision,
        "served_at": "2026-08-31T00:00:00Z",
    }

    before = blueprint._manual_release_operation_id(
        "project-manual", digest, revision, predecessor
    )
    after = blueprint._manual_release_operation_id(
        "project-manual", digest, revision, reconciled
    )

    assert before != after


def test_remote_receipt_only_preserves_approval_id_and_posts_without_loopback(
    monkeypatch,
):
    revision = "5" * 40
    digest = "sha256:" + "6" * 64
    promoted = {
        "version_number": 21,
        "promoted_sha": revision,
        "served_sha": "4" * 40,
        "served_at": "2026-08-30T00:00:00Z",
        "activation_requested_at": "2026-08-31T01:02:03Z",
    }
    root_calls = []
    receipts = []
    monkeypatch.setattr(blueprint, "_fetch_deploy_manifest", lambda _sha: {
        "run": {"createdAt": "now", "databaseId": 3},
        "digest": digest,
        "revision": revision,
    })
    monkeypatch.setattr(blueprint, "_resolve_deploy_binding", lambda *_args: promoted)
    monkeypatch.setattr(blueprint, "_require_write_config", lambda _action: None)
    monkeypatch.setattr(
        blueprint,
        "_execute_deploy_shim",
        lambda *args, **kwargs: root_calls.append((args, kwargs)) or True,
    )
    monkeypatch.setattr(
        blueprint,
        "_local_served_sha",
        lambda: pytest.fail("remote receipt reconciliation must not probe loopback"),
    )
    monkeypatch.setattr(
        blueprint,
        "_post_activation_receipt",
        lambda *args: receipts.append(args),
    )
    args = SimpleNamespace(
        sha=revision,
        host="control-plane",
        dry_run=False,
        receipt_only=True,
        local=False,
        yes=True,
        force=False,
        project_id="project-approved",
    )

    blueprint.cmd_deploy(args)

    expected = blueprint._activation_operation_id("project-approved", promoted)
    assert root_calls == [((args, revision, digest), {
        "inplace": False,
        "operation_id": expected,
        "receipt_only": True,
    })]
    assert receipts == [(args, promoted, revision)]


def test_direct_remote_build_ahead_success_preserves_historical_receipt(
    monkeypatch, capsys,
):
    revision = "7" * 40
    digest = "sha256:" + "8" * 64
    promoted = {
        "id": "version-22",
        "version_number": 22,
        "promoted_sha": "6" * 40,
        "served_sha": "6" * 40,
        "served_at": "2026-08-30T00:00:00Z",
    }
    root_calls = []
    monkeypatch.setattr(
        blueprint,
        "_execute_deploy_shim",
        lambda *args, **kwargs: root_calls.append((args, kwargs)) or True,
    )
    monkeypatch.setattr(
        blueprint,
        "_post_activation_receipt",
        lambda *_args: pytest.fail("historical receipt must remain immutable"),
    )
    args = SimpleNamespace(project_id="project-manual", host="control-plane")

    blueprint._execute_deploy(args, revision, digest, promoted, inplace=False)

    expected = blueprint._manual_release_operation_id(
        "project-manual", digest, revision, promoted
    )
    assert root_calls == [((args, revision, digest), {
        "inplace": False,
        "operation_id": expected,
    })]
    err = capsys.readouterr().err
    assert "immutable historical receipt remains unchanged" in err
    assert "No version receipt was posted" in err


def test_manual_same_sha_still_posts_one_version_receipt(monkeypatch):
    revision = "7" * 40
    promoted = {
        "version_number": 23,
        "promoted_sha": revision,
        "served_sha": revision,
    }
    receipts = []
    monkeypatch.setattr(
        blueprint,
        "_post_activation_receipt",
        lambda *args: receipts.append(args),
    )
    args = SimpleNamespace(project_id="project-manual", host="control-plane")

    assert blueprint._post_compatible_activation_receipt(
        args, promoted, revision
    ) is True
    assert receipts == [(args, promoted, revision)]


def test_local_success_receipt_requires_exact_served_sha_before_api_receipt(
    monkeypatch,
):
    revision = "7" * 40
    digest = "sha256:" + "8" * 64
    promoted = {
        "id": "version-22",
        "version_number": 22,
        "promoted_sha": revision,
        "served_sha": "6" * 40,
        "served_at": "2026-08-30T00:00:00Z",
    }
    root_calls = []
    monkeypatch.setattr(
        blueprint,
        "_execute_deploy_shim",
        lambda *args, **kwargs: root_calls.append((args, kwargs)) or True,
    )
    monkeypatch.setattr(blueprint, "_local_served_sha", lambda: "9" * 40)
    monkeypatch.setattr(
        blueprint,
        "_post_activation_receipt",
        lambda *_args: pytest.fail("mismatched serving evidence must not be posted"),
    )
    args = SimpleNamespace(project_id="project-manual", host="control-plane")

    with pytest.raises(blueprint.APIError, match="local served revision does not match"):
        blueprint._execute_deploy(args, revision, digest, promoted, inplace=True)

    expected = blueprint._manual_release_operation_id(
        "project-manual", digest, revision, promoted
    )
    assert root_calls == [((args, revision, digest), {
        "inplace": True,
        "operation_id": expected,
    })]
