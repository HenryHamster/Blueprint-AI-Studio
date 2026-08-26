"""Focused operator-CLI contracts for the false Fast-claim repair surface."""

import json

import blueprint
import blueprint_api
import pytest


PROJECT_ID = "37d445e1-0775-4403-bacf-1cd7ed129336"
WINDOW_START = "2026-08-01T00:00:00Z"
WINDOW_END = "2026-08-15T00:00:00+00:00"
BUGGY_REVISION = "4" * 40
FIXED_REVISION = "5" * 40
CANDIDATE_DIGEST = "a" * 64

# ``main()`` validates an explicit ``--project-id`` against the catalog before
# dispatching, so the id has to be resolvable from a mock rather than from the
# host's real credential.
PROJECT_CATALOG = [
    {
        "id": PROJECT_ID,
        "name": "hermes/fast-claim-project",
        "github_owner": "hermes",
        "github_repo": "fast-claim-project",
        "role": "owner",
    }
]


def _install_double(monkeypatch, name: str, value) -> None:
    """Install a test double on BOTH bindings of a ``blueprint_api`` name.

    ``blueprint.py`` does ``from blueprint_api import (...)``, so patching only
    the ``blueprint`` module leaves ``blueprint_api``'s internal calls --
    ``main()`` -> ``validate_project_id()`` -> ``list_projects()`` ->
    ``load_config()``/``api()`` -- bound to the real functions. Those resolved
    the operator's ambient ``~/.blueprint-config.json`` and made live
    authenticated requests to the production orchestrator from this unit test.
    """
    monkeypatch.setattr(blueprint_api, name, value)
    monkeypatch.setattr(blueprint, name, value)


def _install_project_catalog(monkeypatch) -> None:
    """Resolve ``--project-id`` from a mock, never from a real credential."""
    _install_double(monkeypatch, "list_projects", lambda: list(PROJECT_CATALOG))
    _install_double(
        monkeypatch, "get_project_id", lambda project_id=None: project_id or PROJECT_ID
    )


def _common_args(mode: str) -> list[str]:
    return [
        "ops",
        "repair-fast-claim-redactions",
        mode,
        "--project-id",
        PROJECT_ID,
        "--window-start",
        WINDOW_START,
        "--window-end",
        WINDOW_END,
        "--buggy-serving-revision",
        BUGGY_REVISION,
        "--expected-serving-revision",
        FIXED_REVISION,
    ]


def _apply_args() -> list[str]:
    return [
        *_common_args("apply"),
        "--operation-id",
        "repair-fast-claim-redactions-20260815",
        "--expected-candidate-count",
        "2",
        "--expected-candidate-digest",
        CANDIDATE_DIGEST,
        "--yes",
    ]


def _operator_config(monkeypatch) -> None:
    _install_project_catalog(monkeypatch)
    _install_double(
        monkeypatch,
        "load_config",
        lambda: {"api_key": "operator-admin-key", "read_only": False},
    )


def test_parser_requires_explicit_mode_apply_fences_and_yes():
    parser = blueprint.build_parser()

    with pytest.raises(SystemExit) as missing_mode:
        parser.parse_args(["ops", "repair-fast-claim-redactions"])
    assert missing_mode.value.code == 2

    dry_run = parser.parse_args(_common_args("dry-run"))
    assert dry_run.fast_claim_repair_mode == "dry-run"
    assert dry_run.window_start == WINDOW_START
    assert not hasattr(dry_run, "operation_id")

    apply = parser.parse_args(_apply_args())
    assert apply.fast_claim_repair_mode == "apply"
    assert apply.yes is True
    assert apply.expected_candidate_count == 2
    assert apply.expected_candidate_digest == CANDIDATE_DIGEST

    without_yes = _apply_args()
    without_yes.remove("--yes")
    with pytest.raises(SystemExit) as missing_yes:
        parser.parse_args(without_yes)
    assert missing_yes.value.code == 2

    without_digest = _apply_args()
    digest_index = without_digest.index("--expected-candidate-digest")
    del without_digest[digest_index:digest_index + 2]
    with pytest.raises(SystemExit) as missing_digest:
        parser.parse_args(without_digest)
    assert missing_digest.value.code == 2


@pytest.mark.parametrize(
    ("flag", "value"),
    [
        ("--window-start", "2026-08-01T00:00:00"),
        ("--buggy-serving-revision", "not safe/value"),
        ("--expected-candidate-count", "0"),
        ("--expected-candidate-digest", "A" * 64),
    ],
)
def test_parser_rejects_unbounded_or_ambiguous_apply_fences(flag, value):
    argv = _apply_args()
    argv[argv.index(flag) + 1] = value

    with pytest.raises(SystemExit) as invalid:
        blueprint.build_parser().parse_args(argv)

    assert invalid.value.code == 2


def test_dry_run_posts_only_review_fences_and_emits_json(monkeypatch, capsys):
    _operator_config(monkeypatch)
    calls = []
    response = {
        "mode": "dry_run",
        "project_id": PROJECT_ID,
        "serving_revision": FIXED_REVISION,
        "buggy_serving_revision": BUGGY_REVISION,
        "window_start": "2026-08-01T00:00:00+00:00",
        "window_end": "2026-08-15T00:00:00+00:00",
        "candidate_count": 2,
        "candidate_digest": CANDIDATE_DIGEST,
        "content_returned": False,
    }

    def fake_api(method, path, body=None):
        calls.append((method, path, body))
        return response

    _install_double(monkeypatch, "api", fake_api)
    _install_double(
        monkeypatch, "get_project_id", lambda project_id=None: project_id
    )

    assert blueprint.main([*_common_args("dry-run"), "--json"]) == 0

    assert calls == [
        (
            "POST",
            f"/api/admin/projects/{PROJECT_ID}/memory/repairs/fast-claim-redactions",
            {
                "mode": "dry_run",
                "window_start": WINDOW_START,
                "window_end": WINDOW_END,
                "buggy_serving_revision": BUGGY_REVISION,
                "expected_serving_revision": FIXED_REVISION,
            },
        )
    ]
    assert json.loads(capsys.readouterr().out) == response


def test_apply_posts_every_exact_fence_and_renders_receipt(monkeypatch, capsys):
    _operator_config(monkeypatch)
    calls = []
    response = {
        "mode": "apply",
        "project_id": PROJECT_ID,
        "serving_revision": FIXED_REVISION,
        "operation_id": "repair-fast-claim-redactions-20260815",
        "audit_source_id": "audit-source-id",
        "candidate_count": 2,
        "candidate_digest": CANDIDATE_DIGEST,
        "restored_count": 2,
        "reconsolidation_pending_count": 2,
        "queued_job_id": "queued-job-id",
        "replayed": False,
        "content_returned": False,
    }

    def fake_api(method, path, body=None):
        calls.append((method, path, body))
        return response

    _install_double(monkeypatch, "api", fake_api)
    _install_double(
        monkeypatch, "get_project_id", lambda project_id=None: project_id
    )

    assert blueprint.main(_apply_args()) == 0

    assert calls == [
        (
            "POST",
            f"/api/admin/projects/{PROJECT_ID}/memory/repairs/fast-claim-redactions",
            {
                "mode": "apply",
                "window_start": WINDOW_START,
                "window_end": WINDOW_END,
                "buggy_serving_revision": BUGGY_REVISION,
                "expected_serving_revision": FIXED_REVISION,
                "operation_id": "repair-fast-claim-redactions-20260815",
                "expected_candidate_count": 2,
                "expected_candidate_digest": CANDIDATE_DIGEST,
            },
        )
    ]
    rendered = capsys.readouterr().out
    assert "FAST-CLAIM REDACTION REPAIR APPLY" in rendered
    assert "Restored:            2" in rendered
    assert "Reconsolidation due: 2" in rendered
    assert "queued-job-id" in rendered
    assert CANDIDATE_DIGEST in rendered


def test_read_only_config_fails_before_repair_request(monkeypatch):
    _install_project_catalog(monkeypatch)
    _install_double(
        monkeypatch,
        "load_config",
        lambda: {"api_key": "agent-jwt", "read_only": True},
    )
    _install_double(
        monkeypatch,
        "api",
        lambda *_args, **_kwargs: pytest.fail("repair endpoint must not be called"),
    )

    with pytest.raises(SystemExit) as denied:
        blueprint.main(_common_args("dry-run"))

    assert denied.value.code == 1


def test_project_operator_is_denied_before_repair_endpoint(monkeypatch):
    calls = []
    _install_project_catalog(monkeypatch)
    _install_double(
        monkeypatch,
        "load_config",
        lambda: {"api_key": "bpsc_project_operator", "read_only": False},
    )
    monkeypatch.setattr(blueprint, "_SCOPED_PROFILE_CACHE", None)

    def fake_api(method, path, body=None):
        calls.append((method, path, body))
        if path == "/api/agent-access/v1/me":
            return {"credential": {"scopes": ["project-operator"]}}
        pytest.fail("owner-only repair endpoint must not be called")

    _install_double(monkeypatch, "api", fake_api)

    assert blueprint.main(_common_args("dry-run")) == 1
    assert calls == [("GET", "/api/agent-access/v1/me", None)]
