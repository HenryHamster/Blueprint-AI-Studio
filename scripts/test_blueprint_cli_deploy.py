"""Focused transport-safety tests for ``blueprint deploy``.

The main CLI test module is intentionally close to its live-tree size cap.
Keep new deployment transport cases here while reusing its hermetic manifest,
API, promotion-binding, and config fixtures.
"""

import blueprint
import pytest

from test_blueprint_cli import _DEPLOY_REV, _deploy_api_fake, _deploy_env


def test_deploy_local_submits_directly_to_release_socket(monkeypatch):
    """A host-resident operator can deploy without an SSH credential."""
    calls = []
    api_fake = _deploy_api_fake(calls)
    _deploy_env(monkeypatch, api_fake)
    socket_calls = []

    monkeypatch.setattr(
        blueprint,
        "_request_release_socket",
        lambda digest, revision: socket_calls.append((digest, revision)) or True,
    )
    monkeypatch.setattr(
        blueprint.subprocess,
        "run",
        lambda *_args, **_kwargs: pytest.fail("--local must not invoke SSH"),
    )

    rc = blueprint.main(["deploy", "--local", "--host", "hamsterwheel", "--yes"])

    assert rc == 0
    assert socket_calls == [("sha256:" + "0" * 64, _DEPLOY_REV)]
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
        lambda digest, revision: socket_calls.append((digest, revision)) or False,
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
