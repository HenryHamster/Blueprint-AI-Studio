# AUTO-SPLIT from test_release_qualifier.py — see refactor: split mega backend test files.
# Behavior-preserving: shared preamble lives in _release_qualifier_testhelpers.py; this file
# holds one domain slice of the original test suite.
"""Domain slice: _classify_check_runs — CI state mapping

Split from test_release_qualifier.py."""
from _release_qualifier_testhelpers import *  # noqa: F403


@pytest.fixture(autouse=True)
def _authorized_qualification_has_a_delivery_target(monkeypatch):
    monkeypatch.setattr(rq, "resolve_project_promotion_enabled", lambda _id: True)


def _with_current_contract(project, version, record):
    """Stamp a synthetic conclusive verdict with today's reusable contract."""
    rubric_snapshot = record.get("rubric_snapshot") or []
    rubric_fingerprint = rq._rubric_fingerprint(rubric_snapshot)
    policy = rq._load_policy(project)
    build_setting = (project.build_config or {}).get("env_preflight_enabled")
    env_preflight_enabled = (
        bool(build_setting)
        if build_setting is not None
        else bool(rq._runtime_config.get("env_preflight_enabled", policy.get("env_preflight_enabled", False)))
    )
    return {
        **record,
        "rubric_snapshot": rubric_snapshot,
        "rubric_fingerprint": rubric_fingerprint,
        "version_goal": version.goal or "",
        "promotion_policy_snapshot": policy,
        "capability_proof_mode": rq._runtime_config.get(
            "capability_proof_gate_mode", "off"
        ),
        "env_preflight_enabled": env_preflight_enabled,
        "contract_fingerprint": rq._qualification_contract_fingerprint(
            version.goal or "",
            rubric_fingerprint,
            promotion_policy=policy,
            capability_proof_mode=rq._runtime_config.get("capability_proof_gate_mode", "off"),
            env_preflight_enabled=env_preflight_enabled,
            requested_probes=[],
        ),
        "requested_probes": [],
    }


# ===========================================================================
# _classify_check_runs — CI state mapping
# ===========================================================================


class TestClassifyCheckRuns:
    def test_none_is_pending(self):
        assert rq._classify_check_runs(None) == rq._CI_PENDING

    def test_non_dict_is_pending(self):
        assert rq._classify_check_runs("bad") == rq._CI_PENDING  # type: ignore[arg-type]

    def test_no_checks_status(self):
        assert rq._classify_check_runs({"status": "no_checks"}) == rq._CI_NO_CHECKS

    def test_completed_success_is_green(self):
        assert (
            rq._classify_check_runs({"status": "completed", "conclusion": "success"})
            == rq._CI_GREEN
        )

    def test_completed_failure_is_red(self):
        assert (
            rq._classify_check_runs({"status": "completed", "conclusion": "failure"})
            == rq._CI_RED
        )

    def test_completed_neutral_conclusion_is_pending(self):
        assert (
            rq._classify_check_runs({"status": "completed", "conclusion": "neutral"})
            == rq._CI_PENDING
        )

    def test_in_progress_is_pending(self):
        assert rq._classify_check_runs({"status": "in_progress"}) == rq._CI_PENDING

    def test_conclusion_without_status_is_pending_not_red(self):
        """A conclusion with NO status falls through to the safe default
        (pending), never silently RED/GREEN. This partial shape is never
        produced by github_checks.get_check_runs — which always emits a
        "completed" status alongside a "failure"/"success" conclusion — so the
        defensive fall-through is correct, not a misroute. (The old CI-red
        run_qualification test relied on this exact shape and so passed for the
        wrong reason; it now sends the faithful completed+failure shape.)"""
        assert rq._classify_check_runs({"conclusion": "failure"}) == rq._CI_PENDING
        assert rq._classify_check_runs({"conclusion": "success"}) == rq._CI_PENDING


# ===========================================================================
# _resolve_candidate_ci — qualifier-scoped strict CI resolver
# ===========================================================================


@pytest.mark.asyncio
async def test_resolve_candidate_ci_no_sha_returns_pending(mocker):
    """Missing candidate SHA → pending state without any API call."""
    result = await rq._resolve_candidate_ci("tok", "owner", "repo", "")
    assert result["ci_green"] is False
    assert result["state"] == rq._CI_PENDING


@pytest.mark.asyncio
async def test_resolve_candidate_ci_direct_green(mocker):
    """Candidate commit carries a completed green build → ci_green=True."""
    mocker.patch(
        "app.github.get_check_runs_cached",
        new=AsyncMock(
            return_value={"status": "completed", "conclusion": "success", "details": []}
        ),
    )
    mocker.patch(
        "app.github.list_pull_requests_for_commit", new=AsyncMock(return_value=[])
    )
    result = await rq._resolve_candidate_ci("tok", "owner", "repo", "abc123")
    assert result["ci_green"] is True
    assert result["state"] == rq._CI_GREEN
    assert result["resolved_sha"] == "abc123"


@pytest.mark.asyncio
async def test_resolve_candidate_ci_direct_red(mocker):
    """Candidate commit carries a completed failing build → ci_green=False."""
    mocker.patch(
        "app.github.get_check_runs_cached",
        new=AsyncMock(
            return_value={"status": "completed", "conclusion": "failure", "details": []}
        ),
    )
    result = await rq._resolve_candidate_ci("tok", "owner", "repo", "abc123")
    assert result["ci_green"] is False
    assert result["state"] == rq._CI_RED


@pytest.mark.asyncio
async def test_resolve_candidate_ci_direct_pending(mocker):
    """Candidate commit has in-progress checks → ci_green=False (pending)."""
    mocker.patch(
        "app.github.get_check_runs_cached",
        new=AsyncMock(
            return_value={"status": "in_progress", "conclusion": None, "details": []}
        ),
    )
    result = await rq._resolve_candidate_ci("tok", "owner", "repo", "abc123")
    assert result["ci_green"] is False
    assert result["state"] == rq._CI_PENDING


@pytest.mark.asyncio
async def test_resolve_candidate_ci_no_checks_resolves_via_pr_head(mocker):
    """Candidate has no check-runs; CI ran on the PR head commit — resolves green."""
    pr_head_sha = "pr_head_deadbeef"
    call_count = {"n": 0}

    async def _check_runs_side_effect(token, owner, repo, sha, **kwargs):
        call_count["n"] += 1
        if sha == "candidate000":
            return {"status": "no_checks", "conclusion": None, "details": []}
        # PR head commit
        return {"status": "completed", "conclusion": "success", "details": []}

    mocker.patch(
        "app.github.get_check_runs_cached", side_effect=_check_runs_side_effect
    )
    mocker.patch(
        "app.github.list_pull_requests_for_commit",
        new=AsyncMock(
            return_value=[
                {
                    "number": 42,
                    "merged_at": "2026-01-01T00:00:00Z",
                    "head": {"sha": pr_head_sha},
                },
            ]
        ),
    )
    result = await rq._resolve_candidate_ci("tok", "owner", "repo", "candidate000")
    assert result["ci_green"] is True
    assert result["state"] == rq._CI_GREEN
    assert result["resolved_sha"] == pr_head_sha


@pytest.mark.asyncio
async def test_resolve_candidate_ci_no_checks_no_prs_returns_no_checks(mocker):
    """Candidate has no check-runs and no associated PRs → definitive no_checks."""
    class _MockResponse:
        def __init__(self, status_code, json_data=None, text=""):
            self.status_code = status_code
            self._json_data = json_data if json_data is not None else {}
            self.text = text
            self.headers = {}
            self.links = {}

        def json(self):
            return self._json_data


    mocker.patch(
        "app.github.get_check_runs_cached",
        new=AsyncMock(
            return_value={"status": "no_checks", "conclusion": None, "details": []}
        ),
    )
    mocker.patch(
        "app.github.list_pull_requests_for_commit",
        new=AsyncMock(return_value=[]),
    )
    mocker.patch(
        "app.github._github_request",
        new=AsyncMock(return_value=_MockResponse(200, [])),
    )
    result = await rq._resolve_candidate_ci("tok", "owner", "repo", "candidate111")
    assert result["ci_green"] is False
    assert result["state"] == rq._CI_NO_CHECKS


@pytest.mark.asyncio
async def test_resolve_candidate_ci_no_checks_pr_list_error_returns_pending(mocker):
    """A collapsed PR-list lookup failure stays pending instead of no_checks."""

    class _MockResponse:
        def __init__(self, status_code, json_data=None, text=""):
            self.status_code = status_code
            self._json_data = json_data if json_data is not None else {}
            self.text = text
            self.headers = {}
            self.links = {}

        def json(self):
            return self._json_data

    async def _check_runs_side_effect(token, owner, repo, sha, **kwargs):
        if sha == "candidate_lookup_error":
            return {"status": "no_checks", "conclusion": None, "details": []}
        raise AssertionError("PR head check-runs should not run on PR-list failure")

    mocker.patch(
        "app.github.get_check_runs_cached", side_effect=_check_runs_side_effect
    )
    mocker.patch(
        "app.github.list_pull_requests_for_commit",
        new=AsyncMock(return_value=[]),
    )
    mocker.patch(
        "app.github._github_request",
        new=AsyncMock(return_value=_MockResponse(500, {"message": "boom"}, text="boom")),
    )

    result = await rq._resolve_candidate_ci(
        "tok", "owner", "repo", "candidate_lookup_error"
    )

    assert result["ci_green"] is False
    assert result["state"] == rq._CI_PENDING
    assert result["resolved_sha"] is None


@pytest.mark.asyncio
async def test_resolve_candidate_ci_check_runs_fetch_error_returns_pending(mocker):
    """Any exception from get_check_runs_cached → pending (never raises)."""
    mocker.patch(
        "app.github.get_check_runs_cached",
        new=AsyncMock(side_effect=RuntimeError("transient network error")),
    )
    result = await rq._resolve_candidate_ci("tok", "owner", "repo", "candidate222")
    assert result["ci_green"] is False
    assert result["state"] == rq._CI_PENDING


@pytest.mark.asyncio
async def test_resolve_candidate_ci_pr_head_fetch_error_returns_pending(mocker):
    """Candidate no_checks is not definitive when PR-head lookup errors."""
    pr_head_sha = "pr_head_api_error"

    async def _check_runs_side_effect(token, owner, repo, sha, **kwargs):
        if sha == "candidate_api_error":
            return {"status": "no_checks", "conclusion": None, "details": []}
        raise RuntimeError("transient PR-head check-runs error")

    mocker.patch(
        "app.github.get_check_runs_cached", side_effect=_check_runs_side_effect
    )
    mocker.patch(
        "app.github.list_pull_requests_for_commit",
        new=AsyncMock(
            return_value=[
                {
                    "number": 9,
                    "merged_at": "2026-01-01T00:00:00Z",
                    "head": {"sha": pr_head_sha},
                }
            ]
        ),
    )
    result = await rq._resolve_candidate_ci("tok", "owner", "repo", "candidate_api_error")
    assert result["ci_green"] is False
    assert result["state"] == rq._CI_PENDING


@pytest.mark.asyncio
async def test_resolve_candidate_ci_no_checks_pr_head_red_returns_red(mocker):
    """Candidate has no check-runs; CI ran on the PR head and is RED → ci_green=False."""
    pr_head_sha = "pr_head_red0001"

    async def _check_runs_side_effect(token, owner, repo, sha, **kwargs):
        if sha == "candidate_red":
            return {"status": "no_checks", "conclusion": None, "details": []}
        return {"status": "completed", "conclusion": "failure", "details": []}

    mocker.patch(
        "app.github.get_check_runs_cached", side_effect=_check_runs_side_effect
    )
    mocker.patch(
        "app.github.list_pull_requests_for_commit",
        new=AsyncMock(
            return_value=[
                {
                    "number": 7,
                    "merged_at": "2026-01-01T00:00:00Z",
                    "head": {"sha": pr_head_sha},
                }
            ]
        ),
    )
    result = await rq._resolve_candidate_ci("tok", "owner", "repo", "candidate_red")
    assert result["ci_green"] is False
    assert result["state"] == rq._CI_RED
    assert result["resolved_sha"] == pr_head_sha


@pytest.mark.asyncio
async def test_resolve_candidate_ci_no_checks_pr_head_pending_returns_pending(mocker):
    """Candidate has no check-runs; PR head CI still in progress → pending (not-green)."""
    pr_head_sha = "pr_head_pending01"

    async def _check_runs_side_effect(token, owner, repo, sha, **kwargs):
        if sha == "candidate_pend":
            return {"status": "no_checks", "conclusion": None, "details": []}
        return {"status": "in_progress", "conclusion": None, "details": []}

    mocker.patch(
        "app.github.get_check_runs_cached", side_effect=_check_runs_side_effect
    )
    mocker.patch(
        "app.github.list_pull_requests_for_commit",
        new=AsyncMock(
            return_value=[
                {
                    "number": 8,
                    "merged_at": "2026-01-01T00:00:00Z",
                    "head": {"sha": pr_head_sha},
                }
            ]
        ),
    )
    result = await rq._resolve_candidate_ci("tok", "owner", "repo", "candidate_pend")
    assert result["ci_green"] is False
    assert result["state"] == rq._CI_PENDING


@pytest.mark.asyncio
async def test_resolve_candidate_ci_no_checks_pr_heads_all_absent_returns_no_checks(
    mocker,
):
    """Every candidate/PR-head probe cleanly absent → definitive no_checks."""
    pr_head_sha = "pr_head_absent01"

    mocker.patch(
        "app.github.get_check_runs_cached",
        new=AsyncMock(
            return_value={"status": "no_checks", "conclusion": None, "details": []}
        ),
    )
    mocker.patch(
        "app.github.list_pull_requests_for_commit",
        new=AsyncMock(
            return_value=[
                {
                    "number": 9,
                    "merged_at": "2026-01-01T00:00:00Z",
                    "head": {"sha": pr_head_sha},
                }
            ]
        ),
    )

    result = await rq._resolve_candidate_ci("tok", "owner", "repo", "candidate_abs")

    assert result["ci_green"] is False
    assert result["state"] == rq._CI_NO_CHECKS
    assert result["resolved_sha"] is None


@pytest.mark.asyncio
async def test_resolve_candidate_ci_no_checks_pr_head_fetch_error_returns_pending(
    mocker,
):
    """A PR-head I/O failure is degraded/pending, not definitive absence."""
    pr_head_sha = "pr_head_error01"

    async def _check_runs_side_effect(token, owner, repo, sha, **kwargs):
        if sha == "candidate_err":
            return {"status": "no_checks", "conclusion": None, "details": []}
        raise RuntimeError("check-runs degraded")

    mocker.patch(
        "app.github.get_check_runs_cached", side_effect=_check_runs_side_effect
    )
    mocker.patch(
        "app.github.list_pull_requests_for_commit",
        new=AsyncMock(
            return_value=[
                {
                    "number": 12,
                    "merged_at": "2026-01-01T00:00:00Z",
                    "head": {"sha": pr_head_sha},
                }
            ]
        ),
    )

    result = await rq._resolve_candidate_ci("tok", "owner", "repo", "candidate_err")

    assert result["ci_green"] is False
    assert result["state"] == rq._CI_PENDING
    assert "fetch failed" in result["detail"]


@pytest.mark.asyncio
async def test_resolve_candidate_ci_red_beats_green_across_multiple_pr_heads(mocker):
    """With multiple PR heads, a red result on any head must win over a green on
    another head — regardless of which order GitHub returns them.  The resolver
    is exhaustive: it inspects all PR heads and returns red if any is red."""
    green_sha = "pr_head_green01"
    red_sha = "pr_head_red0002"

    async def _check_runs_side_effect(token, owner, repo, sha, **kwargs):
        if sha == "candidate_multi":
            return {"status": "no_checks", "conclusion": None, "details": []}
        if sha == green_sha:
            return {"status": "completed", "conclusion": "success", "details": []}
        if sha == red_sha:
            return {"status": "completed", "conclusion": "failure", "details": []}
        return {"status": "no_checks", "conclusion": None, "details": []}

    mocker.patch(
        "app.github.get_check_runs_cached", side_effect=_check_runs_side_effect
    )
    # Green head is listed FIRST — the resolver must not short-circuit on it.
    mocker.patch(
        "app.github.list_pull_requests_for_commit",
        new=AsyncMock(
            return_value=[
                {
                    "number": 10,
                    "merged_at": "2026-01-01T00:00:00Z",
                    "head": {"sha": green_sha},
                },
                {
                    "number": 11,
                    "merged_at": "2026-01-02T00:00:00Z",
                    "head": {"sha": red_sha},
                },
            ]
        ),
    )
    result = await rq._resolve_candidate_ci("tok", "owner", "repo", "candidate_multi")
    # Red must win despite being listed after the green PR.
    assert result["ci_green"] is False
    assert result["state"] == rq._CI_RED
    assert result["resolved_sha"] == red_sha


async def test_resolve_candidate_ci_caps_pr_head_sweep(mocker, caplog):
    """When a commit has more than _PR_HEAD_SWEEP_MAX associated PR heads, the
    sweep is capped at _PR_HEAD_SWEEP_MAX to avoid exhausting API quota.
    A warning is logged when truncation occurs."""
    import logging

    caplog.set_level(logging.WARNING)

    check_runs_call_count = 0

    async def _check_runs_side_effect(token, owner, repo, sha, **kwargs):
        nonlocal check_runs_call_count
        if sha == "candidate_many":
            return {"status": "no_checks", "conclusion": None, "details": []}
        # Count calls to get_check_runs_cached for PR heads (not the candidate).
        check_runs_call_count += 1
        return {"status": "completed", "conclusion": "success", "details": []}

    mocker.patch(
        "app.github.get_check_runs_cached", side_effect=_check_runs_side_effect
    )

    # Create more than _PR_HEAD_SWEEP_MAX PR heads.
    num_prs = rq._PR_HEAD_SWEEP_MAX + 3
    pr_heads = [
        {
            "number": 100 + i,
            "merged_at": f"2026-01-{(i % 28) + 1:02d}T00:00:00Z",
            "head": {"sha": f"pr_head_{i:03d}"},
        }
        for i in range(num_prs)
    ]

    mocker.patch(
        "app.github.list_pull_requests_for_commit",
        new=AsyncMock(return_value=pr_heads),
    )

    await rq._resolve_candidate_ci("tok", "owner", "repo", "candidate_many")

    # Verify that only _PR_HEAD_SWEEP_MAX check-runs fetches were made.
    assert check_runs_call_count == rq._PR_HEAD_SWEEP_MAX
    # Verify a truncation warning was logged.
    assert any(
        "truncated" in record.message.lower()
        and str(num_prs) in record.message
        and str(rq._PR_HEAD_SWEEP_MAX) in record.message
        for record in caplog.records
        if "CI resolution" in record.message
    )


# ===========================================================================
# run_qualification — phase 1: guards + snapshot + spawn
# ===========================================================================


@pytest.mark.asyncio
async def test_run_qualification_never_spawns_retired_smoke_when_flag_is_true(
    db_session, mocker
):
    """The legacy shared smoke flag cannot revive qualifier VM smoke.

    Qualification still snapshots the SHA and spawns its release audit, while
    persisting explicit false/None compatibility fields for old readers.
    """
    from app import shared_state

    shared_state.runtime_config["smoketest_enabled"] = True

    project, version = await _make_project_and_version(db_session, phase="stabilize")

    candidate = "cafe1234" * 5  # 40-char placeholder SHA
    mocker.patch(
        "app.github.get_repo_installation_token",
        new=AsyncMock(return_value="gh-token"),
    )
    mocker.patch(
        "app.github.get_branch_sha",
        new=AsyncMock(return_value=candidate),
    )
    mocker.patch(
        "app.github.get_check_runs_cached",
        new=AsyncMock(return_value={"status": "completed", "conclusion": "success"}),
    )

    result = await rq.run_qualification(
        db_session, project.id, version, explicit_request=True
    )
    await db_session.commit()

    assert result["status"] == "spawned"
    assert result["ci_green"] is True
    assert result["candidate_sha"] == candidate
    assert result["smoketest_enabled"] is False

    # last_qualification stamped pending, attempts bumped.
    await db_session.refresh(version)
    assert version.last_qualification["status"] == "pending"
    assert version.last_qualification["candidate_sha"] == candidate
    assert version.last_qualification["ci_green"] is True
    assert version.last_qualification["smoketest_enabled"] is False
    assert version.qualification_attempts == 1

    # Only release_audit exists; the binding challenge is spawned later once
    # audit findings exist. No runtime flag can create qualifier VM smoke.
    from sqlmodel import select

    tasks = (
        (await db_session.execute(select(Task).where(Task.project_id == project.id)))
        .scalars()
        .all()
    )
    types = sorted((t.first_payload or {}).get("type") for t in tasks)
    assert types == ["release_audit"]

    by_type = {(t.first_payload or {}).get("type"): t for t in tasks}
    audit_task = by_type["release_audit"]

    assert audit_task.first_payload["base_branch"] == "main"
    assert audit_task.first_payload["diff_range"] == f"main...{candidate}"
    assert audit_task.first_payload["candidate_sha"] == candidate
    assert audit_task.context["release_version_id"] == str(version.id)
    assert audit_task.context["scope"] == "medium"
    assert version.last_qualification["audit_task_id"] == str(audit_task.id)
    assert version.last_qualification["challenge_task_id"] is None
    assert version.last_qualification["audit_challenge_task_id"] is None
    assert version.last_qualification["audit_challenge_spawn_pending"] is True

    from datetime import datetime, timezone

    from app import reaper

    hb_cutoff = reaper._heartbeat_stale_cutoff(
        audit_task, datetime(2026, 1, 1, tzinfo=timezone.utc)
    )
    assert (
        datetime(2026, 1, 1, tzinfo=timezone.utc) - hb_cutoff
    ).total_seconds() == 420

    assert version.last_qualification["smoketest_task_id"] is None


@pytest.mark.asyncio
async def test_run_qualification_skips_smoketest_when_disabled(db_session, mocker):
    """Audit H3: with smoketest_enabled=False and env_preflight_enabled=False,
    only the release_audit child is spawned — NO qualifier-smoketest task —
    and last_qualification records smoketest_enabled=False with
    smoketest_task_id None."""
    from app import shared_state

    # Explicit for clarity — this is also the autouse-reset default.
    shared_state.runtime_config["smoketest_enabled"] = False
    shared_state.runtime_config["env_preflight_enabled"] = False

    project, version = await _make_project_and_version(db_session, phase="stabilize")

    candidate = "d15ab1ed" * 5  # 40-char placeholder SHA
    mocker.patch(
        "app.github.get_repo_installation_token",
        new=AsyncMock(return_value="gh-token"),
    )
    mocker.patch(
        "app.github.get_branch_sha",
        new=AsyncMock(return_value=candidate),
    )
    mocker.patch(
        "app.github.get_check_runs_cached",
        new=AsyncMock(return_value={"conclusion": "success"}),
    )

    result = await rq.run_qualification(
        db_session, project.id, version, explicit_request=True
    )
    await db_session.commit()

    assert result["status"] == "spawned"
    assert result["smoketest_enabled"] is False
    assert result["smoketest_task_id"] is None

    await db_session.refresh(version)
    assert version.last_qualification["smoketest_enabled"] is False
    assert version.last_qualification["smoketest_task_id"] is None
    assert version.last_qualification["audit_task_id"] is not None
    assert version.last_qualification["audit_challenge_task_id"] is None
    assert version.last_qualification["audit_challenge_spawn_pending"] is True

    # Exactly one child task — release_audit — and NO smoketest or challenge.
    from sqlmodel import select

    tasks = (
        (await db_session.execute(select(Task).where(Task.project_id == project.id)))
        .scalars()
        .all()
    )
    types = sorted((t.first_payload or {}).get("type") for t in tasks)
    assert types == ["release_audit"]
    by_type = {(t.first_payload or {}).get("type"): t for t in tasks}
    assert by_type["release_audit"].first_payload["candidate_sha"] == candidate
    assert by_type["release_audit"].first_payload["diff_range"] == f"main...{candidate}"


@pytest.mark.asyncio
async def test_run_qualification_produces_env_preflight_evidence_unconditionally(
    db_session, mocker
):
    """Env-preflight evidence is produced unconditionally during
    run_qualification *even when both* smoketest_enabled and
    env_preflight_enabled are False.

    This decouples evidence production from the smoketest child task (the
    grader gap that motived direction c-277fccb6).  The evidence is a cheap
    pure-content introspection of the candidate tree done via the GitHub
    contents API, so it must not depend on any feature flag.
    """
    from app import shared_state
    from pathlib import Path

    shared_state.runtime_config["smoketest_enabled"] = False
    shared_state.runtime_config["env_preflight_enabled"] = False

    project, version = await _make_project_and_version(db_session, phase="stabilize")
    candidate = "a1b2c3d4" * 5

    mocker.patch(
        "app.github.get_repo_installation_token",
        new=AsyncMock(return_value="gh-token"),
    )
    mocker.patch(
        "app.github.get_branch_sha",
        new=AsyncMock(return_value=candidate),
    )
    mocker.patch(
        "app.github.get_check_runs_cached",
        new=AsyncMock(return_value={"conclusion": "success"}),
    )

    # Serve the candidate tree's real source files so the evidence builder
    # produces meaningful content.
    repo_root = Path(__file__).resolve().parent.parent  # backend/
    required_env_py = (repo_root / "app" / "required_runtime_env.py").read_text()
    smoketest_yaml = (repo_root / "testing" / "smoketest.yaml").read_text()

    async def _mock_file_text(path, *, token, owner, repo, ref, **kw):
        if path == "backend/app/required_runtime_env.py":
            return required_env_py
        if path == "backend/testing/smoketest.yaml":
            return smoketest_yaml
        return None

    mocker.patch(
        "app.github.get_file_text_at_ref",
        new=AsyncMock(side_effect=_mock_file_text),
    )

    result = await rq.run_qualification(
        db_session, project.id, version, explicit_request=True
    )
    await db_session.commit()

    assert result["status"] == "spawned"

    await db_session.refresh(version)
    qual = version.last_qualification

    # Evidence is present regardless of both flags being False.
    assert "env_preflight_evidence" in qual, (
        "env_preflight_evidence must be on the qual record when both flags are off"
    )
    evidence = qual["env_preflight_evidence"]
    assert isinstance(evidence, dict), f"Expected dict, got {type(evidence)}"
    assert "required_env" in evidence
    assert "startup_check_names" in evidence
    assert evidence.get("manifest_source") == "backend/app/required_runtime_env.py"

    # The evidence must reflect the current REQUIRED_RUNTIME_ENV manifest.
    from app.required_runtime_env import REQUIRED_RUNTIME_ENV

    expected_names = {r.name for r in REQUIRED_RUNTIME_ENV}
    actual_names = {e["name"] for e in evidence["required_env"]}
    assert expected_names == actual_names, (
        f"Evidence required_env {actual_names} does not match "
        f"current REQUIRED_RUNTIME_ENV {expected_names} — "
        "the evidence builder did not read the candidate tree correctly"
    )


@pytest.mark.asyncio
async def test_env_preflight_evidence_rebuilt_on_full_dimension_reuse(
    db_session, mocker
):
    """When all expensive gate dimensions (audit, test_erosion, smoketest,
    merge_grade) are already resolved in the QualificationDimensionResult ledger
    at the same candidate_sha, _build_env_preflight_evidence_for_candidate is
    still called and env_preflight_evidence is a non-None dict in the resulting
    last_qualification record.

    This locks the fail-closed boot gate against a future reuse-side
    short-circuit that would skip env_preflight evidence collection when every
    ledger dimension is already resolved — the rebuild must always run.
    """
    from app import shared_state
    from pathlib import Path

    shared_state.runtime_config["smoketest_enabled"] = False
    shared_state.runtime_config["env_preflight_enabled"] = False

    project, version = await _make_project_and_version(db_session, phase="stabilize")
    candidate = "deadbeef" * 5  # 40-char placeholder SHA

    # Seed all four reuse-eligible dimensions as already resolved so that
    # run_qualification will carry them forward from the ledger and skip
    # re-spawning any child task.
    for dim in ("audit", "test_erosion", "smoketest", "merge_grade"):
        await _seed_dimension_row(
            db_session, project, version, dim,
            status="passed", candidate_sha=candidate,
        )
    await db_session.commit()

    # Stamp a prior infra_failure qual at the same sha so the reuse path fires.
    prior_attempt_id = uuid.uuid4()
    version.last_qualification = {
        "status": "infra_failure",
        "qualification_attempt_id": str(prior_attempt_id),
        "retry_episode": 0,
        "candidate_sha": candidate,
        "error": "smoketest_task_failed",
        "consecutive_infra_errors": 0,
        "started_at": "2026-01-01T00:00:00",
        "finalized_at": "2026-01-01T00:01:00",
    }
    version.active_qualification_attempt_id = prior_attempt_id
    version.qualification_attempts = 1
    db_session.add(version)
    await db_session.commit()
    await db_session.refresh(version)

    mocker.patch(
        "app.github.get_repo_installation_token",
        new=AsyncMock(return_value="gh-token"),
    )
    mocker.patch(
        "app.github.get_branch_sha",
        new=AsyncMock(return_value=candidate),
    )
    mocker.patch(
        "app.github.get_check_runs_cached",
        new=AsyncMock(return_value={"conclusion": "success"}),
    )
    mocker.patch(
        "app.release_qualifier.compute_audited_surface_hash",
        return_value="hash-deadbeef",
    )

    # Serve real file content so the evidence builder produces a non-None dict.
    repo_root = Path(__file__).resolve().parent.parent
    required_env_py = (repo_root / "app" / "required_runtime_env.py").read_text()
    smoketest_yaml = (repo_root / "testing" / "smoketest.yaml").read_text()

    async def _mock_file_text(path, *, token, owner, repo, ref, **kw):
        if path == "backend/app/required_runtime_env.py":
            return required_env_py
        if path == "backend/testing/smoketest.yaml":
            return smoketest_yaml
        return None

    mocker.patch(
        "app.github.get_file_text_at_ref",
        new=AsyncMock(side_effect=_mock_file_text),
    )

    # Spy on the evidence builder to confirm it is invoked even when all
    # expensive gate dimensions are already resolved in the ledger.
    evidence_spy = mocker.spy(rq, "_build_env_preflight_evidence_for_candidate")

    result = await rq.run_qualification(
        db_session, project.id, version, explicit_request=True
    )
    await db_session.commit()

    assert result["status"] == "spawned", (
        f"Expected 'spawned' but got {result['status']!r}"
    )

    await db_session.refresh(version)
    qual = version.last_qualification

    # Core assertion: the evidence builder must have been called exactly once
    # regardless of full ledger reuse.
    evidence_spy.assert_awaited_once(), (
        "_build_env_preflight_evidence_for_candidate was NOT called — "
        "a reuse-side short-circuit skipped the fail-closed boot gate"
    )

    # The evidence must be present and non-None in the pending record.
    assert "env_preflight_evidence" in qual, (
        "env_preflight_evidence key missing from last_qualification on full-reuse re-drive"
    )
    evidence = qual["env_preflight_evidence"]
    assert isinstance(evidence, dict), (
        f"env_preflight_evidence should be a dict; got {type(evidence)}"
    )
    assert "required_env" in evidence, (
        "env_preflight_evidence missing 'required_env' key — builder did not run"
    )


@pytest.mark.asyncio
async def test_env_preflight_dropped_from_reuse_on_same_sha_redrive(
    db_session, mocker
):
    """Executed same-SHA blocked→refresh regression for env_preflight.

    env_preflight is environment-dependent (mutable): a stale BLOCKED ledger row
    from a prior attempt must NOT be carried forward as reused on a same-candidate
    re-drive — otherwise a boot_prereq failure would remain reused even after the
    operator repaired the deploy environment.  Truly immutable dimensions
    (audit/test_erosion/smoketest/merge_grade) must still be reused.

    Observable proof: ``last_qualification["resolved_dimensions"]`` (the reuse set
    written at spawn) must include the immutable gates but exclude env_preflight,
    forcing env_preflight to be re-measured live this drive.
    """
    from app import shared_state
    from pathlib import Path

    shared_state.runtime_config["smoketest_enabled"] = False
    shared_state.runtime_config["env_preflight_enabled"] = False

    project, version = await _make_project_and_version(db_session, phase="stabilize")
    candidate = "deadbeef" * 5  # 40-char placeholder SHA

    # Immutable gates resolved+passed in the ledger → carried forward as reused.
    for dim in ("audit", "test_erosion", "smoketest", "merge_grade"):
        await _seed_dimension_row(
            db_session, project, version, dim,
            status="passed", candidate_sha=candidate,
        )
    # A stale BLOCKED env_preflight row from a prior attempt (the environment was
    # broken then; the operator has since repaired it).
    await _seed_dimension_row(
        db_session, project, version, "env_preflight",
        status="blocked", blocker="boot_prereq_missing", candidate_sha=candidate,
    )
    await db_session.commit()

    prior_attempt_id = uuid.uuid4()
    version.last_qualification = {
        "status": "infra_failure",
        "qualification_attempt_id": str(prior_attempt_id),
        "retry_episode": 0,
        "candidate_sha": candidate,
        "error": "smoketest_task_failed",
        "consecutive_infra_errors": 0,
        "started_at": "2026-01-01T00:00:00",
        "finalized_at": "2026-01-01T00:01:00",
    }
    version.active_qualification_attempt_id = prior_attempt_id
    version.qualification_attempts = 1
    db_session.add(version)
    await db_session.commit()
    await db_session.refresh(version)

    mocker.patch(
        "app.github.get_repo_installation_token",
        new=AsyncMock(return_value="gh-token"),
    )
    mocker.patch(
        "app.github.get_branch_sha",
        new=AsyncMock(return_value=candidate),
    )
    mocker.patch(
        "app.github.get_check_runs_cached",
        new=AsyncMock(return_value={"conclusion": "success"}),
    )
    mocker.patch(
        "app.release_qualifier.compute_audited_surface_hash",
        return_value="hash-deadbeef",
    )

    repo_root = Path(__file__).resolve().parent.parent
    required_env_py = (repo_root / "app" / "required_runtime_env.py").read_text()
    smoketest_yaml = (repo_root / "testing" / "smoketest.yaml").read_text()

    async def _mock_file_text(path, *, token, owner, repo, ref, **kw):
        if path == "backend/app/required_runtime_env.py":
            return required_env_py
        if path == "backend/testing/smoketest.yaml":
            return smoketest_yaml
        return None

    mocker.patch(
        "app.github.get_file_text_at_ref",
        new=AsyncMock(side_effect=_mock_file_text),
    )

    result = await rq.run_qualification(
        db_session, project.id, version, explicit_request=True
    )
    await db_session.commit()

    assert result["status"] == "spawned", (
        f"Expected 'spawned' but got {result['status']!r}"
    )

    await db_session.refresh(version)
    resolved = version.last_qualification.get("resolved_dimensions") or {}

    # env_preflight (mutable) must be dropped from the reuse set → re-measured.
    assert "env_preflight" not in resolved, (
        "stale blocked env_preflight ledger row must NOT be carried forward as "
        "reused on a same-SHA re-drive; it must be re-measured after an env repair"
    )
    # Truly immutable gates must still be reused (candidate-stable reuse preserved).
    assert "audit" in resolved and "test_erosion" in resolved, (
        "immutable dimension reuse must be preserved for candidate-stable gates"
    )


@pytest.mark.asyncio
async def test_run_qualification_does_not_spawn_smoketest_for_env_preflight_only(
    db_session, mocker
):
    """Audit H3 / boot-gate: with smoketest_enabled=False but env_preflight_enabled=True,
    run_qualification preserves the cheap env check without spawning a VM-backed
    smoketest child."""
    from app import shared_state

    shared_state.runtime_config["smoketest_enabled"] = False
    shared_state.runtime_config["env_preflight_enabled"] = True

    project, version = await _make_project_and_version(db_session, phase="stabilize")

    candidate = "babe5678" * 5  # 40-char placeholder SHA
    mocker.patch(
        "app.github.get_repo_installation_token",
        new=AsyncMock(return_value="gh-token"),
    )
    mocker.patch(
        "app.github.get_branch_sha",
        new=AsyncMock(return_value=candidate),
    )
    mocker.patch(
        "app.github.get_check_runs_cached",
        new=AsyncMock(return_value={"status": "completed", "conclusion": "success"}),
    )

    result = await rq.run_qualification(
        db_session, project.id, version, explicit_request=True
    )
    await db_session.commit()

    assert result["status"] == "spawned"
    assert result["smoketest_enabled"] is False
    assert result["env_preflight_enabled"] is True
    assert result["smoketest_task_id"] is None

    await db_session.refresh(version)
    assert version.last_qualification["smoketest_enabled"] is False
    assert version.last_qualification["env_preflight_enabled"] is True
    assert version.last_qualification["smoketest_task_id"] is None

    from sqlmodel import select

    tasks = (
        (await db_session.execute(select(Task).where(Task.project_id == project.id)))
        .scalars()
        .all()
    )
    types = sorted((t.first_payload or {}).get("type") for t in tasks)
    assert types == ["release_audit"]


@pytest.mark.asyncio
async def test_run_qualification_records_ci_red(db_session, mocker):
    """A red CI conclusion is recorded as ci_green=False (not acted on here —
    the composite applies it as a hard blocker at finalize time)."""
    project, version = await _make_project_and_version(db_session, phase="stabilize")

    mocker.patch(
        "app.github.get_repo_installation_token",
        new=AsyncMock(return_value="gh-token"),
    )
    mocker.patch(
        "app.github.get_branch_sha",
        new=AsyncMock(return_value="dead" * 10),
    )
    # The real get_check_runs_cached always emits ``status`` alongside
    # ``conclusion`` (a "failure" conclusion is only ever produced with
    # status="completed" — see github_checks.get_check_runs). Without the
    # status key the prod classifier (_classify_check_runs) falls through to
    # _CI_PENDING, so the old {"conclusion": "failure"} mock passed for the
    # wrong reason (ci_green is False under BOTH pending and red). The faithful
    # shape exercises the actual red-build branch.
    mocker.patch(
        "app.github.get_check_runs_cached",
        new=AsyncMock(return_value={"status": "completed", "conclusion": "failure"}),
    )

    result = await rq.run_qualification(
        db_session, project.id, version, explicit_request=True
    )
    await db_session.commit()

    assert result["status"] == "spawned"
    assert result["ci_green"] is False
    assert result["ci_state"] == "red"
    await db_session.refresh(version)
    assert version.last_qualification["ci_green"] is False
    assert version.last_qualification["ci_state"] == "red"


@pytest.mark.asyncio
async def test_run_qualification_spawns_one_holistic_audit_when_goal_set(
    db_session, mocker
):
    """The single release audit carries the frozen goal and rubric contract."""
    from app import shared_state

    shared_state.runtime_config["smoketest_enabled"] = False

    project, version = await _make_project_and_version(db_session, phase="stabilize")
    version.goal = "deliver user analytics dashboard"
    db_session.add(version)
    await db_session.commit()
    await db_session.refresh(version)
    await _add_version_criterion(
        db_session,
        project,
        version,
        key="c-analytics-ui",
        text="Render analytics for the operator",
    )

    candidate = "feed1234" * 5
    mocker.patch(
        "app.github.get_repo_installation_token",
        new=AsyncMock(return_value="gh-token"),
    )
    mocker.patch(
        "app.github.get_branch_sha",
        new=AsyncMock(return_value=candidate),
    )
    mocker.patch(
        "app.github.get_check_runs_cached",
        new=AsyncMock(return_value={"status": "completed", "conclusion": "success"}),
    )

    result = await rq.run_qualification(
        db_session, project.id, version, explicit_request=True
    )
    await db_session.commit()

    assert result["status"] == "spawned"

    await db_session.refresh(version)
    lq = version.last_qualification
    assert lq["merge_grade_task_id"] is None

    from sqlmodel import select

    tasks = (
        (await db_session.execute(select(Task).where(Task.project_id == project.id)))
        .scalars()
        .all()
    )
    task_types = sorted((t.first_payload or {}).get("type") for t in tasks)
    assert task_types == ["release_audit"]

    audit_task = next(
        t for t in tasks if (t.first_payload or {}).get("type") == "release_audit"
    )
    payload = audit_task.first_payload
    # The release decision remains blind to mutable progress narration.
    assert not any("running_update" in str(k).lower() for k in payload), (
        "release_audit payload must be BLIND to running_update"
    )
    assert payload["candidate_sha"] == candidate
    assert payload["version_goal"] == "deliver user analytics dashboard"
    assert lq["rubric_snapshot"] == [
        {
            "criterion_key": "c-analytics-ui",
            "text": "Render analytics for the operator",
        },
    ]
    assert payload["categories"] == [
        {"category_key": row["criterion_key"], "text": row["text"]}
        for row in lq["rubric_snapshot"]
    ]
    assert payload["rubric_fingerprint"] == lq["rubric_fingerprint"]
    assert uuid.UUID(lq["qualification_attempt_id"])
    assert payload["qualification_attempt_id"] == lq["qualification_attempt_id"]
    assert lq["rubric_provenance"]["qualification_attempt_id"] == lq[
        "qualification_attempt_id"
    ]
    assert lq["rubric_fingerprint"] == rq._rubric_fingerprint(
        lq["rubric_snapshot"]
    )
    # Regression guard: promotion judgment must be pinned to Opus so a future
    # cost-saving runtime-config edit cannot silently downgrade it.
    assert payload.get("quality_tier") == "opus", (
        f"release_merge_grade must pin quality_tier=opus; got {payload.get('quality_tier')!r}"
    )


@pytest.mark.parametrize(
    ("lifecycle_change", "expected_change"),
    [
        ("add", "added"),
        ("reinstate", "reinstated"),
        ("retract", "retracted"),
        ("supersede", "superseded"),
        ("change", "changed"),
    ],
)
@pytest.mark.asyncio
async def test_in_flight_rubric_lifecycle_changes_require_fresh_grade(
    db_session, mocker, lifecycle_change, expected_change
):
    from app import shared_state

    shared_state.runtime_config["smoketest_enabled"] = False
    shared_state.runtime_config["promotion_enabled"] = True
    project, version = await _make_project_and_version(
        db_session, phase="stabilize"
    )
    version.goal = "Ship a rubric-bound qualification"
    db_session.add(version)
    await db_session.commit()
    await db_session.refresh(version)

    await _add_version_criterion(
        db_session,
        project,
        version,
        key="c-stable",
        text="Stable acceptance requirement",
    )
    mutable = await _add_version_criterion(
        db_session,
        project,
        version,
        key="c-lifecycle",
        text="Lifecycle acceptance requirement",
    )
    if lifecycle_change in {"add", "reinstate"}:
        mutable.status = "retracted"
        db_session.add(mutable)
        await db_session.commit()
        await db_session.refresh(mutable)

    candidate = "a11ce123" * 5
    mocker.patch(
        "app.github.get_repo_installation_token",
        new=AsyncMock(return_value="gh-token"),
    )
    mocker.patch("app.github.get_branch_sha", new=AsyncMock(return_value=candidate))
    mocker.patch(
        "app.github.get_check_runs_cached",
        new=AsyncMock(return_value={"status": "completed", "conclusion": "success"}),
    )
    promote_spy = mocker.patch("app.release_qualifier.promote", new=AsyncMock())

    launched = await rq.run_qualification(
        db_session, project.id, version, explicit_request=True
    )
    await db_session.commit()
    assert launched["status"] == "spawned"
    await db_session.refresh(version)
    first_task_id = version.last_qualification["audit_task_id"]
    first_fingerprint = version.last_qualification["rubric_fingerprint"]
    first_attempt_id = version.last_qualification["qualification_attempt_id"]
    assert uuid.UUID(first_attempt_id)

    tasks = (
        (await db_session.execute(select(Task).where(Task.project_id == project.id)))
        .scalars()
        .all()
    )
    for task in tasks:
        task.status = TaskStatus.COMPLETED
        db_session.add(task)

    if lifecycle_change == "add":
        await _add_version_criterion(
            db_session,
            project,
            version,
            key="c-added",
            text="Added while grading",
            sync_pending_rubric=False,
        )
    elif lifecycle_change == "reinstate":
        mutable.status = "active"
        db_session.add(mutable)
    elif lifecycle_change == "retract":
        mutable.status = "retracted"
        db_session.add(mutable)
    elif lifecycle_change == "supersede":
        mutable.status = "superseded"
        db_session.add(mutable)
    else:
        mutable.text = "Materially changed acceptance requirement"
        db_session.add(mutable)
    await db_session.commit()
    await db_session.refresh(version)

    result = await rq._finalize_qualification(
        db_session,
        version,
        _pre_gates={
            "ci_green": True,
            "ci_resolution": {"state": "green", "resolved_sha": candidate},
        },
    )
    await db_session.commit()

    assert result["status"] == "rubric_changed"
    assert result["blocker"] == "rubric_changed"
    assert expected_change in {
        change["change"] for change in result["rubric_drift"]["changes"]
    }
    promote_spy.assert_not_called()
    await db_session.refresh(version)
    assert version.last_qualification["category_verdicts"] is None
    assert version.last_qualification["rubric_fingerprint"] == first_fingerprint
    verdict = (
        await db_session.execute(
            select(QualificationVerdict)
            .where(QualificationVerdict.release_version_id == version.id)
            .order_by(QualificationVerdict.created_at.desc())
        )
    ).scalars().first()
    assert verdict is not None
    assert verdict.terminal_status == "rubric_changed"
    assert verdict.blocker == "rubric_changed"
    assert verdict.merge_grade_detail["rubric_drift"]["total_changes"] >= 1

    redrive = await rq.run_qualification(
        db_session, project.id, version, explicit_request=True
    )
    await db_session.commit()
    assert redrive["status"] == "spawned"
    await db_session.refresh(version)
    assert version.last_qualification["audit_task_id"] != first_task_id
    assert version.last_qualification["rubric_fingerprint"] != first_fingerprint
    assert uuid.UUID(version.last_qualification["qualification_attempt_id"])
    assert version.last_qualification["qualification_attempt_id"] != first_attempt_id


def test_rubric_dimension_reuse_requires_both_attempt_ids():
    detail = {
        "rubric_provenance": {
            "fingerprint": "same-rubric",
            "qualification_attempt_id": "prior-attempt",
        }
    }

    assert not rq._rubric_dimension_reusable(
        detail, fingerprint="same-rubric", qualification_attempt_id=None
    )
    assert not rq._rubric_dimension_reusable(
        {"rubric_provenance": {"fingerprint": "same-rubric"}},
        fingerprint="same-rubric",
        qualification_attempt_id="current-attempt",
    )
    assert rq._rubric_dimension_reusable(
        detail, fingerprint="same-rubric", qualification_attempt_id="prior-attempt"
    )


@pytest.mark.asyncio
async def test_run_qualification_no_merge_grade_child_when_goal_empty(
    db_session, mocker
):
    """When version.goal is empty, run_qualification skips the merge_grade child
    and records merge_grade_task_id=None (gate is a no-op PASS at finalize time).
    """
    from app import shared_state

    shared_state.runtime_config["smoketest_enabled"] = False

    project, version = await _make_project_and_version(db_session, phase="stabilize")
    # version.goal defaults to None/empty — no merge grade child should spawn.

    candidate = "abc0" * 10
    mocker.patch(
        "app.github.get_repo_installation_token",
        new=AsyncMock(return_value="gh-token"),
    )
    mocker.patch(
        "app.github.get_branch_sha",
        new=AsyncMock(return_value=candidate),
    )
    mocker.patch(
        "app.github.get_check_runs_cached",
        new=AsyncMock(return_value={"status": "completed", "conclusion": "success"}),
    )

    result = await rq.run_qualification(
        db_session, project.id, version, explicit_request=True
    )
    await db_session.commit()

    assert result["status"] == "spawned"

    await db_session.refresh(version)
    assert version.last_qualification.get("merge_grade_task_id") is None

    from sqlmodel import select

    tasks = (
        (await db_session.execute(select(Task).where(Task.project_id == project.id)))
        .scalars()
        .all()
    )
    task_types = [(t.first_payload or {}).get("type") for t in tasks]
    assert "release_merge_grade" not in task_types, (
        "no merge_grade child should be spawned when goal is empty"
    )


@pytest.mark.asyncio
async def test_run_qualification_no_merge_grade_child_when_goal_contentless(
    db_session, mocker
):
    """When version.goal is a contentless placeholder ('test'), run_qualification
    skips the merge_grade child — treats contentless == empty.  This prevents
    placeholder goals from hard-blocking promotion (e.g. v18 wedge via goal='test').
    """
    from app import shared_state

    shared_state.runtime_config["smoketest_enabled"] = False

    project, version = await _make_project_and_version(db_session, phase="stabilize")
    # Set a contentless placeholder goal — should behave the same as no goal.
    version.goal = "test"
    db_session.add(version)
    await db_session.commit()

    candidate = "aaaa" * 10
    mocker.patch(
        "app.github.get_repo_installation_token",
        new=AsyncMock(return_value="gh-token"),
    )
    mocker.patch(
        "app.github.get_branch_sha",
        new=AsyncMock(return_value=candidate),
    )
    mocker.patch(
        "app.github.get_check_runs_cached",
        new=AsyncMock(return_value={"status": "completed", "conclusion": "success"}),
    )

    result = await rq.run_qualification(
        db_session, project.id, version, explicit_request=True
    )
    await db_session.commit()

    assert result["status"] == "spawned"

    await db_session.refresh(version)
    assert version.last_qualification.get("merge_grade_task_id") is None, (
        "contentless goal should be treated as empty — no merge_grade child"
    )

    from sqlmodel import select

    tasks = (
        (await db_session.execute(select(Task).where(Task.project_id == project.id)))
        .scalars()
        .all()
    )
    task_types = [(t.first_payload or {}).get("type") for t in tasks]
    assert "release_merge_grade" not in task_types, (
        "no merge_grade child should be spawned when goal is a contentless placeholder"
    )


@pytest.mark.asyncio
async def test_run_qualification_no_credentials(db_session, mocker):
    """With no resolvable GitHub token, run_qualification records an error and
    does not spawn children."""
    project = Project(name="RQ-NoCreds", user_id="rq-user")  # no github_owner/repo
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)
    version = await rv.open_version(db_session, project.id, version_number=1)
    rv.set_version_phase(version, "stabilize", reasoning="test setup")
    db_session.add(version)
    await db_session.commit()
    await db_session.refresh(version)

    result = await rq.run_qualification(
        db_session, project.id, version, explicit_request=True
    )
    await db_session.commit()

    assert result["status"] == "no_credentials"
    await db_session.refresh(version)
    assert version.last_qualification["status"] == "error"
    assert version.last_qualification["error"] == "no_credentials"
    assert version.qualification_attempts == 1

    # No child tasks spawned.
    from sqlmodel import select

    tasks = (
        (await db_session.execute(select(Task).where(Task.project_id == project.id)))
        .scalars()
        .all()
    )
    assert len(tasks) == 0


# ===========================================================================
# run_qualification — pre-spawn migration-identity collision gate
# ===========================================================================


@pytest.mark.asyncio
async def test_dual_head_collision_blocks_before_attempt_spend(db_session, mocker):
    """A candidate whose migration file list contains a duplicate revision /
    dual alembic head is BLOCKED at run_qualification time BEFORE any child is
    spawned and BEFORE qualification_attempts is incremented.

    The pre-spawn migration-identity gate short-circuits with
    status='migration_identity_collision' without touching qualification_attempts
    (so a deterministic structural defect does not consume an expensive attempt
    slot) and without creating any release_audit / qualifier_smoketest /
    release_merge_grade Task rows.
    """
    from unittest.mock import AsyncMock

    project, version = await _make_project_and_version(db_session, phase="stabilize")
    mocker.patch("app.github.get_branch_sha", new=_branch_sha_mock())

    # Simulate a duplicate-revision collision: patch the migration-identity
    # check so both the candidate and the promotion target list report a
    # shared revision '0143' that maps to DIFFERENT files (content diverges).
    mocker.patch(
        "app.release_qualifier.check_qualification_migration_identity",
        new=AsyncMock(
            return_value=(
                "block",
                "revision '0143' maps to different filenames on candidate vs main",
            )
        ),
    )

    result = await rq.run_qualification(
        db_session, project.id, version, explicit_request=True
    )
    await db_session.commit()

    # Status must indicate a pre-spawn block, not "spawned".
    assert result["status"] == "migration_identity_collision", result
    assert result["blocker"] == "migration_identity_collision"
    assert "0143" in (result.get("detail") or "")

    # qualification_attempts MUST NOT be incremented.
    await db_session.refresh(version)
    assert (version.qualification_attempts or 0) == 0, (
        "qualification_attempts should remain 0 — a structural collision "
        "must not consume an attempt slot"
    )

    # last_qualification should record the block.
    lq = version.last_qualification
    assert lq is not None
    assert lq["status"] == "not_promoted"
    assert lq["blocker"] == "migration_identity_collision"

    # NO child tasks should be created.
    from sqlmodel import select

    tasks = (
        (await db_session.execute(select(Task).where(Task.project_id == project.id)))
        .scalars()
        .all()
    )
    task_types = [(t.first_payload or {}).get("type") for t in tasks]
    assert "release_audit" not in task_types, (
        "release_audit child must NOT be spawned on a pre-spawn collision"
    )
    assert "qualifier_smoketest" not in task_types, (
        "qualifier_smoketest child must NOT be spawned on a pre-spawn collision"
    )
    assert "release_merge_grade" not in task_types, (
        "release_merge_grade child must NOT be spawned on a pre-spawn collision"
    )


@pytest.mark.asyncio
async def test_run_qualification_migration_identity_block_stamps_dimension_result(
    db_session, mocker
):
    """A pre-spawn migration-identity block upserts the migration_identity
    dimension as 'blocked' with blocker='migration_identity_collision' so
    the operator badge and downstream scoring see a consistent verdict
    regardless of whether the block was caught pre-spawn or at finalize."""
    from unittest.mock import AsyncMock

    from sqlmodel import select

    from app.models import QualificationDimensionResult

    project, version = await _make_project_and_version(db_session, phase="stabilize")
    mocker.patch("app.github.get_branch_sha", new=_branch_sha_mock())

    collision_detail = "revision '0200' maps to different files on candidate vs main"
    mocker.patch(
        "app.release_qualifier.check_qualification_migration_identity",
        new=AsyncMock(return_value=("block", collision_detail)),
    )

    await rq.run_qualification(
        db_session, project.id, version, explicit_request=True
    )
    await db_session.commit()

    # Verify the migration_identity dimension row was written.
    rows = (
        await db_session.execute(
            select(QualificationDimensionResult).where(
                QualificationDimensionResult.release_version_id == version.id,
                QualificationDimensionResult.dimension == "migration_identity",
            )
        )
    ).scalars().all()

    assert len(rows) == 1, "expected exactly one migration_identity dimension row"
    row = rows[0]
    assert row.status == "blocked"
    assert row.blocker == "migration_identity_collision"
    assert (row.detail or {}).get("execution_site") == "pre_spawn"


@pytest.mark.asyncio
async def test_run_qualification_migration_identity_deferred_no_attempt_increment(
    db_session, mocker
):
    """When the migration-identity pre-spawn check cannot fetch the migration
    list (defer), run_qualification returns early WITHOUT incrementing
    qualification_attempts and WITHOUT spawning any children."""
    from unittest.mock import AsyncMock

    project, version = await _make_project_and_version(db_session, phase="stabilize")
    mocker.patch("app.github.get_branch_sha", new=_branch_sha_mock())

    mocker.patch(
        "app.release_qualifier.check_qualification_migration_identity",
        new=AsyncMock(return_value=("defer", "GitHub API returned 503")),
    )

    result = await rq.run_qualification(
        db_session, project.id, version, explicit_request=True
    )
    await db_session.commit()

    assert result["status"] == "migration_identity_deferred", result

    # qualification_attempts MUST NOT be incremented.
    await db_session.refresh(version)
    assert (version.qualification_attempts or 0) == 0, (
        "a deferred migration-identity check must not increment attempt count"
    )

    # No child tasks should be created.
    from sqlmodel import select

    tasks = (
        (await db_session.execute(select(Task).where(Task.project_id == project.id)))
        .scalars()
        .all()
    )
    assert len(tasks) == 0, "no children should spawn when migration-identity defers"


@pytest.mark.asyncio
async def test_run_qualification_migration_identity_ok_proceeds_normally(
    db_session, mocker
):
    """When the migration-identity pre-spawn check returns 'ok' (no collision),
    run_qualification continues to the normal spawn path — qualification_attempts
    is incremented and the audit child is spawned (happy-path regression guard)."""
    from unittest.mock import AsyncMock

    project, version = await _make_project_and_version(db_session, phase="stabilize")
    mocker.patch("app.github.get_branch_sha", new=_branch_sha_mock())

    mocker.patch(
        "app.release_qualifier.check_qualification_migration_identity",
        new=AsyncMock(return_value=("ok", None)),
    )

    result = await rq.run_qualification(
        db_session, project.id, version, explicit_request=True
    )
    await db_session.commit()

    assert result["status"] == "spawned", (
        f"clean candidate must proceed to spawn; got {result['status']!r}"
    )

    await db_session.refresh(version)
    assert (version.qualification_attempts or 0) == 1, (
        "qualification_attempts should be 1 after a successful spawn"
    )

    # At least the audit child must be spawned.
    from sqlmodel import select

    tasks = (
        (await db_session.execute(select(Task).where(Task.project_id == project.id)))
        .scalars()
        .all()
    )
    task_types = [(t.first_payload or {}).get("type") for t in tasks]
    assert "release_audit" in task_types, (
        "release_audit child must be spawned for a clean candidate"
    )


@pytest.mark.asyncio
async def test_run_qualification_authorized_renumber_not_blocked_pre_spawn(
    db_session, mocker
):
    """An authorized renumber exemption must NOT be falsely treated as a
    collision: when check_qualification_migration_identity returns 'ok' (the
    authorized-divergence path), run_qualification proceeds normally and does
    not short-circuit with a collision block."""
    from unittest.mock import AsyncMock

    project, version = await _make_project_and_version(db_session, phase="stabilize")
    mocker.patch("app.github.get_branch_sha", new=_branch_sha_mock())

    # check_qualification_migration_identity returns 'ok' for authorized
    # renumbers — mirror that here so we confirm the pre-spawn gate honours it.
    mocker.patch(
        "app.release_qualifier.check_qualification_migration_identity",
        new=AsyncMock(return_value=("ok", None)),
    )

    result = await rq.run_qualification(
        db_session, project.id, version, explicit_request=True
    )
    await db_session.commit()

    assert result["status"] == "spawned", (
        f"authorized renumber must not be blocked pre-spawn; got {result!r}"
    )
    await db_session.refresh(version)
    assert (version.qualification_attempts or 0) == 1


@pytest.mark.asyncio
async def test_run_qualification_proceeds_when_synced(db_session, mocker):
    """When the promotion target is already absorbed into the candidate (synced),
    the gate is a no-op and qualification proceeds normally (pending verdict)."""
    project, version = await _make_project_and_version(db_session, phase="stabilize")
    mocker.patch("app.github.get_branch_sha", new=_branch_sha_mock())
    mocker.patch(
        "app.release_qualifier._is_target_absorbed", new=AsyncMock(return_value=True)
    )

    result = await rq.run_qualification(
        db_session, project.id, version, explicit_request=True
    )
    await db_session.commit()

    assert result["status"] == "spawned"
    await db_session.refresh(version)
    assert version.last_qualification["status"] == "pending"
    assert len(await _audit_tasks(db_session, project.id)) == 1


@pytest.mark.asyncio
async def test_run_qualification_defers_when_candidate_behind_main(db_session, mocker):
    """A candidate the promotion target is NOT yet absorbed into is DEFERRED, not
    scored: no audit child spawns, and the version records a retryable
    deferred_unsynced state (regression for the 2026-06-02 stale-candidate
    promotion break)."""
    project, version = await _make_project_and_version(db_session, phase="stabilize")
    mocker.patch("app.github.get_branch_sha", new=_branch_sha_mock())
    # main NOT absorbed into the candidate.
    mocker.patch(
        "app.release_qualifier._is_target_absorbed", new=AsyncMock(return_value=False)
    )

    result = await rq.run_qualification(
        db_session, project.id, version, explicit_request=True
    )
    await db_session.commit()

    assert result["status"] == "deferred_unsynced"
    await db_session.refresh(version)
    assert version.last_qualification["status"] == "deferred_unsynced"
    assert version.last_qualification["first_deferred_at"]
    # A deferral is not a qualification attempt and spawns nothing.
    assert version.qualification_attempts == 0
    assert await _audit_tasks(db_session, project.id) == []


@pytest.mark.asyncio
async def test_run_qualification_fall_through_when_unsynced_too_long(
    db_session, mocker
):
    """Bounded fall-through: a candidate behind main for >= the fall-through
    window qualifies ANYWAY — a wedged back-merge must never starve promotion."""
    from datetime import datetime, timedelta

    project, version = await _make_project_and_version(db_session, phase="stabilize")
    # Pre-seed a stale deferral so the accumulated wait exceeds the 30-min window.
    version.last_qualification = {
        "status": "deferred_unsynced",
        "first_deferred_at": (datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(minutes=45)).isoformat(),
    }
    db_session.add(version)
    await db_session.commit()
    mocker.patch("app.github.get_branch_sha", new=_branch_sha_mock())
    mocker.patch(
        "app.release_qualifier._is_target_absorbed", new=AsyncMock(return_value=False)
    )

    result = await rq.run_qualification(
        db_session, project.id, version, explicit_request=True
    )
    await db_session.commit()

    # Fell through → scored anyway.
    assert result["status"] == "spawned"
    assert len(await _audit_tasks(db_session, project.id)) == 1


@pytest.mark.asyncio
async def test_run_qualification_no_sync_gate_for_single_branch(db_session, mocker):
    """Single-branch projects (integration == promotion) have no back-merge
    concept — the sync gate is skipped entirely and never runs the compare, even
    when is_ancestor_of would report not-absorbed."""
    project, version = await _make_project_and_version(db_session, phase="stabilize")
    project.integration_branch = "main"
    project.promotion_target = "main"
    db_session.add(project)
    await db_session.commit()
    mocker.patch(
        "app.github.get_branch_sha", new=AsyncMock(return_value="cafe1234" * 5)
    )
    anc = mocker.patch(
        "app.release_qualifier._is_target_absorbed", new=AsyncMock(return_value=False)
    )

    result = await rq.run_qualification(
        db_session, project.id, version, explicit_request=True
    )
    await db_session.commit()

    assert result["status"] == "spawned"
    anc.assert_not_called()  # the gate is short-circuited before any compare


@pytest.mark.asyncio
async def test_run_qualification_wrong_phase_is_noop(db_session, mocker):
    """run_qualification on a build-phase version early-returns wrong_phase:
    no children spawned, last_qualification untouched, attempts not bumped."""
    # Default phase is "build" — not stabilize.
    project, version = await _make_project_and_version(db_session)
    assert version.phase == "build"

    token_mock = mocker.patch(
        "app.github.get_repo_installation_token",
        new=AsyncMock(return_value="gh-token"),
    )
    sha_mock = mocker.patch(
        "app.github.get_branch_sha",
        new=AsyncMock(return_value="cafe" * 10),
    )

    result = await rq.run_qualification(
        db_session, project.id, version, explicit_request=True
    )
    await db_session.commit()

    assert result["status"] == "wrong_phase"
    # The guard fires before any GitHub I/O.
    token_mock.assert_not_called()
    sha_mock.assert_not_called()

    await db_session.refresh(version)
    # No side effects whatsoever.
    assert version.last_qualification is None
    assert version.qualification_attempts == 0

    from sqlmodel import select

    tasks = (
        (await db_session.execute(select(Task).where(Task.project_id == project.id)))
        .scalars()
        .all()
    )
    assert len(tasks) == 0


@pytest.mark.asyncio
async def test_run_qualification_no_anthropic_key_spawns(db_session, mocker):
    """Without a raw ANTHROPIC_API_KEY, run_qualification must still reach 'spawned'.

    The old inline blind-merge-grade pre-gate required ANTHROPIC_API_KEY and
    failed closed without it.  That gate is removed: qualification now proceeds
    directly to spawning the holistic audit task (which uses OAuth, not a
    raw key) regardless of whether ANTHROPIC_API_KEY is set.
    """
    project, version = await _make_project_and_version(db_session, phase="stabilize")
    version.goal = "deliver user analytics dashboard"
    db_session.add(version)
    await db_session.commit()
    candidate = "dead" * 10  # 40-char placeholder SHA

    mocker.patch(
        "app.github.get_branch_sha",
        new=AsyncMock(return_value=candidate),
    )

    result = await rq.run_qualification(
        db_session, project.id, version, explicit_request=True
    )
    await db_session.commit()

    # No raw key needed — qualification reaches spawned.
    assert result["status"] == "spawned", (
        f"expected 'spawned' without ANTHROPIC_API_KEY; got {result['status']!r}"
    )
    assert result["candidate_sha"] == candidate

    await db_session.refresh(version)
    assert version.last_qualification["status"] == "pending"
    assert result.get("merge_grade_task_id") is None

    from sqlmodel import select

    tasks = (
        (await db_session.execute(select(Task).where(Task.project_id == project.id)))
        .scalars()
        .all()
    )
    types = sorted((t.first_payload or {}).get("type") for t in tasks)
    assert types == ["release_audit"]


@pytest.mark.asyncio
async def test_run_qualification_empty_goal_no_merge_grade_task(db_session, mocker):
    """When version.goal is empty/None no merge_grade task is spawned.

    An empty goal means there is nothing to grade the merged diffs against, so
    the qualifier skips spawning a merge_grade child entirely.  Qualification
    must still reach 'spawned' (audit task is always spawned).
    """
    project, version = await _make_project_and_version(db_session, phase="stabilize")
    # version.goal defaults to None — do NOT set it.
    candidate = "cafe" * 10  # 40-char placeholder SHA

    mocker.patch(
        "app.github.get_branch_sha",
        new=AsyncMock(return_value=candidate),
    )

    result = await rq.run_qualification(
        db_session, project.id, version, explicit_request=True
    )
    await db_session.commit()

    assert result["status"] == "spawned", (
        f"empty-goal version must reach 'spawned'; got {result['status']!r}"
    )
    # No merge_grade task for an empty goal.
    assert result.get("merge_grade_task_id") is None, (
        "merge_grade_task_id must be absent when version.goal is empty"
    )

    from sqlmodel import select

    tasks = (
        (await db_session.execute(select(Task).where(Task.project_id == project.id)))
        .scalars()
        .all()
    )
    types = [(t.first_payload or {}).get("type") for t in tasks]
    assert "release_audit" in types
    assert "release_merge_grade" not in types


@pytest.mark.asyncio
async def test_run_qualification_already_pending_is_noop(db_session, mocker):
    """A second run_qualification while a qualification is in flight (pending
    with a still-running child) early-returns already_pending and spawns
    nothing — no duplicate agent VMs, no orphaned prior children."""
    project, version = await _make_project_and_version(db_session, phase="stabilize")

    # Seed a pending qualification whose audit child is still IN_PROGRESS.
    audit_task = Task(
        project_id=project.id,
        title="audit-child",
        payloads=[{"type": "release_audit", "base_branch": "main"}],
        vm_instance_id="vm-audit-inflight",
        status=TaskStatus.IN_PROGRESS,
        context={"release_version_id": str(version.id)},
    )
    smoke_task = Task(
        project_id=project.id,
        title="smoke-child",
        payloads=[{"type": "smoketest", "base_branch": "automain"}],
        vm_instance_id="vm-smoke-inflight",
        status=TaskStatus.COMPLETED,
        context={"release_version_id": str(version.id)},
    )
    db_session.add(audit_task)
    db_session.add(smoke_task)
    await db_session.commit()
    await db_session.refresh(audit_task)
    await db_session.refresh(smoke_task)

    version.last_qualification = {
        "status": "pending",
        "candidate_sha": "feed" * 10,
        "ci_green": True,
        "started_at": "2026-05-20T00:00:00",
        "audit_task_id": str(audit_task.id),
        "smoketest_task_id": str(smoke_task.id),
    }
    db_session.add(version)
    await db_session.commit()
    await db_session.refresh(version)
    attempts_before = version.qualification_attempts

    sha_mock = mocker.patch(
        "app.github.get_branch_sha",
        new=AsyncMock(return_value="cafe" * 10),
    )

    result = await rq.run_qualification(
        db_session, project.id, version, explicit_request=True
    )
    await db_session.commit()

    assert result["status"] == "already_pending"
    # No new SHA snapshot — the guard fired before any GitHub I/O.
    sha_mock.assert_not_called()

    await db_session.refresh(version)
    # The original pending record is untouched; attempts NOT bumped.
    assert version.last_qualification["candidate_sha"] == "feed" * 10
    assert version.qualification_attempts == attempts_before

    # Still exactly the two original children — no third/fourth spawned.
    from sqlmodel import select

    tasks = (
        (await db_session.execute(select(Task).where(Task.project_id == project.id)))
        .scalars()
        .all()
    )
    assert len(tasks) == 2


@pytest.mark.asyncio
async def test_run_qualification_promotion_check_wait_is_noop(db_session, mocker):
    """A version whose audit PASSED and now has an open managed promotion PR
    (status='pending' + a promotion_pending sub-key, children
    terminal) must NOT be re-qualified: doing so would spawn a fresh heavy
    audit/smoketest pair and clobber the promotion-pending provenance.
    run_qualification refuses with promotion_check_wait, spawns nothing, and
    leaves the record untouched — consistent with the version-cut lever."""
    project, version = await _make_project_and_version(db_session, phase="stabilize")

    # Both children already terminal — by the child-task definition this is NOT
    # pending, and 'pending' is not a conclusive verdict, so the managed
    # promotion guard catches it.
    audit_task = Task(
        project_id=project.id,
        title="audit-done",
        payloads=[{"type": "release_audit", "base_branch": "main"}],
        vm_instance_id="vm-audit-promo",
        status=TaskStatus.COMPLETED,
        context={"release_version_id": str(version.id)},
    )
    db_session.add(audit_task)
    await db_session.commit()
    await db_session.refresh(audit_task)

    version.last_qualification = {
        "status": "pending",
        "candidate_sha": "feed" * 10,
        "ci_green": True,
        "started_at": "2026-05-20T00:00:00",
        "audit_task_id": str(audit_task.id),
        "smoketest_task_id": None,
        "smoketest_enabled": False,
        "promotion_pending": {
            "pr_number": 4242,
            "pr_url": "https://github.com/o/r/pull/4242",
            "managed_check_wait": True,
        },
    }
    db_session.add(version)
    await db_session.commit()
    await db_session.refresh(version)
    attempts_before = version.qualification_attempts

    sha_mock = mocker.patch(
        "app.github.get_branch_sha",
        new=AsyncMock(return_value="cafe" * 10),
    )

    result = await rq.run_qualification(
        db_session, project.id, version, explicit_request=True
    )
    await db_session.commit()

    assert result["status"] == "promotion_check_wait"
    assert result["promotion_pr_number"] == 4242
    assert result["promotion_pr_url"] == "https://github.com/o/r/pull/4242"
    # The pre-lock guard fired before any GitHub I/O (no SHA snapshot).
    sha_mock.assert_not_called()

    await db_session.refresh(version)
    # The promotion-pending record is untouched; attempts NOT bumped.
    assert version.last_qualification["promotion_pending"]["pr_number"] == 4242
    assert version.last_qualification["candidate_sha"] == "feed" * 10
    assert version.qualification_attempts == attempts_before

    # Still exactly the one original child — no fresh audit/smoketest pair.
    from sqlmodel import select

    tasks = (
        (await db_session.execute(select(Task).where(Task.project_id == project.id)))
        .scalars()
        .all()
    )
    assert len(tasks) == 1


@pytest.mark.asyncio
async def test_run_qualification_ignores_stale_pending_when_verdict_finalized(
    db_session, mocker
):
    """Stale-pending window: last_qualification.status='pending' with a
    promotion_pending sub-key but the newest QualificationVerdict row already
    has a finalized terminal_status (e.g. 'promoted'). In this window the
    effective state is NOT promotion_check_wait — the verdict already resolved.
    run_qualification must proceed (return 'spawned'), not block with a false
    promotion_check_wait refusal."""
    project, version = await _make_project_and_version(db_session, phase="stabilize")

    # Need a terminal audit child so _qualification_in_flight sees no live child.
    audit_task = Task(
        project_id=project.id,
        title="audit-stale-verdict",
        payloads=[{"type": "release_audit", "base_branch": "main"}],
        vm_instance_id="vm-stale-verdict",
        status=TaskStatus.COMPLETED,
        context={"release_version_id": str(version.id)},
    )
    db_session.add(audit_task)
    await db_session.commit()
    await db_session.refresh(audit_task)

    # Stale-pending: last_qualification says promotion is in flight...
    version.last_qualification = {
        "status": "pending",
        "candidate_sha": "cafe" * 10,
        "ci_green": True,
        "started_at": "2026-06-01T00:00:00",
        "audit_task_id": str(audit_task.id),
        "smoketest_task_id": None,
        "smoketest_enabled": False,
        "promotion_pending": {
            "pr_number": 9999,
            "pr_url": "https://github.com/o/r/pull/9999",
            "managed_check_wait": True,
        },
    }
    db_session.add(version)

    # ...but the verdict row already says 'promoted' — finalized.
    verdict = QualificationVerdict(
        project_id=project.id,
        release_version_id=version.id,
        candidate_sha="cafe" * 10,
        terminal_status="promoted",
    )
    db_session.add(verdict)
    await db_session.commit()
    await db_session.refresh(version)

    # Advance the automain SHA so the pre-lock hash-reuse check doesn't fire.
    mocker.patch(
        "app.github.get_branch_sha",
        new=AsyncMock(return_value="a1b2c3d4" * 5),
    )

    result = await rq.run_qualification(
        db_session, project.id, version, explicit_request=True
    )
    await db_session.commit()

    # Must NOT refuse with promotion_check_wait — the verdict already resolved.
    assert result["status"] == "spawned", (
        f"expected 'spawned' but got {result['status']!r}; "
        "stale-pending+finalized-verdict should be transparent to action guards"
    )


@pytest.mark.asyncio
async def test_run_qualification_stale_verdict_still_blocks_live_promotion(
    db_session, mocker
):
    """Temporal-ordering fix regression: a finalized verdict for the SAME
    candidate_sha that is OLDER than the live promotion_pending must NOT
    suppress the promotion_pending guard.

    Scenario: a 'dry_run' verdict was recorded for sha ABCD, then the operator
    force-promoted that sha, opening a new promotion PR.  The verdict row is NOW
    stale relative to the live attempt.  apply_verdict_substitution must NOT use
    the pre-attempt verdict to classify the state as 'dry_run' — it must keep
    promotion_pending visible so run_qualification blocks with promotion_check_wait.
    """
    from datetime import datetime

    project, version = await _make_project_and_version(db_session, phase="stabilize")

    audit_task = Task(
        project_id=project.id,
        title="audit-done",
        payloads=[{"type": "release_audit", "base_branch": "main"}],
        vm_instance_id="vm-audit-stale-temporal",
        status=TaskStatus.COMPLETED,
        context={"release_version_id": str(version.id)},
    )
    db_session.add(audit_task)
    await db_session.commit()
    await db_session.refresh(audit_task)

    # Verdict was written BEFORE the current promotion attempt (2026-01-01),
    # promotion attempt started AFTER (enabled_at = 2026-06-01).
    verdict = QualificationVerdict(
        project_id=project.id,
        release_version_id=version.id,
        candidate_sha="abcd" * 10,
        terminal_status="dry_run",
        created_at=datetime(2026, 1, 1),  # OLDER than the pending attempt
    )
    db_session.add(verdict)

    version.last_qualification = {
        "status": "pending",
        "candidate_sha": "abcd" * 10,
        "ci_green": True,
        "started_at": "2026-05-01T00:00:00",
        "audit_task_id": str(audit_task.id),
        "smoketest_task_id": None,
        "smoketest_enabled": False,
        "promotion_pending": {
            "pr_number": 7070,
            "pr_url": "https://github.com/o/r/pull/7070",
            "managed_check_wait": True,
            "enabled_at": "2026-06-01T00:00:00",  # AFTER the verdict
        },
    }
    db_session.add(version)
    await db_session.commit()
    await db_session.refresh(version)

    mocker.patch(
        "app.github.get_branch_sha",
        new=AsyncMock(return_value="cafe" * 10),
    )

    result = await rq.run_qualification(
        db_session, project.id, version, explicit_request=True
    )
    await db_session.commit()

    # The stale verdict must NOT unblock the live promotion — should still block.
    assert result["status"] == "promotion_check_wait", (
        f"expected 'promotion_check_wait' but got {result['status']!r}; "
        "a pre-attempt verdict must not suppress an active promotion_pending"
    )
    assert result["promotion_pr_number"] == 7070


@pytest.mark.asyncio
async def test_abandon_current_version_stale_verdict_still_blocks_live_promotion(
    db_session,
):
    """Temporal-ordering fix regression (abandon surface): a finalized verdict for
    the SAME candidate_sha that is OLDER than the live promotion_pending must NOT
    suppress the promotion_pending guard on abandon_current_version.

    Scenario: a 'not_promoted' verdict was written for sha BEEF, then a same-sha
    re-run opened a new promotion PR.  abandon_current_version must still raise
    VersionCutBlockedByPromotion — the stale verdict must not be used to clear
    the live in-flight state.
    """
    from datetime import datetime

    from app.release_version import VersionCutBlockedByPromotion

    project, version = await _make_project_and_version(db_session, phase="stabilize")

    # Verdict written BEFORE the current promotion attempt.
    verdict = QualificationVerdict(
        project_id=project.id,
        release_version_id=version.id,
        candidate_sha="beef" * 10,
        terminal_status="not_promoted",
        created_at=datetime(2026, 1, 1),  # OLDER than the pending attempt
    )
    db_session.add(verdict)

    version.last_qualification = {
        "status": "pending",
        "candidate_sha": "beef" * 10,
        "ci_green": False,
        "promotion_pending": {
            "pr_number": 8080,
            "pr_url": "https://github.com/o/r/pull/8080",
            "managed_check_wait": True,
            "enabled_at": "2026-06-01T00:00:00",  # AFTER the verdict
        },
    }
    db_session.add(version)
    await db_session.commit()
    await db_session.refresh(version)

    with pytest.raises(VersionCutBlockedByPromotion) as exc_info:
        await rv.abandon_current_version(
            db_session,
            project.id,
            reason="operator cut",
            operator_id="op@example.com",
        )

    assert exc_info.value.promotion_pr_number == 8080, (
        "abandon_current_version must block with the live PR number, "
        "not be cleared by the pre-attempt verdict"
    )


@pytest.mark.asyncio
async def test_run_qualification_managed_check_wait_is_noop(db_session, mocker):
    """A version whose migration-carrying promotion PR is held in
    managed_check_wait (status='pending' + promotion_pending.managed_check_wait)
    must ALSO block a duplicate qualify — re-qualifying would clobber the
    check-wait provenance the reconciler retries on. run_qualification refuses
    with the distinct promotion_check_wait status and spawns nothing."""
    project, version = await _make_project_and_version(db_session, phase="stabilize")

    audit_task = Task(
        project_id=project.id,
        title="audit-done",
        payloads=[{"type": "release_audit", "base_branch": "main"}],
        vm_instance_id="vm-audit-cw",
        status=TaskStatus.COMPLETED,
        context={"release_version_id": str(version.id)},
    )
    db_session.add(audit_task)
    await db_session.commit()
    await db_session.refresh(audit_task)

    version.last_qualification = {
        "status": "pending",
        "candidate_sha": "feed" * 10,
        "ci_green": True,
        "started_at": "2026-05-20T00:00:00",
        "audit_task_id": str(audit_task.id),
        "smoketest_task_id": None,
        "smoketest_enabled": False,
        "promotion_pending": {
            "pr_number": 4343,
            "pr_url": "https://github.com/o/r/pull/4343",
            "managed_check_wait": True,
        },
    }
    db_session.add(version)
    await db_session.commit()
    await db_session.refresh(version)
    attempts_before = version.qualification_attempts

    sha_mock = mocker.patch(
        "app.github.get_branch_sha",
        new=AsyncMock(return_value="cafe" * 10),
    )

    result = await rq.run_qualification(
        db_session, project.id, version, explicit_request=True
    )
    await db_session.commit()

    # The managed check-wait hold blocks a duplicate qualify without spawning.
    assert result["status"] == "promotion_check_wait"
    assert result["promotion_pr_number"] == 4343
    assert result["promotion_pr_url"] == "https://github.com/o/r/pull/4343"
    sha_mock.assert_not_called()

    await db_session.refresh(version)
    assert version.last_qualification["promotion_pending"]["pr_number"] == 4343
    assert version.qualification_attempts == attempts_before

    from sqlmodel import select

    tasks = (
        (await db_session.execute(select(Task).where(Task.project_id == project.id)))
        .scalars()
        .all()
    )
    assert len(tasks) == 1


@pytest.mark.asyncio
async def test_run_qualification_refreshes_stale_check_wait_under_lock(
    db_session, mocker
):
    """Regression (re-qual serialization): a re-qualify must no-op even when the
    caller passes a STALE in-memory version whose last_qualification predates the
    promote() that set managed_check_wait in another session.

    This reproduces the redundant-audit incident: the strategic qualify_request
    path loads the open version via current_open_version() and may hold a copy
    cached BEFORE promote() committed the managed check_wait.  Without
    populate_existing on the under-lock FOR UPDATE re-select, that SELECT acquires
    the row lock but returns the SAME stale identity-map instance, so the
    managed-promotion guard reads non-blocking state and spawns a fresh audit +
    merge-grade while the promotion PR is genuinely open.  With the fix the
    re-select overwrites the cached attributes, so the guard sees the committed
    check_wait and no-ops."""
    from sqlalchemy import update as _sa_update

    project, version = await _make_project_and_version(db_session, phase="stabilize")

    audit_task = Task(
        project_id=project.id,
        title="audit-done",
        payloads=[{"type": "release_audit", "base_branch": "main"}],
        vm_instance_id="vm-audit-stale",
        status=TaskStatus.COMPLETED,
        context={"release_version_id": str(version.id)},
    )
    db_session.add(audit_task)
    await db_session.commit()
    await db_session.refresh(audit_task)

    # The caller's in-memory version: a NON-blocking 'pending' (audit done, no
    # promotion_pending) — on its own this would spawn a fresh qualification
    # (cf. test_run_qualification_pending_but_children_terminal_runs_fresh).
    stale_state = {
        "status": "pending",
        "candidate_sha": "feed" * 10,
        "ci_green": True,
        "started_at": "2026-05-20T00:00:00",
        "audit_task_id": str(audit_task.id),
        "smoketest_task_id": None,
        "smoketest_enabled": False,
    }
    version.last_qualification = stale_state
    db_session.add(version)
    await db_session.commit()
    await db_session.refresh(version)
    attempts_before = version.qualification_attempts

    # Simulate another session committing the managed check_wait AFTER this
    # session cached `version`: write it straight to the row with
    # synchronize_session=False so the in-memory instance stays stale.
    db_managed_state = {
        **stale_state,
        "promotion_pending": {
            "pr_number": 909,
            "pr_url": "https://github.com/o/r/pull/909",
            "managed_check_wait": True,
        },
    }
    await db_session.execute(
        _sa_update(rv.ReleaseVersion)
        .where(rv.ReleaseVersion.id == version.id)
        .values(last_qualification=db_managed_state)
        .execution_options(synchronize_session=False)
    )
    # The in-memory copy is still the stale, non-blocking state.
    assert "promotion_pending" not in version.last_qualification

    mocker.patch(
        "app.github.get_repo_installation_token",
        new=AsyncMock(return_value="gh-token"),
    )
    mocker.patch(
        "app.github.get_branch_sha",
        new=AsyncMock(return_value="cafe" * 10),
    )
    mocker.patch(
        "app.github.get_check_runs_cached",
        new=AsyncMock(return_value={"conclusion": "success"}),
    )

    result = await rq.run_qualification(
        db_session, project.id, version, explicit_request=True
    )
    await db_session.commit()

    # The under-lock re-select refreshed the stale instance, so the
    # managed-promotion guard caught the check_wait — no fresh spawn.
    assert result["status"] == "promotion_check_wait"
    assert result["promotion_pr_number"] == 909

    await db_session.refresh(version)
    assert version.last_qualification["promotion_pending"]["pr_number"] == 909
    assert version.qualification_attempts == attempts_before

    # Still exactly the one original audit child — no fresh audit/smoketest pair.
    from sqlmodel import select as _select

    tasks = (
        (await db_session.execute(_select(Task).where(Task.project_id == project.id)))
        .scalars()
        .all()
    )
    assert len(tasks) == 1


@pytest.mark.asyncio
async def test_run_qualification_pending_but_children_terminal_runs_fresh(
    db_session, mocker
):
    """A 'pending' record whose children have ALL terminated is NOT in flight
    — it is merely awaiting finalize.  A fresh run_qualification is allowed
    (the stale record gets overwritten), so a new qualification spawns."""
    project, version = await _make_project_and_version(db_session, phase="stabilize")

    # Both children already terminal, yet last_qualification still 'pending'.
    audit_task = Task(
        project_id=project.id,
        title="audit-old",
        payloads=[{"type": "release_audit", "base_branch": "main"}],
        vm_instance_id="vm-audit-old",
        status=TaskStatus.COMPLETED,
        context={"release_version_id": str(version.id)},
    )
    smoke_task = Task(
        project_id=project.id,
        title="smoke-old",
        payloads=[{"type": "smoketest", "base_branch": "automain"}],
        vm_instance_id="vm-smoke-old",
        status=TaskStatus.FAILED,
        context={"release_version_id": str(version.id)},
    )
    db_session.add(audit_task)
    db_session.add(smoke_task)
    await db_session.commit()
    await db_session.refresh(audit_task)
    await db_session.refresh(smoke_task)

    version.last_qualification = {
        "status": "pending",
        "candidate_sha": "0ld0" * 10,
        "ci_green": True,
        "started_at": "2026-05-18T00:00:00",
        "audit_task_id": str(audit_task.id),
        "smoketest_task_id": str(smoke_task.id),
    }
    db_session.add(version)
    await db_session.commit()
    await db_session.refresh(version)

    mocker.patch(
        "app.github.get_repo_installation_token",
        new=AsyncMock(return_value="gh-token"),
    )
    mocker.patch(
        "app.github.get_branch_sha",
        new=AsyncMock(return_value="newc" * 10),
    )
    mocker.patch(
        "app.github.get_check_runs_cached",
        new=AsyncMock(return_value={"conclusion": "success"}),
    )

    result = await rq.run_qualification(
        db_session, project.id, version, explicit_request=True
    )
    await db_session.commit()

    # A fresh qualification ran — the stale pending record did not block it.
    assert result["status"] == "spawned"
    assert result["candidate_sha"] == "newc" * 10

    await db_session.refresh(version)
    assert version.last_qualification["candidate_sha"] == "newc" * 10


@pytest.mark.asyncio
async def test_run_qualification_second_sequential_call_does_not_double_spawn(
    db_session, mocker
):
    """Audit C3: run_qualification serializes via a FOR UPDATE lock on the
    ReleaseVersion row and re-checks the in-flight guard under the lock.  A
    second call after the first has stamped a pending record (its audit child
    still QUEUED → in flight) early-returns already_pending and spawns nothing
    — so two callers (strategic qualify_request + admin POST /qualify) can
    never produce a duplicate audit/smoketest pair."""
    project, version = await _make_project_and_version(db_session, phase="stabilize")

    mocker.patch(
        "app.github.get_repo_installation_token",
        new=AsyncMock(return_value="gh-token"),
    )
    mocker.patch(
        "app.github.get_branch_sha",
        new=AsyncMock(return_value="5e91a1ed" * 5),
    )
    mocker.patch(
        "app.github.get_check_runs_cached",
        new=AsyncMock(return_value={"conclusion": "success"}),
    )

    # First call — spawns and stamps a pending record.
    first = await rq.run_qualification(
        db_session, project.id, version, explicit_request=True
    )
    await db_session.commit()
    assert first["status"] == "spawned"

    # Second call on the same version — the audit child is still QUEUED
    # (non-terminal) so the under-lock in-flight guard fires.
    second = await rq.run_qualification(
        db_session, project.id, version, explicit_request=True
    )
    await db_session.commit()
    assert second["status"] == "already_pending"

    # Exactly one release_audit task total — no duplicate from the second call.
    from sqlmodel import select

    tasks = (
        (await db_session.execute(select(Task).where(Task.project_id == project.id)))
        .scalars()
        .all()
    )
    assert sorted(t.first_payload.get("type") for t in tasks) == ["release_audit"]
    # qualification_attempts bumped exactly once (the second call no-op'd).
    await db_session.refresh(version)
    assert version.qualification_attempts == 1


@pytest.mark.asyncio
async def test_run_qualification_version_vanished_before_lock(db_session, mocker):
    """Audit C3: if the ReleaseVersion row is gone by the time run_qualification
    takes its FOR UPDATE lock, it returns version_not_found rather than
    crashing or spawning orphan children."""
    project, version = await _make_project_and_version(db_session, phase="stabilize")

    mocker.patch(
        "app.github.get_repo_installation_token",
        new=AsyncMock(return_value="gh-token"),
    )
    mocker.patch(
        "app.github.get_branch_sha",
        new=AsyncMock(return_value="90111111" * 5),
    )
    mocker.patch(
        "app.github.get_check_runs_cached",
        new=AsyncMock(return_value={"conclusion": "success"}),
    )

    # Delete the version row so the FOR UPDATE lock select finds nothing.
    await db_session.delete(version)
    await db_session.commit()

    result = await rq.run_qualification(
        db_session, project.id, version, explicit_request=True
    )

    assert result["status"] == "version_not_found"

    # No child tasks spawned.
    from sqlmodel import select

    tasks = (
        (await db_session.execute(select(Task).where(Task.project_id == project.id)))
        .scalars()
        .all()
    )
    assert len(tasks) == 0


@pytest.mark.asyncio
async def test_run_qualification_cached_dry_run_same_sha_is_noop(db_session, mocker):
    """Idempotent qualification: when a conclusive dry_run verdict exists for
    this candidate_sha and automain HEAD still equals it, run_qualification
    early-returns cached_verdict and spawns nothing — the strategic agent
    re-emitting qualify_request every cycle must not re-spawn 2 agent VMs for
    an identical, already-scored commit. Relying on lucky re-rolls is not a
    sound gate; the next automain advance will trigger a fresh qualification."""
    project, version = await _make_project_and_version(db_session, phase="stabilize")

    dry_run_sha = "drc0de00" * 5  # 40-char placeholder SHA
    version.last_qualification = _with_current_contract(project, version, {
        "status": "dry_run",
        "candidate_sha": dry_run_sha,
        "ci_green": True,
        "would_promote": True,
        "confidence": 0.92,
        "started_at": "2026-05-19T00:00:00",
    })
    db_session.add(version)
    await db_session.commit()
    await db_session.refresh(version)
    attempts_before = version.qualification_attempts

    mocker.patch(
        "app.github.get_repo_installation_token",
        new=AsyncMock(return_value="gh-token"),
    )
    # automain HEAD still equals the dry-run candidate — automain has NOT moved.
    mocker.patch(
        "app.github.get_branch_sha",
        new=AsyncMock(return_value=dry_run_sha),
    )
    mocker.patch(
        "app.github.get_check_runs_cached",
        new=AsyncMock(return_value={"conclusion": "success"}),
    )

    result = await rq.run_qualification(
        db_session, project.id, version, explicit_request=True
    )
    await db_session.commit()

    assert result["status"] == "cached_verdict"
    assert result["cached_verdict_status"] == "dry_run"

    await db_session.refresh(version)
    # The dry_run record is untouched, attempts NOT bumped.
    assert version.last_qualification["status"] == "dry_run"
    assert version.qualification_attempts == attempts_before

    # No child tasks spawned.
    from sqlmodel import select

    tasks = (
        (await db_session.execute(select(Task).where(Task.project_id == project.id)))
        .scalars()
        .all()
    )
    assert len(tasks) == 0


@pytest.mark.asyncio
async def test_run_qualification_dry_run_but_automain_moved_runs_fresh(
    db_session, mocker
):
    """Audit H1: a prior dry_run does NOT block a fresh qualification once
    automain has advanced — the new automain HEAD is a different commit that
    has not been scored, so run_qualification spawns again."""
    project, version = await _make_project_and_version(db_session, phase="stabilize")

    version.last_qualification = {
        "status": "dry_run",
        "candidate_sha": "01dc0de0" * 5,  # the old, already-scored commit
        "ci_green": True,
        "would_promote": True,
        "started_at": "2026-05-18T00:00:00",
    }
    db_session.add(version)
    await db_session.commit()
    await db_session.refresh(version)

    mocker.patch(
        "app.github.get_repo_installation_token",
        new=AsyncMock(return_value="gh-token"),
    )
    # automain HEAD has moved on to a NEW commit since the dry_run.
    new_sha = "freshc0d" * 5
    mocker.patch(
        "app.github.get_branch_sha",
        new=AsyncMock(return_value=new_sha),
    )
    mocker.patch(
        "app.github.get_check_runs_cached",
        new=AsyncMock(return_value={"conclusion": "success"}),
    )

    result = await rq.run_qualification(
        db_session, project.id, version, explicit_request=True
    )
    await db_session.commit()

    # A fresh qualification ran — the new commit is unscored.
    assert result["status"] == "spawned"
    assert result["candidate_sha"] == new_sha

    await db_session.refresh(version)
    assert version.last_qualification["status"] == "pending"
    assert version.last_qualification["candidate_sha"] == new_sha


@pytest.mark.asyncio
class TestComputeAuditedSurfaceHash:
    """The audited-surface content hash excludes mechanically-regenerated
    artifacts (CI workflows, __fixtures__ snapshots, size-limit config, docs) so
    a candidate that differs from a prior audited candidate ONLY in those files
    reuses its verdict — while any real app-code OR test-logic change re-audits."""

    def test_excluded_files_do_not_change_hash(self):
        base = [
            {"path": "backend/app/x.py", "size": 1, "sha": "a"},
            {"path": ".github/workflows/ci.yml", "size": 1, "sha": "w1"},
            {"path": "backend/testing/__fixtures__/snap.json", "size": 1, "sha": "f1"},
            {"path": "docs/readme.md", "size": 1, "sha": "d1"},
            {"path": "backend/testing/module_size_limits.json", "size": 1, "sha": "m1"},
        ]
        changed_excluded_only = [
            {"path": "backend/app/x.py", "size": 1, "sha": "a"},
            {"path": ".github/workflows/ci.yml", "size": 1, "sha": "w2"},
            {"path": "backend/testing/__fixtures__/snap.json", "size": 1, "sha": "f2"},
            {"path": "docs/readme.md", "size": 1, "sha": "d2"},
            {"path": "backend/testing/module_size_limits.json", "size": 1, "sha": "m2"},
        ]
        assert rq.compute_audited_surface_hash(base) == rq.compute_audited_surface_hash(
            changed_excluded_only
        )

    def test_app_code_change_changes_hash(self):
        base = [{"path": "backend/app/x.py", "size": 1, "sha": "a"}]
        changed = [{"path": "backend/app/x.py", "size": 1, "sha": "b"}]
        assert rq.compute_audited_surface_hash(base) != rq.compute_audited_surface_hash(
            changed
        )

    def test_test_logic_is_in_the_surface(self):
        # Test files (NOT fixtures) are audited surface — a test change re-audits.
        base = [{"path": "backend/testing/test_foo.py", "size": 1, "sha": "a"}]
        changed = [{"path": "backend/testing/test_foo.py", "size": 1, "sha": "b"}]
        assert rq.compute_audited_surface_hash(base) != rq.compute_audited_surface_hash(
            changed
        )

    def test_none_or_all_excluded_returns_none(self):
        assert rq.compute_audited_surface_hash(None) is None
        assert rq.compute_audited_surface_hash([]) is None
        assert (
            rq.compute_audited_surface_hash(
                [{"path": ".github/x.yml", "size": 1, "sha": "z"}]
            )
            is None
        )


class TestIncrementalAuditBaseline:
    OLD = "a" * 40
    NEW = "b" * 40

    @staticmethod
    def clean(**overrides):
        record = {
            "status": "not_promoted",
            "candidate_sha": TestIncrementalAuditBaseline.OLD,
            "sub_scores": {"audit": 1.0},
            "audit_findings": [],
            "excluded_audit_findings": [],
            "audit_findings_total": 0,
            "excluded_audit_findings_total": 0,
        }
        record.update(overrides)
        return record

    def test_clean_audit_can_anchor_delta_even_if_another_gate_blocked(self):
        assert rq._incremental_audit_base_sha(self.clean(), self.NEW) == self.OLD

    def test_any_verified_or_unverified_finding_forces_full_audit(self):
        assert rq._incremental_audit_base_sha(
            self.clean(audit_findings=[{"summary": "x"}]), self.NEW
        ) is None
        assert rq._incremental_audit_base_sha(
            self.clean(excluded_audit_findings=[{"summary": "x"}]), self.NEW
        ) is None

    def test_legacy_record_without_audit_score_forces_full_audit(self):
        assert rq._incremental_audit_base_sha(
            self.clean(sub_scores={}), self.NEW
        ) is None


@pytest.mark.asyncio
async def test_delta_scoped_audit_reuses_unchanged_surface_without_respawn(
    db_session,
    mocker,
):
    """Cross-SHA audit facts reuse identical trees and scope changed trees.

    An excluded-only advance copies the prior fact without an audit spawn; a
    one-file change uses the prior candidate as its incremental base and carries
    only findings from unchanged paths.
    """
    project, version = await _make_project_and_version(db_session, phase="stabilize")
    old_sha = "0ldaudit" * 5
    same_surface_sha = "sameonly" * 5
    changed_sha = "changed1" * 5
    origin_attempt = uuid.uuid4()
    old_surface = {
        "backend/app/unchanged.py": "blob-unchanged",
        "backend/app/changed.py": "blob-old",
    }
    old_surface_hash = rq._audited_surface_hash_from_map(old_surface)
    unchanged_finding = {
        "severity": "low",
        "summary": "Unchanged path still leaks internal timing detail",
        "file": "backend/app/unchanged.py",
        "key": rq._audit_finding_key(
            "Unchanged path still leaks internal timing detail",
            "backend/app/unchanged.py",
        ),
    }
    superseded_finding = {
        "severity": "low",
        "summary": "Old changed-path finding",
        "file": "backend/app/changed.py",
        "key": rq._audit_finding_key(
            "Old changed-path finding", "backend/app/changed.py"
        ),
    }
    audit_detail = rq._audit_dimension_detail(
        surface_paths=old_surface,
        audit_findings=[unchanged_finding, superseded_finding],
        excluded_audit_findings=[],
        findings_meta={
            "audit_findings_total": 2,
            "audit_findings_truncated": False,
            "excluded_audit_findings_total": 0,
            "excluded_audit_findings_truncated": False,
        },
        audit_missing_reason=None,
        audit_scope={"mode": "full"},
    )
    audit_row = QualificationDimensionResult(
        project_id=project.id,
        release_version_id=version.id,
        candidate_sha=old_sha,
        dimension="audit",
        status="failed",
        sub_score=0.96,
        audited_surface_hash=old_surface_hash,
        detail=audit_detail,
        origin_attempt_id=origin_attempt,
    )
    erosion_row = QualificationDimensionResult(
        project_id=project.id,
        release_version_id=version.id,
        candidate_sha=old_sha,
        dimension="test_erosion",
        status="passed",
        sub_score=1.0,
        detail={},
        origin_attempt_id=origin_attempt,
    )
    challenge_row = QualificationDimensionResult(
        project_id=project.id,
        release_version_id=version.id,
        candidate_sha=old_sha,
        dimension="challenge",
        status="passed",
        detail={"verdicts": [{"verdict": "confirmed"}]},
        origin_attempt_id=origin_attempt,
    )
    db_session.add_all([audit_row, erosion_row, challenge_row])
    version.last_qualification = {
        "status": "infra_failure",
        "candidate_sha": old_sha,
        "qualification_attempt_id": str(origin_attempt),
        "error": "merge_grade_report_unparseable",
    }
    version.active_qualification_attempt_id = origin_attempt
    db_session.add(version)
    await db_session.commit()

    mocker.patch(
        "app.github.get_branch_sha",
        new=_branch_sha_mock(automain=same_surface_sha),
    )
    mocker.patch(
        "app.github.list_repo_tree_paths_at_ref",
        new=AsyncMock(
            side_effect=[
                [
                    {"path": path, "sha": blob_sha, "size": 1}
                    for path, blob_sha in old_surface.items()
                ],
                [
                    {
                        "path": "backend/app/unchanged.py",
                        "sha": "blob-unchanged",
                        "size": 1,
                    },
                    {
                        "path": "backend/app/changed.py",
                        "sha": "blob-new",
                        "size": 1,
                    },
                ],
            ]
        ),
    )
    spawned_task = MagicMock(id=uuid.uuid4())
    audit_spawn = mocker.patch(
        "app.release_qualifier._spawn_release_audit_task",
        new=AsyncMock(return_value=spawned_task),
    )

    unchanged_result = await rq.run_qualification(
        db_session, project.id, version, explicit_request=True
    )
    await db_session.commit()
    await db_session.refresh(version)

    assert unchanged_result["status"] == "spawned"
    audit_spawn.assert_not_called()
    active_attempt = uuid.UUID(unchanged_result["qualification_attempt_id"])
    reused_row = await rq._load_dimension_result(
        db_session, version, same_surface_sha, "audit"
    )
    assert reused_row is not None
    assert reused_row.origin_attempt_id == origin_attempt
    assert str(active_attempt) in (reused_row.reused_by_attempt_ids or [])
    from app.qualification_state_resolver import (
        resolve_dimension_states_from_ledger,
    )

    states = await resolve_dimension_states_from_ledger(db_session, version)
    audit_state = next(state for state in states if state["key"] == "audit")
    assert audit_state["provenance"]["currency"] == "reused"

    version.last_qualification = {
        "status": "infra_failure",
        "candidate_sha": same_surface_sha,
        "qualification_attempt_id": str(active_attempt),
        "error": "migration_apply_check_unavailable",
    }
    db_session.add(version)
    await db_session.commit()
    mocker.patch(
        "app.github.get_branch_sha",
        new=_branch_sha_mock(automain=changed_sha),
    )

    changed_result = await rq.run_qualification(
        db_session, project.id, version, explicit_request=True
    )
    await db_session.commit()
    await db_session.refresh(version)

    assert changed_result["status"] == "spawned"
    audit_spawn.assert_awaited_once()
    assert audit_spawn.await_args.kwargs["audit_base_sha"] == same_surface_sha
    assert version.last_qualification["changed_audited_paths"] == [
        "backend/app/changed.py"
    ]

    fresh_report = {
        "audit_score": 0.98,
        "findings": [
            {
                "severity": "low",
                "summary": "Fresh changed-path finding",
                "file": "backend/app/changed.py",
            }
        ],
        "reviewed_paths": {"backend/app/changed.py": "finding"},
        "audit_scope": {
            "mode": "incremental",
            "effective_base_sha": same_surface_sha,
            "changed_paths": ["backend/app/changed.py"],
        },
        "test_erosion": {},
    }
    merged_report, _ = rq._merge_scoped_audit_report_with_prior_fact(
        fresh_report,
        fresh_report,
        reused_row.detail,
        expected_base_sha=same_surface_sha,
    )
    merged_findings = rq._extract_audit_findings(merged_report)
    assert {finding["summary"] for finding in merged_findings} == {
        "Unchanged path still leaks internal timing detail",
        "Fresh changed-path finding",
    }
    assert rq._score_audit(merged_report)[0] == pytest.approx(0.96)


@pytest.mark.asyncio
async def test_delta_scoped_audit_finalize_persists_merged_ledger_facts(
    db_session,
    mocker,
):
    """Finalization with scoped audit merging persists the correct merged facts.

    Prior ledger rows exist for an old SHA (audit + test_erosion).  A new
    candidate triggers an incremental audit that finds one fresh issue on a
    changed path and zero test erosion.  After ``_finalize_qualification``:
    - The audit ledger row for the new SHA carries both the prior unchanged-path
      finding and the fresh changed-path finding.
    - The test_erosion ledger row carries cumulative deltas (prior -3 + current +1 = -2).
    - ``last_qualification`` reflects the merged facts.
    """
    from app import shared_state

    shared_state.runtime_config["promotion_enabled"] = True
    promote_spy = mocker.patch("app.release_qualifier.promote", new=AsyncMock())

    project, version = await _make_project_and_version(db_session, phase="stabilize")
    old_sha = "0ldaudit" * 5
    new_sha = "newaudit" * 5
    origin_attempt = uuid.uuid4()
    old_surface = {
        "backend/app/unchanged.py": "blob-unchanged",
        "backend/app/changed.py": "blob-old",
    }

    # --- Prior ledger rows ----------------------------------------------------
    prior_audit_detail = {
        "audited_surface_paths": old_surface,
        "audit_findings": [
            {
                "severity": "low",
                "summary": "Unchanged path still leaks internal timing detail",
                "file": "backend/app/unchanged.py",
                "key": rq._audit_finding_key(
                    "Unchanged path still leaks internal timing detail",
                    "backend/app/unchanged.py",
                ),
            },
            {
                "severity": "low",
                "summary": "Old changed-path finding (superseded)",
                "file": "backend/app/changed.py",
                "key": rq._audit_finding_key(
                    "Old changed-path finding (superseded)",
                    "backend/app/changed.py",
                ),
            },
        ],
        "excluded_audit_findings": [],
        "audit_findings_total": 2,
        "audit_findings_truncated": False,
        "excluded_audit_findings_total": 0,
        "excluded_audit_findings_truncated": False,
        "audit_scope": {"mode": "full"},
    }
    prior_erosion_detail = {
        "deleted_test_files": ["testing/test_removed.py"],
        "test_count_delta": -3,
        "assertion_delta": 0,
    }
    db_session.add(
        QualificationDimensionResult(
            project_id=project.id,
            release_version_id=version.id,
            candidate_sha=old_sha,
            dimension="audit",
            status="failed",
            sub_score=0.96,
            detail=prior_audit_detail,
            origin_attempt_id=origin_attempt,
        )
    )
    db_session.add(
        QualificationDimensionResult(
            project_id=project.id,
            release_version_id=version.id,
            candidate_sha=old_sha,
            dimension="test_erosion",
            status="blocked",
            sub_score=None,
            detail=prior_erosion_detail,
            origin_attempt_id=origin_attempt,
        )
    )
    await db_session.commit()

    # --- Spawn a completed audit task with scoped artifact --------------------
    audit_task = Task(
        project_id=project.id,
        title="scoped-audit",
        payloads=[{"type": "release_audit", "base_branch": "main"}],
        vm_instance_id="vm-audit",
        status=TaskStatus.COMPLETED,
        context={"candidate_sha": new_sha},
    )
    db_session.add(audit_task)
    await db_session.flush()
    await db_session.refresh(audit_task)

    # Scoped audit report: one fresh finding on changed.py, no test deletions
    audit_report = {
        "findings": [
            {
                "severity": "low",
                "summary": "Fresh changed-path finding",
                "file": "backend/app/changed.py",
                "key": rq._audit_finding_key(
                    "Fresh changed-path finding", "backend/app/changed.py"
                ),
            },
        ],
        "computed_test_erosion": {
            "test_count_delta": 1,
            "assertion_delta": 0,
        },
        "audit_scope": {
            "mode": "incremental",
            "effective_base_sha": old_sha,
            "changed_paths": ["backend/app/changed.py"],
        },
        "reviewed_paths": {"backend/app/changed.py": "finding"},
    }
    db_session.add(
        Artifact(
            task_id=audit_task.id,
            artifact_type=ArtifactType.RELEASE_AUDIT_REPORT,
            logs=json.dumps(audit_report),
        )
    )
    await db_session.flush()

    # --- Stamp last_qualification for finalization ---------------------------
    rubric_snapshot, rubric_lifecycle = await rq._snapshot_qualification_rubric(
        db_session, version
    )
    rubric_fingerprint = rq._rubric_fingerprint(rubric_snapshot)
    version.last_qualification = {
        "status": "pending",
        "candidate_sha": new_sha,
        "prior_audit_candidate_sha": old_sha,
        "ci_green": True,
        "smoketest_enabled": False,
        "started_at": "2026-07-16T00:00:00",
        "qualification_attempt_id": str(origin_attempt),
        "audit_task_id": str(audit_task.id),
        "smoketest_task_id": None,
        "challenge_task_id": None,
        "rubric_snapshot": rubric_snapshot,
        "rubric_fingerprint": rubric_fingerprint,
        "rubric_lifecycle_snapshot": rubric_lifecycle,
    }
    db_session.add(version)
    await db_session.commit()
    await db_session.refresh(version)

    # --- Finalize ------------------------------------------------------------
    result = await rq._finalize_qualification(
        db_session, version, _pre_gates=dict(_REUSE_PATH_PRE_GATES)
    )
    await db_session.commit()
    await db_session.refresh(version)

    # Not promoted due to merged finding penalties (prior + fresh = 2 low findings).
    assert result["status"] == "not_promoted"

    # --- Assert audit ledger row ---------------------------------------------
    audit_row = await rq._load_dimension_result(
        db_session, version, new_sha, "audit"
    )
    assert audit_row is not None
    merged_findings = audit_row.detail.get("audit_findings") or []
    merged_summaries = {f["summary"] for f in merged_findings}
    assert "Unchanged path still leaks internal timing detail" in merged_summaries, (
        "prior unchanged-path finding must be carried forward"
    )
    assert "Fresh changed-path finding" in merged_summaries, (
        "current scoped finding must be present"
    )
    assert "Old changed-path finding (superseded)" not in merged_summaries, (
        "superseded path finding must NOT be carried"
    )

    # --- Assert test_erosion ledger row --------------------------------------
    erosion_row = await rq._load_dimension_result(
        db_session, version, new_sha, "test_erosion"
    )
    assert erosion_row is not None
    ed = erosion_row.detail or {}
    assert ed.get("test_count_delta") == -2, (
        f"expected cumulative -3 + 1 = -2, got {ed.get('test_count_delta')}"
    )
    assert ed.get("assertion_delta") == 0
    # Prior deleted file not in current changed_paths → carried forward.
    assert "testing/test_removed.py" in (ed.get("deleted_test_files") or [])
    # Prior was blocked and blocking file still present → still blocked.
    assert erosion_row.status == "blocked"
    assert erosion_row.sub_score is None

    # --- Assert last_qualification ------------------------------------------
    lq = version.last_qualification
    assert lq["status"] == "not_promoted"
    lq_summaries = {f["summary"] for f in (lq.get("audit_findings") or [])}
    assert "Unchanged path still leaks internal timing detail" in lq_summaries
    assert "Fresh changed-path finding" in lq_summaries
    assert "Old changed-path finding (superseded)" not in lq_summaries

    promote_spy.assert_not_called()


@pytest.mark.asyncio
async def test_delta_scoped_audit_finalize_restoration_clears_blocker(
    db_session,
    mocker,
):
    """When a prior blocking file is restored in the current scope, the
    test_erosion blocker clears and the cumulative delta is computed correctly."""
    project, version = await _make_project_and_version(db_session, phase="stabilize")
    old_sha = "erosion0" * 5
    new_sha = "restored" * 5
    origin_attempt = uuid.uuid4()

    # --- Prior ledger: blocked test_erosion due to deleted test file ----------
    prior_erosion_detail = {
        "deleted_test_files": ["testing/test_removed.py"],
        "test_count_delta": -5,
        "assertion_delta": -3,
    }
    db_session.add(
        QualificationDimensionResult(
            project_id=project.id,
            release_version_id=version.id,
            candidate_sha=old_sha,
            dimension="audit",
            status="passed",
            sub_score=1.0,
            detail={"audited_surface_paths": {}, "audit_findings": [],
                    "excluded_audit_findings": [], "audit_scope": {"mode": "full"}},
            origin_attempt_id=origin_attempt,
        )
    )
    db_session.add(
        QualificationDimensionResult(
            project_id=project.id,
            release_version_id=version.id,
            candidate_sha=old_sha,
            dimension="test_erosion",
            status="blocked",
            sub_score=None,
            detail=prior_erosion_detail,
            origin_attempt_id=origin_attempt,
        )
    )
    await db_session.commit()

    # --- Completed audit task: file restored in current scope -----------------
    audit_task = Task(
        project_id=project.id,
        title="restoration-audit",
        payloads=[{"type": "release_audit", "base_branch": "main"}],
        vm_instance_id="vm-audit",
        status=TaskStatus.COMPLETED,
    )
    db_session.add(audit_task)
    await db_session.flush()
    await db_session.refresh(audit_task)

    # testing/test_removed.py is in changed_paths but NOT in deleted_test_files
    # → the scoped computation determined it was restored.
    audit_report = {
        "findings": [],
        "computed_test_erosion": {
            "test_count_delta": 3,
            "assertion_delta": 0,
        },
        "audit_scope": {
            "mode": "incremental",
            "effective_base_sha": old_sha,
            "changed_paths": ["testing/test_removed.py"],
        },
    }
    db_session.add(
        Artifact(
            task_id=audit_task.id,
            artifact_type=ArtifactType.RELEASE_AUDIT_REPORT,
            logs=json.dumps(audit_report),
        )
    )
    await db_session.flush()

    rubric_snapshot, rubric_lifecycle = await rq._snapshot_qualification_rubric(
        db_session, version
    )
    rubric_fingerprint = rq._rubric_fingerprint(rubric_snapshot)
    version.last_qualification = {
        "status": "pending",
        "candidate_sha": new_sha,
        "prior_audit_candidate_sha": old_sha,
        "ci_green": True,
        "smoketest_enabled": False,
        "started_at": "2026-07-16T00:00:00",
        "qualification_attempt_id": str(origin_attempt),
        "audit_task_id": str(audit_task.id),
        "smoketest_task_id": None,
        "challenge_task_id": None,
        "rubric_snapshot": rubric_snapshot,
        "rubric_fingerprint": rubric_fingerprint,
        "rubric_lifecycle_snapshot": rubric_lifecycle,
    }
    db_session.add(version)
    await db_session.commit()
    await db_session.refresh(version)

    await rq._finalize_qualification(
        db_session, version, _pre_gates=dict(_REUSE_PATH_PRE_GATES)
    )
    await db_session.commit()
    await db_session.refresh(version)

    # Test erosion should no longer be blocked.
    erosion_row = await rq._load_dimension_result(
        db_session, version, new_sha, "test_erosion"
    )
    assert erosion_row is not None
    # File was in changed_paths but NOT in deleted_test_files → restored.
    assert "testing/test_removed.py" not in (erosion_row.detail or {}).get(
        "deleted_test_files", []
    ), "restored file must not appear in deleted_test_files"
    assert erosion_row.status == "passed", (
        f"expected passed after restoration, got {erosion_row.status}"
    )
    assert erosion_row.sub_score is not None, (
        "blocker must be cleared; sub_score should not be None"
    )
    # Cumulative delta: -5 + 3 = -2 → still has some erosion but not blocked.
    assert (erosion_row.detail or {}).get("test_count_delta") == -2

    # Audit ledger row: no findings.
    audit_row = await rq._load_dimension_result(
        db_session, version, new_sha, "audit"
    )
    assert audit_row is not None
    assert len((audit_row.detail or {}).get("audit_findings") or []) == 0

    # last_qualification reflects the merged state.
    lq = version.last_qualification
    assert lq.get("sub_scores", {}).get("test_erosion") is not None, (
        "test_erosion sub_score should not be None after restoration"
    )


def test_scoped_audit_merge_requires_bound_path_complete_unique_facts():
    base_sha = "a" * 40
    prior = {
        "audited_surface_paths": {"backend/app/a.py": "blob"},
        "audit_findings": [
            {"file": "backend/app/a.py", "severity": "low", "summary": "one", "surface_key": "same"},
            {"file": "backend/app/a.py", "severity": "low", "summary": "two", "surface_key": "same"},
        ],
        "excluded_audit_findings": [],
        "audit_findings_truncated": False,
        "excluded_audit_findings_truncated": False,
    }
    report = {
        "findings": [],
        "reviewed_paths": {"backend/app/b.py": "clean"},
        "audit_scope": {"mode": "incremental", "effective_base_sha": base_sha, "changed_paths": ["backend/app/b.py"]},
    }
    merged, _ = rq._merge_scoped_audit_report_with_prior_fact(
        report, report, prior, expected_base_sha=base_sha
    )
    assert [finding["summary"] for finding in merged["findings"]] == ["one", "two"]
    fallback = {**report, "audit_scope": {"mode": "full", "effective_base_sha": None}}
    unmerged, _ = rq._merge_scoped_audit_report_with_prior_fact(
        fallback, fallback, prior, expected_base_sha=base_sha
    )
    assert unmerged["findings"] == []
    pathless = {**prior, "audit_findings": [{"severity": "low", "summary": "global"}]}
    assert rq._audit_detail_is_delta_reusable(pathless) is False


def test_scoped_test_erosion_preserves_prior_blocker_and_cumulative_deltas():
    """Prior blocked + file outside current scope carries forward unchanged."""
    score, detail = rq._merge_scoped_test_erosion_with_prior_fact(
        {"findings": [], "computed_test_erosion": {"test_count_delta": 1, "assertion_delta": -2}},
        {"deleted_test_files": ["testing/test_removed.py"], "test_count_delta": -3},
        prior_blocked=True,
    )
    assert score is None
    assert detail == {
        "deleted_test_files": ["testing/test_removed.py"],
        "test_count_delta": -2,
        "assertion_delta": -2,
    }


def test_scoped_test_erosion_restoration_clears_blocker():
    """Prior-deleted file restored in current scope: blocker clears."""
    report = {
        "findings": [],
        "computed_test_erosion": {"test_count_delta": 1, "assertion_delta": 0},
        "audit_scope": {
            "mode": "incremental",
            "changed_paths": ["testing/test_removed.py"],
        },
    }
    prior_detail = {
        "deleted_test_files": ["testing/test_removed.py"],
        "test_count_delta": -3,
    }
    score, detail = rq._merge_scoped_test_erosion_with_prior_fact(
        report, prior_detail, prior_blocked=True,
    )
    # File was in changed_paths but NOT in current deleted_test_files → restored.
    assert score is not None, "blocker should clear after restoration"
    assert "deleted_test_files" not in (detail or {})
    assert detail.get("test_count_delta") == -2  # -3 + 1


def test_scoped_test_erosion_restoration_preserves_other_deletions():
    """Only the restored file drops; other prior deletions remain."""
    report = {
        "findings": [],
        "computed_test_erosion": {
            "test_count_delta": 0, "assertion_delta": 0,
            "deleted_test_files": ["testing/still_gone.py"],
        },
        "audit_scope": {
            "mode": "incremental",
            "changed_paths": ["testing/test_removed.py", "testing/still_gone.py"],
        },
    }
    prior_detail = {
        "deleted_test_files": [
            "testing/test_removed.py", "testing/still_gone.py",
        ],
        "test_count_delta": -5,
    }
    score, detail = rq._merge_scoped_test_erosion_with_prior_fact(
        report, prior_detail, prior_blocked=True,
    )
    assert "testing/still_gone.py" in (detail or {}).get("deleted_test_files", [])
    assert "testing/test_removed.py" not in (detail or {}).get("deleted_test_files", [])


def test_scoped_test_erosion_positive_delta_preservation():
    """Prior positive delta (stripped by old _extract_test_erosion_detail) is
    now losslessly preserved so cumulative sum is correct."""
    score, detail = rq._merge_scoped_test_erosion_with_prior_fact(
        {"findings": [], "computed_test_erosion": {"test_count_delta": -5, "assertion_delta": 0}},
        {"test_count_delta": 10, "assertion_delta": 0},
        prior_blocked=False,
    )
    # Old behavior: prior's +10 stripped → merged = 0 + (-5) = -5 (incorrect penalty)
    # New behavior: prior's +10 preserved → merged = 10 + (-5) = 5 (no penalty)
    assert score == 1.0, f"expected no erosion penalty for cumulative +5, got {score}"
    assert detail["test_count_delta"] == 5


@pytest.mark.asyncio
async def test_run_qualification_content_hash_reuse_across_sha(db_session, mocker):
    """Content-hash cross-SHA reuse for a non-not_promoted verdict: when automain
    HEAD advances to a NEW commit whose audited surface is byte-identical to a
    prior dry_run candidate (differs only in an excluded fixture),
    run_qualification reuses that verdict instead of re-auditing. Cross-SHA reuse
    is restricted to promoted/dry_run (whose outcome IS determined by the audited
    surface); not_promoted is excluded (it can be blocked on external state)."""
    project, version = await _make_project_and_version(db_session, phase="stabilize")

    surface_hash = rq.compute_audited_surface_hash(
        [
            {"path": "backend/app/main.py", "size": 100, "sha": "blobmain"},
            {"path": "backend/app/foo.py", "size": 50, "sha": "blobfoo"},
            {
                "path": "backend/testing/__fixtures__/websocket_event_inventory_v1.json",
                "size": 10,
                "sha": "blobfixtureOLD",
            },
        ]
    )
    assert surface_hash is not None

    old_sha = "oldshaaa" * 5
    new_sha = "newshaaa" * 5  # HEAD advanced to a different commit
    version.last_qualification = _with_current_contract(project, version, {
        "status": "dry_run",
        "candidate_sha": old_sha,
        "candidate_diff_hash": surface_hash,
        "ci_green": True,
        "confidence": 0.95,
        "threshold": 0.90,
        "started_at": "2026-05-19T00:00:00",
        "finalized_at": "2026-05-19T00:05:00",
    })
    db_session.add(version)
    await db_session.commit()
    await db_session.refresh(version)
    attempts_before = version.qualification_attempts

    mocker.patch(
        "app.github.get_repo_installation_token",
        new=AsyncMock(return_value="gh-token"),
    )
    mocker.patch("app.github.get_branch_sha", new=AsyncMock(return_value=new_sha))
    mocker.patch(
        "app.github.get_check_runs_cached",
        new=AsyncMock(return_value={"conclusion": "success"}),
    )
    # The NEW commit's tree hashes to the SAME audited surface (only the EXCLUDED
    # fixture blob changed), so reuse must fire despite the different SHA.
    mocker.patch(
        "app.github.list_repo_tree_paths_at_ref",
        new=AsyncMock(
            return_value=[
                {"path": "backend/app/main.py", "size": 100, "sha": "blobmain"},
                {"path": "backend/app/foo.py", "size": 50, "sha": "blobfoo"},
                {
                    "path": "backend/testing/__fixtures__/websocket_event_inventory_v1.json",
                    "size": 11,
                    "sha": "blobfixtureNEW",
                },
            ]
        ),
    )

    result = await rq.run_qualification(
        db_session, project.id, version, explicit_request=True
    )
    await db_session.commit()

    assert result["status"] == "cached_verdict"
    assert result["cached_verdict_status"] == "dry_run"
    await db_session.refresh(version)
    # No re-audit: attempts unchanged and no child tasks spawned.
    assert version.qualification_attempts == attempts_before
    from sqlmodel import select

    tasks = (
        (await db_session.execute(select(Task).where(Task.project_id == project.id)))
        .scalars()
        .all()
    )
    assert len(list(tasks)) == 0


@pytest.mark.asyncio
async def test_run_qualification_ci_blocked_not_promoted_not_reused_cross_sha(
    db_session, mocker
):
    """Content-hash cross-SHA reuse must NOT fire for a not_promoted verdict.

    A not_promoted can be blocked on state OUTSIDE the audited-surface hash — CI
    (.github/), operator criterion attestation, migration-apply drift, an
    operator-lowered merge-grade threshold — all fixable without changing the
    hash. Reusing such a verdict cross-SHA would wedge the version not_promoted
    even after the real fix lands (round-2 review finding A; the CI blocker here
    is one instance). A fresh qualification must run instead — verified via a
    CI-blocked not_promoted whose CI is now green on a same-surface new SHA."""
    project, version = await _make_project_and_version(db_session, phase="stabilize")

    surface_hash = rq.compute_audited_surface_hash(
        [{"path": "backend/app/main.py", "size": 100, "sha": "blobmain"}]
    )
    assert surface_hash is not None

    old_sha = "ciblokd0" * 5
    new_sha = "cifixed0" * 5  # HEAD advanced after a ci.yml fix (excluded from hash)
    version.last_qualification = {
        "status": "not_promoted",
        "blocker": "ci_not_green",
        "candidate_sha": old_sha,
        "candidate_diff_hash": surface_hash,
        "ci_green": False,
        "confidence": 0.55,
        "threshold": 0.90,
        "started_at": "2026-05-19T00:00:00",
        "finalized_at": "2026-05-19T00:05:00",
    }
    db_session.add(version)
    await db_session.commit()
    await db_session.refresh(version)

    audit_task = Task(
        project_id=project.id,
        title="audit",
        payloads=[{"type": "release_audit", "base_branch": "main"}],
        vm_instance_id="vm-audit",
        status=TaskStatus.QUEUED,
        context={"release_version_id": str(version.id)},
    )
    db_session.add(audit_task)
    await db_session.commit()
    await db_session.refresh(audit_task)

    mocker.patch(
        "app.github.get_repo_installation_token",
        new=AsyncMock(return_value="gh-token"),
    )
    mocker.patch("app.github.get_branch_sha", new=AsyncMock(return_value=new_sha))
    # CI is now GREEN (the .github/ fix worked).
    mocker.patch(
        "app.github.get_check_runs_cached",
        new=AsyncMock(return_value={"conclusion": "success"}),
    )
    # Same audited surface (only the excluded ci.yml changed).
    mocker.patch(
        "app.github.list_repo_tree_paths_at_ref",
        new=AsyncMock(
            return_value=[
                {"path": "backend/app/main.py", "size": 100, "sha": "blobmain"},
            ]
        ),
    )
    mocker.patch(
        "app.release_qualifier._spawn_release_audit_task",
        new=AsyncMock(return_value=audit_task),
    )

    result = await rq.run_qualification(
        db_session, project.id, version, explicit_request=True
    )
    await db_session.commit()

    # Must NOT reuse the stale CI-blocked verdict — a fresh qualification runs.
    assert result["status"] != "cached_verdict"
    assert result["status"] == "spawned"


@pytest.mark.asyncio
async def test_run_qualification_promotion_failed_reuses_audit_and_repromotes(
    db_session, mocker
):
    """promotion_failed + UNCHANGED audited surface → RE-PROMOTE without re-audit.

    A promotion_failed verdict ALWAYS means the audit PASSED and only the
    promotion step failed.  When the harvested fix landed in an EXCLUDED path
    (here: only the excluded fixture blob changed), the live audited surface is
    byte-identical to the passing candidate, so run_qualification re-attempts the
    promotion on the CURRENT candidate WITHOUT re-running the audit — it spawns NO
    audit child and calls promote() with the composite reconstructed from the
    passing verdict."""
    from app import shared_state

    shared_state.runtime_config["promotion_enabled"] = True

    project, version = await _make_project_and_version(db_session, phase="stabilize")

    surface_hash = rq.compute_audited_surface_hash(
        [
            {"path": "backend/app/main.py", "size": 100, "sha": "blobmain"},
            {
                "path": "backend/testing/__fixtures__/websocket_event_inventory_v1.json",
                "size": 10,
                "sha": "blobfixtureOLD",
            },
        ]
    )
    assert surface_hash is not None

    old_sha = "oldfaild" * 5  # 40-char placeholder — the SHA that failed to promote
    new_sha = "fixedsha" * 5  # automain advanced after the CI/fixture-only fix
    version.last_qualification = _promotion_failed_record(
        candidate_sha=old_sha, surface_hash=surface_hash
    )
    db_session.add(version)
    await db_session.commit()
    await db_session.refresh(version)
    attempts_before = version.qualification_attempts

    # automain advanced to new_sha; its tree hashes to the SAME audited surface
    # (only the EXCLUDED fixture blob changed).
    mocker.patch(
        "app.github.get_branch_sha",
        new=_branch_sha_mock(automain=new_sha, main="postmerg" * 5),
    )
    mocker.patch(
        "app.github.list_repo_tree_paths_at_ref",
        new=AsyncMock(
            return_value=[
                {"path": "backend/app/main.py", "size": 100, "sha": "blobmain"},
                {
                    "path": "backend/testing/__fixtures__/websocket_event_inventory_v1.json",
                    "size": 11,
                    "sha": "blobfixtureNEW",
                },
            ]
        ),
    )
    # promote() GitHub surface.
    _patch_promote_github_defaults(mocker)
    mocker.patch("app.github.create_branch", new=_create_branch_mock())
    mocker.patch(
        "app.github.create_pull_request",
        new=AsyncMock(return_value={"number": 91, "html_url": "http://pr/91"}),
    )
    receipt_sha = "receipt0" * 5
    merge_mock = mocker.patch(
        "app.github.merge_pull_request",
        new=AsyncMock(return_value={"merged": True, "message": "Merged"}),
    )
    mocker.patch(
        "app.github.create_tag",
        new=AsyncMock(return_value={"ref": "refs/tags/v1"}),
    )
    # Promotion's staged-direction activation uses a production-owned nested
    # session. SQLite's StaticPool has one connection, so keep that nested
    # best-effort path on the test-owned session just like the connected
    # promotion lifecycle fixtures.
    mocker.patch(
        "app.database.AsyncSessionLocal",
        new=_shared_session_factory(db_session),
    )
    # A reuse must NOT spawn an audit child.
    audit_spawn = mocker.patch(
        "app.release_qualifier._spawn_release_audit_task",
        new=AsyncMock(),
    )
    # Isolate the promote — no post-commit strategic re-trigger / conflict recon.
    mocker.patch(
        "app.release_qualifier._maybe_reconcile_after_promotion_conflict",
        new=AsyncMock(),
    )
    mocker.patch(
        "app.release_qualifier._maybe_trigger_strategic_after_qualification",
        new=AsyncMock(),
    )

    result = await rq.run_qualification(
        db_session,
        project.id,
        version,
        explicit_request=True,
        session_factory=_shared_session_factory(db_session),
        # The synchronous reuse-promote fires only from the safe sweep re-driver
        # context (review finding 1); other callers defer. This test exercises the
        # sweep path.
        allow_synchronous_reuse_promote=True,
    )
    await db_session.commit()

    # A promotion outcome, NOT "spawned" — the audit was reused.
    assert result["status"] == "promoted"
    assert result.get("reused_audit") is True
    # The CURRENT candidate (the one carrying the fix) was promoted, and the
    # durable result is bound to the exact merged-PR receipt rather than a
    # later-moving branch tip.
    assert result["promoted_sha"] == receipt_sha
    # No audit child spawned; qualification_attempts NOT bumped (no fresh spawn).
    audit_spawn.assert_not_called()
    merge_mock.assert_awaited()
    await db_session.refresh(version)
    assert version.status == "promoted"
    assert version.qualification_attempts == attempts_before
    # A fresh open v2 was created — the promotion actually landed.
    next_version = await rv.current_open_version(db_session, project.id)
    assert next_version is not None
    assert next_version.version_number == 2
    assert next_version.base_sha == receipt_sha


@pytest.mark.asyncio
async def test_run_qualification_promotion_failed_reuse_defers_without_flag(
    db_session, mocker
):
    """Default (allow_synchronous_reuse_promote=False): a promotion_failed +
    unchanged surface DEFERS the reuse-promote to the sweep re-driver instead of
    promoting synchronously (review finding 1 — a synchronous promote inside a
    lock-holding strategic caller could hang on staged-direction activation). It
    must NOT re-audit (no treadmill) and NOT open a PR here."""
    from app import shared_state

    shared_state.runtime_config["promotion_enabled"] = True
    project, version = await _make_project_and_version(db_session, phase="stabilize")
    surface_hash = rq.compute_audited_surface_hash(
        [{"path": "backend/app/main.py", "size": 100, "sha": "blobmain"}]
    )
    version.last_qualification = _promotion_failed_record(
        candidate_sha="oldfaild" * 5, surface_hash=surface_hash
    )
    db_session.add(version)
    await db_session.commit()
    await db_session.refresh(version)

    mocker.patch(
        "app.github.get_repo_installation_token",
        new=AsyncMock(return_value="gh-token"),
    )
    mocker.patch(
        "app.github.get_branch_sha", new=_branch_sha_mock(automain="fixedsha" * 5)
    )
    mocker.patch(
        "app.github.list_repo_tree_paths_at_ref",
        new=AsyncMock(
            return_value=[
                {"path": "backend/app/main.py", "size": 100, "sha": "blobmain"}
            ]
        ),
    )
    create_mock = mocker.patch("app.github.create_pull_request", new=AsyncMock())
    audit_spawn = mocker.patch(
        "app.release_qualifier._spawn_release_audit_task", new=AsyncMock()
    )

    # No allow_synchronous_reuse_promote → defer to the sweep.
    result = await rq.run_qualification(
        db_session,
        project.id,
        version,
        explicit_request=True,
        session_factory=_shared_session_factory(db_session),
    )

    assert result["status"] == "promotion_failed_reuse_deferred"
    create_mock.assert_not_called()  # no synchronous promote / PR opened here
    audit_spawn.assert_not_called()  # and NOT a re-audit either (no treadmill)
    await db_session.refresh(version)
    # Version untouched — still promotion_failed, awaiting the sweep's re-drive.
    assert version.last_qualification["status"] == "promotion_failed"


@pytest.mark.asyncio
async def test_run_qualification_promotion_failed_changed_surface_reaudits(
    db_session, mocker
):
    """promotion_failed + CHANGED audited surface → normal re-audit (NO reuse).

    When the fix touched APP CODE (an in-surface blob differs), the audit no
    longer holds — reusing it would ship un-audited code.  run_qualification must
    fall through to a fresh qualification (spawns an audit), NOT re-promote."""
    from app import shared_state

    shared_state.runtime_config["promotion_enabled"] = True

    project, version = await _make_project_and_version(db_session, phase="stabilize")

    surface_hash = rq.compute_audited_surface_hash(
        [{"path": "backend/app/main.py", "size": 100, "sha": "blobmainOLD"}]
    )
    assert surface_hash is not None

    old_sha = "oldfaild" * 5
    new_sha = "appfixed" * 5  # automain advanced after an APP-CODE fix
    version.last_qualification = _promotion_failed_record(
        candidate_sha=old_sha, surface_hash=surface_hash
    )
    db_session.add(version)
    await db_session.commit()
    await db_session.refresh(version)

    audit_task = Task(
        project_id=project.id,
        title="audit",
        payloads=[{"type": "release_audit", "base_branch": "main"}],
        vm_instance_id="vm-audit",
        status=TaskStatus.QUEUED,
        context={"release_version_id": str(version.id)},
    )
    db_session.add(audit_task)
    await db_session.commit()
    await db_session.refresh(audit_task)

    mocker.patch(
        "app.github.get_branch_sha",
        new=_branch_sha_mock(automain=new_sha, main="main0000" * 5),
    )
    # The app-code blob changed → the audited surface hash DIFFERS.
    mocker.patch(
        "app.github.list_repo_tree_paths_at_ref",
        new=AsyncMock(
            return_value=[
                {"path": "backend/app/main.py", "size": 100, "sha": "blobmainNEW"},
            ]
        ),
    )
    # Spy on the reuse chokepoint — it must NOT be reached for a changed surface.
    reuse_spy = mocker.patch(
        "app.release_qualifier._reattempt_promotion_failed_in_dedicated_session",
        new=AsyncMock(),
    )
    audit_spawn = mocker.patch(
        "app.release_qualifier._spawn_release_audit_task",
        new=AsyncMock(return_value=audit_task),
    )

    result = await rq.run_qualification(
        db_session,
        project.id,
        version,
        explicit_request=True,
        session_factory=_shared_session_factory(db_session),
    )
    await db_session.commit()

    # A fresh qualification runs — NOT a reuse, NOT a promotion.
    assert result["status"] == "spawned"
    assert result.get("reused_audit") is None
    reuse_spy.assert_not_called()
    audit_spawn.assert_awaited()


@pytest.mark.asyncio
async def test_run_qualification_promotion_failed_reuse_capped(db_session, mocker):
    """promotion_failure_count at the cap → does NOT reuse (no infinite re-promote).

    The circuit breaker caps autonomous promotion attempts.  With the count at
    ``_MAX_PROMOTION_FAILURE_REQUALIFY_ATTEMPTS`` the reuse gate must NOT fire even
    on an UNCHANGED surface — it falls through to the normal path instead of
    re-promoting a persistently-red candidate forever."""
    from app import shared_state

    shared_state.runtime_config["promotion_enabled"] = True

    project, version = await _make_project_and_version(db_session, phase="stabilize")

    surface_hash = rq.compute_audited_surface_hash(
        [{"path": "backend/app/main.py", "size": 100, "sha": "blobmain"}]
    )
    assert surface_hash is not None

    old_sha = "oldfaild" * 5
    new_sha = "cappedsh" * 5
    version.last_qualification = _promotion_failed_record(
        candidate_sha=old_sha,
        surface_hash=surface_hash,
        # At the cap — the breaker must refuse another autonomous re-promote.
        promotion_failure_count=rq._MAX_PROMOTION_FAILURE_REQUALIFY_ATTEMPTS,
    )
    db_session.add(version)
    await db_session.commit()
    await db_session.refresh(version)

    audit_task = Task(
        project_id=project.id,
        title="audit",
        payloads=[{"type": "release_audit", "base_branch": "main"}],
        vm_instance_id="vm-audit",
        status=TaskStatus.QUEUED,
        context={"release_version_id": str(version.id)},
    )
    db_session.add(audit_task)
    await db_session.commit()
    await db_session.refresh(audit_task)

    mocker.patch(
        "app.github.get_branch_sha",
        new=_branch_sha_mock(automain=new_sha, main="main0000" * 5),
    )
    # SAME audited surface — so ONLY the cap prevents the reuse.
    mocker.patch(
        "app.github.list_repo_tree_paths_at_ref",
        new=AsyncMock(
            return_value=[
                {"path": "backend/app/main.py", "size": 100, "sha": "blobmain"},
            ]
        ),
    )
    reuse_spy = mocker.patch(
        "app.release_qualifier._reattempt_promotion_failed_in_dedicated_session",
        new=AsyncMock(),
    )
    mocker.patch(
        "app.release_qualifier._spawn_release_audit_task",
        new=AsyncMock(return_value=audit_task),
    )

    result = await rq.run_qualification(
        db_session,
        project.id,
        version,
        explicit_request=True,
        session_factory=_shared_session_factory(db_session),
    )
    await db_session.commit()

    # The cap blocks the reuse — the chokepoint is never reached, and no
    # promotion happened.
    reuse_spy.assert_not_called()
    assert result.get("reused_audit") is None
    assert result["status"] != "promoted"
    await db_session.refresh(version)
    assert version.status == "open"


async def test_run_qualification_cached_not_promoted_same_sha_is_noop(
    db_session, mocker
):
    """Idempotent qualification: when a conclusive not_promoted verdict exists
    for this candidate_sha and automain HEAD still equals it, run_qualification
    early-returns cached_verdict and spawns nothing — a candidate that scored
    below the promotion bar is not re-audited on the same commit."""
    project, version = await _make_project_and_version(db_session, phase="stabilize")

    candidate_sha = "notprom0" * 5  # 40-char placeholder SHA
    version.last_qualification = _with_current_contract(project, version, {
        "status": "not_promoted",
        "candidate_sha": candidate_sha,
        "ci_green": True,
        "confidence": 0.82,
        "threshold": 0.90,
        "started_at": "2026-05-19T00:00:00",
        "finalized_at": "2026-05-19T00:05:00",
    })
    db_session.add(version)
    await db_session.commit()
    await db_session.refresh(version)
    attempts_before = version.qualification_attempts

    mocker.patch(
        "app.github.get_repo_installation_token",
        new=AsyncMock(return_value="gh-token"),
    )
    # automain HEAD still equals the not_promoted candidate.
    mocker.patch(
        "app.github.get_branch_sha",
        new=AsyncMock(return_value=candidate_sha),
    )
    mocker.patch(
        "app.github.get_check_runs_cached",
        new=AsyncMock(return_value={"conclusion": "success"}),
    )

    result = await rq.run_qualification(
        db_session, project.id, version, explicit_request=True
    )
    await db_session.commit()

    assert result["status"] == "cached_verdict"
    assert result["cached_verdict_status"] == "not_promoted"

    await db_session.refresh(version)
    assert version.last_qualification["status"] == "not_promoted"
    assert version.qualification_attempts == attempts_before

    # No child tasks spawned.
    from sqlmodel import select

    tasks = (
        (await db_session.execute(select(Task).where(Task.project_id == project.id)))
        .scalars()
        .all()
    )
    assert len(tasks) == 0


@pytest.mark.asyncio
async def test_run_qualification_legacy_smoke_rejection_bypasses_same_sha_cache(
    db_session, mocker
):
    """Retiring a gate can turn its historical rejection into a pass."""
    project, version = await _make_project_and_version(db_session, phase="stabilize")
    candidate_sha = "smokeret" * 5
    version.last_qualification = {
        "status": "not_promoted",
        "candidate_sha": candidate_sha,
        "smoketest_enabled": True,
        "sub_scores": {"audit": 1.0, "test_erosion": 1.0, "smoketest": 0.0},
        "blocker": "smoketest_startup_failed",
        "ci_green": True,
        "confidence": 0.0,
    }
    db_session.add(version)
    await db_session.commit()
    await db_session.refresh(version)

    mocker.patch(
        "app.github.get_repo_installation_token",
        new=AsyncMock(return_value="gh-token"),
    )
    mocker.patch(
        "app.github.get_branch_sha",
        new=_branch_sha_mock(automain=candidate_sha),
    )
    mocker.patch(
        "app.release_qualifier._resolve_candidate_ci",
        new=AsyncMock(
            return_value={
                "state": "green",
                "ci_green": True,
                "resolved_sha": candidate_sha,
                "detail": "checks passed",
            }
        ),
    )
    mocker.patch(
        "app.release_qualifier._is_target_absorbed", new=AsyncMock(return_value=True)
    )

    result = await rq.run_qualification(
        db_session, project.id, version, explicit_request=True
    )
    await db_session.commit()

    assert result["status"] == "spawned"
    await db_session.refresh(version)
    assert version.last_qualification["status"] == "pending"
    assert version.last_qualification["smoketest_enabled"] is False
    assert version.last_qualification["smoketest_task_id"] is None
    assert len(await _audit_tasks(db_session, project.id)) == 1


@pytest.mark.asyncio
async def test_run_qualification_legacy_smoke_dry_run_bypasses_same_sha_cache(
    db_session, mocker
):
    """A legacy smoke-participating dry-run is not a current-policy pass."""
    project, version = await _make_project_and_version(db_session, phase="stabilize")
    candidate_sha = "smokedry" * 5
    version.last_qualification = {
        "status": "dry_run",
        "would_promote": True,
        "candidate_sha": candidate_sha,
        "smoketest_enabled": True,
        "smoketest_task_id": "legacy-smoke-task",
        "sub_scores": {"audit": 1.0, "test_erosion": 1.0, "smoketest": 1.0},
        "ci_green": True,
        "confidence": 1.0,
    }
    db_session.add(version)
    await db_session.commit()
    await db_session.refresh(version)

    mocker.patch(
        "app.github.get_repo_installation_token",
        new=AsyncMock(return_value="gh-token"),
    )
    mocker.patch(
        "app.github.get_branch_sha",
        new=_branch_sha_mock(automain=candidate_sha),
    )
    mocker.patch(
        "app.release_qualifier._resolve_candidate_ci",
        new=AsyncMock(
            return_value={
                "state": "green",
                "ci_green": True,
                "resolved_sha": candidate_sha,
                "detail": "checks passed",
            }
        ),
    )
    mocker.patch(
        "app.release_qualifier._is_target_absorbed", new=AsyncMock(return_value=True)
    )

    result = await rq.run_qualification(
        db_session, project.id, version, explicit_request=True
    )
    await db_session.commit()

    assert result["status"] == "spawned"
    await db_session.refresh(version)
    assert version.last_qualification["status"] == "pending"
    assert version.last_qualification["smoketest_enabled"] is False
    assert version.last_qualification["smoketest_task_id"] is None
    assert len(await _audit_tasks(db_session, project.id)) == 1


@pytest.mark.asyncio
async def test_run_qualification_ci_unresolved_bypasses_cached_verdict(
    db_session, mocker
):
    """A same-SHA not_promoted/ci_unresolved verdict is transient, not cached."""
    project, version = await _make_project_and_version(db_session, phase="stabilize")

    candidate_sha = "ciunres0" * 5
    version.last_qualification = {
        "status": "not_promoted",
        "candidate_sha": candidate_sha,
        "blocker": "ci_unresolved",
        "ci_green": False,
        "ci_resolution": {"state": "pending", "ci_green": False},
        "confidence": 0.0,
        "started_at": "2026-05-19T00:00:00",
        "finalized_at": "2026-05-19T00:05:00",
    }
    db_session.add(version)
    await db_session.commit()
    await db_session.refresh(version)

    mocker.patch(
        "app.github.get_repo_installation_token",
        new=AsyncMock(return_value="gh-token"),
    )
    mocker.patch(
        "app.github.get_branch_sha",
        new=_branch_sha_mock(automain=candidate_sha),
    )
    mocker.patch(
        "app.release_qualifier._resolve_candidate_ci",
        new=AsyncMock(
            return_value={
                "state": "green",
                "ci_green": True,
                "resolved_sha": candidate_sha,
                "detail": "checks passed",
            }
        ),
    )
    mocker.patch(
        "app.release_qualifier._is_target_absorbed", new=AsyncMock(return_value=True)
    )

    result = await rq.run_qualification(
        db_session, project.id, version, explicit_request=True
    )
    await db_session.commit()

    assert result["status"] == "spawned"
    assert result["candidate_sha"] == candidate_sha

    await db_session.refresh(version)
    assert version.last_qualification["status"] == "pending"
    assert version.last_qualification["candidate_sha"] == candidate_sha
    assert len(await _audit_tasks(db_session, project.id)) == 1


@pytest.mark.asyncio


@pytest.mark.asyncio


@pytest.mark.asyncio


@pytest.mark.asyncio
async def test_run_qualification_cached_promoted_same_sha_is_noop(db_session, mocker):
    """Idempotent qualification: when a conclusive promoted verdict exists
    for this candidate_sha and automain HEAD still equals it, run_qualification
    early-returns cached_verdict and spawns nothing — a successfully promoted
    commit is not re-audited."""
    project, version = await _make_project_and_version(db_session, phase="stabilize")

    candidate_sha = "promot00" * 5  # 40-char placeholder SHA
    version.last_qualification = _with_current_contract(project, version, {
        "status": "promoted",
        "candidate_sha": candidate_sha,
        "ci_green": True,
        "confidence": 0.95,
        "threshold": 0.90,
        "started_at": "2026-05-19T00:00:00",
        "finalized_at": "2026-05-19T00:05:00",
    })
    db_session.add(version)
    await db_session.commit()
    await db_session.refresh(version)
    attempts_before = version.qualification_attempts

    mocker.patch(
        "app.github.get_repo_installation_token",
        new=AsyncMock(return_value="gh-token"),
    )
    # automain HEAD still equals the promoted candidate.
    mocker.patch(
        "app.github.get_branch_sha",
        new=AsyncMock(return_value=candidate_sha),
    )
    mocker.patch(
        "app.github.get_check_runs_cached",
        new=AsyncMock(return_value={"conclusion": "success"}),
    )

    result = await rq.run_qualification(
        db_session, project.id, version, explicit_request=True
    )
    await db_session.commit()

    assert result["status"] == "cached_verdict"
    assert result["cached_verdict_status"] == "promoted"

    await db_session.refresh(version)
    assert version.last_qualification["status"] == "promoted"
    assert version.qualification_attempts == attempts_before

    # No child tasks spawned.
    from sqlmodel import select

    tasks = (
        (await db_session.execute(select(Task).where(Task.project_id == project.id)))
        .scalars()
        .all()
    )
    assert len(tasks) == 0


@pytest.mark.asyncio
async def test_run_qualification_non_conclusive_error_always_retries(
    db_session, mocker
):
    """Non-idempotent infra/error verdicts: when a prior qualification
    finalized with an error status (not a conclusive verdict) and the
    candidate_sha is unchanged, run_qualification proceeds with a fresh run
    (not cached) because the error produced no real quality verdict."""
    project, version = await _make_project_and_version(db_session, phase="stabilize")

    candidate_sha = "errorsh0" * 5  # 40-char placeholder SHA
    version.last_qualification = {
        "status": "error",
        "error": "spawn_failed",
        "candidate_sha": candidate_sha,
        "started_at": "2026-05-19T00:00:00",
    }
    db_session.add(version)
    await db_session.commit()
    await db_session.refresh(version)

    # Create a mock audit task to return from spawn.
    audit_task = Task(
        project_id=project.id,
        title="audit-retry",
        payloads=[{"type": "release_audit", "base_branch": "main"}],
        vm_instance_id="vm-audit-retry",
        status=TaskStatus.QUEUED,
        context={"release_version_id": str(version.id)},
    )
    db_session.add(audit_task)
    await db_session.commit()
    await db_session.refresh(audit_task)

    mocker.patch(
        "app.github.get_repo_installation_token",
        new=AsyncMock(return_value="gh-token"),
    )
    # automain HEAD still equals the error candidate.
    mocker.patch(
        "app.github.get_branch_sha",
        new=AsyncMock(return_value=candidate_sha),
    )
    mocker.patch(
        "app.github.get_check_runs_cached",
        new=AsyncMock(return_value={"conclusion": "success"}),
    )
    mocker.patch(
        "app.release_qualifier._spawn_release_audit_task",
        new=AsyncMock(return_value=audit_task),
    )

    result = await rq.run_qualification(
        db_session, project.id, version, explicit_request=True
    )
    await db_session.commit()

    # Not cached — a fresh qualification spawned despite unchanged sha.
    assert result["status"] == "spawned"


@pytest.mark.asyncio
async def test_legacy_pending_rubric_reconstructed_from_merge_grade_payload(
    db_session,
):
    project, version = await _make_project_and_version(db_session)
    task = Task(
        project_id=project.id,
        title="legacy merge grade",
        payloads=[{
            "type": "release_merge_grade",
            "categories": [{
                "category_key": "c-legacy",
                "text": "Legacy payload criterion",
            }],
        }],
        vm_instance_id="vm-legacy-grade",
        status=TaskStatus.COMPLETED,
    )
    db_session.add(task)
    await db_session.flush()
    qual = {"status": "pending", "merge_grade_task_id": str(task.id)}
    await db_session.commit()

    snapshot, provenance, error = await rq._resolve_launched_rubric(
        db_session, version, qual
    )
    assert error is None
    assert snapshot == [{
        "criterion_key": "c-legacy",
        "text": "Legacy payload criterion",
    }]
    assert provenance["source"] == "legacy_merge_grade_task_payload"
    assert provenance["fingerprint"] == rq._rubric_fingerprint(snapshot)


@pytest.mark.asyncio
async def test_legacy_pending_without_snapshot_or_task_is_provenance_unknown(
    db_session,
):
    project, version = await _make_project_and_version(db_session)
    snapshot, provenance, error = await rq._resolve_launched_rubric(
        db_session, version, {"status": "pending"}
    )

    assert snapshot is None
    assert provenance is None
    assert error == "pending_attempt_has_no_rubric_snapshot_or_grade_payload"


@pytest.mark.asyncio
async def test_legacy_pending_unknown_rubric_holds_without_live_substitution(
    db_session, mocker
):
    project, version = await _make_project_and_version(db_session)
    await _add_version_criterion(
        db_session,
        project,
        version,
        key="c-unknown",
        text="Must not be borrowed from live state",
    )
    await _seed_qualification_children(
        db_session,
        project,
        version,
        audit_logs={"audit_score": 1.0, "test_erosion": {}},
        smoke_logs={"passed": True, "suites": {"startup": {"passed": True}}},
        version_goal="Ship provenance safety",
        seed_merge_grade=True,
        merge_grade_logs={"covers_goal": True, "coverage_score": 1.0},
    )
    qual = dict(version.last_qualification or {})
    for key in ("rubric_snapshot", "rubric_fingerprint", "rubric_lifecycle_snapshot"):
        qual.pop(key, None)
    merge_task = await db_session.get(Task, uuid.UUID(qual["merge_grade_task_id"]))
    merge_task.payloads = [
        {key: value for key, value in merge_task.first_payload.items() if key != "categories"}
    ]
    version.last_qualification = qual
    db_session.add_all([version, merge_task])
    await db_session.commit()
    await db_session.refresh(version)
    promote_spy = mocker.patch("app.release_qualifier.promote", new=AsyncMock())

    result = await rq._finalize_qualification(
        db_session, version, _pre_gates=dict(_REUSE_PATH_PRE_GATES)
    )
    await db_session.commit()

    assert result["status"] == "rubric_provenance_unknown"
    assert result["rubric_provenance_error"] == "merge_grade_task_payload_has_no_rubric"
    promote_spy.assert_not_called()
    await db_session.refresh(version)
    assert version.last_qualification["category_verdicts"] is None


@pytest.mark.asyncio
async def test_criterion_coverage_incomplete_blocks_qualification(
    db_session, mocker
):
    """An active acceptance CATEGORY graded not_met by the blind merge-grade
    hard-blocks qualification via the goal-verification gate (per-category verdict
    ledger), re-emitting the preserved criterion_coverage_incomplete blocker."""
    from app import shared_state

    shared_state.runtime_config["promotion_enabled"] = True

    project, version = await _make_project_and_version(db_session)
    await _seed_qualification_children(
        db_session,
        project,
        version,
        audit_logs={
            "audit_score": 1.0,
            "test_erosion": {
                "deleted_test_files": [],
                "assertion_delta": 5,
                "test_count_delta": 2,
            },
            "covers_goal": True,
            "coverage_score": 0.9,
            "biggest_gap": None,
            "category_verdicts": [
                {"category_key": "c-covered1", "status": "met",
                 "citations": [{"kind": "diff", "ref": "app/widget.py:10"}]},
                {"category_key": "c-missing1", "status": "not_met",
                 "gap": "No reachability evidence for the widget path.",
                 "citations": []},
            ],
        },
        smoke_logs={
            "passed": True,
            "tests": [{"name": "a", "status": "pass"}],
            "suites": {"startup": {"passed": True}},
        },
        # Goal-verification redesign: the merge-grade agent grades the merged diff
        # per ACTIVE acceptance-criterion category.  Give the version a goal and a
        # merge-grade artifact that grades c-missing1 not_met (its criterion had no
        # merge evidence) so the per-category gate fires from the ledger.
        version_goal="Ship the widget end to end",
    )
    await _add_version_criterion(
        db_session,
        project,
        version,
        key="c-covered1",
        text="Merge-test evidence exists",
        source_kind="task_merge",
    )
    await _add_version_criterion(
        db_session,
        project,
        version,
        key="c-missing1",
        text="Reachability evidence missing",
    )
    promote_spy = mocker.patch("app.release_qualifier.promote", new=AsyncMock())

    result = await rq._finalize_qualification(db_session, version)
    await db_session.commit()

    assert result["status"] == "not_promoted"
    assert result["blocker"] == "criterion_coverage_incomplete"
    assert result["confidence"] == 0.0
    promote_spy.assert_not_called()

    await db_session.refresh(version)
    assert version.last_qualification["blocker"] == "criterion_coverage_incomplete"
    # Coverage summary is still computed for observability (evidence-based).
    assert version.last_qualification["criterion_coverage"]["total"] == 2
    assert version.last_qualification["criterion_coverage"]["covered"] == 1
    assert version.last_qualification["criterion_coverage"]["uncovered"] == 1
    # The per-category verdicts that actually DROVE the gate are persisted + the
    # uncovered category grades not_met.
    _cv = {
        v["category_key"]: v
        for v in version.last_qualification["category_verdicts"]
    }
    assert _cv["c-missing1"]["status"] == "not_met"
    assert _cv["c-covered1"]["status"] == "met"


@pytest.mark.asyncio
async def test_finalize_gate_ignores_retracted_criterion_stale_not_met_verdict(
    db_session, mocker
):
    """Regression: a RETRACTED criterion's stale ``not_met`` CategoryVerdict row
    must NOT re-block the finalize goal-verification gate.

    Retraction flips a criterion's status to non-active but never CLEARS its
    ``CategoryVerdict`` row.  The finalize gate reads verdicts back from the
    sha-keyed ledger; before the fix it read EVERY row for the (version, sha) pair
    — including the retracted criterion's stale ``not_met`` — so the gate stayed
    permanently blocked even though the sweep's ``is_candidate_fully_met`` (which
    filters to ACTIVE criteria) reported the candidate FULLY MET.  That mismatch is
    the permanent finalize wedge + re-qualification loop this test guards.  The
    gate now filters the read-back to the currently-active criterion keys, so with
    only ``A`` active (and ``A`` met) the gate CLEARS.  Driven on the dry-run track
    (promotion disabled) so the persisted verdict is observable: without the fix the
    stale row re-blocks with ``criterion_coverage_incomplete`` (not_promoted).
    """
    from app import category_verdict_ledger as _cvl
    from app import shared_state

    # Dry-run track: the candidate WOULD promote (gate cleared) but promotion ships
    # dark, so _finalize_qualification records the composite on last_qualification
    # (blocker + the filtered category_verdicts) instead of delegating to promote().
    shared_state.runtime_config["promotion_enabled"] = False
    mocker.patch(
        "app.release_qualifier.resolve_project_promotion_enabled",
        return_value=False,
    )

    _candidate_sha = "feed" * 10

    project, version = await _make_project_and_version(db_session)
    await _seed_qualification_children(
        db_session,
        project,
        version,
        audit_logs={
            "audit_score": 1.0,
            "test_erosion": {
                "deleted_test_files": [],
                "assertion_delta": 5,
                "test_count_delta": 2,
            },
            "covers_goal": True,
            "coverage_score": 0.95,
            "biggest_gap": None,
            "category_verdicts": [
                {"category_key": "c-active-A", "status": "met",
                 "citations": [{"kind": "diff", "ref": "app/widget.py:10"}]},
            ],
        },
        smoke_logs={
            "passed": True,
            "tests": [{"name": "a", "status": "pass"}],
            "suites": {"startup": {"passed": True}},
        },
        version_goal="Ship the widget end to end",
        # The re-grade at candidate X grades ONLY the still-active criterion A;
        # criterion B was retracted, so the merge-grade rubric no longer includes
        # it (evaluate_merge_grade grades against the ACTIVE categories).
    )
    # A stays ACTIVE and carries machine-verified merge evidence → it is covered.
    await _add_version_criterion(
        db_session,
        project,
        version,
        key="c-active-A",
        text="Widget path proven",
        source_kind="task_merge",
    )
    # B was RETRACTED (an uncoverable criterion the operator retired) — it is no
    # longer part of the acceptance contract, so it is excluded from
    # active_categories at finalize.
    retracted_b = VersionAcceptanceCriterion(
        project_id=project.id,
        release_version_id=version.id,
        criterion_key="c-retracted-B",
        text="Unreachable criterion since retired",
        status="retracted",
        created_by="test",
    )
    db_session.add(retracted_b)
    await db_session.flush()
    # Its stale ``not_met`` verdict from a PRIOR finalize (when B was still active)
    # persists in the sha-keyed ledger at THIS candidate sha — retraction never
    # cleared it.  This is the exact row that used to permanently re-block the gate.
    await _cvl.upsert_category_verdict(
        db_session,
        version_id=version.id,
        candidate_sha=_candidate_sha,
        category_key="c-retracted-B",
        status="not_met",
        tier="judged",
        gap="No reachability evidence — criterion since retracted.",
        citations=[],
        grader_run_id="prior-run",
    )
    await db_session.commit()

    promote_spy = mocker.patch("app.release_qualifier.promote", new=AsyncMock())

    result = await rq._finalize_qualification(db_session, version)
    await db_session.commit()

    # The gate CLEARS: B's stale not_met is filtered out (retracted → inactive),
    # leaving only the met active criterion A, so the goal-verification blocker does
    # NOT fire.  The candidate WOULD promote (dry-run track), not hold at
    # not_promoted / criterion_coverage_incomplete as it did before the fix.
    assert result["status"] == "dry_run"
    assert result["would_promote"] is True
    promote_spy.assert_not_called()

    await db_session.refresh(version)
    # The gate cleared: no goal-verification blocker on the recorded verdict.
    assert version.last_qualification["blocker"] is None
    # The persisted verdicts that DROVE the gate exclude the retracted criterion, so
    # the release_promotion CI-unresolved recompute inherits the same filtered set.
    _gate_keys = {
        v["category_key"]
        for v in (version.last_qualification.get("category_verdicts") or [])
    }
    assert "c-retracted-B" not in _gate_keys
    assert "c-active-A" in _gate_keys


@pytest.mark.asyncio
async def test_finalize_dual_writes_per_dimension_ledger(db_session, mocker):
    """Phase 1: _finalize_qualification dual-writes a per-(candidate_sha, dimension)
    QualificationDimensionResult row for EVERY resolved dimension, alongside the
    existing last_qualification / QualificationVerdict writes, and a single-gate
    re-record upserts in place without discarding the other dimensions.

    Grounds the direction's success evidence: "A gate re-check ... does not
    discard or re-run other gates that already passed for the same
    candidate_sha".
    """
    from app import shared_state

    # Dry-run track so finalize records the composite and leaves the version open
    # (no promote() side effects) while still flowing through the dual-write.
    shared_state.runtime_config["promotion_enabled"] = False
    mocker.patch(
        "app.release_qualifier.resolve_project_promotion_enabled",
        return_value=False,
    )

    project, version = await _make_project_and_version(db_session)
    await _seed_qualification_children(
        db_session,
        project,
        version,
        audit_logs={
            "audit_score": 1.0,
            "test_erosion": {
                "deleted_test_files": [],
                "assertion_delta": 3,
                "test_count_delta": 1,
            },
        },
        smoke_logs={
            "passed": True,
            "tests": [{"name": "a", "status": "pass"}],
            "suites": {"startup": {"passed": True}},
        },
    )
    promote_spy = mocker.patch("app.release_qualifier.promote", new=AsyncMock())

    result = await rq._finalize_qualification(db_session, version)
    await db_session.commit()

    assert result["status"] == "dry_run"
    promote_spy.assert_not_called()

    candidate_sha = version.last_qualification["candidate_sha"]

    rows = (
        (
            await db_session.execute(
                select(QualificationDimensionResult).where(
                    QualificationDimensionResult.release_version_id == version.id
                )
            )
        )
        .scalars()
        .all()
    )
    by_dim = {r.dimension: r for r in rows}

    # Every dimension is recorded exactly once for this candidate tree.
    # env_preflight IS ledger_backed (mutable, re-measured live) so it is
    # dual-written too — the judge is DARK by default in this fixture, so its row
    # records the inert "unavailable" state.
    expected_dims = {
        "ci",
        "audit",
        "test_erosion",
        "challenge",
        "migration_identity",
        "migration_apply",
        "merge_grade",
        "acceptance_window",
        "debt_pressure",
        "capability_proof",
        "env_preflight",
    }
    assert set(by_dim) == expected_dims
    assert len(rows) == len(expected_dims), "dual-write must not duplicate rows"
    assert all(r.candidate_sha == candidate_sha for r in rows)
    assert all(r.project_id == project.id for r in rows)

    # The clean-candidate outcomes are recorded with the right status/sub_score.
    assert by_dim["ci"].status == "passed"
    assert by_dim["ci"].blocker is None
    assert by_dim["audit"].status == "passed"
    assert by_dim["audit"].sub_score == 1.0
    assert by_dim["test_erosion"].status == "passed"
    assert by_dim["migration_identity"].status == "passed"
    assert by_dim["migration_apply"].status == "passed"
    # No version goal / merge-grade child was seeded → not-applicable → unavailable.
    assert by_dim["merge_grade"].status == "unavailable"
    # env_preflight judge is DARK by default (not enabled in this fixture) → the
    # deployed-side judge is inert, so no score and the row is unavailable.
    assert by_dim["env_preflight"].status == "unavailable"
    assert by_dim["env_preflight"].blocker is None
    assert by_dim["env_preflight"].sub_score is None

    # Failure-isolation (Phase 1b enabler): re-recording ONE dimension for the
    # SAME candidate tree upserts that dimension's row in place (no duplicate) and
    # leaves the OTHER already-passed dimensions untouched — this is exactly the
    # "re-check a gate without discarding other gates" property.
    await rq._upsert_dimension_result(
        db_session,
        version,
        candidate_sha,
        "ci",
        status="blocked",
        blocker="ci_not_green",
        detail={"state": "red"},
    )
    await db_session.commit()

    rows2 = (
        (
            await db_session.execute(
                select(QualificationDimensionResult).where(
                    QualificationDimensionResult.release_version_id == version.id
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(rows2) == len(rows), "single-gate re-record must UPSERT, not append"
    by_dim2 = {r.dimension: r for r in rows2}
    assert by_dim2["ci"].status == "blocked"
    assert by_dim2["ci"].blocker == "ci_not_green"
    # The other dimensions that already passed are NOT discarded by the re-check.
    assert by_dim2["audit"].status == "passed"
    assert by_dim2["audit"].sub_score == 1.0


@pytest.mark.asyncio
async def test_dual_write_tags_blocked_dimension_execution_site(db_session):
    """The per-dimension ledger tags the blocked dimension's detail with its
    execution-site (dir: tag promotion blockers inert-until-promote).

    A DEPLOYED-side blocker (migration-identity collision, which flows through the
    comprehensive dual-write rather than the early audit/test-erosion writes) is
    tagged execution_site='deployed' on its ledger row — signalling it is inert
    until this version itself promotes — while dimensions that did not block carry
    no execution-site tag.
    """
    project, version = await _make_project_and_version(db_session)
    candidate_sha = "deadbeef" * 5  # 40-char candidate SHA

    base_record = {
        "candidate_sha": candidate_sha,
        "ci_green": True,
        "ci_resolution": {"state": "green"},
        "smoketest_enabled": True,
        "sub_scores": {},
        # Deployed-side gate fired: revision-identity collision against the
        # deployed promotion target.
        "migration_safety": "block",
        "migration_safety_detail": "0459 collides with the deployed head",
        "migration_apply_safety": "ok",
    }
    composite = {"blocker": "migration_identity_collision"}

    await rq._dual_write_dimension_results(db_session, version, base_record, composite)
    await db_session.commit()

    rows = (
        (
            await db_session.execute(
                select(QualificationDimensionResult).where(
                    QualificationDimensionResult.release_version_id == version.id
                )
            )
        )
        .scalars()
        .all()
    )
    by_dim = {r.dimension: r for r in rows}

    # Deployed-side: migration-identity block is inert until this version promotes.
    assert by_dim["migration_identity"].status == "blocked"
    assert by_dim["migration_identity"].blocker == "migration_identity_collision"
    assert (
        (by_dim["migration_identity"].detail or {}).get("execution_site")
        == "deployed"
    )

    # Dimensions that did not block carry no execution-site tag.
    assert by_dim["ci"].status == "passed"
    assert by_dim["ci"].blocker is None
    assert (by_dim["migration_apply"].detail or {}).get("execution_site") is None


class TestInferDimensionFromInfraError:
    """_infer_dimension_from_infra_error maps every live infra_error code."""

    @pytest.mark.parametrize(
        ("infra_error", "expected"),
        [
            # audit leg (default) — every audit_* / bare / unknown code
            ("audit_task_failed", "audit"),
            ("audit_task_cancelled", "audit"),
            ("audit_task_blocked", "audit"),
            ("audit_report_missing", "audit"),
            ("audit_report_parseable_but_unscoreable", "audit"),
            ("audit_report_empty_dict", "audit"),
            ("audit_report_findings_wrong_type", "audit"),
            ("audit_rate_limit_respawn_cap_exceeded", "audit"),
            ("audit_infra_respawn_cap_exceeded", "audit"),
            # _enrich_infra_error appends a sanitized category suffix — prefix match
            ("audit_task_failed_provision_failure", "audit"),
            # challenge sub-leg owns a distinct dimension (matched before 'audit')
            ("audit_challenge_missing", "challenge"),
            ("audit_challenge_sha_mismatch", "challenge"),
            ("audit_challenge_binding_incomplete", "challenge"),
            ("challenge_infra_respawn_cap_exceeded", "challenge"),
            # the two distinct migration gates
            ("migration_identity_check_unavailable", "migration_identity"),
            ("migration_apply_check_unavailable", "migration_apply"),
            ("migration_something_else", "migration_identity"),
            # smoketest / merge-grade / ci / test-erosion
            ("smoketest_task_failed", "smoketest"),
            ("smoketest_report_missing", "smoketest"),
            ("merge_grade_task_failed", "merge_grade"),
            ("merge_grade_report_missing", "merge_grade"),
            ("merge_grade_report_unparseable", "merge_grade"),
            ("ci_not_green", "ci"),
            ("test_erosion_regressed", "test_erosion"),
            # ledger-integrity codes → None (no dimension; caller skips upsert)
            ("qualification_dimension_ledger_incomplete", None),
            ("qualification_dimension_ledger_empty", None),
            # unrecognized / empty → 'audit' fallback
            ("something_unrecognized", "audit"),
            ("", "audit"),
        ],
    )
    def test_maps_code_to_dimension(self, infra_error, expected):
        from app import release_qualifier_audit as rqa

        assert rqa._infer_dimension_from_infra_error(infra_error) == expected


class TestInferInfraLeg:
    """_infer_infra_leg maps infra_error codes to the leg component of the
    infra-failure signature.  Mirror of TestInferDimensionFromInfraError above
    (same migration-prefix ordering, same fallback behavior)."""

    @pytest.mark.parametrize(
        ("infra_error", "expected"),
        [
            # challenge sub-leg (matched before bare 'audit')
            ("audit_challenge_missing", "challenge"),
            ("audit_challenge_binding_incomplete", "challenge"),
            ("challenge_infra_respawn_cap_exceeded", "challenge"),
            # the two distinct migration gates (matched before bare 'migration')
            ("migration_identity_check_unavailable", "migration_identity"),
            ("migration_apply_check_unavailable", "migration_apply"),
            ("migration_apply_check_unavailable: scratch DB timeout", "migration_apply"),
            ("migration_something_else", "migration_identity"),
            # smoketest
            ("smoketest_task_failed", "smoketest"),
            ("smoketest_report_missing", "smoketest"),
            # audit (default) — every audit_* / bare / unknown code
            ("audit_task_failed", "audit"),
            ("audit_report_missing", "audit"),
            ("audit_infra_respawn_cap_exceeded", "audit"),
            ("audit_task_failed_provision_failure", "audit"),
            # unrecognized / empty → 'audit' fallback
            ("something_unrecognized", "audit"),
            ("", "audit"),
        ],
    )
    def test_infers_leg(self, infra_error, expected):
        from app import release_qualifier_audit as rqa

        assert rqa._infer_infra_leg(infra_error) == expected


@pytest.mark.asyncio
async def test_finalize_infra_failure_persists_partial_dimension_results(
    db_session, mocker
):
    """When _finalize_qualification returns via _handle_infra_failure at a LATE
    gate (after audit/smoketest scoring, before _dual_write_dimension_results),
    the already-computed dimension results (CI, audit, test_erosion, smoketest)
    are persisted in the per-dimension ledger via eager writes, surviving for
    the re-drive's spawn-skip logic."""
    from app import shared_state

    shared_state.runtime_config["promotion_enabled"] = True

    project, version = await _make_project_and_version(db_session)
    await _seed_qualification_children(
        db_session, project, version,
        audit_logs={
            # One high-severity finding with a verification_note so the
            # deterministic _score_audit deducts 0.15 → derived score 0.85.
            "findings": [
                {
                    "severity": "high",
                    "verification_note": "enough chars to pass the verification threshold ------",
                },
            ],
            "test_erosion": {
                "deleted_test_files": [],
                "assertion_delta": 5,
                "test_count_delta": 2,
            },
        },
        smoke_logs={
            "passed": True,
            "tests": [{"name": "a", "status": "pass"}],
            "suites": {"startup": {"passed": True}},
        },
    )

    # Make the migration-identity gate defer (GitHub fetch failure), so
    # _finalize_qualification returns via _handle_infra_failure at line ~4665
    # -- after CI, audit, and smoketest have been scored and eagerly written
    # to the ledger, but before _dual_write_dimension_results (line 5065).
    mocker.patch(
        "app.github.list_version_shas_at_ref",
        new=AsyncMock(side_effect=RuntimeError("GitHub 502")),
    )
    promote_spy = mocker.patch(
        "app.release_qualifier.promote", new=AsyncMock()
    )

    result = await rq._finalize_qualification(db_session, version)
    await db_session.commit()

    # The finalize was held as an infra failure -- not promoted.
    assert result["status"] == "infra_failure"
    assert result["error"].startswith("migration_identity_check_unavailable")
    assert "GitHub 502" in result["error"]
    promote_spy.assert_not_called()

    # Query the per-dimension ledger.
    by_dim = await _dimension_rows_by_dim(db_session, version)
    candidate_sha = version.last_qualification.get("candidate_sha")

    # CI was eagerly written with the correct status.
    assert "ci" in by_dim
    assert by_dim["ci"].status == "passed"  # ci_green=True from _seed
    assert by_dim["ci"].candidate_sha == candidate_sha

    # Audit was eagerly written with its computed sub_score and status (one
    # high-severity finding → derived score 0.85 → status "failed").
    assert "audit" in by_dim
    assert by_dim["audit"].status == "failed"
    assert by_dim["audit"].sub_score == 0.85
    assert by_dim["audit"].candidate_sha == candidate_sha

    # Test_erosion was eagerly written (no deleted test files -> passed).
    assert "test_erosion" in by_dim
    assert by_dim["test_erosion"].status == "passed"
    assert by_dim["test_erosion"].candidate_sha == candidate_sha

    # Legacy smoke artifacts never create a new qualifier ledger fact.
    assert "smoketest" not in by_dim

    # Migration_identity was recorded as "unavailable" by _handle_infra_failure.
    assert "migration_identity" in by_dim
    assert by_dim["migration_identity"].status == "unavailable"




@pytest.mark.asyncio
async def test_infra_failure_isolates_to_failing_dimension(db_session):
    """A migration-gate infra failure marks ONLY migration_identity 'unavailable'
    — the audit dimension's already-'passed' fact for the same candidate tree
    survives untouched (per-dimension failure isolation)."""
    from app import release_qualifier_audit as rqa

    project, version = await _make_project_and_version(db_session)

    # audit already passed for this candidate tree in an earlier finalize.
    await _seed_dimension_row(
        db_session, project, version, "audit", status="passed", sub_score=1.0
    )
    version.last_qualification = {
        "status": "pending",
        "candidate_sha": _LEDGER_SHA_A,
        "ci_green": True,
        "smoketest_enabled": True,
        "started_at": "2026-07-10T00:00:00",
    }
    db_session.add(version)
    await db_session.commit()

    result = await rqa._handle_infra_failure(
        db_session,
        version,
        dict(version.last_qualification),
        "migration_identity_check_unavailable",
    )
    await db_session.commit()

    # The attempt is held as an infra failure isolated to the migration gate.
    assert result["status"] == "infra_failure"
    assert result["failing_dimension"] == "migration_identity"
    assert result["dimension_infra_streak"] == 1

    by_dim = await _dimension_rows_by_dim(db_session, version)
    # Only the failing dimension was marked unavailable, with a PER-DIMENSION
    # streak recorded in its detail JSONB.
    assert by_dim["migration_identity"].status == "unavailable"
    assert by_dim["migration_identity"].detail["consecutive_infra_errors"] == 1
    assert (
        by_dim["migration_identity"].detail["infra_error"]
        == "migration_identity_check_unavailable"
    )
    # The audit fact that already passed is NOT discarded by the migration hiccup.
    assert by_dim["audit"].status == "passed"
    assert by_dim["audit"].sub_score == 1.0


@pytest.mark.asyncio
async def test_infra_failure_record_stamps_engine_sha(db_session, monkeypatch):
    """_handle_infra_failure stamps engine_sha into the top-level record and
    each attempt_entry in infra_attempt_history so engine vs candidate triage
    reads directly off the persisted qualification record."""
    from app import release_qualifier_audit as rqa

    monkeypatch.setenv("DEPLOY_SHA", "aabbccddeeff001")

    project, version = await _make_project_and_version(db_session)
    version.last_qualification = {
        "status": "pending",
        "candidate_sha": _LEDGER_SHA_A,
        "ci_green": True,
        "smoketest_enabled": True,
        "started_at": "2026-07-10T00:00:00",
    }
    db_session.add(version)
    await db_session.commit()

    result = await rqa._handle_infra_failure(
        db_session,
        version,
        dict(version.last_qualification),
        "migration_apply_check_unavailable",
    )
    await db_session.commit()

    assert result["status"] == "infra_failure"
    assert result["engine_sha"] == "aabbccddeeff001", (
        f"expected engine_sha 'aabbccddeeff001' in return value, got {result.get('engine_sha')!r}"
    )
    # Verify the persisted record also carries engine_sha.
    lq = version.last_qualification
    assert lq["engine_sha"] == "aabbccddeeff001", (
        f"expected engine_sha 'aabbccddeeff001' in last_qualification record, got {lq.get('engine_sha')!r}"
    )
    history = lq["infra_attempt_history"]
    assert history, "infra_attempt_history must be non-empty"
    latest = history[-1]
    assert latest["engine_sha"] == "aabbccddeeff001", (
        f"expected engine_sha 'aabbccddeeff001' in attempt_entry, got {latest.get('engine_sha')!r}"
    )


@pytest.mark.asyncio
async def test_infra_failure_engine_sha_none_when_deploy_sha_absent(db_session, monkeypatch):
    """When DEPLOY_SHA is not set, engine_sha degrades gracefully to None
    rather than raising — callers read 'unknown' in messages but the record
    stays structurally intact."""
    from app import release_qualifier_audit as rqa

    monkeypatch.delenv("DEPLOY_SHA", raising=False)

    project, version = await _make_project_and_version(db_session)
    version.last_qualification = {
        "status": "pending",
        "candidate_sha": _LEDGER_SHA_A,
        "ci_green": True,
        "smoketest_enabled": True,
        "started_at": "2026-07-10T00:00:00",
    }
    db_session.add(version)
    await db_session.commit()

    result = await rqa._handle_infra_failure(
        db_session,
        version,
        dict(version.last_qualification),
        "migration_apply_check_unavailable",
    )
    await db_session.commit()

    assert result["status"] == "infra_failure"
    assert result.get("engine_sha") is None, (
        f"expected engine_sha None when DEPLOY_SHA absent, got {result.get('engine_sha')!r}"
    )
    lq = version.last_qualification
    assert lq.get("engine_sha") is None, (
        f"expected engine_sha None in last_qualification, got {lq.get('engine_sha')!r}"
    )
    history = lq["infra_attempt_history"]
    assert history[-1].get("engine_sha") is None


@pytest.mark.asyncio
async def test_permanent_gate_incapacity_message_includes_engine_sha(db_session, monkeypatch):
    """The permanent-gate-incapacity operator notification embeds the engine SHA
    so the triage reader can distinguish engine build without parsing tracebacks."""
    from app import release_qualifier_audit as rqa
    from app import release_qualifier as rq_module
    from app.release_qualifier_audit import _MIN_INFRA_ERROR_STREAK, _infra_failure_signature

    monkeypatch.setenv("DEPLOY_SHA", "deadbeef12345ab")

    notified: list[str] = []

    async def _capture_notify(session, version, event, message, extra=None):
        notified.append(message)

    monkeypatch.setattr(rq_module, "_notify_operator", _capture_notify)

    project, version = await _make_project_and_version(db_session)
    # Compute the expected signature so pre-seeded history entries match.
    _infra_error = "migration_apply_check_unavailable"
    _sig = _infra_failure_signature(_infra_error, [])
    _seed_count = _MIN_INFRA_ERROR_STREAK - 1
    version.last_qualification = {
        "status": "pending",
        "candidate_sha": _LEDGER_SHA_A,
        "ci_green": True,
        "smoketest_enabled": True,
        "started_at": "2026-07-10T00:00:00",
        # prior_infra_streak drives current_streak so the alert fires.
        "prior_infra_streak": _seed_count,
        # Pre-seed matching-signature history so same_signature_streak reaches threshold.
        "infra_attempt_history": [
            {
                "finalized_at": "2026-07-10T00:00:00",
                "error": _infra_error,
                "signature": _sig,
                "candidate_sha": _LEDGER_SHA_A,
            }
        ] * _seed_count,
    }
    db_session.add(version)
    await db_session.commit()

    await rqa._handle_infra_failure(
        db_session,
        version,
        dict(version.last_qualification),
        _infra_error,
    )
    await db_session.commit()

    assert notified, "operator notification was never sent"
    msg = notified[-1]
    assert "deadbeef12345ab" in msg, (
        f"engine SHA 'deadbeef12345ab' not found in permanent-incapacity message: {msg!r}"
    )


@pytest.mark.asyncio
async def test_infra_failure_streak_is_per_dimension(db_session):
    """The consecutive-infra-error streak is tracked PER-DIMENSION: two migration
    infra failures raise migration_identity's streak to 2 while a single audit
    infra failure keeps the audit dimension at 1 — one gate's flakiness cannot
    burn another gate's retry budget."""
    from app import release_qualifier_audit as rqa

    project, version = await _make_project_and_version(db_session)
    version.last_qualification = {
        "status": "pending",
        "candidate_sha": _LEDGER_SHA_A,
        "ci_green": True,
        "smoketest_enabled": True,
        "started_at": "2026-07-10T00:00:00",
    }
    db_session.add(version)
    await db_session.commit()

    # migration gate fails twice for the same candidate tree.
    r1 = await rqa._handle_infra_failure(
        db_session, version, dict(version.last_qualification),
        "migration_identity_check_unavailable",
    )
    await db_session.commit()
    r2 = await rqa._handle_infra_failure(
        db_session, version, dict(version.last_qualification),
        "migration_identity_check_unavailable",
    )
    await db_session.commit()
    # the audit gate fails once.
    r3 = await rqa._handle_infra_failure(
        db_session, version, dict(version.last_qualification),
        "audit_report_missing",
    )
    await db_session.commit()

    assert r1["dimension_infra_streak"] == 1
    assert r2["dimension_infra_streak"] == 2
    assert r3["failing_dimension"] == "audit"
    assert r3["dimension_infra_streak"] == 1

    by_dim = await _dimension_rows_by_dim(db_session, version)
    assert by_dim["migration_identity"].detail["consecutive_infra_errors"] == 2
    assert by_dim["audit"].detail["consecutive_infra_errors"] == 1


@pytest.mark.asyncio
async def test_finalize_refreshes_migration_identity_from_ledger(db_session, mocker):
    """A legacy resolved_dimensions marker cannot suppress the live identity gate."""
    from app import shared_state

    shared_state.runtime_config["promotion_enabled"] = True

    project, version = await _make_project_and_version(db_session)
    await _seed_qualification_children(
        db_session,
        project,
        version,
        audit_logs={
            "audit_score": 1.0,
            "test_erosion": {
                "deleted_test_files": [],
                "assertion_delta": 0,
                "test_count_delta": 0,
            },
        },
        smoke_logs={
            "passed": True,
            "tests": [{"name": "a", "status": "pass"}],
            "suites": {"startup": {"passed": True}},
        },
    )

    # Simulate what run_qualification would have stored after loading a prior
    # migration_identity=passed fact from the ledger.
    qual = dict(version.last_qualification)
    qual["resolved_dimensions"] = {
        "migration_identity": {
            "status": "passed",
            "sub_score": None,
            "blocker": None,
            "detail": {"verdict": "ok", "detail": None},
        }
    }
    version.last_qualification = qual
    db_session.add(version)
    await db_session.commit()
    await db_session.refresh(version)

    # A live result distinct from the legacy row proves finalize re-measures it.
    identity_gate = mocker.patch(
        "app.release_qualifier._resolve_migration_identity_at_finalize",
        new=AsyncMock(return_value=("ok", "current-identity")),
    )
    # Let the apply gate succeed so finalize can proceed past both gates.
    mocker.patch(
        "app.release_qualifier._resolve_migration_apply_at_finalize",
        new=AsyncMock(return_value=("pass", None)),
    )
    mocker.patch("app.release_qualifier.promote", new=AsyncMock())

    result = await rq._finalize_qualification(db_session, version)
    await db_session.commit()

    assert result.get("status") != "infra_failure", (
        f"live migration_identity gate unexpectedly deferred: {result.get('error')}"
    )
    identity_gate.assert_awaited_once()
    by_dim = await _dimension_rows_by_dim(db_session, version)
    assert "migration_identity" in by_dim
    assert by_dim["migration_identity"].status == "passed"
    assert by_dim["migration_identity"].detail["detail"] == "current-identity"


@pytest.mark.asyncio
async def test_finalize_refreshes_migration_apply_from_ledger(db_session, mocker):
    """A legacy resolved_dimensions marker cannot suppress the live apply gate."""
    from app import shared_state

    shared_state.runtime_config["promotion_enabled"] = True

    project, version = await _make_project_and_version(db_session)
    await _seed_qualification_children(
        db_session,
        project,
        version,
        audit_logs={
            "audit_score": 1.0,
            "test_erosion": {
                "deleted_test_files": [],
                "assertion_delta": 0,
                "test_count_delta": 0,
            },
        },
        smoke_logs={
            "passed": True,
            "tests": [{"name": "a", "status": "pass"}],
            "suites": {"startup": {"passed": True}},
        },
    )

    # Simulate run_qualification having stored both migration gate facts from a
    # prior finalize — identity resolves via ledger AND apply resolves via ledger.
    qual = dict(version.last_qualification)
    qual["resolved_dimensions"] = {
        "migration_apply": {
            "status": "passed",
            "sub_score": None,
            "blocker": None,
            "detail": {"verdict": "pass", "detail": None},
        }
    }
    version.last_qualification = qual
    db_session.add(version)
    await db_session.commit()
    await db_session.refresh(version)

    # Let the identity gate succeed (it has no ledger entry here).
    mocker.patch(
        "app.release_qualifier._resolve_migration_identity_at_finalize",
        new=AsyncMock(return_value=("ok", None)),
    )
    # A live result distinct from the legacy row proves finalize re-measures it.
    apply_gate = mocker.patch(
        "app.release_qualifier._resolve_migration_apply_at_finalize",
        new=AsyncMock(return_value=("pass", "current-apply")),
    )
    mocker.patch("app.release_qualifier.promote", new=AsyncMock())

    result = await rq._finalize_qualification(db_session, version)
    await db_session.commit()

    assert result.get("status") != "infra_failure", (
        f"live migration_apply gate unexpectedly deferred: {result.get('error')}"
    )
    apply_gate.assert_awaited_once()
    by_dim = await _dimension_rows_by_dim(db_session, version)
    assert "migration_apply" in by_dim
    assert by_dim["migration_apply"].status == "passed"
    assert by_dim["migration_apply"].detail["detail"] == "current-apply"


@pytest.mark.asyncio
async def test_migration_apply_skipped_records_unavailable_in_ledger(db_session, mocker):
    """When MIGRATION_APPLY_SCRATCH_DATABASE_URL is absent the apply gate returns
    ('skipped', 'scratch_database_not_configured').  The dual-write ledger must
    record status='unavailable' — NOT 'passed' — so a skipped gate is never
    indistinguishable from a gate that genuinely verified the migration."""
    from app import shared_state

    shared_state.runtime_config["promotion_enabled"] = True

    project, version = await _make_project_and_version(db_session)
    await _seed_qualification_children(
        db_session,
        project,
        version,
        audit_logs={
            "audit_score": 1.0,
            "test_erosion": {
                "deleted_test_files": [],
                "assertion_delta": 0,
                "test_count_delta": 0,
            },
        },
        smoke_logs={
            "passed": True,
            "tests": [{"name": "a", "status": "pass"}],
            "suites": {"startup": {"passed": True}},
        },
    )

    mocker.patch(
        "app.release_qualifier._resolve_migration_identity_at_finalize",
        new=AsyncMock(return_value=("ok", None)),
    )
    # Simulate absent scratch DB config — gate never ran.
    mocker.patch(
        "app.release_qualifier._resolve_migration_apply_at_finalize",
        new=AsyncMock(return_value=("skipped", "scratch_database_not_configured")),
    )
    mocker.patch("app.release_qualifier.promote", new=AsyncMock())

    result = await rq._finalize_qualification(db_session, version)
    await db_session.commit()

    # Finalize must complete (not infra_failure) — the skipped gate is not fatal.
    assert result.get("status") != "infra_failure", (
        f"unexpected infra_failure when apply gate skipped: {result}"
    )

    by_dim = await _dimension_rows_by_dim(db_session, version)
    assert "migration_apply" in by_dim, "migration_apply ledger row missing"
    # CRITICAL: skipped must never read as passed — the whole point of the
    # distinct 'skipped' return value is to avoid a silent pass masquerade.
    assert by_dim["migration_apply"].status == "unavailable", (
        f"skipped apply gate wrote status={by_dim['migration_apply'].status!r}; "
        "expected 'unavailable' so a missing scratch DB cannot masquerade as a pass"
    )


@pytest.mark.asyncio
async def test_migration_gates_eagerly_persisted_before_dual_write(db_session, mocker):
    """migration_identity and migration_apply are eagerly written to the ledger
    right after their verdicts are computed — a later gate's infra failure cannot
    discard them before _dual_write_dimension_results is reached."""
    from app import shared_state

    shared_state.runtime_config["promotion_enabled"] = True

    project, version = await _make_project_and_version(db_session)
    await _seed_qualification_children(
        db_session,
        project,
        version,
        audit_logs={
            "audit_score": 1.0,
            "test_erosion": {
                "deleted_test_files": [],
                "assertion_delta": 0,
                "test_count_delta": 0,
            },
        },
        smoke_logs={
            "passed": True,
            "tests": [{"name": "a", "status": "pass"}],
            "suites": {"startup": {"passed": True}},
        },
    )

    mocker.patch(
        "app.release_qualifier._resolve_migration_identity_at_finalize",
        new=AsyncMock(return_value=("ok", None)),
    )
    mocker.patch(
        "app.release_qualifier._resolve_migration_apply_at_finalize",
        new=AsyncMock(return_value=("pass", None)),
    )
    # Suppress the end-of-finalize dual-write so only eager writes can populate
    # the ledger for these dimensions.
    mocker.patch(
        "app.release_qualifier._dual_write_dimension_results",
        new=AsyncMock(),
    )
    mocker.patch("app.release_qualifier.promote", new=AsyncMock())

    await rq._finalize_qualification(db_session, version)
    await db_session.commit()

    by_dim = await _dimension_rows_by_dim(db_session, version)

    assert "migration_identity" in by_dim, (
        "migration_identity ledger row missing — eager write not called"
    )
    assert by_dim["migration_identity"].status == "passed"

    assert "migration_apply" in by_dim, (
        "migration_apply ledger row missing — eager write not called"
    )
    assert by_dim["migration_apply"].status == "passed"


@pytest.mark.asyncio
async def test_mutable_dimension_ledger_refreshed_on_same_sha_redrive(db_session):
    """A same-SHA re-drive refreshes live CI/migration facts but retains audit.

    This is the merge-verdict ledger seam: the new measurement replaces only
    the mutable row's status/detail/origin, while an immutable audit verdict
    stays pinned to its original producer and score.
    """
    project, version = await _make_project_and_version(db_session)

    old_attempt = uuid.uuid4()
    new_attempt = uuid.uuid4()
    # Prior finalize has resolved facts for every mutable gate plus a distinctive
    # immutable audit score.  The next merge verdict disagrees with those live
    # facts, making a stale/reused write observable.
    await _seed_dimension_row(
        db_session, project, version, "audit", status="passed", sub_score=0.42,
        origin_attempt_id=old_attempt,
    )
    await _seed_dimension_row(
        db_session, project, version, "ci", status="passed",
        detail={"state": "old-green"}, origin_attempt_id=old_attempt,
    )
    await _seed_dimension_row(
        db_session, project, version, "migration_identity", status="passed",
        detail={"verdict": "ok", "detail": "old-identity"},
        origin_attempt_id=old_attempt,
    )
    await _seed_dimension_row(
        db_session, project, version, "migration_apply", status="blocked",
        detail={"verdict": "block", "detail": "old-apply"},
        origin_attempt_id=old_attempt,
    )
    db_session.add(version)
    await db_session.commit()

    base_record = {
        "candidate_sha": _LEDGER_SHA_A,
        "sub_scores": {"audit": 1.0, "test_erosion": 1.0, "smoketest": 1.0},
        "ci_green": False,
        "ci_resolution": {"state": "current-red"},
        "smoketest_enabled": True,
        "migration_safety": "block",
        "migration_safety_detail": "current-identity",
        "migration_apply_safety": "pass",
        "migration_apply_safety_detail": "current-apply",
        "merge_grade_status": "not_applicable",
        "qualification_attempt_id": str(new_attempt),
    }
    composite = {"blocker": None}

    await rq._dual_write_dimension_results(db_session, version, base_record, composite)
    await db_session.commit()

    by_dim = await _dimension_rows_by_dim(db_session, version)
    # audit is immutable → REUSED verbatim (distinctive 0.42 preserved, NOT
    # overwritten with the freshly-computed 1.0).
    assert by_dim["audit"].status == "passed"
    assert by_dim["audit"].sub_score == 0.42
    assert by_dim["audit"].origin_attempt_id == old_attempt
    # Every mutable gate is refreshed from this re-drive's merge verdict,
    # including facts that had already resolved on the same candidate SHA.
    assert by_dim["ci"].status == "blocked"
    assert by_dim["ci"].detail == {"state": "current-red"}
    assert by_dim["migration_identity"].status == "blocked"
    assert by_dim["migration_identity"].detail["detail"] == "current-identity"
    assert by_dim["migration_apply"].status == "passed"
    assert by_dim["migration_apply"].detail["detail"] == "current-apply"
    for dimension in ("ci", "migration_identity", "migration_apply"):
        assert by_dim[dimension].origin_attempt_id == new_attempt
    # No duplicate audit rows were created by the re-finalize (upsert-in-place).
    all_audit_rows = (
        (
            await db_session.execute(
                select(QualificationDimensionResult).where(
                    QualificationDimensionResult.release_version_id == version.id,
                    QualificationDimensionResult.dimension == "audit",
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(all_audit_rows) == 1


@pytest.mark.asyncio
async def test_promotion_dimension_ledger_complete_requires_full_same_sha_set(
    db_session,
):
    """The same-tree promotion invariant: _promotion_dimension_ledger_complete is
    True only when every required gating dimension has a terminal fact at ONE
    candidate_sha; a missing dimension or one stranded at a different sha fails."""
    project, version = await _make_project_and_version(db_session)

    required = ["ci", "audit", "test_erosion", "migration_identity",
                "migration_apply", "merge_grade"]
    for dim in required:
        await _seed_dimension_row(db_session, project, version, dim, status="passed")
    await db_session.commit()

    complete, missing = await rq._promotion_dimension_ledger_complete(
        db_session, version, _LEDGER_SHA_A
    )
    assert complete is True
    assert missing == []

    # A required dimension whose only fact is at a DIFFERENT candidate tree does
    # NOT satisfy the same-tree invariant.
    complete_b, missing_b = await rq._promotion_dimension_ledger_complete(
        db_session, version, _LEDGER_SHA_B
    )
    assert complete_b is False
    assert set(missing_b) == set(required)


@pytest.mark.asyncio
async def test_promotion_dimension_ledger_incomplete_when_dimension_missing(
    db_session,
):
    """A promotion may not compose from the ledger while a required gating
    dimension has no terminal fact at the candidate_sha."""
    project, version = await _make_project_and_version(db_session)

    # Everything except migration_apply is resolved for this candidate tree.
    for dim in ["ci", "audit", "test_erosion", "migration_identity", "merge_grade"]:
        await _seed_dimension_row(db_session, project, version, dim, status="passed")
    await db_session.commit()

    complete, missing = await rq._promotion_dimension_ledger_complete(
        db_session, version, _LEDGER_SHA_A
    )
    assert complete is False
    assert missing == ["migration_apply"]


@pytest.mark.asyncio
async def test_promotion_dimension_ledger_incomplete_when_required_fact_unavailable(
    db_session,
):
    """A transient unavailable fact is not durable terminal proof for a required
    dimension, even though the ledger row exists at the correct candidate SHA."""
    project, version = await _make_project_and_version(db_session)

    for dim in [
        "ci",
        "audit",
        "test_erosion",
        "migration_identity",
        "merge_grade",
    ]:
        await _seed_dimension_row(db_session, project, version, dim, status="passed")
    await _seed_dimension_row(
        db_session,
        project,
        version,
        "migration_apply",
        status="unavailable",
        detail={
            "infra_error": "migration_apply_check_unavailable",
            "verdict": "defer",
        },
    )
    await db_session.commit()

    complete, missing = await rq._promotion_dimension_ledger_complete(
        db_session, version, _LEDGER_SHA_A
    )
    assert complete is False
    assert missing == ["migration_apply"]


@pytest.mark.asyncio
async def test_promotion_dimension_ledger_accepts_explicit_non_applicable_fact(
    db_session,
):
    """An explicitly non-applicable merge-grade fact remains valid terminal
    evidence when there is no gradeable release goal."""
    project, version = await _make_project_and_version(db_session)

    for dim in [
        "ci",
        "audit",
        "test_erosion",
        "migration_identity",
        "migration_apply",
    ]:
        await _seed_dimension_row(db_session, project, version, dim, status="passed")
    await _seed_dimension_row(
        db_session,
        project,
        version,
        "merge_grade",
        status="unavailable",
        detail={"status": "not_applicable"},
    )
    await db_session.commit()

    complete, missing = await rq._promotion_dimension_ledger_complete(
        db_session, version, _LEDGER_SHA_A
    )
    assert complete is True
    assert missing == []


@pytest.mark.asyncio
async def test_finalize_holds_when_ledger_incomplete_at_promote(db_session, mocker):
    """Integration: a clean candidate that would promote is HELD as an infra
    failure — promote() is NOT called — when the per-dimension ledger is
    populated but missing a required dimension at the candidate_sha (the facts do
    not agree on one tree). This exercises the same-tree promotion guard in the
    live finalize path."""
    from app import shared_state

    shared_state.runtime_config["promotion_enabled"] = True

    project, version = await _make_project_and_version(db_session)
    await _seed_qualification_children(
        db_session,
        project,
        version,
        audit_logs={
            "audit_score": 1.0,
            "test_erosion": {
                "deleted_test_files": [],
                "assertion_delta": 5,
                "test_count_delta": 2,
            },
        },
        smoke_logs={
            "passed": True,
            "tests": [{"name": "a", "status": "pass"}],
            "suites": {"startup": {"passed": True}},
        },
    )

    # Simulate a ledger that loses one required gate across both the eager and
    # final writers.  Patching only the final dual-write is insufficient now that
    # migration gates are durably recorded as soon as they resolve.
    real_upsert = rq._upsert_dimension_result

    async def _drop_migration_apply(
        session,
        ver,
        sha,
        dimension,
        **kwargs,
    ):
        if dimension == "migration_apply":
            return
        await real_upsert(session, ver, sha, dimension, **kwargs)

    mocker.patch(
        "app.release_qualifier._upsert_dimension_result",
        new=_drop_migration_apply,
    )
    promote_spy = mocker.patch(
        "app.release_qualifier.promote",
        new=AsyncMock(return_value={"status": "promoted"}),
    )

    result = await rq._finalize_qualification(db_session, version)
    await db_session.commit()

    # Held, not promoted — the incomplete ledger blocks the split-tree promotion.
    assert result["status"] == "infra_failure"
    assert result["error"] == "qualification_dimension_ledger_incomplete"
    promote_spy.assert_not_called()
    await db_session.refresh(version)
    assert version.last_qualification["status"] == "infra_failure"


@pytest.mark.asyncio
@pytest.mark.parametrize("resolved_status", ["passed", "failed", "blocked"])
async def test_infra_failure_does_not_downgrade_resolved_dimension(
    db_session, resolved_status
):
    """_record_infra_dimension_unavailable must not overwrite a dimension row
    that is already in a resolved state (passed/failed/blocked).

    This is the asymmetry fix: _dual_write_dimension_results already skips
    re-recording resolved dims, but the infra-failure writer (_upsert_dimension_result
    called from _record_infra_dimension_unavailable) did not — it unconditionally
    overwrote existing.status.  The guard must fire for all three resolved statuses
    and return 0 (indicating no streak was recorded).
    """
    from app import release_qualifier as rq
    from app.models import QualificationDimensionResult
    from sqlalchemy import select as sa_select

    project, version = await _make_project_and_version(db_session)
    candidate_sha = "deadbeefdeadbeefdeadbeefdeadbeefdeadbeef"

    # Seed a pre-resolved dimension row.
    resolved_row = QualificationDimensionResult(
        project_id=version.project_id,
        release_version_id=version.id,
        candidate_sha=candidate_sha,
        dimension="audit",
        status=resolved_status,
        sub_score=1.0 if resolved_status == "passed" else 0.0,
    )
    db_session.add(resolved_row)
    await db_session.flush()

    streak = await rq._record_infra_dimension_unavailable(
        db_session,
        version,
        candidate_sha,
        "audit",
        infra_error="some_transient_infra_error",
        signature="sig-abc",
    )
    await db_session.flush()

    # Guard must have fired: streak == 0, row status unchanged.
    assert streak == 0, (
        f"expected streak=0 (skipped) for resolved status '{resolved_status}', got {streak}"
    )
    row = (
        await db_session.execute(
            sa_select(QualificationDimensionResult).where(
                QualificationDimensionResult.release_version_id == version.id,
                QualificationDimensionResult.candidate_sha == candidate_sha,
                QualificationDimensionResult.dimension == "audit",
            )
        )
    ).scalar_one_or_none()
    assert row is not None, "dimension row disappeared"
    assert row.status == resolved_status, (
        f"infra failure downgraded resolved status '{resolved_status}' to '{row.status}'"
    )


@pytest.mark.asyncio
async def test_ledger_integrity_infra_failure_does_not_overwrite_passed_audit(
    db_session, mocker
):
    """_handle_infra_failure with a ledger-integrity error code must NOT overwrite
    a legitimately-resolved audit dimension fact.

    Regression guard: _infer_dimension_from_infra_error previously returned 'audit'
    for 'qualification_dimension_ledger_incomplete', causing _record_infra_dimension_unavailable
    to overwrite a passed audit row with 'unavailable'.
    """
    from app import release_qualifier_audit as rqa
    from app.models import QualificationDimensionResult

    project, version = await _make_project_and_version(db_session)
    candidate_sha = "abc123def456abc123def456abc123def456abc1"
    version.qualification_attempts = 2
    db_session.add(version)

    # Seed a legitimately-passed audit dimension fact.
    passed_audit_row = QualificationDimensionResult(
        project_id=version.project_id,
        release_version_id=version.id,
        candidate_sha=candidate_sha,
        dimension="audit",
        status="passed",
        sub_score=1.0,
    )
    db_session.add(passed_audit_row)
    await db_session.flush()

    qual = {
        "candidate_sha": candidate_sha,
        "ci_green": True,
        "smoketest_enabled": True,
        "prior_infra_streak": 0,
    }

    await rqa._handle_infra_failure(
        db_session, version, qual, "qualification_dimension_ledger_incomplete"
    )
    await db_session.flush()

    # The audit row must still be 'passed' — not overwritten with 'unavailable'.
    from sqlalchemy import select as sa_select

    row = (
        await db_session.execute(
            sa_select(QualificationDimensionResult).where(
                QualificationDimensionResult.release_version_id == version.id,
                QualificationDimensionResult.candidate_sha == candidate_sha,
                QualificationDimensionResult.dimension == "audit",
            )
        )
    ).scalar_one_or_none()
    assert row is not None, "audit dimension row disappeared"
    assert row.status == "passed", (
        f"audit dimension was overwritten: expected 'passed', got {row.status!r}"
    )


@pytest.mark.asyncio
async def test_infra_failure_surfaces_passed_audit_from_ledger(db_session):
    """A passed audit dimension already in the per-dimension ledger is surfaced in
    version.last_qualification['resolved_dimensions'] and in the recorded
    QualificationVerdict sub_scores when a DIFFERENT gate (migration_identity) hits
    a transient infra defer.

    Acceptance evidence for criterion c-1caa7785 (Phase 1b read cutover): the
    infra-failure verdict must not be a black-box 'everything unknown' — already-
    resolved dimension facts survive the transient defer on an unrelated gate.
    """
    from app import release_qualifier_audit as rqa

    project, version = await _make_project_and_version(db_session)

    # Seed an already-passed audit row for this candidate tree, simulating an
    # earlier eager write that completed the audit gate before migration_identity
    # hit a transient infra defer.
    await _seed_dimension_row(
        db_session, project, version, "audit", status="passed", sub_score=1.0
    )
    await db_session.flush()

    version.last_qualification = {
        "status": "pending",
        "candidate_sha": _LEDGER_SHA_A,
        "ci_green": True,
        "smoketest_enabled": True,
        "started_at": "2026-07-10T00:00:00",
    }
    db_session.add(version)
    await db_session.commit()

    result = await rqa._handle_infra_failure(
        db_session,
        version,
        dict(version.last_qualification),
        "migration_identity_check_unavailable",
    )
    await db_session.commit()

    # Infra-failure hold is still in place — control flow is unchanged.
    assert result["status"] == "infra_failure"
    assert result["failing_dimension"] == "migration_identity"

    # version.last_qualification must surface the passed audit via resolved_dimensions.
    lq = version.last_qualification
    assert "resolved_dimensions" in lq, (
        "resolved_dimensions missing from last_qualification — passed audit was discarded"
    )
    assert "audit" in lq["resolved_dimensions"], (
        "audit not present in resolved_dimensions despite having a passed ledger row"
    )
    assert lq["resolved_dimensions"]["audit"]["status"] == "passed"
    assert lq["resolved_dimensions"]["audit"]["sub_score"] == 1.0

    # The failing dimension (migration_identity) must NOT appear among resolved facts
    # — it was just marked unavailable, not resolved.
    assert "migration_identity" not in lq["resolved_dimensions"], (
        "migration_identity should not appear in resolved_dimensions"
    )

    # The recorded QualificationVerdict sub_scores must also expose the passed audit.
    verdict_row = (
        await db_session.execute(
            select(QualificationVerdict).where(
                QualificationVerdict.release_version_id == version.id
            ).order_by(QualificationVerdict.finalized_at.desc())
        )
    ).scalars().first()
    assert verdict_row is not None, "no QualificationVerdict recorded"
    assert verdict_row.sub_scores is not None, "QualificationVerdict.sub_scores is None"
    assert "infra_failure" in verdict_row.sub_scores, "infra_failure sub_score missing"
    assert "audit" in verdict_row.sub_scores, (
        "audit not surfaced in QualificationVerdict sub_scores"
    )
    assert verdict_row.sub_scores["audit"]["status"] == "passed"
    assert verdict_row.sub_scores["audit"]["sub_score"] == 1.0
    assert "migration_identity" not in verdict_row.sub_scores, (
        "migration_identity (unavailable) must not appear in verdict sub_scores"
    )


@pytest.mark.asyncio
async def test_finalize_holds_when_ledger_empty_on_redrive(db_session, mocker):
    """When the per-dimension ledger is completely empty at the promotion gate
    on a RE-DRIVE (qualification_attempts > 1), the promotion is HELD as an
    infra failure rather than failing open -- the ledger should have content
    from a prior successful finalize, and its absence means
    _dual_write_dimension_results silently failed."""
    from app import shared_state

    shared_state.runtime_config["promotion_enabled"] = True

    project, version = await _make_project_and_version(db_session)
    await _seed_qualification_children(
        db_session, project, version,
        audit_logs={
            "audit_score": 1.0,
            "test_erosion": {
                "deleted_test_files": [],
                "assertion_delta": 5,
                "test_count_delta": 2,
            },
        },
        smoke_logs={
            "passed": True,
            "tests": [{"name": "a", "status": "pass"}],
            "suites": {"startup": {"passed": True}},
        },
    )

    # Simulate a re-drive (qualification_attempts > 1).
    version.qualification_attempts = 2
    db_session.add(version)
    await db_session.flush()

    # Prevent ALL dimension writes (both eager incremental writes and the
    # final dual-write) -- simulating a silent SAVEPOINT failure.
    mocker.patch(
        "app.release_qualifier._upsert_dimension_result",
        new=AsyncMock(),
    )
    promote_spy = mocker.patch(
        "app.release_qualifier.promote",
        new=AsyncMock(return_value={"status": "promoted"}),
    )

    result = await rq._finalize_qualification(db_session, version)
    await db_session.commit()

    # Held, not promoted -- empty ledger on re-drive triggers a HOLD.
    assert result["status"] == "infra_failure"
    assert result["error"] == "qualification_dimension_ledger_empty"
    promote_spy.assert_not_called()


@pytest.mark.asyncio
async def test_finalize_holds_on_empty_ledger_first_attempt(db_session, mocker):
    """When the per-dimension ledger is completely empty at the promotion gate
    on the FIRST qualification (qualification_attempts == 1), promotion fails
    closed.  A candidate with no durable same-tree facts must never promote from
    only the mutable live composite."""
    from app import shared_state

    shared_state.runtime_config["promotion_enabled"] = True

    project, version = await _make_project_and_version(db_session)
    await _seed_qualification_children(
        db_session, project, version,
        audit_logs={
            "audit_score": 1.0,
            "test_erosion": {
                "deleted_test_files": [],
                "assertion_delta": 5,
                "test_count_delta": 2,
            },
        },
        smoke_logs={
            "passed": True,
            "tests": [{"name": "a", "status": "pass"}],
            "suites": {"startup": {"passed": True}},
        },
    )

    # First qualification: qualification_attempts == 1 (the default after
    # run_qualification).
    version.qualification_attempts = 1
    db_session.add(version)
    await db_session.flush()

    # Prevent ALL dimension writes -- simulating a silent SAVEPOINT failure.
    mocker.patch(
        "app.release_qualifier._upsert_dimension_result",
        new=AsyncMock(),
    )
    promote_spy = mocker.patch(
        "app.release_qualifier.promote",
        new=AsyncMock(return_value={"status": "promoted"}),
    )

    result = await rq._finalize_qualification(db_session, version)
    await db_session.commit()

    # Held, not promoted -- empty ledger fails closed on the first attempt too.
    assert result["status"] == "infra_failure"
    assert result["error"] == "qualification_dimension_ledger_empty"
    promote_spy.assert_not_called()


@pytest.mark.asyncio
async def test_reused_blocked_legacy_smoketest_fact_is_ignored(
    db_session, mocker
):
    """A same-SHA legacy smoke fact cannot reintroduce a retired hard gate.

    Old rows may retain ``resolved_dimensions.smoketest``. Finalization must
    compose only active audit/test-erosion facts and reach the ordinary dry-run
    result instead of resurrecting ``smoketest_startup_failed``.
    """
    from app import shared_state

    shared_state.runtime_config["promotion_enabled"] = False
    mocker.patch(
        "app.release_qualifier.resolve_project_promotion_enabled",
        return_value=False,
    )

    project, version = await _make_project_and_version(db_session)
    sha = _LEDGER_SHA_A
    version.last_qualification = {
        "status": "pending",
        **_EMPTY_RUBRIC_PROJECTION,
        "candidate_sha": sha,
        "ci_green": True,
        "smoketest_enabled": True,
        "started_at": "2026-07-10T00:00:00",
        "audit_task_id": None,
        "smoketest_task_id": None,
        "challenge_task_id": None,
        "resolved_dimensions": {
            "audit": {"status": "passed", "sub_score": 1.0},
            "test_erosion": {"status": "passed", "sub_score": 1.0},
            "smoketest": {"status": "blocked", "sub_score": None},
        },
    }
    version.last_qualification = _with_current_contract(
        project, version, version.last_qualification
    )
    db_session.add(version)
    await db_session.commit()
    await db_session.refresh(version)

    promote_spy = mocker.patch("app.release_qualifier.promote", new=AsyncMock())

    result = await rq._finalize_qualification(
        db_session, version, _pre_gates=dict(_REUSE_PATH_PRE_GATES)
    )
    await db_session.commit()

    assert result["status"] == "dry_run"
    assert result.get("blocker") is None
    assert "smoketest" not in version.last_qualification["sub_scores"]
    promote_spy.assert_not_called()


@pytest.mark.asyncio
async def test_reused_blocked_test_erosion_fact_stays_blocked_not_scored_1_0(
    db_session, mocker
):
    """Same-SHA ledger reuse of a BLOCKED test_erosion fact must propagate the
    hard-blocker sentinel (None), never coerce to a clean 1.0.

    A prior finalize resolved test_erosion as 'blocked' (a real test file was
    deleted → the test_file_deleted hard blocker) and stored sub_score=None.  On
    a re-drive the audit child is not re-spawned and finalize composes from the
    ledger; the fail-open coerced None→1.0, dropping the test_file_deleted
    blocker.  Blocked must stay blocked.
    """
    from app import shared_state

    shared_state.runtime_config["promotion_enabled"] = True

    project, version = await _make_project_and_version(db_session)
    sha = _LEDGER_SHA_A
    version.last_qualification = {
        "status": "pending",
        **_EMPTY_RUBRIC_PROJECTION,
        "candidate_sha": sha,
        "ci_green": True,
        "smoketest_enabled": True,
        "started_at": "2026-07-10T00:00:00",
        "audit_task_id": None,
        "smoketest_task_id": None,
        "challenge_task_id": None,
        "resolved_dimensions": {
            "audit": {"status": "passed", "sub_score": 1.0},
            # A prior genuine test-file deletion: blocked with the None sentinel.
            "test_erosion": {"status": "blocked", "sub_score": None},
            "smoketest": {"status": "passed", "sub_score": 1.0},
        },
    }
    db_session.add(version)
    await db_session.commit()
    await db_session.refresh(version)

    promote_spy = mocker.patch("app.release_qualifier.promote", new=AsyncMock())

    result = await rq._finalize_qualification(
        db_session, version, _pre_gates=dict(_REUSE_PATH_PRE_GATES)
    )
    await db_session.commit()

    assert result["status"] == "not_promoted"
    assert result["blocker"] == "test_file_deleted"
    assert result["confidence"] == 0.0
    promote_spy.assert_not_called()


@pytest.mark.asyncio
async def test_archival_passed_smoketest_fact_is_ignored(
    db_session, mocker
):
    """An archival passed smoke fact with no score is ignored, not neutralized.

    Retired smoke rows must contribute neither a hard blocker nor a synthesized
    1.0 sub-score to the active qualification composite.
    """
    from app import shared_state

    shared_state.runtime_config["promotion_enabled"] = False
    mocker.patch(
        "app.release_qualifier.resolve_project_promotion_enabled",
        return_value=False,
    )

    project, version = await _make_project_and_version(db_session)
    sha = _LEDGER_SHA_A
    version.last_qualification = {
        "status": "pending",
        **_EMPTY_RUBRIC_PROJECTION,
        "candidate_sha": sha,
        "ci_green": True,
        "smoketest_enabled": True,
        "started_at": "2026-07-10T00:00:00",
        "audit_task_id": None,
        "smoketest_task_id": None,
        "challenge_task_id": None,
        "resolved_dimensions": {
            "audit": {"status": "passed", "sub_score": 1.0},
            "test_erosion": {"status": "passed", "sub_score": 1.0},
            # Archival only: never materialize a neutral active score.
            "smoketest": {"status": "passed", "sub_score": None},
        },
    }
    version.last_qualification = _with_current_contract(
        project, version, version.last_qualification
    )
    db_session.add(version)
    await db_session.commit()
    await db_session.refresh(version)

    promote_spy = mocker.patch("app.release_qualifier.promote", new=AsyncMock())

    result = await rq._finalize_qualification(
        db_session, version, _pre_gates=dict(_REUSE_PATH_PRE_GATES)
    )
    await db_session.commit()

    assert result["status"] == "dry_run"
    assert result.get("blocker") is None
    assert "smoketest" not in version.last_qualification["sub_scores"]
    promote_spy.assert_not_called()


@pytest.mark.asyncio
async def test_category_gate_read_back_exception_fails_closed(db_session, mocker):
    """Fail-open state 3/3: when the per-category verdict ledger read RAISES, the
    goal-verification gate must fail CLOSED (block), not skip.

    The candidate has an active acceptance criterion, so its category MUST be
    verified.  Making the ledger read-back raise used to null the gate input
    (_gate_category_verdicts=None), which compute_composite treats as "no rubric"
    and SKIPS — letting an unverified candidate promote on a transient DB error.
    The fix synthesises a fail-closed 'unverified' verdict per active criterion so
    the gate blocks with criterion_coverage_incomplete.
    """
    from app import shared_state

    shared_state.runtime_config["promotion_enabled"] = True

    project, version = await _make_project_and_version(db_session)
    await _seed_qualification_children(
        db_session,
        project,
        version,
        audit_logs={
            "audit_score": 1.0,
            "test_erosion": {
                "deleted_test_files": [],
                "assertion_delta": 5,
                "test_count_delta": 2,
            },
            "covers_goal": True,
            "coverage_score": 0.9,
            "biggest_gap": None,
            "category_verdicts": [
                {
                    "category_key": "c-needs-verify",
                    "status": "met",
                    "citations": [{"kind": "diff", "ref": "widget.py:1"}],
                }
            ],
        },
        smoke_logs={
            "passed": True,
            "tests": [{"name": "a", "status": "pass"}],
            "suites": {"startup": {"passed": True}},
        },
        version_goal="Ship the widget end to end",
        # A CAPABLE grader that graded the criterion MET.  The gate input is still
        # taken from the ledger READ-BACK (the canonical source, which also covers
        # the re-drive path), and that read is mocked to raise below — so the block
        # this test asserts can ONLY come from the unreadable ledger, not from an
        # unmet verdict.  (Omitting `category_verdicts` entirely would instead mean
        # the grader CANNOT grade per-category, which is a HOLD, not this gate —
        # see test_grader_omitting_the_key_entirely_is_unsupported_not_not_met.)
    )
    await _add_version_criterion(
        db_session,
        project,
        version,
        key="c-needs-verify",
        text="Widget path must be verified",
    )
    # Exercise the ledger read-back path: the holistic grade is already a
    # resolved same-candidate fact, so no fresh in-memory category verdict is
    # available to bypass the canonical read.
    version.last_qualification = _with_current_contract(
        project,
        version,
        {
            **(version.last_qualification or {}),
            "audit_task_id": None,
            "smoketest_task_id": None,
            "resolved_dimensions": {
                "audit": {"status": "passed", "sub_score": 1.0},
                "test_erosion": {"status": "passed", "sub_score": 1.0},
                "merge_grade": {"status": "passed", "sub_score": 0.9},
            },
        },
    )
    db_session.add(version)
    await db_session.commit()
    await db_session.refresh(version)
    # Make the ledger read-back raise (unreadable evidence).
    from app import category_verdict_ledger as _cvl

    mocker.patch.object(
        _cvl,
        "get_category_verdicts",
        new=AsyncMock(side_effect=RuntimeError("ledger read boom")),
    )
    promote_spy = mocker.patch("app.release_qualifier.promote", new=AsyncMock())

    result = await rq._finalize_qualification(db_session, version)
    await db_session.commit()

    # Fail closed: an unreadable category gate BLOCKS instead of promoting.
    assert result["status"] == "not_promoted"
    assert result["blocker"] == "criterion_coverage_incomplete"
    assert result["confidence"] == 0.0
    promote_spy.assert_not_called()


@pytest.mark.asyncio
async def test_category_gate_no_verdict_for_active_criterion_fails_closed(
    db_session, mocker
):
    """Fail-open state 2/3: when the read-back succeeds but returns NO verdict row
    for any currently-active criterion, the gate must fail CLOSED.

    Exercised on the re-drive shape: merge_grade is composed from the per-dimension
    ledger (no fresh grade → no fresh category verdicts), and the category-verdict
    ledger is empty for the active criterion.  Active criteria that were never
    graded into the ledger are UNVERIFIED, not clean.  The old code fell back to
    None (skip gate) via ``... or None``; the fix synthesises fail-closed
    'unverified' verdicts so promotion is blocked.
    """
    from app import shared_state

    shared_state.runtime_config["promotion_enabled"] = True

    project, version = await _make_project_and_version(db_session)
    sha = _LEDGER_SHA_A
    # An active criterion that was never graded into the category-verdict ledger.
    await _add_version_criterion(
        db_session,
        project,
        version,
        key="c-ungraded",
        text="This criterion was never graded into the ledger",
    )
    version.goal = "Ship the widget end to end"
    rubric_snapshot = [
        {
            "criterion_key": "c-ungraded",
            "text": "This criterion was never graded into the ledger",
        }
    ]
    version.last_qualification = {
        "status": "pending",
        "candidate_sha": sha,
        "ci_green": True,
        "smoketest_enabled": True,
        "started_at": "2026-07-10T00:00:00",
        "audit_task_id": None,
        "smoketest_task_id": None,
        "challenge_task_id": None,
        "merge_grade_task_id": None,
        "rubric_snapshot": rubric_snapshot,
        "rubric_fingerprint": rq._rubric_fingerprint(rubric_snapshot),
        # Re-drive: every quality gate already resolved for this tree, including
        # merge_grade — so no fresh merge-grade artifact / category verdicts are
        # produced and the gate must read the (empty) category ledger back.
        "resolved_dimensions": {
            "audit": {"status": "passed", "sub_score": 1.0},
            "test_erosion": {"status": "passed", "sub_score": 1.0},
            "smoketest": {"status": "passed", "sub_score": 1.0},
            "merge_grade": {"status": "passed", "sub_score": 0.9},
        },
    }
    version.last_qualification = _with_current_contract(
        project, version, version.last_qualification
    )
    db_session.add(version)
    await db_session.commit()
    await db_session.refresh(version)

    promote_spy = mocker.patch("app.release_qualifier.promote", new=AsyncMock())

    result = await rq._finalize_qualification(
        db_session, version, _pre_gates=dict(_REUSE_PATH_PRE_GATES)
    )
    await db_session.commit()

    assert result["status"] == "not_promoted"
    assert result["blocker"] == "criterion_coverage_incomplete"
    assert result["confidence"] == 0.0
    promote_spy.assert_not_called()


@pytest.mark.asyncio
async def test_qualification_trigger_state_surface_explains_armed_and_blocked(
    client, db_session
):
    """The agent qualification read surfaces ``qualification_trigger_state`` and
    explains BOTH an armed trigger (stabilize phase, no prior attempt) and a
    held trigger (a not_promoted/criterion_coverage verdict whose sha-keyed
    acceptance ledger is not yet fully met) with the correct machine-readable
    reason.

    Exercises the production read-surface wiring in
    ``routers/agent_strategic_fetch.py`` (which threads the shared
    ``explain_qualification_trigger`` classifier onto every branch of
    ``agent_fetch_qualification``), end-to-end over the real HTTP route.
    """
    from httpx import AsyncClient  # noqa: PLC0415
    from app.auth import create_agent_token  # noqa: PLC0415
    from app.release_qualifier_sweep import _MAX_ERROR_REDRIVE_ATTEMPTS  # noqa: PLC0415
    from app.shared_state import runtime_config as _test_runtime_config  # noqa: PLC0415

    assert isinstance(client, AsyncClient)

    # Enable auto-qualification triggers so the explain surface returns "armed"
    # instead of "observe".
    _test_runtime_config["auto_qualification_triggers"] = "on"

    # Stabilize-phase open version with no prior qualification attempt.
    project, version = await _make_project_and_version(
        db_session, phase="stabilize"
    )
    assert version.last_qualification is None
    assert (version.qualification_attempts or 0) == 0

    task = Task(
        project_id=project.id, payloads=[{}], vm_instance_id="agent-vm-trigger"
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)
    token = create_agent_token(str(task.id), task.vm_instance_id)
    headers = {"x-agent-token": token}
    url = f"/api/agent/projects/{project.id}/qualification"

    # ARMED: stabilize phase, no prior attempt → the initial trigger fires now
    # (Stage-1 decoupling: no pre-qualification coverage precondition). A
    # criterion is present but does not gate arming.
    await _add_version_criterion(
        db_session,
        project,
        version,
        key="c-covered-armed",
        text="A criterion",
        source_kind="task_merge",
    )

    res = await client.get(url, headers=headers)
    assert res.status_code == 200, res.text
    armed = res.json()["qualification_trigger_state"]
    assert armed is not None
    assert armed["auto_trigger"] == "armed"
    assert armed["next_expected_trigger"] is not None
    assert armed["decision"]["reason"] == "armed"
    assert armed["decision"]["fire"] is True
    assert armed["decision"]["blocking"] == []

    # HELD: a not_promoted verdict whose only blocker was criterion coverage
    # holds until the sha-keyed acceptance ledger reports the candidate fully
    # met. With an active criterion that has no ledger verdict for the candidate
    # sha, is_candidate_fully_met is False → the redrive leg holds
    # (reason=criterion_not_met).
    await _add_version_criterion(
        db_session,
        project,
        version,
        key="c-uncovered-blocked",
        text="Ungraded criterion",
    )
    version.last_qualification = {
        "status": "not_promoted",
        "blocker": "criterion_coverage_incomplete",
        "candidate_sha": "deadbeef" * 5,
    }
    db_session.add(version)
    await db_session.commit()
    await db_session.refresh(version)

    res = await client.get(url, headers=headers)
    assert res.status_code == 200, res.text
    blocked = res.json()["qualification_trigger_state"]
    assert blocked is not None
    assert blocked["auto_trigger"] == "waiting"
    assert blocked["next_expected_trigger"] is not None
    assert blocked["lifetime_qualification_attempts"] == 0
    assert blocked["error_retry_episode"] == {
        "count": 0,
        "cap": _MAX_ERROR_REDRIVE_ATTEMPTS,
    }
    assert blocked["decision"]["reason"] == "criterion_not_met"
    assert blocked["decision"]["fire"] is False
    assert any(
        "ledger" in b.lower() for b in blocked["decision"]["blocking"]
    ), f"expected ledger message in blocking list: {blocked['decision']['blocking']}"


@pytest.mark.asyncio
async def test_ungraded_acceptance_contract_fails_closed(
    db_session, mocker
):
    """Active criteria with NO merge-grade verdict block — they do not sail through.

    Replaces ``test_criterion_coverage_all_covered_passes_qualification``, which
    asserted the RETIRED mechanism (task-merge evidence "covers" a criterion ⇒ the
    gate passes).  Evidence rows no longer certify a criterion.
    """
    from app import shared_state

    shared_state.runtime_config["promotion_enabled"] = False

    project, version = await _make_project_and_version(db_session)
    await _seed_qualification_children(
        db_session,
        project,
        version,
        audit_logs={
            "audit_score": 1.0,
            "test_erosion": {
                "deleted_test_files": [],
                "assertion_delta": 5,
                "test_count_delta": 2,
            },
        },
        smoke_logs={
            "passed": True,
            "tests": [{"name": "a", "status": "pass"}],
            "suites": {"startup": {"passed": True}},
        },
    )
    await _add_version_criterion(
        db_session,
        project,
        version,
        key="c-covered1",
        text="First merge-test criterion",
        source_kind="task_merge",
    )
    await _add_version_criterion(
        db_session,
        project,
        version,
        key="c-covered2",
        text="Second merge-test criterion",
        source_kind="task_merge",
    )
    promote_spy = mocker.patch("app.release_qualifier.promote", new=AsyncMock())

    result = await rq._finalize_qualification(db_session, version)
    await db_session.commit()

    # This test used to assert that task-merge EVIDENCE ("2/2 covered") satisfied the
    # gate and let the candidate pass.  That mechanism is retired: evidence rows no
    # longer certify a criterion — the per-category merge-grade does, at qualify time.
    #
    # These two criteria are ACTIVE and were never graded (this fixture's version has
    # no usable goal, so no merge-grade child is spawned and no CategoryVerdict rows
    # exist for the candidate).  An ungraded acceptance contract must FAIL CLOSED, not
    # sail through on a `None` gate input — otherwise the version promotes over a
    # contract nothing ever checked.
    assert result["status"] == "not_promoted", result
    await db_session.refresh(version)
    assert version.last_qualification["blocker"] == "criterion_coverage_incomplete"
    # And the WHY is recorded per category, not just a bare blocker code.
    _verdicts = version.last_qualification["category_verdicts"]
    assert {v["category_key"] for v in _verdicts} == {"c-covered1", "c-covered2"}
    assert all(v["status"] == "not_met" for v in _verdicts)
    assert all("never graded" in v["gap"] for v in _verdicts)
    promote_spy.assert_not_called()


@pytest.mark.asyncio
async def test_finalize_absent_integration_branch_candidate_ci_self_clears_when_pre_gates_omit_ci_required(
    db_session, mocker
):
    """Pre-gates without ci_required → _ci_required_for_finalize reads topology."""
    from app import shared_state

    shared_state.runtime_config["promotion_enabled"] = False
    mocker.patch(
        "app.release_qualifier.resolve_project_promotion_enabled",
        return_value=False,
    )

    _project, version = await _seed_relaxed_ci_clean_qualification(db_session)

    mocker.patch(
        "app.github.get_branch_sha",
        new=AsyncMock(side_effect=AssertionError("branch head lookup should not run")),
    )
    promote_spy = mocker.patch("app.release_qualifier.promote", new=AsyncMock())

    result = await rq._finalize_qualification(
        db_session,
        version,
        _pre_gates=_clean_finalize_pre_gates(
            rq._CI_NO_CHECKS,
            ci_required=False,
            include_ci_required=False,
            integration_branch_topology={
                "integration_branch": "automain",
                "promotion_target": "main",
                "candidate_reachable_from_integration": True,
                "detail": "candidate is reachable from integration branch",
            },
        ),
    )
    await db_session.commit()

    assert result["status"] == "dry_run"
    assert result["would_promote"] is True
    assert result.get("blocker") is None
    promote_spy.assert_not_called()
    await db_session.refresh(version)
    assert version.last_qualification["ci_resolution"]["state"] == rq._CI_NO_CHECKS
    assert version.last_qualification["blocker"] is None


@pytest.mark.asyncio
async def test_finalize_absent_integration_branch_candidate_ci_stays_blocked_when_not_ancestor(
    db_session, mocker
):
    """A definitive no_checks candidate outside the relaxed branch stays blocked."""
    from app import shared_state

    shared_state.runtime_config["promotion_enabled"] = False

    _project, version = await _seed_relaxed_ci_clean_qualification(db_session)
    candidate_sha = version.last_qualification["candidate_sha"]

    mocker.patch(
        "app.release_qualifier._candidate_already_on_main",
        new=AsyncMock(return_value=False),
    )
    mocker.patch(
        "app.github.get_open_pull_requests",
        new=AsyncMock(return_value=[]),
    )
    mocker.patch(
        "app.github.get_branch_sha",
        new=AsyncMock(side_effect=AssertionError("branch head lookup should not run")),
    )
    promote_spy = mocker.patch("app.release_qualifier.promote", new=AsyncMock())

    result = await rq._finalize_qualification(
        db_session,
        version,
        _pre_gates=_clean_finalize_pre_gates(
            rq._CI_NO_CHECKS,
            ci_required=False,
            include_ci_required=False,
            integration_branch_topology={
                "integration_branch": "automain",
                "promotion_target": "main",
                "candidate_reachable_from_integration": False,
                "detail": "candidate is not reachable from integration branch",
            },
        ),
    )
    await db_session.commit()

    assert result["status"] == "not_promoted"
    assert result["blocker"] == "ci_unresolved"
    assert result["confidence"] == 0.0
    promote_spy.assert_not_called()
    await db_session.refresh(version)
    assert version.last_qualification["candidate_sha"] == candidate_sha
    assert version.last_qualification["ci_resolution"]["state"] == rq._CI_NO_CHECKS
    assert version.last_qualification["blocker"] == "ci_unresolved"


@pytest.mark.asyncio
async def test_finalize_absent_integration_branch_candidate_ci_stays_blocked_when_branch_rejected(
    db_session, mocker
):
    """A topology-safe candidate still blocks when the branch policy rejects it."""
    from app import shared_state

    shared_state.runtime_config["promotion_enabled"] = False

    _project, version = await _seed_relaxed_ci_clean_qualification(db_session)

    proceed_spy = mocker.patch(
        "app.release_qualifier.proceed_on_no_checks", return_value=False
    )
    mocker.patch(
        "app.release_qualifier._candidate_already_on_main",
        new=AsyncMock(return_value=False),
    )
    mocker.patch(
        "app.github.get_open_pull_requests",
        new=AsyncMock(return_value=[]),
    )
    mocker.patch(
        "app.github.get_branch_sha",
        new=AsyncMock(side_effect=AssertionError("branch head lookup should not run")),
    )
    promote_spy = mocker.patch("app.release_qualifier.promote", new=AsyncMock())

    result = await rq._finalize_qualification(
        db_session,
        version,
        _pre_gates=_clean_finalize_pre_gates(
            rq._CI_NO_CHECKS,
            ci_required=False,
            include_ci_required=False,
            integration_branch_topology={
                "integration_branch": "automain",
                "promotion_target": "main",
                "candidate_reachable_from_integration": True,
                "detail": "candidate is reachable from integration branch",
            },
        ),
    )
    await db_session.commit()

    assert result["status"] == "not_promoted"
    assert result["blocker"] == "ci_unresolved"
    assert result["confidence"] == 0.0
    promote_spy.assert_not_called()
    proceed_spy.assert_called_once()
    await db_session.refresh(version)
    assert version.last_qualification["ci_resolution"]["state"] == rq._CI_NO_CHECKS
    assert version.last_qualification["blocker"] == "ci_unresolved"


@pytest.mark.asyncio
async def test_finalize_absent_integration_branch_candidate_ci_self_clears_when_head_advances(
    db_session, mocker
):
    """A candidate still reachable from a newer integration head can still relax no_checks."""
    from app import shared_state

    shared_state.runtime_config["promotion_enabled"] = False
    mocker.patch(
        "app.release_qualifier.resolve_project_promotion_enabled",
        return_value=False,
    )

    _project, version = await _seed_relaxed_ci_clean_qualification(db_session)
    candidate_sha = version.last_qualification["candidate_sha"]
    current_integration_head_sha = "f" * 40

    mocker.patch(
        "app.github.get_branch_sha",
        new=AsyncMock(side_effect=AssertionError("branch head lookup should not run")),
    )
    promote_spy = mocker.patch("app.release_qualifier.promote", new=AsyncMock())

    result = await rq._finalize_qualification(
        db_session,
        version,
        _pre_gates=_clean_finalize_pre_gates(
            rq._CI_NO_CHECKS,
            ci_required=False,
            include_ci_required=False,
            integration_branch_topology={
                "integration_branch": "automain",
                "promotion_target": "main",
                "candidate_reachable_from_integration": True,
                "detail": "candidate is reachable from integration branch",
            },
        ),
    )
    await db_session.commit()

    assert candidate_sha != current_integration_head_sha
    assert result["status"] == "dry_run"
    assert result["would_promote"] is True
    assert result.get("blocker") is None
    promote_spy.assert_not_called()
    await db_session.refresh(version)
    assert version.last_qualification["ci_resolution"]["state"] == rq._CI_NO_CHECKS
    assert version.last_qualification["blocker"] is None


@pytest.mark.parametrize(
    ("ci_state", "blocker"),
    [(rq._CI_PENDING, "ci_unresolved"), (rq._CI_RED, "ci_not_green")],
)
@pytest.mark.asyncio
async def test_finalize_non_absent_candidate_ci_blocks_under_relaxed_no_checks_policy(
    db_session, mocker, ci_state, blocker
):
    """Pending/API-error and confirmed-red candidate CI remain strict."""
    from app import shared_state

    shared_state.runtime_config["promotion_enabled"] = False

    _project, version = await _seed_relaxed_ci_clean_qualification(db_session)
    promote_spy = mocker.patch("app.release_qualifier.promote", new=AsyncMock())
    # Pending/red CI is the contract under test. The merge-grade exemption is
    # a separate fail-closed GitHub ancestry check and must not perform live I/O
    # against the fixture repository.
    mocker.patch.object(
        rq,
        "_candidate_already_on_main_at_finalize",
        new=AsyncMock(return_value=False),
    )

    result = await rq._finalize_qualification(
        db_session,
        version,
        _pre_gates=_clean_finalize_pre_gates(ci_state),
    )
    await db_session.commit()

    assert result["status"] == "not_promoted"
    assert result["blocker"] == blocker
    promote_spy.assert_not_called()


@pytest.mark.asyncio
async def test_finalize_locked_version_precomputes_relaxed_no_checks_topology_before_finalize(
    db_session, mocker
):
    """The row-lock owner must precompute ci_required from topology pre-lock."""
    from app import shared_state

    shared_state.runtime_config["promotion_enabled"] = False
    mocker.patch(
        "app.release_qualifier.resolve_project_promotion_enabled",
        return_value=False,
    )

    _project, version = await _seed_relaxed_ci_clean_qualification(db_session)
    fq_entered = [False]

    # _resolve_ci_at_finalize returns topology already annotated (as the real
    # implementation does via _annotate_absent_ci_integration_topology).
    mocker.patch.object(
        rq,
        "_resolve_ci_at_finalize",
        new=AsyncMock(
            return_value=(
                False,
                {
                    "state": rq._CI_NO_CHECKS,
                    "resolved_sha": None,
                    "detail": "forced no_checks for test",
                    "integration_branch_topology": {
                        "integration_branch": "automain",
                        "promotion_target": "main",
                        "candidate_reachable_from_integration": True,
                        "detail": "candidate is reachable from integration branch",
                    },
                },
            )
        ),
    )
    mocker.patch.object(
        rq,
        "_resolve_migration_identity_at_finalize",
        new=AsyncMock(return_value=("ok", None)),
    )
    mocker.patch.object(
        rq,
        "_resolve_migration_apply_at_finalize",
        new=AsyncMock(return_value=("ok", None)),
    )

    mocker.patch(
        "app.github.get_branch_sha",
        new=AsyncMock(side_effect=AssertionError("branch head lookup should not run")),
    )
    promote_spy = mocker.patch("app.release_qualifier.promote", new=AsyncMock())
    orig_fq = rq._finalize_qualification

    async def tracking_fq(session, version, *, _pre_gates=None):
        fq_entered[0] = True
        assert _pre_gates is not None
        # ci_required must be computed from topology pre-lock, not deferred.
        assert _pre_gates["ci_required"] is False
        return await orig_fq(session, version, _pre_gates=_pre_gates)

    mocker.patch("app.release_qualifier._finalize_qualification", side_effect=tracking_fq)

    factory = _shared_session_factory(db_session)
    result = await rq._finalize_in_dedicated_session(
        version.id, timeout_now=None, session_factory=factory
    )
    await db_session.commit()

    assert result is not None
    assert result["status"] == "dry_run"
    assert result["would_promote"] is True
    assert fq_entered[0], "tracking_fq was never entered"
    promote_spy.assert_not_called()
