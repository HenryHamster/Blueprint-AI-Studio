# AUTO-SPLIT from test_release_qualifier.py — see refactor: split mega backend test files.
# Behavior-preserving: shared preamble lives in _release_qualifier_testhelpers.py; this file
# holds one domain slice of the original test suite.
"""Domain slice: _finalize_qualification — phase 2: collect + decide

Split from test_release_qualifier.py."""
from _release_qualifier_testhelpers import *  # noqa: F403


@pytest.mark.asyncio
async def test_finalize_locked_version_keeps_relaxed_no_checks_strict_when_topology_unavailable(
    db_session, mocker
):
    """Absent topology in ci_resolution keeps no_checks strict and blocks finalize."""
    from app import shared_state

    shared_state.runtime_config["promotion_enabled"] = False

    _project, version = await _seed_relaxed_ci_clean_qualification(db_session)
    fq_entered = [False]

    # No integration_branch_topology in the ci_resolution → _ci_required_for_finalize
    # stays strict without calling is_ancestor_of.
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
        # No topology → ci_required must remain strict (True).
        assert _pre_gates["ci_required"] is True
        return await orig_fq(session, version, _pre_gates=_pre_gates)

    mocker.patch("app.release_qualifier._finalize_qualification", side_effect=tracking_fq)

    factory = _shared_session_factory(db_session)
    result = await rq._finalize_in_dedicated_session(
        version.id, timeout_now=None, session_factory=factory
    )
    await db_session.commit()

    assert result is not None
    assert result["status"] == "not_promoted"
    assert result["blocker"] == "ci_unresolved"
    assert fq_entered[0], "tracking_fq was never entered"
    promote_spy.assert_not_called()


@pytest.mark.asyncio
async def test_no_criteria_no_criterion_blocker(db_session, mocker):
    """Versions with no criteria skip the criterion coverage gate."""
    from app import shared_state

    shared_state.runtime_config["promotion_enabled"] = False

    project, version = await _make_project_and_version(db_session)
    # The fixture declares an authorship budget, but that policy row is excluded
    # from ordinary criterion coverage, so this remains a no-product-criteria
    # test while still satisfying the explicit promotion-gate contract.
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
    promote_spy = mocker.patch("app.release_qualifier.promote", new=AsyncMock())

    result = await rq._finalize_qualification(db_session, version)
    await db_session.commit()

    assert result["status"] == "dry_run"
    await db_session.refresh(version)
    assert version.last_qualification["blocker"] is None
    coverage = version.last_qualification["criterion_coverage"]
    assert coverage["total"] == 0
    assert coverage["covered"] == 0
    assert coverage["uncovered"] == 0
    assert coverage["phantom_bookkeeping"]["registered_count"] == 0
    assert [criterion["criterion_key"] for criterion in coverage["criteria"]] == [
        "c-test-operator-authorship"
    ]
    promote_spy.assert_not_called()


@pytest.mark.asyncio
async def test_finalize_absent_integration_branch_candidate_ci_self_clears_ci_unresolved(
    db_session,
    mocker,
):
    """Definitive no_checks on a reachable integration candidate is non-blocking."""
    from app import shared_state

    shared_state.runtime_config["promotion_enabled"] = False
    _patch_finalize_ci_test_incidental_gates(mocker)
    project, version = await _make_project_and_version(db_session)
    project.build_config = {
        "requires_ci_checks": False,
        "has_ci": True,
        "ci_required_branches": ["main"],
    }
    db_session.add(project)
    await _seed_green_finalize_candidate(db_session, project, version)
    candidate_sha = version.last_qualification["candidate_sha"]

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
    # Mock the raw PR-verification request that runs when list_pull_requests_for_commit
    # returns [] — ensures _resolve_candidate_ci reaches _CI_NO_CHECKS, not _CI_PENDING.
    _mock_pr_resp = MagicMock()
    _mock_pr_resp.status_code = 200
    _mock_pr_resp.json.return_value = []
    mocker.patch("app.github._github_request", new=AsyncMock(return_value=_mock_pr_resp))

    async def _is_ancestor(token, owner, repo, ancestor, descendant, **kwargs):
        if descendant == "automain":
            return True
        return False  # main idempotency check should not be reached in dry_run path

    ancestry = mocker.patch(
        "app.github.is_ancestor_of",
        new=AsyncMock(side_effect=_is_ancestor),
    )
    promote_spy = mocker.patch("app.release_qualifier.promote", new=AsyncMock())

    result = await rq._finalize_qualification(db_session, version)
    await db_session.commit()

    assert result["status"] == "dry_run", result
    ancestry.assert_awaited_once_with(
        "gh-token",
        "acme",
        "widget",
        candidate_sha,
        "automain",
        project_id=str(project.id),
    )
    promote_spy.assert_not_called()

    await db_session.refresh(version)
    assert version.last_qualification["status"] == "dry_run"
    assert version.last_qualification["blocker"] is None
    assert version.last_qualification["ci_green"] is False
    assert version.last_qualification["ci_resolution"]["state"] == rq._CI_NO_CHECKS
    assert version.last_qualification["ci_resolution"][
        "integration_branch_topology"
    ] == {
        "integration_branch": "automain",
        "promotion_target": "main",
        "candidate_reachable_from_integration": True,
        "detail": "candidate is reachable from integration branch",
    }
    verdict = (
        (
            await db_session.execute(
                select(QualificationVerdict).where(
                    QualificationVerdict.release_version_id == version.id
                )
            )
        )
        .scalars()
        .one()
    )
    assert verdict.blocker is None
    assert verdict.would_promote is True


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("check_result", "ancestry_result", "expected_state", "expected_blocker"),
    [
        (
            {"status": "completed", "conclusion": "failure", "details": []},
            True,
            rq._CI_RED,
            "ci_not_green",
        ),
        (
            {"status": "in_progress", "conclusion": None, "details": []},
            True,
            rq._CI_PENDING,
            "ci_unresolved",
        ),
        (
            {"status": "no_checks", "conclusion": None, "details": []},
            False,
            rq._CI_NO_CHECKS,
            "ci_unresolved",
        ),
        (
            {"status": "no_checks", "conclusion": None, "details": []},
            None,
            rq._CI_NO_CHECKS,
            "ci_unresolved",
        ),
    ],
)
async def test_finalize_integration_candidate_ci_strict_cases(
    db_session,
    mocker,
    check_result,
    ancestry_result,
    expected_state,
    expected_blocker,
):
    """Red, pending, non-ancestor, and indeterminate topology remain strict."""
    from app import shared_state

    shared_state.runtime_config["promotion_enabled"] = False
    _patch_finalize_ci_test_incidental_gates(mocker)
    project, version = await _make_project_and_version(db_session)
    project.build_config = {"requires_ci_checks": False}
    db_session.add(project)
    await _seed_green_finalize_candidate(db_session, project, version)

    mocker.patch(
        "app.github.get_check_runs_cached",
        new=AsyncMock(return_value=check_result),
    )
    mocker.patch(
        "app.github.list_pull_requests_for_commit",
        new=AsyncMock(return_value=[]),
    )

    async def _is_ancestor(token, owner, repo, ancestor, descendant, **kwargs):
        if descendant == "automain":
            return ancestry_result
        return False

    mocker.patch("app.github.is_ancestor_of", new=AsyncMock(side_effect=_is_ancestor))
    promote_spy = mocker.patch("app.release_qualifier.promote", new=AsyncMock())

    result = await rq._finalize_qualification(db_session, version)

    assert result["status"] == "not_promoted"
    assert result["blocker"] == expected_blocker
    await db_session.refresh(version)
    assert version.last_qualification["ci_resolution"]["state"] == expected_state
    promote_spy.assert_not_called()


@pytest.mark.asyncio
async def test_finalize_absent_candidate_ci_stays_strict_when_topology_raises(
    db_session,
    mocker,
):
    """Topology exceptions are recorded as unavailable and keep ci_unresolved."""
    from app import shared_state

    shared_state.runtime_config["promotion_enabled"] = False
    _patch_finalize_ci_test_incidental_gates(mocker)
    project, version = await _make_project_and_version(db_session)
    project.build_config = {"requires_ci_checks": False}
    db_session.add(project)
    await _seed_green_finalize_candidate(db_session, project, version)

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

    async def _is_ancestor(token, owner, repo, ancestor, descendant, **kwargs):
        if descendant == "automain":
            raise RuntimeError("compare unavailable")
        return False

    mocker.patch("app.github.is_ancestor_of", new=AsyncMock(side_effect=_is_ancestor))

    result = await rq._finalize_qualification(db_session, version)

    assert result["status"] == "not_promoted"
    assert result["blocker"] == "ci_unresolved"
    await db_session.refresh(version)
    assert version.last_qualification["ci_resolution"]["state"] == rq._CI_NO_CHECKS
    topology = version.last_qualification["ci_resolution"][
        "integration_branch_topology"
    ]
    assert topology["candidate_reachable_from_integration"] is None
    assert "compare unavailable" in topology["detail"]


@pytest.mark.asyncio
async def test_finalize_absent_candidate_ci_stays_strict_for_single_branch_project(
    db_session,
    mocker,
):
    """no_checks does not self-clear when integration branch is promotion target."""
    from app import shared_state

    shared_state.runtime_config["promotion_enabled"] = False
    _patch_finalize_ci_test_incidental_gates(mocker)
    project, version = await _make_project_and_version(db_session)
    project.integration_branch = "main"
    project.promotion_target = "main"
    project.build_config = {"requires_ci_checks": False}
    db_session.add(project)
    await _seed_green_finalize_candidate(db_session, project, version)

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
        "app.github.is_ancestor_of",
        new=AsyncMock(return_value=False),
    )

    result = await rq._finalize_qualification(db_session, version)

    assert result["status"] == "not_promoted"
    assert result["blocker"] == "ci_unresolved"
    await db_session.refresh(version)
    assert version.last_qualification["ci_resolution"]["state"] == rq._CI_NO_CHECKS
    assert version.last_qualification["ci_resolution"][
        "integration_branch_topology"
    ] == {
        "integration_branch": "main",
        "promotion_target": "main",
        "candidate_reachable_from_integration": None,
        "detail": "integration branch equals promotion target",
    }


@pytest.mark.asyncio
async def test_challenge_hold_respawns_challenge_only_with_primary_findings(
    db_session,
):
    """A binding challenge hold re-spawns only the challenge child with the
    primary audit's deductible findings, keeping the qualification pending."""
    project, version = await _make_project_and_version(db_session)
    audit_task, smoke_task = await _seed_qualification_children(
        db_session,
        project,
        version,
        audit_logs={
            "test_erosion": {
                "deleted_test_files": [],
                "assertion_delta": 0,
                "test_count_delta": 0,
            },
            "findings": [
                {
                    "severity": "high",
                    "summary": "Unbounded retry can promote stale challenge state.",
                    "file": "backend/app/release_qualifier.py",
                    "verification_note": "Read backend/app/release_qualifier.py:1950 and confirmed the stale challenge state can be reused.",
                },
                {
                    "severity": "medium",
                    "description": "Challenge matching loses the release scoring file.",
                    "file_path": "backend/app/release_scoring.py",
                    "verification_note": "Read backend/app/release_scoring.py:1300 and confirmed the binding match can fail.",
                },
                {
                    "severity": "low",
                    "summary": "Cosmetic label is stale.",
                    "file": "backend/app/release_qualifier.py",
                },
                {
                    "severity": "high",
                    "summary": "Speculative unverified high finding.",
                    "file": "backend/app/release_qualifier.py",
                },
            ],
        },
        smoke_logs={
            "passed": True,
            "tests": [{"name": "startup", "status": "pass"}],
            "suites": {"startup": {"passed": True}},
        },
        challenge_verdicts=None,
    )
    await _seed_completed_challenge_without_artifact(db_session, project, version)
    original_challenge_id = version.last_qualification["challenge_task_id"]

    result = await rq._finalize_qualification(db_session, version)
    await db_session.commit()
    await db_session.refresh(version)

    assert result == {
        "status": "audit_challenge_respawned",
        "reason": "challenge_initial_no_primary_findings",
        "respawn_count": 1,
        "challenge_task_id": version.last_qualification["challenge_task_id"],
    }
    assert version.last_qualification["status"] == "pending"
    assert version.last_qualification["audit_task_id"] == str(audit_task.id)
    assert version.last_qualification["smoketest_task_id"] == str(smoke_task.id)
    assert version.last_qualification["challenge_task_id"] != original_challenge_id
    assert (
        version.last_qualification["audit_challenge_task_id"]
        == (version.last_qualification["challenge_task_id"])
    )
    assert version.last_qualification["audit_challenge_respawn_count"] == 1
    assert version.last_qualification["audit_challenge_retry_reason"] == (
        "challenge_initial_no_primary_findings"
    )

    tasks = (
        (await db_session.execute(select(Task).where(Task.project_id == project.id)))
        .scalars()
        .all()
    )
    audit_tasks = [
        t for t in tasks if (t.first_payload or {}).get("type") == "release_audit"
    ]
    smoke_tasks = [
        t for t in tasks if (t.first_payload or {}).get("type") == "smoketest"
    ]
    challenge_tasks = [
        t
        for t in tasks
        if (t.first_payload or {}).get("type") == "release_audit_challenge"
    ]
    assert [str(t.id) for t in audit_tasks] == [str(audit_task.id)]
    assert [str(t.id) for t in smoke_tasks] == [str(smoke_task.id)]
    assert len(challenge_tasks) == 2

    retry_task = next(
        t
        for t in challenge_tasks
        if str(t.id) == version.last_qualification["challenge_task_id"]
    )
    assert retry_task.first_payload["primary_audit_task_id"] == str(audit_task.id)
    assert (
        retry_task.first_payload["candidate_sha"]
        == version.last_qualification["candidate_sha"]
    )
    assert retry_task.first_payload["primary_findings"] == [
        {
            "severity": "high",
            "summary": "Unbounded retry can promote stale challenge state.",
            "file": "backend/app/release_qualifier.py",
        },
        {
            "severity": "medium",
            "summary": "Challenge matching loses the release scoring file.",
            "file": "backend/app/release_scoring.py",
        },
    ]


@pytest.mark.asyncio
async def test_initial_challenge_receives_primary_findings_before_reconciliation(
    db_session,
):
    """Pre-reconciliation guard: an initial challenge (no primary_findings in
    payload) that completed while the audit has deductible findings must be
    re-spawned with those findings BEFORE reconciliation runs.  The new
    challenge payload carries the findings so reconciliation can proceed
    without a binding-failure infra override on the next pass."""
    project, version = await _make_project_and_version(db_session)
    audit_task, smoke_task = await _seed_qualification_children(
        db_session,
        project,
        version,
        audit_logs={
            "test_erosion": {
                "deleted_test_files": [],
                "assertion_delta": 0,
                "test_count_delta": 0,
            },
            "findings": [
                {
                    "severity": "high",
                    "summary": "SQL query is missing parameterization.",
                    "file": "backend/app/models.py",
                    "verification_note": "Read backend/app/models.py:42 and confirmed the query is unparameterized.",
                },
                {
                    "severity": "critical",
                    "summary": "Secret key written to disk.",
                    "file": "backend/app/services.py",
                    "verification_note": "Read backend/app/services.py:100 and confirmed the key is written.",
                },
            ],
        },
        smoke_logs={
            "passed": True,
            "tests": [{"name": "startup", "status": "pass"}],
            "suites": {"startup": {"passed": True}},
        },
        challenge_verdicts=None,
    )
    # Defer the challenge spawn entirely. Finalize should create the first
    # binding challenge only after it can carry the primary findings.
    version.last_qualification = {
        **version.last_qualification,
        "challenge_task_id": None,
        "audit_challenge_task_id": None,
        "audit_challenge_spawn_pending": True,
    }
    db_session.add(version)
    await db_session.commit()

    result = await rq._finalize_qualification(db_session, version)
    await db_session.commit()
    await db_session.refresh(version)

    # Guard must fire before reconciliation and spawn the first bound challenge.
    assert result["status"] == "audit_challenge_respawned"
    assert result["reason"] == "challenge_initial_primary_findings"
    assert result["respawn_count"] == 1
    assert version.last_qualification["status"] == "pending"
    assert version.last_qualification["challenge_task_id"] is not None
    assert (
        version.last_qualification["audit_challenge_task_id"]
        == (version.last_qualification["challenge_task_id"])
    )
    assert version.last_qualification["audit_challenge_spawn_pending"] is False
    assert version.last_qualification["audit_challenge_respawn_count"] == 1
    assert version.last_qualification["audit_challenge_retry_reason"] == (
        "challenge_initial_primary_findings"
    )

    # The new challenge payload must carry primary_findings so the next
    # reconciliation can verify them without a binding-failure hold.
    from app.models import Task as _Task  # noqa: PLC0415

    new_challenge_id = uuid.UUID(version.last_qualification["challenge_task_id"])
    new_challenge = await db_session.get(_Task, new_challenge_id)
    assert new_challenge is not None
    assert new_challenge.first_payload.get("primary_findings") == [
        {
            "severity": "critical",
            "summary": "Secret key written to disk.",
            "file": "backend/app/services.py",
        },
        {
            "severity": "high",
            "summary": "SQL query is missing parameterization.",
            "file": "backend/app/models.py",
        },
    ]


@pytest.mark.asyncio
async def test_deferred_challenge_spawn_produces_primary_findings_binding(
    db_session, mocker
):
    """End-to-end: deferred challenge spawn → binding verdicts → quality verdict.

    Acceptance proof for the audit-leg primary-findings binding flow:

    Pass 1 (deferred spawn):
      - version.last_qualification has audit_challenge_spawn_pending=True and no
        challenge_task_id — the audit completed with deductible findings but the
        challenge was intentionally deferred.
      - _finalize_qualification spawns the initial challenge with primary_findings
        in its payload (verbatim summaries and severities from the audit report).

    Pass 2 (reconciliation → quality verdict):
      - The challenge task completes with binding verdicts that use
        primary_finding_index to map back to the exact primary findings.
      - _finalize_qualification reconciles successfully, produces a real quality
        verdict (not_promoted), and records a QualificationVerdict row.

    This is the acceptance criterion for test_deferred_challenge_spawn_produces_
    primary_findings_binding: the test must run green under a real merge-test
    verdict (not_promoted), exercising _finalize_qualification's deferred-spawn
    block and _reconcile_audit_with_challenge's primary_finding_index binding.
    """
    from app import shared_state
    from app.models import QualificationVerdict

    shared_state.runtime_config["promotion_enabled"] = (
        False  # dry-run / not_promoted path
    )

    project, version = await _make_project_and_version(db_session)

    # Seed audit with two deductible (high-severity, verified) findings.
    audit_task, smoke_task = await _seed_qualification_children(
        db_session,
        project,
        version,
        audit_logs={
            "audit_score": 0.75,
            "findings": [
                {
                    "severity": "high",
                    "summary": "Unbounded read from untrusted input in parser.",
                    "file": "backend/app/parser.py",
                    "verification_note": "Read backend/app/parser.py:120 and confirmed the buffer is not bounded.",
                },
                {
                    "severity": "high",
                    "summary": "Missing CSRF token validation on state-changing endpoint.",
                    "file": "backend/app/routers/admin.py",
                    "verification_note": "Read backend/app/routers/admin.py:45 and confirmed no CSRF check.",
                },
            ],
            "test_erosion": {
                "deleted_test_files": [],
                "assertion_delta": 0,
                "test_count_delta": 0,
            },
        },
        smoke_logs={
            "passed": True,
            "tests": [{"name": "startup", "status": "pass"}],
            "suites": {"startup": {"passed": True}},
        },
        challenge_verdicts=None,  # No challenge yet — will be spawned in pass 1
    )

    # Mark the qualification as having a deferred challenge spawn.
    candidate_sha = version.last_qualification["candidate_sha"]
    version.last_qualification = {
        **version.last_qualification,
        "challenge_task_id": None,
        "audit_challenge_task_id": None,
        "audit_challenge_spawn_pending": True,
    }
    db_session.add(version)
    await db_session.commit()
    await db_session.refresh(version)

    # --- Pass 1: deferred spawn ---
    result1 = await rq._finalize_qualification(db_session, version)
    await db_session.commit()
    await db_session.refresh(version)

    assert result1["status"] == "audit_challenge_respawned"
    assert result1["reason"] in (
        "challenge_initial_primary_findings",
        "challenge_initial_no_primary_findings",
    )
    assert result1["reason"] == "challenge_initial_primary_findings", (
        "deferred spawn must carry primary findings when the audit has deductible findings"
    )
    new_challenge_id = uuid.UUID(version.last_qualification["challenge_task_id"])
    assert version.last_qualification["audit_challenge_spawn_pending"] is False

    # Verify the spawned challenge payload carries the primary findings verbatim.
    new_challenge = await db_session.get(Task, new_challenge_id)
    assert new_challenge is not None
    pf = new_challenge.first_payload.get("primary_findings") or []
    assert len(pf) == 2
    # Findings are sorted by severity rank (critical first, then high, etc.).
    pf_summaries = [f["summary"] for f in pf]
    assert "Unbounded read from untrusted input in parser." in pf_summaries
    assert "Missing CSRF token validation on state-changing endpoint." in pf_summaries

    # --- Seed the challenge with indexed binding verdicts ---
    # Use primary_finding_index (1-based) to bind verdicts to findings.
    pf_index_by_summary = {f["summary"]: i + 1 for i, f in enumerate(pf)}
    challenge_artifact = {
        "challenged_candidate_sha": candidate_sha,
        "verdicts": [
            {
                "primary_finding_index": pf_index_by_summary[pf[0]["summary"]],
                "severity": pf[0]["severity"],
                "summary": pf[0]["summary"],
                "file": pf[0]["file"],
                "verdict": "confirmed",
                "reason": "Independent review confirms the parser does not bound the input buffer.",
            },
            {
                "primary_finding_index": pf_index_by_summary[pf[1]["summary"]],
                "severity": pf[1]["severity"],
                "summary": pf[1]["summary"],
                "file": pf[1]["file"],
                "verdict": "confirmed",
                "reason": "Independent review confirms no CSRF token is validated on the endpoint.",
            },
        ],
    }
    new_challenge.status = TaskStatus.COMPLETED
    db_session.add(new_challenge)
    db_session.add(
        Artifact(
            task_id=new_challenge_id,
            artifact_type=ArtifactType.RELEASE_AUDIT_CHALLENGE,
            logs=json.dumps(challenge_artifact),
        )
    )
    await db_session.commit()
    await db_session.refresh(version)

    # --- Pass 2: reconciliation → quality verdict ---
    mocker.patch("app.release_qualifier.promote", new=AsyncMock())
    result2 = await rq._finalize_qualification(db_session, version)
    await db_session.commit()
    await db_session.refresh(version)

    # Both confirmed findings still deduct → not_promoted (below promotion threshold).
    assert result2["status"] in ("not_promoted", "dry_run"), (
        f"expected a final quality verdict but got {result2['status']!r}"
    )
    lq = version.last_qualification
    assert lq["status"] in ("not_promoted", "dry_run")
    assert lq.get("audit_findings") is not None

    # A durable QualificationVerdict row must be written (this is what
    # distinguishes a quality verdict from an infra_failure hold).
    verdict_count = (
        await db_session.execute(
            select(sa_func.count()).where(
                QualificationVerdict.release_version_id == version.id
            )
        )
    ).scalar()
    assert verdict_count == 1, (
        f"expected 1 QualificationVerdict row but found {verdict_count} — "
        "the deferred-spawn + binding flow must produce a durable quality verdict"
    )


@pytest.mark.asyncio
async def test_sha_mismatch_respawn_roundtrip(db_session, mocker):
    """End-to-end: SHA-mismatch hold → respawn with correct SHA → quality verdict.

    Validates the full audit_challenge_sha_mismatch roundtrip:

    Pass 1 (SHA mismatch):
      - The initial challenge artifact carries challenged_candidate_sha that does
        not match the qualification's candidate_sha.
      - _finalize_qualification detects the mismatch via _reconcile_audit_with_
        challenge, re-spawns the challenge with the correct candidate_sha, and
        records audit_challenge_sha_mismatch_observed_sha in last_qualification
        so operators can see what SHA the challenge actually ran on.

    Pass 2 (correct SHA → quality verdict):
      - The respawned challenge completes with challenged_candidate_sha == the
        qualification's candidate_sha and indexed binding verdicts.
      - _finalize_qualification reconciles successfully, produces a real quality
        verdict, and records a QualificationVerdict row.
    """
    from app import shared_state
    from app.models import QualificationVerdict

    shared_state.runtime_config["promotion_enabled"] = False

    WRONG_SHA = "dead" * 10  # 40-char hex, intentionally different from candidate

    project, version = await _make_project_and_version(db_session)
    audit_task, smoke_task = await _seed_qualification_children(
        db_session,
        project,
        version,
        audit_logs={
            "audit_score": 0.80,
            "findings": [
                {
                    "severity": "high",
                    "summary": "Credential logged in plaintext on authentication failure.",
                    "file": "backend/app/auth.py",
                    "verification_note": "Read backend/app/auth.py:88 and confirmed the password is written to the log.",
                },
            ],
            "test_erosion": {
                "deleted_test_files": [],
                "assertion_delta": 0,
                "test_count_delta": 0,
            },
        },
        smoke_logs={
            "passed": True,
            "tests": [{"name": "startup", "status": "pass"}],
            "suites": {"startup": {"passed": True}},
        },
        challenge_verdicts=None,
    )

    candidate_sha = version.last_qualification["candidate_sha"]

    # Seed an initial challenge artifact with a WRONG candidate SHA.
    mismatched_challenge = Task(
        project_id=project.id,
        title="audit-challenge-mismatched",
        payloads=[
            {
                "type": "release_audit_challenge",
                "base_branch": "main",
                "candidate_sha": WRONG_SHA,
            }
        ],
        vm_instance_id="vm-challenge-mismatch",
        status=TaskStatus.COMPLETED,
        context={"candidate_sha": WRONG_SHA},
    )
    db_session.add(mismatched_challenge)
    await db_session.flush()
    db_session.add(
        Artifact(
            task_id=mismatched_challenge.id,
            artifact_type=ArtifactType.RELEASE_AUDIT_CHALLENGE,
            logs=json.dumps(
                {
                    "challenged_candidate_sha": WRONG_SHA,  # WRONG — does not match candidate_sha
                    "verdicts": [
                        {
                            "severity": "high",
                            "summary": "Credential logged in plaintext on authentication failure.",
                            "file": "backend/app/auth.py",
                            "verdict": "confirmed",
                            "reason": "Reviewed auth.py and confirmed the credential appears in the log line.",
                        }
                    ],
                }
            ),
        )
    )
    version.last_qualification = {
        **version.last_qualification,
        "challenge_task_id": str(mismatched_challenge.id),
        "audit_challenge_task_id": str(mismatched_challenge.id),
        "audit_challenge_respawn_count": 0,
    }
    db_session.add(version)
    await db_session.commit()
    await db_session.refresh(version)

    # --- Pass 1: SHA mismatch → respawn ---
    result1 = await rq._finalize_qualification(db_session, version)
    await db_session.commit()
    await db_session.refresh(version)

    assert result1["status"] == "audit_challenge_respawned", (
        f"expected respawn for SHA mismatch but got {result1['status']!r}"
    )
    assert result1["reason"] == "audit_challenge_sha_mismatch"

    lq1 = version.last_qualification
    assert lq1["audit_challenge_retry_reason"] == "audit_challenge_sha_mismatch"
    assert lq1["audit_challenge_respawn_count"] == 1

    # The production change: observed SHA must be recorded so operators know
    # what SHA the mismatched challenge actually ran on.
    assert lq1.get("audit_challenge_sha_mismatch_observed_sha") == WRONG_SHA, (
        "audit_challenge_sha_mismatch_observed_sha must record the wrong SHA "
        "so operators can debug identity drift"
    )

    # The new challenge payload must carry the CORRECT candidate_sha.
    new_challenge_id = uuid.UUID(lq1["challenge_task_id"])
    new_challenge = await db_session.get(Task, new_challenge_id)
    assert new_challenge is not None
    assert new_challenge.first_payload.get("candidate_sha") == candidate_sha, (
        "the respawned challenge must be pinned to the qualification's candidate_sha"
    )
    # Primary findings must also be carried so reconciliation can succeed.
    assert new_challenge.first_payload.get("primary_findings"), (
        "respawned challenge must include primary_findings for indexed binding"
    )

    # --- Seed the respawned challenge with matching SHA + binding verdicts ---
    pf = new_challenge.first_payload["primary_findings"]
    new_challenge.status = TaskStatus.COMPLETED
    db_session.add(new_challenge)
    db_session.add(
        Artifact(
            task_id=new_challenge_id,
            artifact_type=ArtifactType.RELEASE_AUDIT_CHALLENGE,
            logs=json.dumps(
                {
                    "challenged_candidate_sha": candidate_sha,  # CORRECT SHA
                    "verdicts": [
                        {
                            "primary_finding_index": 1,
                            "severity": pf[0]["severity"],
                            "summary": pf[0]["summary"],
                            "file": pf[0]["file"],
                            "verdict": "confirmed",
                            "reason": "Confirmed on the correct candidate commit — credential appears in log.",
                        },
                    ],
                }
            ),
        )
    )
    await db_session.commit()
    await db_session.refresh(version)

    # --- Pass 2: correct SHA → quality verdict ---
    mocker.patch("app.release_qualifier.promote", new=AsyncMock())
    result2 = await rq._finalize_qualification(db_session, version)
    await db_session.commit()
    await db_session.refresh(version)

    assert result2["status"] in ("not_promoted", "dry_run"), (
        f"expected a final quality verdict after SHA-matched challenge but got "
        f"{result2['status']!r}"
    )
    lq2 = version.last_qualification
    assert lq2["status"] in ("not_promoted", "dry_run")

    verdict_count = (
        await db_session.execute(
            select(sa_func.count()).where(
                QualificationVerdict.release_version_id == version.id
            )
        )
    ).scalar()
    assert verdict_count == 1, (
        f"expected 1 QualificationVerdict row but found {verdict_count} — "
        "the SHA-mismatch respawn roundtrip must produce a durable quality verdict"
    )


@pytest.mark.asyncio
async def test_empty_challenge_verdicts_respawn_with_distinct_reason(
    db_session,
):
    project, version = await _make_project_and_version(db_session)
    await _seed_qualification_children(
        db_session,
        project,
        version,
        audit_logs={
            "findings": [
                {
                    "severity": "high",
                    "summary": "Empty challenge verdicts must retry with a precise reason.",
                    "file": "backend/app/release_scoring.py",
                    "verification_note": "Read backend/app/release_scoring.py:1300 and confirmed empty verdicts block reconciliation.",
                }
            ],
            "test_erosion": {"deleted_test_files": []},
        },
        smoke_logs={
            "passed": True,
            "tests": [{"name": "startup", "status": "pass"}],
            "suites": {"startup": {"passed": True}},
        },
        challenge_verdicts=[],
    )
    original_challenge_id = version.last_qualification["challenge_task_id"]

    result = await rq._finalize_qualification(db_session, version)
    await db_session.commit()
    await db_session.refresh(version)

    assert result == {
        "status": "audit_challenge_respawned",
        "reason": "audit_challenge_empty_verdicts",
        "respawn_count": 1,
        "challenge_task_id": version.last_qualification["challenge_task_id"],
    }
    assert version.last_qualification["challenge_task_id"] != original_challenge_id
    assert version.last_qualification["audit_challenge_retry_reason"] == (
        "audit_challenge_empty_verdicts"
    )


@pytest.mark.asyncio
async def test_challenge_hold_respawn_cap_exhausted_falls_back_to_infra_failure(
    db_session,
):
    project, version = await _make_project_and_version(db_session)
    await _seed_qualification_children(
        db_session,
        project,
        version,
        audit_logs={
            "findings": [
                {
                    "severity": "high",
                    "summary": "Retry cap finding still needs a challenge verdict.",
                    "file": "backend/app/release_qualifier.py",
                    "verification_note": "Read backend/app/release_qualifier.py:1950 and confirmed this still needs a verdict.",
                }
            ],
            "test_erosion": {"deleted_test_files": []},
        },
        smoke_logs={
            "passed": True,
            "tests": [{"name": "startup", "status": "pass"}],
            "suites": {"startup": {"passed": True}},
        },
        challenge_verdicts=None,
    )
    await _seed_completed_challenge_without_artifact(db_session, project, version)
    version.last_qualification = {
        **version.last_qualification,
        "audit_challenge_respawn_count": rq._MAX_AUDIT_CHALLENGE_BINDING_RESPAWNS,
    }
    db_session.add(version)
    await db_session.commit()

    result = await rq._finalize_qualification(db_session, version)
    await db_session.commit()
    await db_session.refresh(version)

    assert result["status"] == "infra_failure"
    assert result["error"] == "audit_challenge_missing"
    assert version.last_qualification["status"] == "infra_failure"
    assert version.last_qualification["error"] == "audit_challenge_missing"


@pytest.mark.asyncio
async def test_challenge_hold_does_not_retry_with_stale_primary_audit_candidate(
    db_session,
):
    project, version = await _make_project_and_version(db_session)
    await _seed_qualification_children(
        db_session,
        project,
        version,
        audit_logs={
            "findings": [
                {
                    "severity": "high",
                    "summary": "Old candidate finding must not drive a retry.",
                    "file": "backend/app/release_qualifier.py",
                    "verification_note": "Read backend/app/release_qualifier.py:1950 and confirmed stale audit evidence must not be reused.",
                }
            ],
            "test_erosion": {"deleted_test_files": []},
        },
        smoke_logs={
            "passed": True,
            "tests": [{"name": "startup", "status": "pass"}],
            "suites": {"startup": {"passed": True}},
        },
        challenge_verdicts=None,
    )
    await _seed_completed_challenge_without_artifact(db_session, project, version)
    original_challenge_id = version.last_qualification["challenge_task_id"]
    version.last_qualification = {
        **version.last_qualification,
        "candidate_sha": "deed" * 10,
    }
    db_session.add(version)
    await db_session.commit()

    result = await rq._finalize_qualification(db_session, version)
    await db_session.commit()
    await db_session.refresh(version)

    assert result["status"] == "infra_failure"
    assert result["error"] == "audit_challenge_missing"
    assert (
        version.last_qualification["audit_challenge_task_id"] == original_challenge_id
    )

    tasks = (
        (await db_session.execute(select(Task).where(Task.project_id == project.id)))
        .scalars()
        .all()
    )
    challenge_tasks = [
        t
        for t in tasks
        if (t.first_payload or {}).get("type") == "release_audit_challenge"
    ]
    assert len(challenge_tasks) == 1


@pytest.mark.asyncio
async def test_required_children_terminal_waits_for_challenge_task(db_session):
    project, version = await _make_project_and_version(db_session)
    await _seed_qualification_children(
        db_session,
        project,
        version,
        audit_logs={"findings": [], "test_erosion": {"deleted_test_files": []}},
        smoke_logs={
            "passed": True,
            "tests": [{"name": "startup", "status": "pass"}],
            "suites": {"startup": {"passed": True}},
        },
        challenge_verdicts=[],
    )
    challenge_task = await db_session.get(
        Task, uuid.UUID(version.last_qualification["challenge_task_id"])
    )
    assert challenge_task is not None
    challenge_task.status = TaskStatus.QUEUED
    db_session.add(challenge_task)
    await db_session.commit()

    assert await rq._required_children_terminal(db_session, version) is False

    challenge_task.status = TaskStatus.COMPLETED
    db_session.add(challenge_task)
    await db_session.commit()

    assert await rq._required_children_terminal(db_session, version) is True


@pytest.mark.asyncio
async def test_required_children_terminal_waits_for_fresh_holistic_audit_with_reused_facts(
    db_session,
):
    """A fresh holistic child remains authoritative over reused code facts."""
    project, version = await _make_project_and_version(db_session)
    attempt_id = uuid.uuid4()
    audit_task = Task(
        project_id=project.id,
        title="fresh holistic release audit",
        payloads=[{"type": "release_audit", "base_branch": "main"}],
        vm_instance_id="vm-fresh-holistic",
        status=TaskStatus.QUEUED,
        context={"qualification_attempt_id": str(attempt_id)},
    )
    db_session.add(audit_task)
    await db_session.flush()
    version.last_qualification = {
        "status": "pending",
        "qualification_attempt_id": str(attempt_id),
        "audit_task_id": str(audit_task.id),
        "resolved_dimensions": {
            "audit": {"status": "passed", "sub_score": 0.9},
            "test_erosion": {"status": "passed", "sub_score": 1.0},
        },
    }
    db_session.add(version)
    await db_session.commit()

    assert await rq._required_children_terminal(db_session, version) is False

    audit_task.status = TaskStatus.COMPLETED
    db_session.add(audit_task)
    await db_session.commit()

    assert await rq._required_children_terminal(db_session, version) is True


@pytest.mark.asyncio
async def test_finalize_dry_run_when_promotion_disabled(db_session, mocker):
    """A high-confidence candidate is NOT promoted while promotion_enabled is
    False — the composite is recorded as a dry-run and the version stays open."""
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

    # promote() must NOT be called in dry-run mode.
    promote_spy = mocker.patch(
        "app.release_qualifier.promote",
        new=AsyncMock(),
    )

    result = await rq._finalize_qualification(db_session, version)
    await db_session.commit()

    assert result["status"] == "dry_run"
    assert result["would_promote"] is True
    assert result["confidence"] == pytest.approx(1.0)
    promote_spy.assert_not_called()

    await db_session.refresh(version)
    assert version.status == "open"
    lq = version.last_qualification
    assert lq["status"] == "dry_run"
    # Pre-cap completeness meta is persisted on last_qualification so consumers
    # can tell whether the carried findings lists were clipped at the cap. This
    # clean audit carries no findings → totals zero, neither list truncated.
    assert lq["audit_findings_total"] == len(lq["audit_findings"]) == 0
    assert lq["audit_findings_truncated"] is False
    assert (
        lq["excluded_audit_findings_total"] == len(lq["excluded_audit_findings"]) == 0
    )
    assert lq["excluded_audit_findings_truncated"] is False


def test_audit_leg_proof_state_matrix():
    """The audit-leg proof snapshot distinguishes proven, unproven, and advisory."""
    cases = [
        (
            "proven",
            {
                "candidate_sha": "feed" * 10,
                "audit_task_id": "audit-1",
                "audit_task_candidate_sha": "feed" * 10,
                "challenge_task_id": "challenge-1",
                "challenged_candidate_sha": "feed" * 10,
                "binding_expected_count": 2,
                "binding_record_count": 2,
                "reconciliation_status": "available",
                "reconciliation_reason": None,
                "legacy_advisory_only": False,
            },
        ),
        (
            "unproven",
            {
                "candidate_sha": "feed" * 10,
                "audit_task_id": "audit-1",
                "audit_task_candidate_sha": "feed" * 10,
                "challenge_task_id": "challenge-1",
                "challenged_candidate_sha": "beef" * 10,
                "binding_expected_count": 2,
                "binding_record_count": 2,
                "reconciliation_status": "available",
                "reconciliation_reason": "audit_challenge_sha_mismatch",
                "legacy_advisory_only": False,
            },
        ),
        (
            "unproven",
            {
                "candidate_sha": "feed" * 10,
                "audit_task_id": "audit-1",
                "audit_task_candidate_sha": "feed" * 10,
                "challenge_task_id": "challenge-1",
                "challenged_candidate_sha": "feed" * 10,
                "binding_expected_count": 2,
                "binding_record_count": 1,
                "reconciliation_status": "available",
                "reconciliation_reason": None,
                "legacy_advisory_only": False,
            },
        ),
        (
            "not_required",
            {
                "candidate_sha": "feed" * 10,
                "audit_task_id": "audit-1",
                "audit_task_candidate_sha": "feed" * 10,
                "challenge_task_id": None,
                "challenged_candidate_sha": None,
                "binding_expected_count": 0,
                "binding_record_count": 0,
                "reconciliation_status": "unavailable",
                "reconciliation_reason": "challenge_absent",
                "legacy_advisory_only": False,
            },
        ),
        (
            "not_required",
            {
                "candidate_sha": "feed" * 10,
                "audit_task_id": "audit-1",
                "audit_task_candidate_sha": "feed" * 10,
                "challenge_task_id": None,
                "challenged_candidate_sha": None,
                "binding_expected_count": 1,
                "binding_record_count": 0,
                "reconciliation_status": "unavailable",
                "reconciliation_reason": "challenge_absent",
                "legacy_advisory_only": True,
            },
        ),
    ]

    for expected_state, kwargs in cases:
        proof = rq._build_audit_leg_proof(**kwargs)
        assert proof["state"] == expected_state
        assert proof["required"] is (expected_state == "proven")
        assert proof["candidate_sha"] == kwargs["candidate_sha"]
        assert proof["audit_task_id"] == kwargs["audit_task_id"]
        assert proof["binding_expected_count"] == kwargs["binding_expected_count"]
        assert proof["binding_record_count"] == kwargs["binding_record_count"]


@pytest.mark.asyncio
async def test_finalize_persists_advisory_audit_challenge_synthesis(db_session, mocker):
    """Challenge synthesis is nested in audit_reconciliation and persisted."""
    from app import shared_state

    shared_state.runtime_config["promotion_enabled"] = False

    project, version = await _make_project_and_version(db_session)
    await _seed_qualification_children(
        db_session,
        project,
        version,
        audit_logs={
            "audit_score": 1.0,
            "findings": [
                {
                    "severity": "high",
                    "summary": "Primary migration concern.",
                    "file": "backend/app/models.py",
                    "verification_note": "confirmed against the migration code",
                }
            ],
            "test_erosion": {
                "deleted_test_files": [],
                "assertion_delta": 1,
                "test_count_delta": 0,
            },
        },
        # Keep a realistic legacy smoke artifact in the fixture to prove it does
        # not interfere with the active challenge/audit verdict.
        smoke_logs={
            "passed": True,
            "tests": [{"name": "a", "status": "pass"}],
            "suites": {"startup": {"passed": True}},
        },
    )
    await _attach_audit_challenge(
        db_session,
        project,
        version,
        logs={
            "agreement": "partial",
            "agreement_assessment": "Primary found one issue but missed another.",
            "missing_findings": [
                {
                    "severity": "medium",
                    "summary": "Missed rollout-risk finding.",
                    "file": "backend/app/release_qualifier.py",
                    "verification_note": "verified by reading the finalize branch",
                }
            ],
            "overstated_findings": [
                {
                    "severity": "low",
                    "summary": "Primary migration concern.",
                    "file": "backend/app/models.py",
                    "verification_note": "the severity is overstated in the diff",
                }
            ],
            "challenge_severity": "medium",
        },
    )

    result = await rq._finalize_qualification(db_session, version)
    await db_session.commit()

    assert result["status"] == "dry_run"
    assert result["would_promote"] is True

    await db_session.refresh(version)
    # The advisory RELEASE_AUDIT_CHALLENGE_REPORT no longer drives the synthesis.
    # Only the binding RELEASE_AUDIT_CHALLENGE artifact does; since none was produced
    # here (legacy advisory path), the synthesis is unavailable.
    synthesis = version.last_qualification["audit_reconciliation"]["audit_challenge"]
    assert synthesis["status"] == "unavailable"
    assert synthesis["reason"] == "challenge_absent"
    assert synthesis["binding"] is True
    proof = version.last_qualification["audit_reconciliation"]["audit_leg_proof"]
    assert proof["state"] == "not_required"
    assert proof["required"] is False
    assert proof["binding_expected_count"] == 1
    assert proof["binding_record_count"] == 0

    verdict = (
        (
            await db_session.execute(
                select(QualificationVerdict).where(
                    QualificationVerdict.release_version_id == version.id
                )
            )
        )
        .scalars()
        .one()
    )
    assert verdict.terminal_status == "dry_run"
    assert verdict.audit_reconciliation["audit_challenge"] == synthesis
    assert verdict.audit_reconciliation["audit_leg_proof"] == proof


@pytest.mark.asyncio
async def test_finalize_unparseable_challenge_is_advisory_and_verdict_identical(
    db_session, mocker
):
    """Unparseable challenge evidence records a reason but cannot affect verdict."""
    from app import shared_state

    shared_state.runtime_config["promotion_enabled"] = False
    audit_logs = {
        "audit_score": 1.0,
        "findings": [],
        "test_erosion": {
            "deleted_test_files": [],
            "assertion_delta": 0,
            "test_count_delta": 0,
        },
    }
    smoke_logs = {"passed": True}

    baseline_project, baseline_version = await _make_project_and_version(db_session)
    await _seed_qualification_children(
        db_session,
        baseline_project,
        baseline_version,
        audit_logs=audit_logs,
        smoke_logs=smoke_logs,
    )
    baseline_result = await rq._finalize_qualification(db_session, baseline_version)
    await db_session.commit()
    await db_session.refresh(baseline_version)
    baseline_tuple = (
        baseline_result["status"],
        baseline_result["confidence"],
        baseline_result["would_promote"],
        baseline_version.last_qualification["blocker"],
    )
    baseline_challenge = baseline_version.last_qualification["audit_reconciliation"][
        "audit_challenge"
    ]
    assert baseline_challenge["status"] == "unavailable"
    assert baseline_challenge["reason"] == "challenge_absent"

    challenged_project, challenged_version = await _make_project_and_version(db_session)
    await _seed_qualification_children(
        db_session,
        challenged_project,
        challenged_version,
        audit_logs=audit_logs,
        smoke_logs=smoke_logs,
    )
    await _attach_audit_challenge(
        db_session,
        challenged_project,
        challenged_version,
        raw_logs="not valid json {{{",
    )
    challenged_result = await rq._finalize_qualification(db_session, challenged_version)
    await db_session.commit()
    await db_session.refresh(challenged_version)
    challenged_tuple = (
        challenged_result["status"],
        challenged_result["confidence"],
        challenged_result["would_promote"],
        challenged_version.last_qualification["blocker"],
    )

    assert challenged_tuple == baseline_tuple
    challenge = challenged_version.last_qualification["audit_reconciliation"][
        "audit_challenge"
    ]
    assert challenge["status"] == "unavailable"
    # The dead _REPORT reader is gone; the advisory path no longer reads any
    # artifact, so unparseable evidence yields "challenge_absent" (same as the
    # baseline), confirming the finalize path never consults _REPORT.
    assert challenge["reason"] == "challenge_absent"


@pytest.mark.asyncio
async def test_finalize_challenge_synthesis_resolves_emitted_artifact_only(
    db_session, mocker
):
    """Synthesis resolves the RELEASE_AUDIT_CHALLENGE artifact the live handler
    emits.  A real emitted-artifact challenge drives status='available' and lands
    the verdict in the challenged telemetry cohort.  No live finalize path
    consults RELEASE_AUDIT_CHALLENGE_REPORT."""
    from app import shared_state
    from app.release_scoring import compute_challenged_vs_unchallenged_telemetry

    shared_state.runtime_config["promotion_enabled"] = False

    project, version = await _make_project_and_version(db_session)
    _candidate_sha = "feed" * 10
    await _seed_qualification_children(
        db_session,
        project,
        version,
        audit_logs={
            "audit_score": 1.0,
            "findings": [
                {
                    "severity": "high",
                    "summary": "Binding proof finding.",
                    "file": "backend/app/release_qualifier.py",
                    "verification_note": "confirmed in the binding test path",
                }
            ],
            "test_erosion": {
                "deleted_test_files": [],
                "assertion_delta": 0,
                "test_count_delta": 0,
            },
        },
        smoke_logs={"passed": True},
        challenge_verdicts=[
            {
                "verdict": "confirmed",
                "summary": "Binding proof finding.",
                "severity": "high",
            }
        ],
    )

    await rq._finalize_qualification(db_session, version)
    await db_session.commit()
    await db_session.refresh(version)

    # The binding RELEASE_AUDIT_CHALLENGE artifact drove the synthesis to
    # 'available' — the dead _REPORT reader was never consulted.
    synthesis = version.last_qualification["audit_reconciliation"]["audit_challenge"]
    assert synthesis["status"] == "available", (
        f"expected 'available', got {synthesis!r}"
    )
    assert synthesis["binding"] is True
    assert synthesis["challenged_candidate_sha"] == _candidate_sha
    assert synthesis["confirmed_count"] >= 1
    proof = version.last_qualification["audit_reconciliation"]["audit_leg_proof"]
    assert proof["state"] == "proven"
    assert proof["required"] is True
    assert proof["challenged_candidate_sha"] == _candidate_sha

    # The verdict row should also land in the 'challenged' telemetry cohort.
    verdict = (
        (
            await db_session.execute(
                select(QualificationVerdict).where(
                    QualificationVerdict.release_version_id == version.id
                )
            )
        )
        .scalars()
        .one()
    )
    assert verdict.audit_reconciliation["audit_challenge"]["status"] == "available"
    assert verdict.audit_reconciliation["audit_leg_proof"] == proof

    verdict_dict = {
        "candidate_sha": verdict.candidate_sha,
        "audit_reconciliation": verdict.audit_reconciliation,
        "would_promote": verdict.would_promote,
        "terminal_status": verdict.terminal_status,
        "post_promotion_outcome": verdict.post_promotion_outcome,
        "excluded_audit_findings": verdict.excluded_audit_findings,
    }
    telemetry = compute_challenged_vs_unchallenged_telemetry([verdict_dict])
    # With only one verdict the cohort is too small for rate stats, but it must
    # be classified into the 'challenged' bucket (not 'unchallenged').
    assert telemetry["challenged"]["sample_size"] == 1
    assert telemetry["unchallenged"]["sample_size"] == 0


@pytest.mark.asyncio
async def test_finalize_clean_audit_mismatched_challenge_is_unavailable_and_unchallenged(
    db_session,
):
    from app import shared_state
    from app.release_scoring import compute_challenged_vs_unchallenged_telemetry

    shared_state.runtime_config["promotion_enabled"] = False

    project, version = await _make_project_and_version(db_session)
    _candidate_sha = "feed" * 10
    await _seed_qualification_children(
        db_session,
        project,
        version,
        audit_logs={
            "audit_score": 1.0,
            "findings": [],
            "test_erosion": {
                "deleted_test_files": [],
                "assertion_delta": 0,
                "test_count_delta": 0,
            },
        },
        smoke_logs={"passed": True},
        challenge_verdicts=[
            {
                "finding_title": "No issue",
                "verdict": "confirmed",
                "rationale": "challenger agrees",
            }
        ],
    )

    challenge_task_id = uuid.UUID(version.last_qualification["challenge_task_id"])
    challenge_artifact = (
        (
            await db_session.execute(
                select(Artifact).where(
                    Artifact.task_id == challenge_task_id,
                    Artifact.artifact_type == ArtifactType.RELEASE_AUDIT_CHALLENGE,
                )
            )
        )
        .scalars()
        .one()
    )
    challenge_artifact.logs = json.dumps(
        {
            "challenged_candidate_sha": "beef" * 10,
            "verdicts": [
                {
                    "finding_title": "No issue",
                    "verdict": "confirmed",
                }
            ],
        }
    )
    db_session.add(challenge_artifact)
    await db_session.commit()

    await rq._finalize_qualification(db_session, version)
    await db_session.commit()
    await db_session.refresh(version)

    synthesis = version.last_qualification["audit_reconciliation"]["audit_challenge"]
    assert synthesis["status"] == "unavailable"
    assert synthesis["reason"] == "audit_challenge_sha_mismatch"
    assert synthesis["challenged_candidate_sha"] == "beef" * 10

    verdict = (
        (
            await db_session.execute(
                select(QualificationVerdict).where(
                    QualificationVerdict.release_version_id == version.id
                )
            )
        )
        .scalars()
        .one()
    )
    telemetry = compute_challenged_vs_unchallenged_telemetry(
        [
            {
                "candidate_sha": verdict.candidate_sha,
                "audit_reconciliation": verdict.audit_reconciliation,
                "would_promote": verdict.would_promote,
                "terminal_status": verdict.terminal_status,
                "post_promotion_outcome": verdict.post_promotion_outcome,
                "excluded_audit_findings": verdict.excluded_audit_findings,
            }
        ]
    )
    assert telemetry["challenged"]["sample_size"] == 0
    assert telemetry["unchallenged"]["sample_size"] == 1


@pytest.mark.asyncio
async def test_finalize_records_excluded_audit_findings(db_session, mocker):
    """_finalize_qualification carries excluded (unverified higher-severity)
    findings into last_qualification for display, while audit_findings still
    omits them and the reconciliation count agrees."""
    from app import shared_state

    shared_state.runtime_config["promotion_enabled"] = False

    project, version = await _make_project_and_version(db_session)
    await _seed_qualification_children(
        db_session,
        project,
        version,
        audit_logs={
            # Model self-reports a clean score; the only finding is an
            # unverified high — excluded from scoring and audit_findings, but
            # surfaced in excluded_audit_findings.
            "audit_score": 1.0,
            "findings": [
                {"severity": "high", "summary": "Unverified injection risk."},
            ],
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

    mocker.patch("app.release_qualifier.promote", new=AsyncMock())

    await rq._finalize_qualification(db_session, version)
    await db_session.commit()
    await db_session.refresh(version)

    lq = version.last_qualification
    # The unverified high finding is carried for display, marked unverified.
    assert lq["excluded_audit_findings"] == [
        {
            "severity": "high",
            "summary": "Unverified injection risk.",
            "unverified": "true",
            "key": rq._audit_finding_key("Unverified injection risk.", None),
        }
    ]
    # ...but it must NOT appear in the scored/actionable findings list.
    assert lq["audit_findings"] == []
    # ...and the reconciliation count agrees.
    assert lq["audit_reconciliation"]["excluded_findings_count"] == 1
    # Pre-cap completeness meta agrees with the capped lists.
    assert lq["excluded_audit_findings_total"] == 1
    assert lq["excluded_audit_findings_truncated"] is False
    assert lq["audit_findings_total"] == 0


@pytest.mark.asyncio
async def test_finalize_not_promoted_on_blocker_sets_stabilize(db_session):
    """A hard blocker (deleted test file) keeps the version open and flips it
    back to the stabilize phase."""
    from app import shared_state

    shared_state.runtime_config["promotion_enabled"] = True

    project, version = await _make_project_and_version(db_session)
    # Version starts in build; finalize on a blocker must move it to stabilize.
    assert version.phase == "build"
    await _seed_qualification_children(
        db_session,
        project,
        version,
        audit_logs={
            "audit_score": 1.0,
            "test_erosion": {
                "deleted_test_files": ["testing/test_deleted.py"],
                "assertion_delta": 0,
                "test_count_delta": -3,
            },
            "deleted_test_file_classifications": [
                {
                    "path": "testing/test_deleted.py",
                    "classification": "genuine_erosion",
                    "rationale": "Subject module present and unchanged; deletion is genuine erosion.",
                }
            ],
        },
        smoke_logs={
            "passed": True,
            "tests": [{"name": "a", "status": "pass"}],
            "suites": {"startup": {"passed": True}},
        },
    )

    result = await rq._finalize_qualification(db_session, version)
    await db_session.commit()

    assert result["status"] == "not_promoted"
    assert result["blocker"] == "test_file_deleted"

    await db_session.refresh(version)
    assert version.status == "open"
    assert version.phase == "stabilize"
    assert version.last_qualification["status"] == "not_promoted"
    assert version.last_qualification["blocker"] == "test_file_deleted"


@pytest.mark.asyncio
async def test_finalize_blocks_on_migration_identity_collision(db_session, mocker):
    """An UNAUTHORIZED revision-identity collision (a revision id reused for
    different content, NOT on the authorized-reconciliation registry) must be a
    HARD qualification blocker end-to-end: deployed main keeps revision 0138 ==
    digest_cache_table, the candidate reassigned 0138 to task_blocked_state_fields.
    An otherwise-clean candidate (audit 1.0, smoketest pass, CI green) must be
    HARD-blocked at qualification: confidence forced to 0, promote False, and the
    blocker names the diverging revision — never a soft audit deduction.

    (The revision-0143 renumber is the ONE authorized, deploy-time-reconciled
    divergence — its non-blocking path is covered by
    ``test_qualification_gate_authorizes_reconciled_0143_divergence`` in
    test_migration_divergence.py; using it here would no longer block.)"""
    from app import shared_state

    shared_state.runtime_config["promotion_enabled"] = True

    project, version = await _make_project_and_version(db_session)
    await _seed_qualification_children(
        db_session,
        project,
        version,
        # A pristine quality profile — without the migration gate this candidate
        # would promote, isolating the migration collision as the sole blocker.
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

    # Deployed main keeps 0138 == digest_cache_table; the candidate reused 0138
    # for a DIFFERENT migration (task_blocked_state_fields) — an unrelated
    # revision-identity collision the deterministic detector flags, and one NOT
    # on the authorized-reconciliation registry (so it still hard-blocks).
    mocker.patch(
        "app.github.list_version_shas_at_ref",
        new=_versions_by_ref(
            main_list=["0138_digest_cache_table.py"],
            candidate_list=["0138_task_blocked_state_fields.py"],
        ),
    )

    # A real collision must NEVER promote, even with promotion enabled.
    promote_spy = mocker.patch("app.release_qualifier.promote", new=AsyncMock())

    result = await rq._finalize_qualification(db_session, version)
    await db_session.commit()

    assert result["status"] == "not_promoted"
    assert result["blocker"] == "migration_identity_collision"
    assert result["confidence"] == 0.0
    promote_spy.assert_not_called()

    await db_session.refresh(version)
    lq = version.last_qualification
    assert lq["status"] == "not_promoted"
    assert lq["blocker"] == "migration_identity_collision"
    assert lq["confidence"] == 0.0
    assert version.phase == "stabilize"
    # The detail names the specific diverging revision (0138) so the operator
    # sees WHICH revision collides, not just a generic blocker label.
    assert lq["migration_safety"] == "block"
    detail = lq["migration_safety_detail"]
    assert "0138" in detail
    assert "digest_cache_table" in detail


@pytest.mark.asyncio
async def test_finalize_holds_when_migration_listing_fetch_fails(db_session, mocker):
    """Fail-safe: when the candidate's / target's migration list cannot be
    fetched, migration safety is UNKNOWN — the release is HELD (deferred to the
    next cycle) rather than waved through. A fetch error is not 'no collision'."""
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

    # The migration listing fetch fails transiently — uncertainty must HOLD.
    mocker.patch(
        "app.github.list_version_shas_at_ref",
        new=AsyncMock(side_effect=RuntimeError("GitHub 502")),
    )
    promote_spy = mocker.patch("app.release_qualifier.promote", new=AsyncMock())

    result = await rq._finalize_qualification(db_session, version)
    await db_session.commit()

    # Held as an infra failure — no quality verdict, never promoted.
    assert result["status"] == "infra_failure"
    assert result["error"].startswith("migration_identity_check_unavailable")
    assert "GitHub 502" in result["error"]
    promote_spy.assert_not_called()

    await db_session.refresh(version)
    assert version.status == "open"
    assert version.phase == "stabilize"
    assert version.last_qualification["status"] == "infra_failure"


@pytest.mark.asyncio
async def test_finalize_blocks_on_migration_apply_drift(db_session, mocker):
    """The scratch-DB migration-apply gate is a SECOND deterministic dimension:
    when it returns 'block' (the candidate's chain, replayed against production's
    real recorded schema, leaves a required object uncreated — the v5 computejob
    renumber-skip class) an otherwise-clean candidate is HARD-blocked. Confidence
    forced to 0, promote False, blocker migration_apply_drift, and the verdict
    record carries migration_apply_safety='block' with the specific detail.

    The filename-identity gate stays CLEAN here (autouse default), isolating the
    apply gate as the sole blocker — proving the two dimensions are distinct."""
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

    # The apply gate reports deploy-fatal drift: a required table is uncreated
    # when the candidate's chain is applied against production's recorded state.
    apply_detail = (
        "candidate migrations applied against production's recorded revision "
        "'0375' leave the schema missing required object(s) (deploy-fatal "
        "drift): table 'computejob'"
    )
    mocker.patch(
        "app.release_qualifier.run_migration_apply_gate",
        new=AsyncMock(return_value=("block", apply_detail)),
    )
    promote_spy = mocker.patch("app.release_qualifier.promote", new=AsyncMock())

    result = await rq._finalize_qualification(db_session, version)
    await db_session.commit()

    assert result["status"] == "not_promoted"
    assert result["blocker"] == "migration_apply_drift"
    assert result["confidence"] == 0.0
    promote_spy.assert_not_called()

    await db_session.refresh(version)
    lq = version.last_qualification
    assert lq["status"] == "not_promoted"
    assert lq["blocker"] == "migration_apply_drift"
    assert lq["confidence"] == 0.0
    assert version.phase == "stabilize"
    # The new fields are recorded alongside (not replacing) the existing
    # migration-identity fields — the identity gate stayed clean here.
    assert lq["migration_safety"] == "ok"
    assert lq["migration_apply_safety"] == "block"
    assert lq["migration_apply_safety_detail"] == apply_detail
    assert "computejob" in lq["migration_apply_safety_detail"]


def _stamp_current_goal_contract(project, version) -> None:
    """Complete legacy pending fixtures with the decision contract at launch."""
    qual = dict(version.last_qualification or {})
    policy = rq._load_policy(project)
    capability_mode = str(
        rq._runtime_config.get("capability_proof_gate_mode", "off") or "off"
    ).strip().lower()
    qual.update(
        {
            "version_goal": version.goal or "",
            "promotion_policy_snapshot": policy,
            "capability_proof_mode": capability_mode,
            "requested_probes": qual.get("requested_probes") or [],
            "contract_fingerprint": rq._qualification_contract_fingerprint(
                version.goal or "",
                str(qual.get("rubric_fingerprint") or ""),
                promotion_policy=policy,
                capability_proof_mode=capability_mode,
                env_preflight_enabled=bool(
                    qual.get("env_preflight_enabled", False)
                ),
                requested_probes=qual.get("requested_probes") or [],
            ),
        }
    )
    version.last_qualification = qual


@pytest.mark.asyncio
async def test_finalize_consumes_holistic_audit_goal_grade(db_session, mocker):
    """The holistic release audit owns the goal-grade promotion contract.

    Four scenarios prove the artifact is still unskippable, while the
    merge-grade miss gate remains default-off:
      (a) a FAILING merge-grade -> promote proceeds while the gate is unarmed.
      (b) the required holistic goal grade MISSING -> held (infra_failure),
          NOT promoted (the gate cannot be skipped).
      (c) a PASSING merge-grade with everything else green -> promote proceeds.
      (d) a FAILING merge-grade -> not_promoted when the gate is armed.
    """
    from app import shared_state

    mocker.patch.dict(shared_state.runtime_config, {"promotion_enabled": True})

    clean_audit = {
        "audit_score": 1.0,
        "test_erosion": {
            "deleted_test_files": [],
            "assertion_delta": 5,
            "test_count_delta": 2,
        },
    }
    clean_smoke = {
        "passed": True,
        "tests": [{"name": "a", "status": "pass"}],
        "suites": {"startup": {"passed": True}},
    }
    goal = "Deliver the blind merge-grade qualification gate on stabilize->qualify"

    # --- (a) FAILING holistic grade, unarmed threshold -> promote proceeds
    # merge_grade_block_threshold is not set here (defaults to 0.0), so the gate
    # is not armed and a failing merge grade does NOT block promotion.
    project_a, version_a = await _make_project_and_version(db_session)
    await _seed_qualification_children(
        db_session,
        project_a,
        version_a,
        audit_logs={
            **clean_audit,
            "covers_goal": False,
            "coverage_score": 0.1,
            "biggest_gap": "The gate was never wired into compute_composite.",
        },
        smoke_logs=clean_smoke,
        version_goal=goal,
    )
    _stamp_current_goal_contract(project_a, version_a)
    promote_a = mocker.patch(
        "app.release_qualifier.promote",
        new=AsyncMock(return_value={"status": "promoted"}),
    )
    result_a = await rq._finalize_qualification(db_session, version_a)
    await db_session.commit()

    promote_a.assert_called_once()
    assert result_a == {"status": "promoted"}

    # --- (b) MISSING goal grade in the holistic report -> infra hold --------
    project_b, version_b = await _make_project_and_version(db_session)
    await _seed_qualification_children(
        db_session,
        project_b,
        version_b,
        audit_logs=clean_audit,
        smoke_logs=clean_smoke,
        version_goal=goal,
    )
    _stamp_current_goal_contract(project_b, version_b)
    promote_b = mocker.patch("app.release_qualifier.promote", new=AsyncMock())
    result_b = await rq._finalize_qualification(db_session, version_b)
    await db_session.commit()

    assert result_b["status"] == "infra_failure"
    promote_b.assert_not_called()
    await db_session.refresh(version_b)
    assert version_b.last_qualification["status"] == "infra_failure"
    assert version_b.phase == "stabilize"
    assert version_b.status == "open"

    # --- (c) PASSING holistic goal grade -> promote proceeds ----------------
    project_c, version_c = await _make_project_and_version(db_session)
    await _seed_qualification_children(
        db_session,
        project_c,
        version_c,
        audit_logs={
            **clean_audit,
            "covers_goal": True,
            "coverage_score": 0.95,
            "biggest_gap": "Minor: docs could mention the env override.",
        },
        smoke_logs=clean_smoke,
        version_goal=goal,
    )
    _stamp_current_goal_contract(project_c, version_c)
    promote_c = mocker.patch(
        "app.release_qualifier.promote",
        new=AsyncMock(return_value={"status": "promoted"}),
    )
    result_c = await rq._finalize_qualification(db_session, version_c)
    await db_session.commit()

    promote_c.assert_called_once()
    assert result_c == {"status": "promoted"}

    # --- (d) FAILING holistic goal grade + armed threshold -> hard blocker --
    mocker.patch.dict(
        shared_state.runtime_config,
        {"promotion_enabled": True, "merge_grade_block_threshold": 0.1},
    )
    # An armed + failing merge-grade consults the already-on-main ancestry
    # exemption (finding 0). This candidate is NOT on the promotion target, so
    # the block persists — pin is_ancestor_of=False to keep the test hermetic.
    mocker.patch("app.github.is_ancestor_of", new=AsyncMock(return_value=False))
    project_d, version_d = await _make_project_and_version(db_session)
    await _seed_qualification_children(
        db_session,
        project_d,
        version_d,
        audit_logs={
            **clean_audit,
            "covers_goal": False,
            "coverage_score": 0.1,
            "biggest_gap": "The merged work still misses the stated goal.",
        },
        smoke_logs=clean_smoke,
        version_goal=goal,
    )
    _stamp_current_goal_contract(project_d, version_d)
    promote_d = mocker.patch(
        "app.release_qualifier.promote",
        new=AsyncMock(return_value={"status": "promoted"}),
    )
    result_d = await rq._finalize_qualification(db_session, version_d)
    await db_session.commit()

    promote_d.assert_not_called()
    assert result_d["status"] == "not_promoted"
    assert result_d["blocker"] == "merge_grade_misses"
    await db_session.refresh(version_d)
    assert version_d.last_qualification["blocker"] == "merge_grade_misses"
    assert version_d.last_qualification["merge_grade_misses"] is True


@pytest.mark.asyncio
async def test_finalize_holistic_goal_grade_suppressed_for_prior_superseded_version(
    db_session, mocker
):
    """Already-promoted exemption: when the previous qualification was
    qualification_superseded (force-promoted out-of-band), the merge-grade block
    is suppressed even when covers_goal=False.  A fresh version that never had
    qualification_superseded still blocks.

    Scenarios (with the merge-grade gate ARMED via merge_grade_block_threshold):
      (a) prior_superseded=True + failing merge-grade -> NOT blocked, promote proceeds
      (b) prior_superseded absent/False + failing merge-grade (candidate NOT on
          main) -> hard block (gate remains load-bearing for never-promoted
          versions)
    """
    from app import shared_state

    shared_state.runtime_config["promotion_enabled"] = True
    # Arm the merge-grade gate — since the gate honours merge_grade_block_threshold
    # (default 0.0 = off), a failing grade only blocks when the threshold is raised.
    mocker.patch.dict(
        shared_state.runtime_config, {"merge_grade_block_threshold": 1.0}
    )
    # The armed + failing merge-grade path consults the already-on-main ancestry
    # exemption (finding 0); these candidates are NOT on the promotion target, so
    # pin is_ancestor_of=False to keep the block load-bearing and the test hermetic.
    mocker.patch("app.github.is_ancestor_of", new=AsyncMock(return_value=False))

    clean_audit = {
        "audit_score": 1.0,
        "test_erosion": {
            "deleted_test_files": [],
            "assertion_delta": 5,
            "test_count_delta": 2,
        },
    }
    clean_smoke = {
        "passed": True,
        "tests": [{"name": "a", "status": "pass"}],
        "suites": {"startup": {"passed": True}},
    }
    failing_holistic_audit = {
        **clean_audit,
        "covers_goal": False,
        "coverage_score": 0.05,
        "biggest_gap": "Stabilization-only tail commits; feature content already on main.",
    }
    goal = "Deliver the already-promoted merge-grade exemption"

    # --- (a) prior_superseded=True -> merge-grade block suppressed, promote proceeds
    project_a, version_a = await _make_project_and_version(db_session)
    await _seed_qualification_children(
        db_session,
        project_a,
        version_a,
        audit_logs=failing_holistic_audit,
        smoke_logs=clean_smoke,
        version_goal=goal,
    )
    _stamp_current_goal_contract(project_a, version_a)
    # Stamp prior_superseded=True directly on last_qualification (simulates
    # run_qualification carrying forward the qualification_superseded blocker).
    version_a.last_qualification = {
        **version_a.last_qualification,
        "prior_superseded": True,
    }
    db_session.add(version_a)
    await db_session.commit()
    await db_session.refresh(version_a)

    promote_a = mocker.patch(
        "app.release_qualifier.promote",
        new=AsyncMock(return_value={"status": "promoted"}),
    )
    result_a = await rq._finalize_qualification(db_session, version_a)
    await db_session.commit()

    promote_a.assert_called_once()
    assert result_a == {"status": "promoted"}, (
        f"Expected promotion for superseded version but got: {result_a}"
    )

    # --- (b) prior_superseded absent -> fresh versions still block -----------
    project_b, version_b = await _make_project_and_version(db_session)
    await _seed_qualification_children(
        db_session,
        project_b,
        version_b,
        audit_logs=failing_holistic_audit,
        smoke_logs=clean_smoke,
        version_goal=goal,
    )
    _stamp_current_goal_contract(project_b, version_b)
    # Do NOT set prior_superseded — version_b is a never-promoted fresh version.
    promote_b = mocker.patch(
        "app.release_qualifier.promote",
        new=AsyncMock(return_value={"status": "promoted"}),
    )
    result_b = await rq._finalize_qualification(db_session, version_b)
    await db_session.commit()

    promote_b.assert_not_called()
    assert result_b["status"] == "not_promoted"
    assert result_b["blocker"] == "merge_grade_misses"
    await db_session.refresh(version_b)
    assert version_b.last_qualification["blocker"] == "merge_grade_misses"
    assert version_b.last_qualification["merge_grade_status"] == "fail"


@pytest.mark.asyncio
async def test_finalize_holistic_goal_grade_exempts_candidate_already_on_main(
    db_session, mocker
):
    """Finding 0: an armed + failing merge-grade must NOT wedge a candidate that
    has already landed on the promotion target.

    An out-of-band force-promoted candidate never gets the prior_superseded flag
    (that is only set on the version-cut path), so without an ancestry exemption
    the restored hard merge-grade gate would keep writing not_promoted /
    merge_grade_misses forever with no reconciler to clear it. When the candidate
    is already reachable from main, the grade is exempted and promotion proceeds.

    Control: the SAME armed + failing grade with the candidate NOT on main still
    hard-blocks (the exemption is scoped to already-landed candidates only).
    """
    from app import shared_state

    shared_state.runtime_config["promotion_enabled"] = True
    # Arm the gate so a failing merge-grade is load-bearing.
    mocker.patch.dict(
        shared_state.runtime_config, {"merge_grade_block_threshold": 1.0}
    )

    clean_audit = {
        "audit_score": 1.0,
        "test_erosion": {
            "deleted_test_files": [],
            "assertion_delta": 5,
            "test_count_delta": 2,
        },
    }
    clean_smoke = {
        "passed": True,
        "tests": [{"name": "a", "status": "pass"}],
        "suites": {"startup": {"passed": True}},
    }
    failing_holistic_audit = {
        **clean_audit,
        "covers_goal": False,
        "coverage_score": 0.05,
        "biggest_gap": "Feature content already merged straight to main.",
    }
    goal = "Deliver the already-on-main merge-grade exemption"

    # --- Candidate ALREADY on main -> exempted, promote proceeds -------------
    mocker.patch("app.github.is_ancestor_of", new=AsyncMock(return_value=True))
    project_a, version_a = await _make_project_and_version(db_session)
    await _seed_qualification_children(
        db_session,
        project_a,
        version_a,
        audit_logs=failing_holistic_audit,
        smoke_logs=clean_smoke,
        version_goal=goal,
    )
    _stamp_current_goal_contract(project_a, version_a)
    promote_a = mocker.patch(
        "app.release_qualifier.promote",
        new=AsyncMock(return_value={"status": "promoted"}),
    )
    result_a = await rq._finalize_qualification(db_session, version_a)
    await db_session.commit()

    # promote() is only reached because the failing grade was exempted; the
    # base_record it receives records the exemption (provenance), not a pass.
    promote_a.assert_called_once()
    assert result_a == {"status": "promoted"}
    _base_record_a = promote_a.call_args.kwargs["base_record"]
    assert _base_record_a["merge_grade_status"] == "already_on_main_exempt"
    assert _base_record_a["merge_grade_misses"] is False

    # --- Control: candidate NOT on main -> the armed failing grade blocks ----
    mocker.patch("app.github.is_ancestor_of", new=AsyncMock(return_value=False))
    project_b, version_b = await _make_project_and_version(db_session)
    await _seed_qualification_children(
        db_session,
        project_b,
        version_b,
        audit_logs=failing_holistic_audit,
        smoke_logs=clean_smoke,
        version_goal=goal,
    )
    _stamp_current_goal_contract(project_b, version_b)
    promote_b = mocker.patch(
        "app.release_qualifier.promote",
        new=AsyncMock(return_value={"status": "promoted"}),
    )
    result_b = await rq._finalize_qualification(db_session, version_b)
    await db_session.commit()

    promote_b.assert_not_called()
    assert result_b["status"] == "not_promoted"
    assert result_b["blocker"] == "merge_grade_misses"


@pytest.mark.asyncio
async def test_finalize_not_promoted_carries_keyless_requalification_marker(
    db_session, mocker
):
    """Finding 1: the not_promoted record must carry the one-shot
    ``keyless_requalification_consumed_at`` marker forward from the prior
    (pending) qualification.

    The sweep's same-SHA keyless re-drive bounds itself to exactly-once by
    checking this marker on the not_promoted record. Because base_record REPLACES
    last_qualification wholesale, dropping the marker would break the exactly-once
    bound and let heavy re-qualification loop on an unchanged HEAD.
    """
    from app import shared_state

    shared_state.runtime_config["promotion_enabled"] = True

    # A clean audit but a DELETED test file classified as genuine_erosion -> a
    # deterministic test_file_deleted hard blocker, so finalize takes the
    # not_promoted path without any merge-grade / GitHub ancestry interaction.
    # (_score_audit reads the deleted set from test_erosion and the per-file
    # classification from the report-level deleted_test_file_classifications.)
    eroding_audit = {
        "audit_score": 1.0,
        "test_erosion": {
            "deleted_test_files": ["backend/testing/test_gone.py"],
        },
        "deleted_test_file_classifications": [
            {
                "path": "backend/testing/test_gone.py",
                "classification": "genuine_erosion",
            }
        ],
    }
    clean_smoke = {
        "passed": True,
        "tests": [{"name": "a", "status": "pass"}],
        "suites": {"startup": {"passed": True}},
    }

    project, version = await _make_project_and_version(db_session)
    await _seed_qualification_children(
        db_session,
        project,
        version,
        audit_logs=eroding_audit,
        smoke_logs=clean_smoke,
    )
    # Stamp the one-shot keyless marker on the pending record, exactly as
    # run_qualification carries it forward from the prior verdict.
    marker = "2026-06-30T00:00:00+00:00"
    version.last_qualification = {
        **version.last_qualification,
        "keyless_requalification_consumed_at": marker,
    }
    db_session.add(version)
    await db_session.commit()
    await db_session.refresh(version)

    promote = mocker.patch("app.release_qualifier.promote", new=AsyncMock())
    result = await rq._finalize_qualification(db_session, version)
    await db_session.commit()

    promote.assert_not_called()
    assert result["status"] == "not_promoted"
    assert result["blocker"] == "test_file_deleted"
    await db_session.refresh(version)
    # The marker survives the base_record replacement (finding 1 fix).
    assert (
        version.last_qualification["keyless_requalification_consumed_at"] == marker
    )


@pytest.mark.asyncio
async def test_finalize_rolls_back_after_pre_lock_phase(
    db_session, mocker
):
    """The pre-lock phase must rollback the session before taking the FOR UPDATE.

    Regression guard: a swallowed DBAPI error inside a fail-closed pre-lock
    helper (e.g. _resolve_ci_at_finalize catching Exception) leaves the
    SQLAlchemy transaction aborted. Without the post-pre-lock rollback the later
    FOR UPDATE/commit surfaces as PendingRollbackError and the sweep
    misclassifies the qualification as a finalizer crash.
    """
    from app import shared_state

    shared_state.runtime_config["promotion_enabled"] = True

    clean_audit = {
        "audit_score": 1.0,
        "test_erosion": {
            "deleted_test_files": [],
            "assertion_delta": 5,
            "test_count_delta": 2,
        },
    }
    clean_smoke = {
        "passed": True,
        "tests": [{"name": "a", "status": "pass"}],
        "suites": {"startup": {"passed": True}},
    }

    project, version = await _make_project_and_version(db_session)
    await _seed_qualification_children(
        db_session,
        project,
        version,
        audit_logs=clean_audit,
        smoke_logs=clean_smoke,
        version_goal="Deliver rollback recovery for pre-lock phase",
    )
    await db_session.commit()
    await db_session.refresh(version)

    rollback_calls = 0
    original_rollback = db_session.rollback

    async def tracked_rollback():
        nonlocal rollback_calls
        rollback_calls += 1
        await original_rollback()

    mocker.patch.object(db_session, "rollback", new=tracked_rollback)

    result = await rq._finalize_in_dedicated_session(
        version.id,
        timeout_now=None,
        session_factory=_shared_session_factory(db_session),
    )
    await db_session.commit()

    assert rollback_calls >= 1
    assert result is not None


@pytest.mark.asyncio
async def test_finalize_holds_when_migration_apply_gate_defers(db_session, mocker):
    """Fail-safe: when the apply gate cannot determine safety (prod state
    unreadable, candidate fetch failed, scratch DB unprovisionable) it returns
    'defer'. Migration-apply safety is UNKNOWN, so the release is HELD as an
    infra failure (no quality verdict, returned to stabilize) — never waved
    through, mirroring the identity gate's defer path."""
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

    mocker.patch(
        "app.release_qualifier.run_migration_apply_gate",
        new=AsyncMock(
            return_value=("defer", "could not read production's recorded state")
        ),
    )
    promote_spy = mocker.patch("app.release_qualifier.promote", new=AsyncMock())

    result = await rq._finalize_qualification(db_session, version)
    await db_session.commit()

    assert result["status"] == "infra_failure"
    assert result["error"].startswith("migration_apply_check_unavailable")
    assert "could not read production" in result["error"]
    promote_spy.assert_not_called()

    await db_session.refresh(version)
    assert version.status == "open"
    assert version.phase == "stabilize"
    assert version.last_qualification["status"] == "infra_failure"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("scratch_value", "expected_verdict", "expected_code"),
    [
        # Absent config is a permanent operator choice, recorded as a DISTINCT
        # "skipped" state (never "defer"/"ok") so a gate that was never attempted
        # can never be mistaken for one that ran and passed.
        (None, "skipped", "scratch_database_not_configured"),
        # A malformed URL is a config error the finalizer HOLDS on (defer), with
        # the offending raw value kept out of every diagnostic.
        ("malformed://secret-value", "defer", "scratch_database_url_invalid"),
    ],
)
async def test_migration_apply_preflight_holds_without_prod_io(
    db_session, mocker, monkeypatch, scratch_value, expected_verdict, expected_code
):
    _project, version = await _make_project_and_version(
        db_session, phase="stabilize"
    )
    candidate_sha = "abc1234" * 5 + "abcd1234"
    if scratch_value is None:
        monkeypatch.delenv("MIGRATION_APPLY_SCRATCH_DATABASE_URL", raising=False)
    else:
        monkeypatch.setenv("MIGRATION_APPLY_SCRATCH_DATABASE_URL", scratch_value)

    mocker.patch(
        "app.release_qualifier._resolve_github_with_session",
        return_value=("token", "acme", "widget"),
    )
    resolve_engine = mocker.patch("app.release_qualifier.resolve_async_engine")
    apply_gate = mocker.patch(
        "app.release_qualifier.run_migration_apply_gate", new=AsyncMock()
    )

    verdict, detail = await rq._resolve_migration_apply_at_finalize(
        db_session, version, candidate_sha
    )

    assert verdict == expected_verdict
    assert expected_code in (detail or "")
    if expected_verdict == "defer":
        # The defer diagnostic names the env var so operators know what to fix.
        assert "MIGRATION_APPLY_SCRATCH_DATABASE_URL" in (detail or "")
    # The raw scratch value must never leak into any diagnostic.
    assert "secret-value" not in (detail or "")
    # Neither preflight-hold path performs production I/O.
    resolve_engine.assert_not_called()
    apply_gate.assert_not_awaited()


# ===========================================================================
# Retry-on-defer: migration identity and apply gates retry once with a fresh
# GitHub token before propagating the defer (covers token-expiry mid-run).
# ===========================================================================


@pytest.mark.asyncio
async def test_migration_identity_gate_retries_on_defer_and_succeeds(
    db_session, mocker
):
    """When the first check_qualification_migration_identity call raises (401-shaped
    RuntimeError from github.py) the gate re-resolves credentials and retries.
    If the retry succeeds (ok/block), that result is used — not infra_failure."""
    project, version = await _make_project_and_version(db_session, phase="stabilize")
    candidate_sha = "abc1234" * 5 + "abcd1234"

    call_count = 0

    async def _check_side_effect(token, owner, repo, candidate_sha, promo, **kw):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            raise RuntimeError("GitHub API returned HTTP 401")
        return ("ok", None)

    mocker.patch(
        "app.release_qualifier.check_qualification_migration_identity",
        side_effect=_check_side_effect,
    )
    # Fresh-token call on retry should succeed (returning a valid resolved tuple).
    mocker.patch(
        "app.release_qualifier._resolve_github_with_session",
        return_value=("fresh-token", "acme", "widget"),
    )

    verdict, detail = await rq._resolve_migration_identity_at_finalize(
        db_session, version, candidate_sha
    )

    assert verdict == "ok"
    assert detail is None
    assert call_count == 2, "check must be called twice (first attempt + retry)"


@pytest.mark.asyncio
async def test_migration_identity_gate_retries_on_defer_both_fail(db_session, mocker):
    """When both the first and the retry call return defer, the final verdict is
    defer — no infinite retry, fails closed."""
    project, version = await _make_project_and_version(db_session, phase="stabilize")
    candidate_sha = "abc1234" * 5 + "abcd1234"

    call_count = 0

    async def _check_side_effect(token, owner, repo, candidate_sha, promo, **kw):
        nonlocal call_count
        call_count += 1
        return ("defer", f"transient-error-attempt-{call_count}")

    mocker.patch(
        "app.release_qualifier.check_qualification_migration_identity",
        side_effect=_check_side_effect,
    )
    mocker.patch(
        "app.release_qualifier._resolve_github_with_session",
        return_value=("fresh-token", "acme", "widget"),
    )

    verdict, detail = await rq._resolve_migration_identity_at_finalize(
        db_session, version, candidate_sha
    )

    assert verdict == "defer"
    assert call_count == 2, "must not retry more than once"


@pytest.mark.asyncio
async def test_migration_apply_gate_retries_on_defer_and_succeeds(db_session, mocker):
    """When run_migration_apply_gate returns defer on the first attempt, the gate
    re-resolves credentials and retries. If the retry succeeds (ok), that result
    is used instead of infra_failure."""
    project, version = await _make_project_and_version(db_session, phase="stabilize")
    candidate_sha = "abc1234" * 5 + "abcd1234"

    call_count = 0

    async def _apply_side_effect(**kw):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return ("defer", "could not read production's recorded state")
        return ("ok", None)

    mocker.patch(
        "app.release_qualifier.run_migration_apply_gate",
        side_effect=_apply_side_effect,
    )
    mocker.patch(
        "app.release_qualifier._resolve_github_with_session",
        return_value=("fresh-token", "acme", "widget"),
    )
    mocker.patch(
        "app.release_qualifier.resolve_async_engine",
        return_value=object(),
    )

    verdict, detail = await rq._resolve_migration_apply_at_finalize(
        db_session, version, candidate_sha
    )

    assert verdict == "ok"
    assert detail is None
    assert call_count == 2, "apply gate must be called twice (first attempt + retry)"


@pytest.mark.asyncio
async def test_migration_apply_timeout_defer_is_not_blindly_retried(
    db_session, mocker
):
    """Fresh credentials cannot repair a timed-out local gate stage."""

    _project, version = await _make_project_and_version(
        db_session, phase="stabilize"
    )
    candidate_sha = "abc1234" * 5 + "abcd1234"
    gate = mocker.patch(
        "app.release_qualifier.run_migration_apply_gate",
        return_value=(
            "defer",
            "migration_apply_replay_apply_timeout: exceeded 180s wall-clock budget",
        ),
    )
    resolve = mocker.patch(
        "app.release_qualifier._resolve_github_with_session",
        return_value=("token", "acme", "widget"),
    )
    mocker.patch(
        "app.release_qualifier.resolve_async_engine",
        return_value=object(),
    )

    verdict, detail = await rq._resolve_migration_apply_at_finalize(
        db_session, version, candidate_sha
    )

    assert verdict == "defer"
    assert detail.startswith("migration_apply_replay_apply_timeout:")
    assert gate.await_count == 1
    assert resolve.await_count == 1
    assert rq._migration_apply_infra_error(detail).startswith(
        "migration_apply_replay_apply_timeout:"
    )

@pytest.mark.asyncio
async def test_migration_apply_gate_retries_on_defer_both_fail(db_session, mocker):
    """When both the first and the retry call to run_migration_apply_gate return
    defer, the final verdict is defer — fails closed, no infinite retry."""
    project, version = await _make_project_and_version(db_session, phase="stabilize")
    candidate_sha = "abc1234" * 5 + "abcd1234"

    call_count = 0

    async def _apply_side_effect(**kw):
        nonlocal call_count
        call_count += 1
        return ("defer", f"persistent-error-attempt-{call_count}")

    mocker.patch(
        "app.release_qualifier.run_migration_apply_gate",
        side_effect=_apply_side_effect,
    )
    mocker.patch(
        "app.release_qualifier._resolve_github_with_session",
        return_value=("fresh-token", "acme", "widget"),
    )
    mocker.patch(
        "app.release_qualifier.resolve_async_engine",
        return_value=object(),
    )

    verdict, detail = await rq._resolve_migration_apply_at_finalize(
        db_session, version, candidate_sha
    )

    assert verdict == "defer"
    assert call_count == 2, "must not retry more than once"


@pytest.mark.asyncio
async def test_apply_gate_defer_with_duplicate_revision_blocks_instead_of_infra_retry():
    """Structural defect detection: when the candidate's migration chain has a
    duplicate revision id, the apply gate returns 'block' (a hard deterministic
    finding) even when production state is unavailable (the scenario that would
    normally produce a 'defer' / infra-retry).

    This exercises the structural pre-check added to run_migration_apply_gate:
    sources are resolved and checked for dup revisions BEFORE the prod-state DB
    read, so a structurally-defective candidate is surfaced as a real blocking
    finding instead of being auto-retried as migration_apply_check_unavailable."""
    from sqlalchemy.ext.asyncio import create_async_engine

    from app.migration_apply_gate import run_migration_apply_gate

    # Two migration files share the same revision id — a deterministic defect.
    dup_sources = {
        "0001_init.py": (
            "revision = 'dup01'\ndown_revision = None\ndef upgrade(): pass\n"
        ),
        "0001_also_init.py": (
            "revision = 'dup01'\ndown_revision = None\ndef upgrade(): pass\n"
        ),
        "0002_next.py": (
            "revision = 'next01'\ndown_revision = 'dup01'\ndef upgrade(): pass\n"
        ),
    }

    # A postgres-URL engine pointing at a non-existent host: passes the postgres
    # URL check but would fail (defer) at the prod-state read step. The structural
    # check runs before that step, so the result must be 'block', not 'defer'.
    engine = create_async_engine("postgresql+asyncpg://u:p@127.0.0.1:1/nope")
    try:
        verdict, detail = await run_migration_apply_gate(
            prod_engine=engine,
            candidate_sources=dup_sources,
            scratch_db_url="postgresql+asyncpg://scratch:p@localhost/control",
        )
    finally:
        await engine.dispose()

    assert verdict == "block", (
        f"expected block on dup revision, got {verdict!r}: {detail}"
    )
    assert "dup01" in (detail or ""), f"detail should name the dup revision: {detail!r}"


@pytest.mark.asyncio
async def test_apply_gate_defer_with_broken_chain_blocks_instead_of_infra_retry():
    """Structural defect detection: when the candidate's migration chain has a
    broken down_revision pointer (a revision referencing a parent not in the
    chain) or a cyclic chain (zero structural heads), the apply gate returns
    'block' (a hard deterministic finding) even when production state is
    unavailable (the scenario that would normally produce a 'defer'/infra-retry).

    This exercises the structural pre-checks added to run_migration_apply_gate:
    sources are checked for chain integrity BEFORE the prod-state DB read, so
    structurally-defective candidates surface as real blocking findings instead
    of being auto-retried as migration_apply_check_unavailable."""
    from sqlalchemy.ext.asyncio import create_async_engine

    from app.migration_apply_gate import run_migration_apply_gate

    # Case 1: Broken down_revision — the root migration references a parent
    # that is not in the chain. The chain appears to have a single head ('0002'),
    # so the existing multi-head check does NOT catch this; the broken-pointer
    # check must fire.
    broken_sources = {
        "0001_init.py": (
            "revision = '0001'\ndown_revision = 'missing_parent'\ndef upgrade(): pass\n"
        ),
        "0002_next.py": (
            "revision = '0002'\ndown_revision = '0001'\ndef upgrade(): pass\n"
        ),
    }

    # A postgres-URL engine pointing at a non-existent host: passes the postgres
    # URL check but would fail (defer) at the prod-state read step. The structural
    # check runs before that step, so the result must be 'block', not 'defer'.
    engine = create_async_engine("postgresql+asyncpg://u:p@127.0.0.1:1/nope")
    try:
        verdict, detail = await run_migration_apply_gate(
            prod_engine=engine,
            candidate_sources=broken_sources,
            scratch_db_url="postgresql+asyncpg://scratch:p@localhost/control",
        )
    finally:
        await engine.dispose()

    assert verdict == "block", (
        f"expected block on broken down_revision, got {verdict!r}: {detail}"
    )
    assert "0001" in (detail or "") or "missing_parent" in (detail or ""), (
        f"detail should name the broken revision or missing parent: {detail!r}"
    )
    assert "broken" in (detail or "").lower() or "pointer" in (detail or "").lower(), (
        f"detail should describe the defect class: {detail!r}"
    )

    # Case 2: Cyclic chain — every revision points to another in the chain, so
    # there are zero structural heads. The existing multi-head check (>1) does
    # NOT catch the zero-head case; the cycle check must fire.
    cyclic_sources = {
        "0001_a.py": (
            "revision = '0001'\ndown_revision = '0002'\ndef upgrade(): pass\n"
        ),
        "0002_b.py": (
            "revision = '0002'\ndown_revision = '0001'\ndef upgrade(): pass\n"
        ),
    }

    engine2 = create_async_engine("postgresql+asyncpg://u:p@127.0.0.1:1/nope")
    try:
        verdict2, detail2 = await run_migration_apply_gate(
            prod_engine=engine2,
            candidate_sources=cyclic_sources,
            scratch_db_url="postgresql+asyncpg://scratch:p@localhost/control",
        )
    finally:
        await engine2.dispose()

    assert verdict2 == "block", (
        f"expected block on cyclic chain, got {verdict2!r}: {detail2}"
    )
    assert "cycle" in (detail2 or "").lower() or "zero" in (detail2 or "").lower(), (
        f"detail should mention cycle or zero heads: {detail2!r}"
    )


@pytest.mark.asyncio
async def test_finalize_holds_when_deductible_finding_lacks_challenge_confirmation(
    db_session, mocker
):
    """Fail-closed audit challenge gate: when a verified higher-severity finding
    has no challenge confirmation the qualification attempt is HELD as an infra
    failure so the version returns to stabilize for re-evaluation.  Neither a
    false promote nor a false block is recorded.

    This is the named acceptance criterion for the bind-audit-scoring task.
    """
    from app import shared_state

    shared_state.runtime_config["promotion_enabled"] = True

    project, version = await _make_project_and_version(db_session)
    # Audit reports a verified HIGH finding (verification_note ≥ 20 chars so it
    # passes _finding_is_verified and would normally deduct 0.15).
    _audit_task, _smoke = await _seed_qualification_children(
        db_session,
        project,
        version,
        audit_logs={
            "audit_score": 0.85,
            "findings": [
                {
                    "severity": "high",
                    "summary": "SQL injection risk in user input path.",
                    "file": "app/routes.py",
                    "verification_note": "Confirmed via full route inspection — no sanitisation.",
                },
            ],
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
    # Patch candidate_sha to the known value so challenge SHA-matching works.
    version.last_qualification = {
        **(version.last_qualification or {}),
        "candidate_sha": _CANDIDATE_SHA,
    }
    db_session.add(version)
    await db_session.commit()
    await db_session.refresh(version)

    # No challenge task / artifact in last_qualification → _reconcile returns hold.
    promote_spy = mocker.patch("app.release_qualifier.promote", new=AsyncMock())

    result = await rq._finalize_qualification(db_session, version)
    await db_session.commit()

    # Must be held as infra_failure — not promoted, not recorded as a quality verdict.
    assert result["status"] == "infra_failure"
    assert result["error"] in {
        "audit_challenge_missing",
        "audit_challenge_sha_mismatch",
    }
    promote_spy.assert_not_called()

    await db_session.refresh(version)
    assert version.status == "open"
    assert version.phase == "stabilize"
    lq = version.last_qualification
    assert lq["status"] == "infra_failure"


@pytest.mark.asyncio
async def test_finalize_confirmed_finding_still_deducts_with_challenge(
    db_session, mocker
):
    """A challenger-confirmed finding deducts from the audit sub-score as before.
    A challenge verdict of 'confirmed' is not a false positive — it must still
    count against the audit score."""
    from app import shared_state

    shared_state.runtime_config["promotion_enabled"] = False  # dry-run

    project, version = await _make_project_and_version(db_session)
    audit_task, _smoke = await _seed_qualification_children(
        db_session,
        project,
        version,
        audit_logs={
            "audit_score": 0.85,
            "findings": [
                {
                    "severity": "high",
                    "summary": "SQL injection risk in user input path.",
                    "file": "app/routes.py",
                    "verification_note": "Confirmed via full route inspection — no sanitisation.",
                },
            ],
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
    await _seed_challenge_artifact(
        db_session,
        project,
        version,
        audit_task=audit_task,
        verdicts=[
            {
                "severity": "high",
                "summary": "SQL injection risk in user input path.",
                "file": "app/routes.py",
                "verdict": "confirmed",
                "reason": "Independent review confirms the injection vector is real.",
            }
        ],
    )

    mocker.patch("app.release_qualifier.promote", new=AsyncMock())

    result = await rq._finalize_qualification(db_session, version)
    await db_session.commit()

    # Should score and proceed (not hold) — dry_run because promotion is disabled.
    assert result["status"] == "dry_run"
    await db_session.refresh(version)
    lq = version.last_qualification
    # Confirmed finding deducted 0.15 → audit_score ≈ 0.85 (1.0 - 0.15).
    assert lq["sub_scores"]["audit"] == pytest.approx(0.85, abs=0.01)
    # challenger_refuted_audit_findings must be empty — this finding was confirmed.
    assert lq.get("challenger_refuted_audit_findings") == []


@pytest.mark.asyncio
async def test_finalize_refuted_finding_excluded_and_recorded(db_session, mocker):
    """A challenger-confirmed false positive (refuted_false_positive) is excluded
    from the audit sub-score and recorded in challenger_refuted_audit_findings with
    the auditable reason — distinct from excluded_audit_findings."""
    from app import shared_state

    shared_state.runtime_config["promotion_enabled"] = False  # dry-run

    project, version = await _make_project_and_version(db_session)
    audit_task, _smoke = await _seed_qualification_children(
        db_session,
        project,
        version,
        audit_logs={
            "audit_score": 0.85,
            "findings": [
                {
                    "severity": "high",
                    "summary": "False alarm: path is already sanitised.",
                    "file": "app/routes.py",
                    "verification_note": "Reviewer noted sanitization is missing in diff.",
                },
            ],
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
    await _seed_challenge_artifact(
        db_session,
        project,
        version,
        audit_task=audit_task,
        verdicts=[
            {
                "severity": "high",
                "summary": "False alarm: path is already sanitised.",
                "file": "app/routes.py",
                "verdict": "refuted_false_positive",
                "reason": "The sanitizer middleware covers this path — the auditor missed it.",
            }
        ],
    )

    mocker.patch("app.release_qualifier.promote", new=AsyncMock())

    result = await rq._finalize_qualification(db_session, version)
    await db_session.commit()

    # Refuted finding must not reduce the audit score → clean 1.0 → dry_run passes.
    assert result["status"] == "dry_run"
    await db_session.refresh(version)
    lq = version.last_qualification
    # No deduction applied (finding refuted) → audit score is 1.0.
    assert lq["sub_scores"]["audit"] == pytest.approx(1.0, abs=0.01)
    # Refuted finding recorded in challenger_refuted_audit_findings.
    refuted = lq.get("challenger_refuted_audit_findings", [])
    assert len(refuted) == 1
    assert refuted[0]["severity"] == "high"
    assert (
        "sanitiser" in refuted[0]["challenger_reason"].lower()
        or "sanitizer" in refuted[0]["challenger_reason"].lower()
    )
    # excluded_audit_findings must NOT contain this finding (different concept).
    excluded = lq.get("excluded_audit_findings", [])
    assert not any("False alarm" in f.get("summary", "") for f in excluded)


@pytest.mark.asyncio
async def test_reconciliation_counts_challenge_exclusion_separately_from_verification_gate(
    db_session, mocker
):
    """The audit_reconciliation record uses TWO distinct exclusion counts.

    - excluded_findings_count  — findings the VERIFICATION GATE dropped (unverified
      primary findings that lack a substantive verification_note).
    - challenge_excluded_findings_count — findings the CHALLENGE GATE excluded
      (challenger marked refuted_false_positive).

    A run with one unverified-primary drop AND one challenger-refuted finding must
    show each in its own count; neither may appear in the other.  Furthermore,
    findings_derived_score in the reconciliation record must agree with the applied
    audit sub-score (no contradiction visible to an operator reading the record).
    """
    from app import shared_state

    shared_state.runtime_config["promotion_enabled"] = False  # dry-run

    project, version = await _make_project_and_version(db_session)
    audit_task, _smoke = await _seed_qualification_children(
        db_session,
        project,
        version,
        audit_logs={
            "audit_score": 0.70,
            "findings": [
                {
                    # VERIFIED high finding — challenger will refute this one.
                    "severity": "high",
                    "summary": "XSS vulnerability in template rendering.",
                    "file": "app/templates.py",
                    "verification_note": "Confirmed: user input reaches template without escaping.",
                },
                {
                    # UNVERIFIED high finding — verification gate drops this one.
                    "severity": "high",
                    "summary": "Potential SQL injection in query builder.",
                    # No verification_note → fails _finding_is_verified → excluded_findings_count
                },
                {
                    # VERIFIED medium the challenger does NOT refute. It survives
                    # both gates, so the RECONCILED report still carries a verified
                    # higher-severity finding and stays scoreable. Without it, the
                    # only higher-sev finding left after the challenge removes the
                    # XSS would be the unverified SQLi → _score_audit fail-closes to
                    # None (``all_findings_unverified``) → ``audit_missing`` hard
                    # blocker → not_promoted, which is a DIFFERENT contract (pinned
                    # in TestAuditMissingReason) and would mask the two-exclusion-
                    # count behaviour this test is about.
                    "severity": "medium",
                    "summary": "Missing rate limit on the login endpoint.",
                    "file": "app/api.py",
                    "verification_note": "Read app/api.py lines 40-70: the login route has no rate-limit decorator.",
                },
            ],
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
    # Challenge refutes the verified finding (the unverified one is invisible to
    # the challenge gate since it never passed the verification gate).
    await _seed_challenge_artifact(
        db_session,
        project,
        version,
        audit_task=audit_task,
        verdicts=[
            {
                "severity": "high",
                "summary": "XSS vulnerability in template rendering.",
                "file": "app/templates.py",
                "verdict": "refuted_false_positive",
                "reason": "Template output is auto-escaped by the framework — not a real XSS.",
            },
            {
                # The challenge binds fail-closed: EVERY deductible finding (one
                # that passed the verification gate) must carry exactly one
                # verdict, else reconciliation holds with
                # ``audit_challenge_binding_incomplete`` and respawns the
                # challenge. The verified medium above is deductible, so it needs
                # its own verdict. ``confirmed`` (not refuted) keeps it in the
                # reconciled report, so it still deducts — which is what keeps the
                # report scoreable and pins the audit sub-score below.
                "severity": "medium",
                "summary": "Missing rate limit on the login endpoint.",
                "file": "app/api.py",
                "verdict": "confirmed",
                "reason": "Read app/api.py lines 40-70: the login route genuinely has no rate-limit decorator.",
            },
        ],
    )

    mocker.patch("app.release_qualifier.promote", new=AsyncMock())

    result = await rq._finalize_qualification(db_session, version)
    await db_session.commit()

    assert result["status"] == "dry_run", (
        f"expected dry_run, got {result['status']!r} "
        f"(blocker={result.get('blocker')!r}, confidence={result.get('confidence')!r})"
    )
    await db_session.refresh(version)
    lq = version.last_qualification

    recon = lq["audit_reconciliation"]

    # 1. The verified+refuted finding is excluded via challenge gate (not verification gate).
    assert recon["challenge_excluded_findings_count"] == 1

    # 2. The unverified finding is excluded via verification gate (not challenge gate).
    assert recon["excluded_findings_count"] == 1

    # 3. The two counts represent DIFFERENT findings — they must never overlap.
    #    (Both equal 1, so their sum is 2, covering both findings separately.)
    assert (
        recon["challenge_excluded_findings_count"] + recon["excluded_findings_count"]
        == 2
    )

    # 4. findings_derived_score agrees with the applied audit sub-score.
    #    After the challenge removes the refuted XSS high, the reconciled report
    #    holds the unverified SQLi high (skipped by the verification gate — no
    #    deduction) and the verified medium (deducted). Both the applied score and
    #    the reconciliation record are computed on that same reconciled report, so
    #    an operator reading the record sees no contradiction.
    applied_audit = lq["sub_scores"]["audit"]
    assert recon["findings_derived_score"] == pytest.approx(applied_audit, abs=0.01), (
        f"findings_derived_score {recon['findings_derived_score']} disagrees with "
        f"applied audit sub-score {applied_audit}"
    )
    # The refuted high no longer deducts; only the surviving verified medium does.
    assert applied_audit == pytest.approx(
        1.0 - rq._AUDIT_DEDUCTIONS["medium"], abs=0.01
    )

    # 5. Binding record is persisted alongside audit_reconciliation and contains
    #    the refuted entry with verdict + summary fields.
    #    The challenge binds fail-closed: EVERY deductible finding carries exactly
    #    one verdict, so both the refuted XSS high and the confirmed medium appear.
    #    (The unverified SQLi high is not deductible — it never reached the
    #    challenge — which is precisely the two-exclusion split this test pins.)
    binding = recon.get("challenge_binding_record")
    assert isinstance(binding, list), f"expected list, got {type(binding)}"
    assert len(binding) == 2, f"expected 2 binding entries, got {len(binding)}"
    refuted_entries = [b for b in binding if b["verdict"] == "refuted_false_positive"]
    assert len(refuted_entries) == 1, "expected exactly one refuted binding entry"
    assert "XSS vulnerability" in refuted_entries[0]["finding_summary"]
    confirmed_entries = [b for b in binding if b["verdict"] == "confirmed"]
    assert len(confirmed_entries) == 1, "expected exactly one confirmed binding entry"
    assert "rate limit" in confirmed_entries[0]["finding_summary"]


@pytest.mark.asyncio
async def test_finalize_challenge_matches_nonverbatim_challenger_summary(
    db_session, mocker
):
    """Fuzzy token-overlap matching lets a challenger verdict with a differently-worded
    summary (but describing the same finding) bind to the primary audit finding.
    The challenge runs in parallel without primary_findings, so its free-text
    summary will differ from the primary's — exact matching would permanently HOLD."""
    from app import shared_state

    shared_state.runtime_config["promotion_enabled"] = False  # dry-run

    project, version = await _make_project_and_version(db_session)
    audit_task, _smoke = await _seed_qualification_children(
        db_session,
        project,
        version,
        audit_logs={
            "audit_score": 0.85,
            "findings": [
                {
                    "severity": "high",
                    # Primary audit's exact wording
                    "summary": "SQL injection vulnerability in authentication endpoint",
                    "file": "app/auth.py",
                    "verification_note": "User input reaches the query without escaping.",
                },
            ],
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
    await _seed_challenge_artifact(
        db_session,
        project,
        version,
        audit_task=audit_task,
        verdicts=[
            {
                "severity": "high",
                # Challenger independently describes the same finding with different wording
                "summary": "SQL injection flaw in auth endpoint login handler",
                "file": "app/auth.py",
                "verdict": "confirmed",
                "reason": "Independent review confirms unsanitised SQL query in auth path.",
            }
        ],
    )

    mocker.patch("app.release_qualifier.promote", new=AsyncMock())

    result = await rq._finalize_qualification(db_session, version)
    await db_session.commit()

    # Must score normally (not HOLD as audit_challenge_missing) — the fuzzy match
    # binds the challenger's non-verbatim summary to the primary finding.
    assert result["status"] == "dry_run", (
        f"Expected dry_run but got {result['status']} "
        f"(error={result.get('error')!r}) — fuzzy summary matching may have failed"
    )
    await db_session.refresh(version)
    lq = version.last_qualification
    # Confirmed finding deducts 0.15 → audit_score ≈ 0.85.
    assert lq["sub_scores"]["audit"] == pytest.approx(0.85, abs=0.01)
    assert lq.get("challenger_refuted_audit_findings") == []


@pytest.mark.asyncio
async def test_challenger_refuted_finding_excluded_from_audit_score(db_session, mocker):
    """Acceptance criterion: a challenger-refuted HIGH finding does not deduct
    from the audit sub-score and is recorded in challenger_refuted_audit_findings.
    Fail-closed companion: when the challenge is absent, the deduction stands.

    Part 1 — refuted path: audit reports one verified HIGH finding; the
    independent challenge marks it refuted_false_positive.  The audit sub-score
    must be 1.0 (no deduction) and the finding must appear in
    challenger_refuted_audit_findings with its auditable reason.

    Part 2 — fail-closed path: same audit finding, but no challenge artifact
    seeded.  The qualifier must HOLD as infra_failure (audit_challenge_missing)
    so the deduction is not silently erased.
    """
    from app import shared_state

    # ── Part 1: refuted finding → no deduction, recorded as challenger-refuted ──
    shared_state.runtime_config["promotion_enabled"] = False  # dry-run

    project, version = await _make_project_and_version(db_session)
    audit_task, _smoke = await _seed_qualification_children(
        db_session,
        project,
        version,
        audit_logs={
            "audit_score": 0.85,
            "findings": [
                {
                    "severity": "high",
                    "summary": "Unvalidated redirect in OAuth callback handler.",
                    "file": "app/oauth.py",
                    "verification_note": "Reviewed callback handler — no destination validation.",
                },
            ],
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
    await _seed_challenge_artifact(
        db_session,
        project,
        version,
        audit_task=audit_task,
        verdicts=[
            {
                "severity": "high",
                "summary": "Unvalidated redirect in OAuth callback handler.",
                "file": "app/oauth.py",
                "verdict": "refuted_false_positive",
                "reason": "The destination is validated by the allow-list middleware — auditor missed it.",
            }
        ],
    )

    mocker.patch("app.release_qualifier.promote", new=AsyncMock())

    result = await rq._finalize_qualification(db_session, version)
    await db_session.commit()

    assert result["status"] == "dry_run", (
        f"Expected dry_run but got {result['status']} (error={result.get('error')!r})"
    )
    await db_session.refresh(version)
    lq = version.last_qualification
    # Refuted finding must NOT deduct → audit_score is 1.0.
    assert lq["sub_scores"]["audit"] == pytest.approx(1.0, abs=0.01), (
        f"Expected audit score 1.0 but got {lq['sub_scores']['audit']} — "
        "refuted finding should not deduct"
    )
    # Must be recorded in challenger_refuted_audit_findings with auditable reason.
    refuted = lq.get("challenger_refuted_audit_findings", [])
    assert len(refuted) == 1, f"Expected 1 refuted finding, got {refuted!r}"
    assert refuted[0]["severity"] == "high"
    assert (
        "allow-list" in refuted[0]["challenger_reason"].lower()
        or "allow_list" in refuted[0]["challenger_reason"].lower()
        or "middleware" in refuted[0]["challenger_reason"].lower()
    )
    # Must NOT appear in excluded_audit_findings (different exclusion concept).
    excluded = lq.get("excluded_audit_findings", [])
    assert not any(
        "oauth" in f.get("summary", "").lower()
        or "redirect" in f.get("summary", "").lower()
        for f in excluded
    )

    # ── Part 2: absent challenge → fail-closed, deduction preserved ──
    project2, version2 = await _make_project_and_version(db_session)
    await _seed_qualification_children(
        db_session,
        project2,
        version2,
        audit_logs={
            "audit_score": 0.85,
            "findings": [
                {
                    "severity": "high",
                    "summary": "Unvalidated redirect in OAuth callback handler.",
                    "file": "app/oauth.py",
                    "verification_note": "Reviewed callback handler — no destination validation.",
                },
            ],
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
    # No challenge artifact seeded — challenge_task_id absent from last_qualification.
    version2.last_qualification = {
        **(version2.last_qualification or {}),
        "candidate_sha": _CANDIDATE_SHA,
    }
    db_session.add(version2)
    await db_session.commit()
    await db_session.refresh(version2)

    promote_spy = mocker.patch("app.release_qualifier.promote", new=AsyncMock())
    result2 = await rq._finalize_qualification(db_session, version2)
    await db_session.commit()

    # Must hold as infra_failure — no silent erasure of the deduction.
    assert result2["status"] == "infra_failure", (
        f"Expected infra_failure (fail-closed) but got {result2['status']}"
    )
    assert result2.get("error") in {
        "audit_challenge_missing",
        "audit_challenge_sha_mismatch",
    }
    promote_spy.assert_not_called()
    await db_session.refresh(version2)
    assert version2.phase == "stabilize"


@pytest.mark.asyncio
async def test_finalize_confirmed_finding_with_severity_drift_still_deducts(
    db_session, mocker
):
    """A challenger-confirmed finding still deducts when severity labels drift."""
    from app import shared_state

    shared_state.runtime_config["promotion_enabled"] = False  # dry-run

    project, version = await _make_project_and_version(db_session)
    audit_task, _smoke = await _seed_qualification_children(
        db_session,
        project,
        version,
        audit_logs={
            "audit_score": 0.85,
            "findings": [
                {
                    "severity": "high",
                    "summary": "SQL injection vulnerability in authentication endpoint",
                    "file": "app/auth.py",
                    "verification_note": "User input reaches the query without escaping.",
                },
            ],
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
    await _seed_challenge_artifact(
        db_session,
        project,
        version,
        audit_task=audit_task,
        verdicts=[
            {
                "severity": "medium",
                "summary": "SQL injection vulnerability in authentication endpoint",
                "file": "app/auth.py",
                "verdict": "confirmed",
                "reason": "Independent review confirms the same issue even though severity was bucketed lower.",
            }
        ],
    )

    mocker.patch("app.release_qualifier.promote", new=AsyncMock())

    result = await rq._finalize_qualification(db_session, version)
    await db_session.commit()

    assert result["status"] == "dry_run"
    await db_session.refresh(version)
    lq = version.last_qualification
    assert lq["sub_scores"]["audit"] == pytest.approx(0.85, abs=0.01)
    assert lq.get("challenger_refuted_audit_findings") == []


@pytest.mark.asyncio
async def test_finalize_apply_gate_ok_does_not_block_promotion(db_session, mocker):
    """When the apply gate returns 'ok' (the autouse default) qualification
    proceeds normally — the new dimension is inert on a clean candidate and the
    verdict records migration_apply_safety='ok'. This is the complement of the
    block/defer cases above: an 'ok' apply gate never perturbs the promotion
    path."""
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

    # Leave the autouse ("ok", None) apply-gate default in place. The candidate
    # is otherwise pristine, so it clears every dimension and reaches promote();
    # the apply gate never blocks it.
    promote_spy = mocker.patch(
        "app.release_qualifier.promote",
        new=AsyncMock(return_value={"status": "promoted"}),
    )

    await rq._finalize_qualification(db_session, version)
    await db_session.commit()

    # Reaching promote() at all proves the apply gate did not block; the verdict
    # record threaded into promote carries the clean apply-safety fields.
    promote_spy.assert_called_once()
    base_record = promote_spy.call_args.kwargs["base_record"]
    assert base_record["migration_apply_safety"] == "ok"
    assert base_record["migration_apply_safety_detail"] is None
    assert base_record["blocker"] is None


@pytest.mark.asyncio
async def test_finalize_threads_snapshotted_candidate_sha_into_apply_gate(
    db_session, mocker
):
    """The finalize path must drive migration-apply from last_qualification."""
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

    candidate_sha = "cafe" * 10
    version.last_qualification = {
        **(version.last_qualification or {}),
        "candidate_sha": candidate_sha,
    }
    db_session.add(version)
    await db_session.commit()
    await db_session.refresh(version)

    observed = {}

    async def _fake_apply_gate(**kwargs):
        observed["candidate_ref"] = kwargs["candidate_ref"]
        return ("ok", None)

    mocker.patch(
        "app.release_qualifier.run_migration_apply_gate",
        new=AsyncMock(side_effect=_fake_apply_gate),
    )
    promote_spy = mocker.patch(
        "app.release_qualifier.promote",
        new=AsyncMock(return_value={"status": "promoted"}),
    )

    await rq._finalize_qualification(db_session, version)
    await db_session.commit()

    assert observed["candidate_ref"] == candidate_sha
    promote_spy.assert_called_once()


@pytest.mark.asyncio
async def test_finalize_carries_audit_findings_into_last_qualification(
    db_session, mocker
):
    """A below-threshold qualification stores the audit findings on the version
    so the strategic agent's payload (via to_phase_block) can target them —
    not just the composite score."""
    from app import shared_state

    shared_state.runtime_config["promotion_enabled"] = True

    project, version = await _make_project_and_version(db_session)
    await _seed_qualification_children(
        db_session,
        project,
        version,
        # Three findings derive audit 0.68; with erosion 1.0 and smoketest 0.5
        # (half-passing), the composite lands at 0.722, under the 0.90
        # bar and on the soft-miss path.
        audit_logs={
            "audit_score": 0.35,
            "test_erosion": {
                "deleted_test_files": [],
                "assertion_delta": 840,
                "test_count_delta": 373,
            },
            "findings": [
                {"severity": "low", "summary": "Private cross-module import."},
                {
                    "severity": "high",
                    "summary": "Token baked into VM image.",
                    "verification_note": "Read infra/packer/agent.pkr.hcl lines 20-35: API token hardcoded in provisioner shell command, present in diff with no mitigation.",
                },
                {
                    "severity": "high",
                    "summary": "Unsafe dependency pinning.",
                    "verification_note": "Read agent/package.json: dependency version unpinned (uses ^), confirmed change in diff allows semver drift.",
                },
            ],
        },
        smoke_logs={
            "passed": True,
            "tests": [
                {"name": "a", "status": "pass"},
                {"name": "b", "status": "fail"},
            ],
            "suites": {"startup": {"passed": True}},
        },
        challenge_verdicts=[
            {
                "severity": "high",
                "summary": "Token baked into VM image.",
                "file": "",
                "verdict": "confirmed",
                "reason": "Independently confirmed: API token is hardcoded in the provisioner command.",
            },
            {
                "severity": "high",
                "summary": "Unsafe dependency pinning.",
                "file": "",
                "verdict": "confirmed",
                "reason": "Independently confirmed: semver range operator allows drift.",
            },
        ],
    )

    result = await rq._finalize_qualification(db_session, version)
    await db_session.commit()

    assert result["status"] == "not_promoted"
    assert result["blocker"] is None

    await db_session.refresh(version)
    findings = version.last_qualification["audit_findings"]
    # Most-severe first, all three findings carried with severity + summary +
    # a deterministic content key (no raw file on these → file omitted).
    assert findings == [
        {
            "severity": "high",
            "summary": "Token baked into VM image.",
            "key": rq._audit_finding_key("Token baked into VM image.", None),
        },
        {
            "severity": "high",
            "summary": "Unsafe dependency pinning.",
            "key": rq._audit_finding_key("Unsafe dependency pinning.", None),
        },
        {
            "severity": "low",
            "summary": "Private cross-module import.",
            "key": rq._audit_finding_key("Private cross-module import.", None),
        },
    ]
    # Pre-cap completeness meta agrees with the carried (capped) list.
    assert version.last_qualification["audit_findings_total"] == len(findings)
    assert version.last_qualification["audit_findings_truncated"] is False
    # Test-erosion detail carries the lossless deltas (positive included) so
    # a later scoped-audit merge can reconstruct cumulative erosion correctly.
    assert "test_erosion_detail" in version.last_qualification
    assert version.last_qualification["test_erosion_detail"] == {
        "test_count_delta": 373, "assertion_delta": 840,
    }
    # Old smoke artifacts remain readable input, but new qualification verdicts
    # do not preserve or act on retired qualifier-smoke detail.
    assert "smoketest_detail" not in version.last_qualification


@pytest.mark.asyncio
async def test_finalize_promotes_when_enabled_and_confident(db_session, mocker):
    """With promotion_enabled=True and a confident candidate, finalize calls
    promote(), which opens + merges the PR and advances the version."""
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
                "assertion_delta": 10,
                "test_count_delta": 4,
            },
        },
        smoke_logs={
            "passed": True,
            "tests": [{"name": "a", "status": "pass"}],
            "suites": {"startup": {"passed": True}},
        },
    )

    mocker.patch(
        "app.github.get_repo_installation_token",
        new=AsyncMock(return_value="gh-token"),
    )
    # candidate_sha recorded by _seed_qualification_children is "feed" * 10;
    # promote() pins the PR to it (no staleness guard) and the post-merge main
    # HEAD is the new merge commit.
    merge_sha = "merged0" * 5 + "00000"
    mocker.patch(
        "app.github.get_branch_sha",
        new=_branch_sha_mock(main=merge_sha),
    )
    _patch_promote_github_defaults(mocker)
    branch_mock = mocker.patch(
        "app.github.create_branch",
        new=_create_branch_mock(),
    )
    create_mock = mocker.patch(
        "app.github.create_pull_request",
        new=AsyncMock(return_value={"number": 77, "html_url": "http://pr/77"}),
    )
    merge_mock = mocker.patch(
        "app.github.merge_pull_request",
        new=AsyncMock(
            return_value={"merged": True, "message": "Merged", "sha": merge_sha}
        ),
    )
    lock_mock = mocker.patch(
        "app.task_locking.acquire_merge_submission_locks",
        new=AsyncMock(return_value=None),
    )
    from app import github as github_mod

    calls = mocker.Mock()
    calls.attach_mock(github_mod.get_pull_request, "get_pr")
    calls.attach_mock(lock_mock, "lock")
    calls.attach_mock(merge_mock, "merge")

    result = await rq._finalize_qualification(db_session, version)
    await db_session.commit()

    assert result["status"] == "promoted"
    # The recorded promoted_sha is the REAL merge commit, not the candidate.
    assert result["promoted_sha"] == merge_sha
    assert result["next_version_number"] == 2

    # merge_pull_request must use merge_method="merge" (preserve history).
    assert merge_mock.call_args.kwargs["merge_method"] == "merge"
    assert merge_mock.call_args.kwargs["expected_head_sha"] == "feed" * 10
    lock_mock.assert_awaited_once_with(
        db_session,
        project_id=project.id,
        repo_identity="acme/widget",
        base_branch="main",
        pr_number=77,
    )
    assert [call[0] for call in calls.mock_calls][-3:] == ["lock", "get_pr", "merge"]

    # Audit C4: a release branch was created at the EXACT candidate SHA, and
    # the PR is opened from that branch (not live automain) into main.
    branch_mock.assert_called_once()
    assert branch_mock.call_args.args[3] == "release-v1"  # branch name
    assert branch_mock.call_args.args[4] == "feed" * 10  # pinned to candidate
    assert create_mock.call_args.kwargs["head"] == "release-v1"
    assert create_mock.call_args.kwargs["base"] == "main"

    # Version v1 is promoted with the merge SHA stamped.
    await db_session.refresh(version)
    assert version.status == "promoted"
    assert version.promoted_sha == merge_sha
    assert version.promoted_at is not None

    # A fresh open v2 was created in the build phase, based on the merge SHA.
    next_version = await rv.current_open_version(db_session, project.id)
    assert next_version is not None
    assert next_version.version_number == 2
    assert next_version.phase == "build"
    assert next_version.base_sha == merge_sha


# ===========================================================================
# promote — staleness guard, real-merge-SHA, failure paths
# ===========================================================================


@pytest.mark.asyncio
async def test_promote_pat_credential_refusal_is_a_recorded_failure_not_ci_wait(
    db_session, mocker
):
    """A PAT refusal is permanent — it must never be reported as pending CI.

    ``merge_pull_request`` refuses before any PUT when the only resolvable
    credential is the operator's own PAT (this lane's resolver really can fall
    back to ``get_user_secret``).  The refusal shares ``merged=False`` /
    ``conflict=False`` with a pending-checks 405, so without an explicit branch
    the version parks in the managed check waiter with
    ``reason="required_checks_pending"``: the reconciler re-submits and is
    refused identically forever, no promotion failure is ever recorded, and the
    operator-facing reason is a lie about CI.
    """
    project, version = await _make_project_and_version(db_session)

    mocker.patch(
        "app.github.get_repo_installation_token",
        new=AsyncMock(return_value="gh-token"),
    )
    mocker.patch("app.github.get_branch_sha", new=_branch_sha_mock())
    _patch_promote_github_defaults(mocker)
    mocker.patch("app.github.create_branch", new=_create_branch_mock())
    mocker.patch(
        "app.github.create_pull_request",
        new=AsyncMock(return_value={"number": 93, "html_url": "http://pr/93"}),
    )
    mocker.patch(
        "app.github.merge_pull_request",
        new=AsyncMock(
            return_value={
                "merged": False,
                "conflict": False,
                "pat_fallback_blocked": True,
                "message": "refusing to merge with an operator PAT credential",
            }
        ),
    )
    wait_mock = mocker.patch(
        "app.release_promotion._enter_managed_check_wait",
        new=AsyncMock(return_value={"status": "managed_check_wait"}),
    )

    result = await rq.promote(
        db_session,
        version,
        "abcd" * 10,
        composite={"confidence": 0.9},
        base_record={"candidate_sha": "abcd" * 10},
    )
    await db_session.commit()

    wait_mock.assert_not_awaited()
    assert result["status"] == "promotion_failed"
    assert result["error"] == "merge_credential_blocked"

    await db_session.refresh(version)
    assert version.status == "open"
    assert version.phase == "stabilize"
    assert version.last_qualification["status"] == "promotion_failed"
    assert version.last_qualification["error"] == "merge_credential_blocked"
    detail = version.last_qualification.get("detail") or ""
    assert "personal access token" in detail
    assert "GitHub App" in detail


@pytest.mark.asyncio
async def test_promote_non_merged_result_is_failed_promotion(db_session, mocker):
    """merge_pull_request returning {merged: False} (405/409) is a FAILED
    promotion — the version stays open and goes back to stabilize, and the
    next version is NOT opened."""
    project, version = await _make_project_and_version(db_session)

    mocker.patch(
        "app.github.get_repo_installation_token",
        new=AsyncMock(return_value="gh-token"),
    )
    mocker.patch(
        "app.github.get_branch_sha",
        new=_branch_sha_mock(),
    )
    # is_ancestor_of=False on BOTH the pre-check and the post-merge re-check:
    # the candidate is genuinely NOT on main, so a 409-conflict non-merge is a
    # real failure (not an idempotent already-merged retry).
    _patch_promote_github_defaults(mocker)
    mocker.patch(
        "app.github.create_branch",
        new=_create_branch_mock(),
    )
    mocker.patch(
        "app.github.create_pull_request",
        new=AsyncMock(return_value={"number": 91, "html_url": "http://pr/91"}),
    )
    mocker.patch(
        "app.github.merge_pull_request",
        new=AsyncMock(
            return_value={
                "merged": False,
                "conflict": True,
                "message": "merge conflict",
            }
        ),
    )

    result = await rq.promote(
        db_session,
        version,
        "abcd" * 10,
        composite={"confidence": 0.9},
        base_record={"candidate_sha": "abcd" * 10},
    )
    await db_session.commit()

    assert result["status"] == "promotion_failed"
    assert result["error"] == "merge_blocked"

    await db_session.refresh(version)
    # Version stays open — a failed promotion only ever blocks.
    assert version.status == "open"
    assert version.phase == "stabilize"
    assert version.last_qualification["status"] == "promotion_failed"
    assert version.last_qualification["error"] == "merge_blocked"
    assert version.last_qualification["conflict"] is True

    # No next version was opened.
    versions = await rv.current_open_version(db_session, project.id)
    assert versions is not None
    assert versions.version_number == 1


@pytest.mark.asyncio
async def test_promote_success_opens_next_version(db_session, mocker):
    """A successful merge marks v1 promoted and opens v2 (build phase).  The PR
    is opened from the release-v{N} branch (pinned to the candidate) into
    main (audit C4)."""
    _project, version = await _make_project_and_version(db_session)

    mocker.patch(
        "app.github.get_repo_installation_token",
        new=AsyncMock(return_value="gh-token"),
    )
    merge_sha = "mainnew" * 5 + "00000"
    mocker.patch(
        "app.github.get_branch_sha",
        new=_branch_sha_mock(main=merge_sha),
    )
    _patch_promote_github_defaults(mocker)
    branch_mock = mocker.patch(
        "app.github.create_branch",
        new=_create_branch_mock(),
    )
    create_mock = mocker.patch(
        "app.github.create_pull_request",
        new=AsyncMock(return_value={"number": 5, "html_url": "http://pr/5"}),
    )
    mocker.patch(
        "app.github.merge_pull_request",
        new=AsyncMock(
            return_value={"merged": True, "message": "Merged", "sha": merge_sha}
        ),
    )

    result = await rq.promote(
        db_session,
        version,
        "1234" * 10,
        composite={"confidence": 0.88},
    )
    await db_session.commit()

    assert result["status"] == "promoted"
    assert result["next_version_number"] == 2

    # Audit C4: a release-v1 branch was created at the candidate SHA, and the
    # PR is opened release-v1 → main (NOT live automain → main).
    branch_mock.assert_called_once()
    assert branch_mock.call_args.args[3] == "release-v1"
    assert branch_mock.call_args.args[4] == "1234" * 10
    assert create_mock.call_args.kwargs["head"] == "release-v1"
    assert create_mock.call_args.kwargs["base"] == "main"

    await db_session.refresh(version)
    assert version.status == "promoted"
    # promoted_sha is GitHub's exact merge receipt, not the candidate or a
    # later moving target tip.
    assert version.promoted_sha == merge_sha


@pytest.mark.asyncio
async def test_force_promote_current_version_skips_audit_and_promotes(
    db_session, mocker
):
    """Operator FORCE-promote SKIPS the audit gate but still cuts the release
    branch, opens the PR, and (CI mocked green) merges — NO audit/qualify children
    are spawned. 'force' skips the AUDIT, not CI. The verdict records the forced
    provenance."""
    project, version = await _make_project_and_version(db_session, phase="stabilize")

    candidate = "facec0de" * 5  # 40-char candidate sha
    merge_sha = "mainnew" * 5 + "00000"
    mocker.patch(
        "app.github.get_repo_installation_token",
        new=AsyncMock(return_value="gh-token"),
    )
    mocker.patch(
        "app.github.get_branch_sha",
        new=_branch_sha_mock(automain=candidate, main=merge_sha),
    )
    mocker.patch(
        "app.github.list_repo_tree_paths_at_ref",
        new=AsyncMock(
            return_value=[{"path": "backend/app/main.py", "size": 10, "sha": "b1"}]
        ),
    )
    _patch_promote_github_defaults(mocker)
    mocker.patch("app.github.create_branch", new=_create_branch_mock())
    create_mock = mocker.patch(
        "app.github.create_pull_request",
        new=AsyncMock(return_value={"number": 7, "html_url": "http://pr/7"}),
    )
    mocker.patch(
        "app.github.merge_pull_request",
        new=AsyncMock(
            return_value={"merged": True, "message": "Merged", "sha": merge_sha}
        ),
    )
    spawn_mock = mocker.patch(
        "app.release_qualifier._spawn_release_audit_task", new=AsyncMock()
    )
    mocker.patch.object(
        rq,
        "_resolve_ci_at_finalize",
        new=AsyncMock(return_value=(True, {"state": "green", "resolved_sha": candidate, "detail": "ci green for test"})),
    )

    result = await rq.force_promote_current_version(
        db_session,
        project.id,
        actor="operator-1",
        session_factory=_shared_session_factory(db_session),
    )

    assert result["status"] == "promoted"
    # The AUDIT gate was skipped — no audit child spawned.
    spawn_mock.assert_not_called()
    # But the promotion PR was still opened release-vN → main (CI still gates it).
    assert create_mock.call_args.kwargs["base"] == "main"
    await db_session.refresh(version)
    assert version.status == "promoted"
    assert version.last_qualification.get("forced_promote") is True
    assert version.last_qualification.get("forced_promote_by") == "operator-1"


@pytest.mark.asyncio
async def test_hotfix_promote_rejects_non_allowlisted_file(db_session, mocker):
    """The scope check FAILS CLOSED on a diff touching a non-allowlisted (product)
    file: hotfix_promote returns scope_rejected WITHOUT cutting any branch or
    opening a PR, and the version stays open."""
    project, version = await _make_project_and_version(db_session, phase="stabilize")
    create_branch_mock, create_pr_mock = _patch_hotfix_promote_github(
        mocker,
        changed_files=[
            ("backend/app/release_scoring.py", "@@ -1 +1 @@\n-x\n+y\n"),
            ("backend/app/compute_jobs.py", "@@ -1 +1 @@\n-a\n+b\n"),  # product code
        ],
    )

    result = await rq.hotfix_promote_current_version(
        db_session,
        project.id,
        actor="operator-1",
        reason="gate fix",
        scope_modules=["backend/app/release_scoring.py"],
        session_factory=_shared_session_factory(db_session),
    )

    assert result["status"] == "scope_rejected"
    assert "backend/app/compute_jobs.py" in result["offending_files"]
    # No branch cut, no PR opened — the lane refused before promoting.
    create_branch_mock.assert_not_called()
    create_pr_mock.assert_not_called()
    await db_session.refresh(version)
    assert version.status == "open"


@pytest.mark.asyncio
async def test_hotfix_promote_rejects_new_migration_file(db_session, mocker):
    """A diff that adds a backend/alembic/versions/ migration is NOT a
    machinery-only fix — the lane refuses fail-closed (routes it to the full
    gate)."""
    project, version = await _make_project_and_version(db_session, phase="stabilize")
    create_branch_mock, create_pr_mock = _patch_hotfix_promote_github(
        mocker,
        changed_files=[
            ("backend/app/release_scoring.py", "@@ -1 +1 @@\n-x\n+y\n"),
            ("backend/alembic/versions/0999_new.py", "@@ +1 @@\n+revision='0999'\n"),
        ],
    )

    result = await rq.hotfix_promote_current_version(
        db_session,
        project.id,
        actor="operator-1",
        reason="gate fix",
        scope_modules=["backend/app/release_scoring.py"],
        session_factory=_shared_session_factory(db_session),
    )

    assert result["status"] == "scope_rejected"
    assert "backend/alembic/versions/0999_new.py" in result["offending_files"]
    create_branch_mock.assert_not_called()
    create_pr_mock.assert_not_called()


@pytest.mark.asyncio
async def test_hotfix_promote_rejects_expected_schema_revision_bump(db_session, mocker):
    """main.py IS allowlisted but GUARDED: a diff whose main.py hunk alters
    EXPECTED_SCHEMA_REVISION is rejected (a bump without the matching migration
    deployed wedges boot)."""
    project, version = await _make_project_and_version(db_session, phase="stabilize")
    create_branch_mock, create_pr_mock = _patch_hotfix_promote_github(
        mocker,
        changed_files=[
            (
                "backend/app/main.py",
                '@@ -10 +10 @@\n-EXPECTED_SCHEMA_REVISION = "0530"\n'
                '+EXPECTED_SCHEMA_REVISION = "0580"\n',
            ),
        ],
    )

    result = await rq.hotfix_promote_current_version(
        db_session,
        project.id,
        actor="operator-1",
        reason="bump schema",
        scope_modules=["backend/app/main.py"],
        session_factory=_shared_session_factory(db_session),
    )

    assert result["status"] == "scope_rejected"
    assert "backend/app/main.py" in result["offending_files"]
    assert "EXPECTED_SCHEMA_REVISION" in result["reason"]
    create_branch_mock.assert_not_called()
    create_pr_mock.assert_not_called()


@pytest.mark.asyncio
async def test_hotfix_promote_accepts_machinery_diff_and_writes_audit_trail(
    db_session, mocker
):
    """A machinery-only diff (release_scoring.py) is ACCEPTED: the promote path
    runs (branch cut, PR opened + merged, no audit child), the base_record carries
    the hotfix provenance, and the durable audit trail is written — a
    hotfix_deploy_pending_backfill completion-debt entry with the expected detail
    fields, plus an operator Alert."""
    project, version = await _make_project_and_version(db_session, phase="stabilize")
    _, create_pr_mock = _patch_hotfix_promote_github(
        mocker,
        changed_files=[
            ("backend/app/release_scoring.py", "@@ -1 +1 @@\n-x\n+y\n"),
            ("backend/app/release_qualifier.py", "@@ -1 +1 @@\n-a\n+b\n"),
        ],
    )

    # Promotion opens a best-effort staged-direction session. Under the in-memory
    # SQLite StaticPool it must share this test session or a second session close
    # can roll back the outer transaction.
    mocker.patch(
        "app.database.AsyncSessionLocal",
        new=_shared_session_factory(db_session),
    )

    result = await rq.hotfix_promote_current_version(
        db_session,
        project.id,
        actor="operator-1",
        reason="prod gate false-block — machinery-only fix",
        scope_modules=["backend/app/release_scoring.py"],
        session_factory=_shared_session_factory(db_session),
    )

    assert result["status"] == "promoted"
    # The AUDIT gate was skipped but the PR was still opened toward main (CI gates).
    assert create_pr_mock.call_args.kwargs["base"] == "main"

    # ── Durable audit trail: completion debt (the new hotfix seam) ──
    await db_session.commit()
    debt = (
        (
            await db_session.execute(
                select(CompletionDebtEntry).where(
                    CompletionDebtEntry.project_id == project.id,
                    CompletionDebtEntry.kind == "hotfix_deploy_pending_backfill",
                )
            )
        )
        .scalars()
        .first()
    )
    assert debt is not None, "hotfix must open a completion-debt entry"
    assert debt.status == "open"
    detail = debt.detail if isinstance(debt.detail, dict) else json.loads(debt.detail)
    assert detail["actor"] == "operator-1"
    assert detail["reason"] == "prod gate false-block — machinery-only fix"
    assert detail["scope_modules"] == ["backend/app/release_scoring.py"]
    assert "backend/app/release_scoring.py" in detail["diff_file_list"]
    assert detail["candidate_sha"]
    assert "prior_deployed_sha" in detail

    # ── Durable audit trail: operator Alert ──
    alert = (
        (
            await db_session.execute(
                select(Alert).where(
                    Alert.project_id == project.id,
                    Alert.source == "hotfix_promote",
                )
            )
        )
        .scalars()
        .first()
    )
    assert alert is not None, "hotfix must raise an operator Alert"
    assert alert.severity == "WARNING"

    # The promoted version records the hotfix provenance in its verdict blob.
    await db_session.refresh(version)
    assert version.status == "promoted"
    assert version.last_qualification.get("hotfix_promote") is True
    assert version.last_qualification.get("hotfix_promote_by") == "operator-1"
    assert version.last_qualification.get("hotfix_promote_scope_modules") == [
        "backend/app/release_scoring.py"
    ]


@pytest.mark.asyncio
async def test_hotfix_promote_managed_check_wait_still_writes_audit_trail(
    db_session, mocker
):
    """PRIMARY CI-gated flow: the promotion PR opens in managed-check wait but
    does NOT merge synchronously (required checks still pending), so promote()
    returns ``pending_managed_check_wait`` — NOT ``promoted``.  The bypass has already
    taken effect (audit gate skipped, PR open toward main), so the durable audit
    trail (completion-debt backfill + operator Alert) MUST still be written.
    This is the regression the review flagged: the trail was gated on
    ``status == 'promoted'`` and stayed silent on this primary path."""
    project, version = await _make_project_and_version(db_session, phase="stabilize")
    _patch_hotfix_promote_github(
        mocker,
        changed_files=[
            ("backend/app/release_scoring.py", "@@ -1 +1 @@\n-x\n+y\n"),
        ],
    )
    # Override the helper's merge default: 405-style not-merged-no-conflict —
    # required checks still pending — so promote() retains exact-head merge
    # authority in managed_check_wait.
    mocker.patch(
        "app.github.merge_pull_request",
        new=AsyncMock(
            return_value={"merged": False, "message": "required checks pending"}
        ),
    )
    # Promotion opens a best-effort staged-direction session. Under the in-memory
    # SQLite StaticPool it must share this test session or a second session close
    # can roll back the outer transaction.
    mocker.patch(
        "app.database.AsyncSessionLocal",
        new=_shared_session_factory(db_session),
    )

    result = await rq.hotfix_promote_current_version(
        db_session,
        project.id,
        actor="operator-1",
        reason="prod gate false-block — machinery-only fix",
        scope_modules=["backend/app/release_scoring.py"],
        session_factory=_shared_session_factory(db_session),
    )

    # The primary CI-gated outcome: PR open under Blueprint's managed waiter.
    assert result["status"] == "pending_managed_check_wait"
    assert result["managed_check_wait"] is True

    # ── The audit trail is written on THIS path, not only on 'promoted'. ──
    await db_session.commit()
    debt = (
        (
            await db_session.execute(
                select(CompletionDebtEntry).where(
                    CompletionDebtEntry.project_id == project.id,
                    CompletionDebtEntry.kind == "hotfix_deploy_pending_backfill",
                )
            )
        )
        .scalars()
        .first()
    )
    assert debt is not None, "pending managed hotfix must open a completion-debt entry"
    assert debt.status == "open"
    detail = debt.detail if isinstance(debt.detail, dict) else json.loads(debt.detail)
    assert detail["actor"] == "operator-1"
    assert "backend/app/release_scoring.py" in detail["diff_file_list"]

    alert = (
        (
            await db_session.execute(
                select(Alert).where(
                    Alert.project_id == project.id,
                    Alert.source == "hotfix_promote",
                )
            )
        )
        .scalars()
        .first()
    )
    assert alert is not None, "pending managed hotfix must raise an operator Alert"
    assert alert.severity == "WARNING"


@pytest.mark.asyncio
async def test_hotfix_promote_narrow_lane_ships_named_repair_from_wide_candidate(
    db_session, mocker
):
    """The lane ships a two-file machinery repair out of a wide candidate.

    Integration has accumulated unrelated product work since the last promotion,
    so the whole-candidate compare is ineligible (asserted below via the
    no-repair-named control).  Naming the repair commit makes the lane
    materialize a narrow candidate on the promotion target and promote THAT SHA:
    the release branch is cut at the narrow SHA, CI is resolved against the
    commit the repair was actually built on, and the audit trail records both
    which commits were asserted and which files shipped.
    """
    project, version = await _make_project_and_version(db_session, phase="stabilize")
    integration_head = "facec0de" * 5
    prior_deployed = "mainnew" * 5 + "00000"
    repair_sha = "ab" * 20
    narrow_sha = "9f" * 20
    repair_files = [
        "backend/app/release_stall_alerts.py",
        "backend/testing/test_release_stall_alerts.py",
    ]
    wide_entries = [
        {"filename": name, "patch": None}
        for name in repair_files + ["backend/app/reaper.py", "backend/app/admin.py"]
    ]
    repair_entries = [{"filename": name, "patch": None} for name in repair_files]

    branch_mock, create_pr_mock = _patch_hotfix_promote_github(
        mocker, changed_files=[(name, None) for name in repair_files]
    )

    async def _compare(token, owner, repo, base_ref, head_ref, project_id=None):
        if head_ref == integration_head:
            return list(wide_entries)  # the accumulated candidate — ineligible
        if (base_ref, head_ref) == (f"{repair_sha}~1", repair_sha):
            return list(repair_entries)  # the repair's own diff
        if (base_ref, head_ref) == (prior_deployed, narrow_sha):
            return list(repair_entries)  # what the narrow candidate actually ships
        raise AssertionError(f"unexpected compare {base_ref}...{head_ref}")

    mocker.patch(
        "app.github_refs.get_changed_files_with_patches_between_refs",
        new=AsyncMock(side_effect=_compare),
    )
    mocker.patch(
        "app.github.get_file_blob_sha_at_ref",
        new=AsyncMock(return_value=(True, "blob-sha")),
    )
    dangling_mock = mocker.patch(
        "app.github.create_dangling_commit_from_blob_refs",
        new=AsyncMock(return_value=narrow_sha),
    )
    ci_mock = mocker.patch.object(
        rq,
        "_resolve_ci_at_finalize",
        new=AsyncMock(return_value=(True, {"state": "green", "detail": "ci green"})),
    )
    mocker.patch(
        "app.database.AsyncSessionLocal", new=_shared_session_factory(db_session)
    )

    # Control: without naming the repair, the accumulated candidate still fails
    # closed — the narrow lane changes the INPUT, not the predicate.
    control = await rq.hotfix_promote_current_version(
        db_session,
        project.id,
        actor="operator-1",
        reason="latched gate repair",
        session_factory=_shared_session_factory(db_session),
    )
    assert control["status"] == "scope_rejected"
    assert "backend/app/reaper.py" in control["offending_files"]
    dangling_mock.assert_not_awaited()

    result = await rq.hotfix_promote_current_version(
        db_session,
        project.id,
        actor="operator-1",
        reason="latched gate repair",
        scope_modules=["backend/app/release_stall_alerts.py"],
        repair_commit_shas=[repair_sha],
        session_factory=_shared_session_factory(db_session),
    )

    assert result["status"] == "promoted", result
    # The narrow commit is parented on the promotion target and carries only the
    # repair's paths.
    assert dangling_mock.await_args.kwargs["base_ref"] == prior_deployed
    assert [e["path"] for e in dangling_mock.await_args.kwargs["tree_entries"]] == (
        repair_files
    )
    # promote() cut the release branch at the NARROW sha, not integration HEAD.
    assert branch_mock.call_args.args[3] == "release-v1"
    assert branch_mock.call_args.args[4] == narrow_sha
    assert create_pr_mock.call_args.kwargs["base"] == "main"
    # CI is resolved against the content source (the tested commit), never the
    # just-synthesized object that no CI run has ever seen.
    assert ci_mock.await_args.args[2] == integration_head

    await db_session.commit()
    debt = (
        (
            await db_session.execute(
                select(CompletionDebtEntry).where(
                    CompletionDebtEntry.project_id == project.id,
                    CompletionDebtEntry.kind == "hotfix_deploy_pending_backfill",
                )
            )
        )
        .scalars()
        .first()
    )
    assert debt is not None
    detail = debt.detail if isinstance(debt.detail, dict) else json.loads(debt.detail)
    assert detail["repair_commit_shas"] == [repair_sha]
    assert detail["diff_file_list"] == repair_files
    assert detail["candidate_sha"] == narrow_sha

    await db_session.refresh(version)
    assert version.status == "promoted"
    assert version.last_qualification.get("hotfix_promote_repair_commit_shas") == [
        repair_sha
    ]


@pytest.mark.asyncio
async def test_hotfix_promote_narrow_lane_rejects_repair_commit_with_product_code(
    db_session, mocker
):
    """Naming a repair commit that ALSO touches product code fails closed.

    The narrow lane must never become a way to smuggle non-machinery files into
    the bypass: the scope check runs over the named commits' own diffs, and a
    rejection must create no commit, cut no branch, and open no PR."""
    project, version = await _make_project_and_version(db_session, phase="stabilize")
    repair_sha = "cd" * 20
    branch_mock, create_pr_mock = _patch_hotfix_promote_github(
        mocker, changed_files=[("backend/app/release_scoring.py", None)]
    )
    mocker.patch(
        "app.github_refs.get_changed_files_with_patches_between_refs",
        new=AsyncMock(
            return_value=[
                {"filename": "backend/app/release_scoring.py", "patch": None},
                {"filename": "backend/app/reaper.py", "patch": None},
            ]
        ),
    )
    dangling_mock = mocker.patch(
        "app.github.create_dangling_commit_from_blob_refs", new=AsyncMock()
    )

    result = await rq.hotfix_promote_current_version(
        db_session,
        project.id,
        actor="operator-1",
        reason="latched gate repair",
        repair_commit_shas=[repair_sha],
        session_factory=_shared_session_factory(db_session),
    )

    assert result["status"] == "scope_rejected"
    assert result["offending_files"] == ["backend/app/reaper.py"]
    dangling_mock.assert_not_awaited()
    branch_mock.assert_not_called()
    create_pr_mock.assert_not_called()
    await db_session.refresh(version)
    assert version.status == "open"


@pytest.mark.asyncio
async def test_hotfix_promote_narrow_lane_rejects_unresolvable_repair_ref(
    db_session, mocker
):
    """Fail-closed survives the resolve-don't-reject change.

    The lane used to refuse ANY abbreviated repair sha on shape alone, which
    handed the operator back a lookup the engine could perform.  It now resolves
    the ref -- but a ref GitHub cannot resolve (unknown, or a prefix matching
    more than one object) is STILL rejected, before any branch is cut, and the
    reason carries GitHub's own message instead of a generic string.
    """
    from fastapi import HTTPException

    project, version = await _make_project_and_version(db_session, phase="stabilize")
    branch_mock, create_pr_mock = _patch_hotfix_promote_github(
        mocker, changed_files=[("backend/app/release_scoring.py", None)]
    )
    candidate = "facec0de" * 5

    async def _resolve(_token, _owner, _repo, ref, **_kwargs):
        if ref == candidate:
            return candidate
        raise HTTPException(
            status_code=400,
            detail=(
                f"Could not resolve {ref!r} to a full 40-character commit sha "
                f"(GitHub 422: No commit found for SHA: {ref})."
            ),
        )

    mocker.patch("app.github.resolve_commit_sha", new=AsyncMock(side_effect=_resolve))
    dangling_mock = mocker.patch(
        "app.github.create_dangling_commit_from_blob_refs", new=AsyncMock()
    )

    result = await rq.hotfix_promote_current_version(
        db_session,
        project.id,
        actor="operator-1",
        reason="latched gate repair",
        repair_commit_shas=["2a5a9833"],
        session_factory=_shared_session_factory(db_session),
    )

    assert result["status"] == "scope_rejected"
    assert "No commit found for SHA" in result["reason"], (
        "GitHub's own words must reach the operator, not a generic refusal"
    )
    assert "2a5a9833" in result["reason"], "the ref the operator typed must be named"
    dangling_mock.assert_not_awaited()
    branch_mock.assert_not_called()
    create_pr_mock.assert_not_called()
    await db_session.refresh(version)
    assert version.status == "open"


@pytest.mark.asyncio
async def test_hotfix_promote_narrow_lane_resolves_an_abbreviated_repair_sha(
    db_session, mocker
):
    """An abbreviated repair sha ships, and the AUDIT TRAIL records the full one.

    A human types what ``git log`` prints.  The engine holds the token, the owner
    and the repo, so it resolves the ref itself instead of handing the lookup
    back.  The split-brain guard is the second half: every downstream consumer --
    the compare, the materialized commit, the completion-debt detail and the
    persisted qualification -- must key off the CANONICAL sha, never the
    abbreviation.
    """
    project, version = await _make_project_and_version(db_session, phase="stabilize")
    integration_head = "facec0de" * 5
    prior_deployed = "mainnew" * 5 + "00000"
    repair_sha = "ab" * 20
    repair_abbrev = repair_sha[:9]
    narrow_sha = "9f" * 20
    repair_files = ["backend/app/release_stall_alerts.py"]
    repair_entries = [{"filename": name, "patch": None} for name in repair_files]

    branch_mock, _create_pr_mock = _patch_hotfix_promote_github(
        mocker, changed_files=[(name, None) for name in repair_files]
    )

    async def _resolve(_token, _owner, _repo, ref, **_kwargs):
        if len(ref) == 40:
            return ref
        assert ref == repair_abbrev, f"unexpected ref to resolve: {ref}"
        return repair_sha

    resolve_mock = mocker.patch(
        "app.github.resolve_commit_sha", new=AsyncMock(side_effect=_resolve)
    )

    async def _compare(token, owner, repo, base_ref, head_ref, project_id=None):
        # The own-diff must be taken against the RESOLVED sha, never the prefix.
        assert base_ref != repair_abbrev and head_ref != repair_abbrev, (
            "the abbreviation must never reach the compare"
        )
        if (base_ref, head_ref) == (f"{repair_sha}~1", repair_sha):
            return list(repair_entries)
        if (base_ref, head_ref) == (prior_deployed, narrow_sha):
            return list(repair_entries)
        raise AssertionError(f"unexpected compare {base_ref}...{head_ref}")

    mocker.patch(
        "app.github_refs.get_changed_files_with_patches_between_refs",
        new=AsyncMock(side_effect=_compare),
    )
    mocker.patch(
        "app.github.get_file_blob_sha_at_ref",
        new=AsyncMock(return_value=(True, "blob-sha")),
    )
    dangling_mock = mocker.patch(
        "app.github.create_dangling_commit_from_blob_refs",
        new=AsyncMock(return_value=narrow_sha),
    )
    ci_mock = mocker.patch.object(
        rq,
        "_resolve_ci_at_finalize",
        new=AsyncMock(return_value=(True, {"state": "green", "detail": "ci green"})),
    )
    mocker.patch(
        "app.database.AsyncSessionLocal", new=_shared_session_factory(db_session)
    )

    result = await rq.hotfix_promote_current_version(
        db_session,
        project.id,
        actor="operator-1",
        reason="latched gate repair",
        repair_commit_shas=[repair_abbrev],
        session_factory=_shared_session_factory(db_session),
    )

    assert result["status"] == "promoted", result
    assert resolve_mock.await_count >= 1
    assert branch_mock.call_args.args[4] == narrow_sha
    # CI resolves against the CONTENT source -- the canonical integration head.
    assert ci_mock.await_args.args[2] == integration_head
    # The materialized commit names the resolved sha, not the abbreviation.
    message = dangling_mock.await_args.kwargs["message"]
    assert repair_sha in message
    assert f"{repair_abbrev} " not in message and f"{repair_abbrev}," not in message

    await db_session.commit()
    debt = (
        (
            await db_session.execute(
                select(CompletionDebtEntry).where(
                    CompletionDebtEntry.project_id == project.id,
                    CompletionDebtEntry.kind == "hotfix_deploy_pending_backfill",
                )
            )
        )
        .scalars()
        .first()
    )
    assert debt is not None
    detail = debt.detail if isinstance(debt.detail, dict) else json.loads(debt.detail)
    assert detail["repair_commit_shas"] == [repair_sha], (
        "the audit trail must record what was verified, not what was typed"
    )

    await db_session.refresh(version)
    assert version.last_qualification.get("hotfix_promote_repair_commit_shas") == [
        repair_sha
    ]


def test_evaluate_hotfix_scope_pure_predicate():
    """Unit coverage for the pure fail-closed scope predicate."""
    from app import hotfix_policy

    # Literal file set from 41dd92106: the authorship-gate repair must be
    # eligible as a whole, including its independently green-pinned tests.
    authorship_gate_repair_files = [
        "backend/app/release_commit_authorship.py",
        "backend/app/release_promotion.py",
        "backend/app/release_qualifier.py",
        "backend/app/release_qualifier_audit.py",
        "backend/app/release_scoring.py",
        "backend/testing/_release_qualifier_testhelpers.py",
        "backend/testing/test_release_operator_commit_gate.py",
        "backend/testing/test_release_qualifier.py",
        "backend/testing/test_release_qualifier__ci_unresolved_blocker.py",
        "backend/testing/test_release_qualifier__classify_check_runs.py",
        "backend/testing/test_release_reconciliation.py",
        "backend/testing/test_release_scoring.py",
    ]
    assert hotfix_policy.evaluate_hotfix_scope(authorship_gate_repair_files).eligible

    # Literal file set from 3029329ff: the latch alert and its operator-facing
    # delivery path must be eligible as one repair.
    latch_announcement_repair_files = [
        "backend/app/message_audience.py",
        "backend/app/project_message_creation.py",
        "backend/app/release_stall_alerts.py",
        "backend/testing/test_release_stall_alerts.py",
    ]
    assert hotfix_policy.evaluate_hotfix_scope(latch_announcement_repair_files).eligible

    # Literal file set from 8731cfa17: a release-gate invariant may travel with
    # the production repair even though the general test tree remains blocked.
    authorship_budget_repair_files = [
        "backend/app/release_scoring.py",
        "backend/testing/test_invariants.py",
        "backend/testing/test_release_operator_commit_gate.py",
    ]
    assert hotfix_policy.evaluate_hotfix_scope(authorship_budget_repair_files).eligible

    # A product-code path remains outside the authorization surface.
    product_file = hotfix_policy.evaluate_hotfix_scope(["backend/app/reaper.py"])
    assert product_file.eligible is False
    assert product_file.offending_files == ["backend/app/reaper.py"]

    # Machinery-only → eligible.
    v = hotfix_policy.evaluate_hotfix_scope(["backend/app/release_scoring.py"])
    assert v.eligible is True

    # Product file → ineligible.
    v = hotfix_policy.evaluate_hotfix_scope(
        ["backend/app/release_scoring.py", "backend/app/compute_jobs.py"]
    )
    assert v.eligible is False
    assert "backend/app/compute_jobs.py" in v.offending_files

    # Empty diff → fail-closed reject.
    assert hotfix_policy.evaluate_hotfix_scope([]).eligible is False

    # Hard rejects remain hard rejects even when the surrounding diff contains
    # otherwise eligible release machinery.
    assert (
        hotfix_policy.evaluate_hotfix_scope(
            [
                "backend/app/release_commit_authorship.py",
                "backend/alembic/versions/9999_hotfix.py",
            ]
        ).eligible
        is False
    )
    assert (
        hotfix_policy.evaluate_hotfix_scope([".github/workflows/deploy.yml"]).eligible
        is False
    )

    # main.py with a schema-revision bump → reject; without → allow.
    bump = '+EXPECTED_SCHEMA_REVISION = "0580"\n'
    v = hotfix_policy.evaluate_hotfix_scope(
        ["backend/app/main.py"], main_py_patch=bump
    )
    assert v.eligible is False
    v = hotfix_policy.evaluate_hotfix_scope(
        ["backend/app/main.py"], main_py_patch="@@\n-log.info('x')\n+log.info('y')\n"
    )
    assert v.eligible is True

    # main.py changed but patch unavailable → fail-closed reject.
    assert (
        hotfix_policy.evaluate_hotfix_scope(
            ["backend/app/main.py"], main_py_patch=None
        ).eligible
        is False
    )


@pytest.mark.asyncio
async def test_force_promote_records_operator_reason(db_session, mocker):
    """The operator-supplied justification string is plumbed through
    force_promote_current_version into the recorded promotion audit metadata
    (forced_promote_reason), alongside forced_promote_by / forced_promote_at."""
    project, version = await _make_project_and_version(db_session, phase="stabilize")

    candidate = "facec0de" * 5  # 40-char candidate sha
    merge_sha = "mainnew" * 5 + "00000"
    mocker.patch(
        "app.github.get_repo_installation_token",
        new=AsyncMock(return_value="gh-token"),
    )
    mocker.patch(
        "app.github.get_branch_sha",
        new=_branch_sha_mock(automain=candidate, main=merge_sha),
    )
    mocker.patch(
        "app.github.list_repo_tree_paths_at_ref",
        new=AsyncMock(
            return_value=[{"path": "backend/app/main.py", "size": 10, "sha": "b1"}]
        ),
    )
    _patch_promote_github_defaults(mocker)
    mocker.patch("app.github.create_branch", new=_create_branch_mock())
    mocker.patch(
        "app.github.create_pull_request",
        new=AsyncMock(return_value={"number": 7, "html_url": "http://pr/7"}),
    )
    mocker.patch(
        "app.github.merge_pull_request",
        new=AsyncMock(return_value={"merged": True, "message": "Merged"}),
    )
    mocker.patch(
        "app.release_qualifier._spawn_release_audit_task", new=AsyncMock()
    )

    result = await rq.force_promote_current_version(
        db_session,
        project.id,
        actor="operator-1",
        reason="prod incident #123 — hotfix must ship now",
        session_factory=_shared_session_factory(db_session),
    )

    assert result["status"] == "promoted"
    await db_session.refresh(version)
    assert version.last_qualification.get("forced_promote") is True
    assert version.last_qualification.get("forced_promote_by") == "operator-1"
    assert (
        version.last_qualification.get("forced_promote_reason")
        == "prod incident #123 — hotfix must ship now"
    )


@pytest.mark.asyncio
async def test_force_promote_reason_defaults_to_none(db_session, mocker):
    """When no reason is supplied, forced_promote_reason is recorded as None
    (the metadata key is always present so the audit trail is unambiguous)."""
    project, version = await _make_project_and_version(db_session, phase="stabilize")

    candidate = "facec0de" * 5
    merge_sha = "mainnew" * 5 + "00000"
    mocker.patch(
        "app.github.get_repo_installation_token",
        new=AsyncMock(return_value="gh-token"),
    )
    mocker.patch(
        "app.github.get_branch_sha",
        new=_branch_sha_mock(automain=candidate, main=merge_sha),
    )
    mocker.patch(
        "app.github.list_repo_tree_paths_at_ref",
        new=AsyncMock(
            return_value=[{"path": "backend/app/main.py", "size": 10, "sha": "b1"}]
        ),
    )
    _patch_promote_github_defaults(mocker)
    mocker.patch("app.github.create_branch", new=_create_branch_mock())
    mocker.patch(
        "app.github.create_pull_request",
        new=AsyncMock(return_value={"number": 7, "html_url": "http://pr/7"}),
    )
    mocker.patch(
        "app.github.merge_pull_request",
        new=AsyncMock(return_value={"merged": True, "message": "Merged"}),
    )
    mocker.patch(
        "app.release_qualifier._spawn_release_audit_task", new=AsyncMock()
    )

    result = await rq.force_promote_current_version(
        db_session,
        project.id,
        actor="operator-1",
        session_factory=_shared_session_factory(db_session),
    )

    assert result["status"] == "promoted"
    await db_session.refresh(version)
    assert "forced_promote_reason" in version.last_qualification
    assert version.last_qualification.get("forced_promote_reason") is None


@pytest.mark.asyncio
async def test_hotfix_scope_rejects_incomplete_compare(db_session, mocker):
    """A capped GitHub compare cannot authorize a machinery-only hotfix.

    This exercises the real compare helper through the promotion seam: GitHub
    returns its documented 300-file cap, so the helper returns its
    indeterminate sentinel and promotion declines before any branch or PR side
    effect.
    """
    project, version = await _make_project_and_version(db_session, phase="stabilize")
    real_compare_helper = github_refs.get_changed_files_with_patches_between_refs
    create_branch_mock, create_pr_mock = _patch_hotfix_promote_github(
        mocker,
        changed_files=[("backend/app/release_qualifier.py", "@@ -1 +1 @@\n-x\n+y\n")],
    )
    compare_response = MagicMock(status_code=200)
    compare_response.json.return_value = {
        "files": [
            {"filename": "backend/app/release_qualifier.py", "patch": None}
            for _ in range(300)
        ],
    }
    # Override the helper mock installed by the ordinary hotfix fixture: this
    # assertion must exercise github_refs' actual completeness check.
    mocker.patch("app.github._github_request", new=AsyncMock(return_value=compare_response))
    mocker.patch(
        "app.github_refs.get_changed_files_with_patches_between_refs",
        new=real_compare_helper,
    )

    result = await rq.hotfix_promote_current_version(
        db_session,
        project.id,
        actor="operator-1",
        reason="candidate diff must be complete",
        session_factory=_shared_session_factory(db_session),
    )

    assert result["status"] == "scope_rejected"
    assert "could not resolve the candidate diff" in result["reason"]
    create_branch_mock.assert_not_called()
    create_pr_mock.assert_not_called()
    await db_session.refresh(version)
    assert version.status == "open"


@pytest.mark.asyncio
async def test_hotfix_promote_audit_repair_failure_does_not_mask_committed_result(
    db_session, mocker
):
    """The audit remains best-effort because the promotion commits first."""
    project, _version = await _make_project_and_version(
        db_session, phase="stabilize"
    )
    _patch_hotfix_promote_github(
        mocker,
        changed_files=[
            ("backend/app/release_scoring.py", "@@ -1 +1 @@\n-x\n+y\n"),
        ],
    )
    mocker.patch(
        "app.release_qualifier._reconcile_hotfix_audit_trail",
        new=AsyncMock(side_effect=RuntimeError("audit database unavailable")),
    )

    result = await rq.hotfix_promote_current_version(
        db_session,
        project.id,
        actor="operator-1",
        reason="best-effort audit regression",
        scope_modules=["backend/app/release_scoring.py"],
        session_factory=_shared_session_factory(db_session),
    )

    assert result["status"] == "promoted"


@pytest.mark.asyncio
async def test_hotfix_promotion_reconciles_audit_after_crash(db_session, mocker):
    """A committed hotfix survives request rollback with its debt and Alert restored.

    ``hotfix_promote_current_version`` commits the promotion in its dedicated
    session but stages its audit trail in the caller's session.  This models a
    crash in that gap, then exercises the real periodic sweep twice to prove
    recovery and idempotency rather than merely testing the writer in isolation.
    """
    project, version = await _make_project_and_version(db_session, phase="stabilize")
    project_id, version_id = project.id, version.id
    _patch_hotfix_promote_github(
        mocker,
        changed_files=[
            ("backend/app/release_scoring.py", "@@ -1 +1 @@\n-x\n+y\n"),
        ],
    )

    result = await rq.hotfix_promote_current_version(
        db_session,
        project_id,
        actor="operator-crash-test",
        reason="restore the promised audit trail",
        scope_modules=["backend/app/release_scoring.py"],
        session_factory=_shared_session_factory(db_session),
    )
    assert result["status"] == "promoted"

    # Simulate process/request loss after the dedicated promotion commit.
    await db_session.rollback()
    reloaded_version = await db_session.get(type(version), version_id)
    assert reloaded_version.last_qualification["hotfix_promote_diff_file_list"] == [
        "backend/app/release_scoring.py"
    ]

    first_count = await rq.sweep_stuck_qualifications(
        db_session, session_factory=_shared_session_factory(db_session)
    )
    await db_session.commit()
    second_count = await rq.sweep_stuck_qualifications(
        db_session, session_factory=_shared_session_factory(db_session)
    )
    await db_session.commit()

    debt_rows = list(
        (
            await db_session.execute(
                select(CompletionDebtEntry).where(
                    CompletionDebtEntry.project_id == project_id,
                    CompletionDebtEntry.kind == "hotfix_deploy_pending_backfill",
                )
            )
        ).scalars()
    )
    alert_rows = list(
        (
            await db_session.execute(
                select(Alert).where(
                    Alert.project_id == project_id,
                    Alert.source == "hotfix_promote",
                )
            )
        ).scalars()
    )
    assert first_count >= 1
    assert second_count == 0
    assert len(debt_rows) == 1
    assert debt_rows[0].detail["actor"] == "operator-crash-test"
    assert len(alert_rows) == 1
    assert alert_rows[0].dedupe_key == f"hotfix_deploy_alert:{project_id}:" + (
        "facec0de" * 5
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "promotion_pending",
    [
        {
            "pr_number": 42,
            "pr_url": "https://github.test/pull/42",
            "managed_check_wait": True,
            "enabled_at": "2026-07-15T00:00:00",
        },
    ],
    ids=["pending_managed_check_wait"],
)
async def test_hotfix_pending_wait_reconciles_audit_after_request_crash(
    db_session, promotion_pending
):
    """The managed return-only wait persists as pending + sub-state.

    Model a crash after that durable promotion commit but before request-side
    audit repair.  The sweep must recognize both persisted shapes and create
    the missing debt and Alert.
    """
    project, version = await _make_project_and_version(db_session, phase="stabilize")
    candidate_sha = "c1wa1t00" * 5
    version.last_qualification = {
        "status": "pending",
        "candidate_sha": candidate_sha,
        "hotfix_promote": True,
        "hotfix_promote_by": "operator-crashed",
        "hotfix_promote_reason": "CI wait crash recovery",
        "hotfix_promote_scope_modules": ["backend/app/release_qualifier.py"],
        "hotfix_promote_diff_file_list": ["backend/app/release_qualifier.py"],
        "hotfix_promote_prior_deployed_sha": "prior000" * 5,
        "promotion_pending": promotion_pending,
    }
    db_session.add(version)
    await db_session.commit()

    count = await rq.sweep_stuck_qualifications(
        db_session, session_factory=_shared_session_factory(db_session)
    )
    await db_session.commit()

    debt_key = f"hotfix_deploy:{project.id}:{candidate_sha}"
    alert_key = f"hotfix_deploy_alert:{project.id}:{candidate_sha}"
    assert count >= 1
    assert (
        await db_session.execute(
            select(CompletionDebtEntry).where(
                CompletionDebtEntry.dedupe_key == debt_key
            )
        )
    ).scalar_one_or_none() is not None
    assert (
        await db_session.execute(select(Alert).where(Alert.dedupe_key == alert_key))
    ).scalar_one_or_none() is not None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("debt_status", "seed_alert", "expected_debt_status"),
    [
        ("open", False, "open"),
        (None, True, "open"),
        ("resolved", False, "resolved"),
        ("dismissed", False, "dismissed"),
    ],
    ids=["debt_only", "alert_only", "resolved_debt", "dismissed_debt"],
)
async def test_hotfix_partial_audit_repair_preserves_existing_debt_state(
    db_session, debt_status, seed_alert, expected_debt_status
):
    """Partial recovery creates only the missing artifact.

    In particular, a later normal qualification's resolved debt and an
    operator-dismissed debt are historical facts and must never be reopened.
    """
    project, version = await _make_project_and_version(db_session, phase="stabilize")
    candidate_sha = "part1al0" * 5
    debt_key = f"hotfix_deploy:{project.id}:{candidate_sha}"
    alert_key = f"hotfix_deploy_alert:{project.id}:{candidate_sha}"
    version.status = "promoted"
    version.last_qualification = {
        "status": "promoted",
        "candidate_sha": candidate_sha,
        "hotfix_promote": True,
        "hotfix_promote_by": "operator-partial",
        "hotfix_promote_reason": "partial audit repair",
        "hotfix_promote_scope_modules": ["backend/app/release_qualifier.py"],
        "hotfix_promote_diff_file_list": ["backend/app/release_qualifier.py"],
    }
    if debt_status is not None:
        debt = CompletionDebtEntry(
            project_id=project.id,
            kind="hotfix_deploy_pending_backfill",
            dedupe_key=debt_key,
            status=debt_status,
            title="existing hotfix debt",
            source="hotfix_promote",
            resolution_reason=(
                "normal gate already passed" if debt_status != "open" else None
            ),
        )
        db_session.add(debt)
    if seed_alert:
        db_session.add(
            Alert(
                project_id=project.id,
                severity="WARNING",
                title="existing hotfix alert",
                message="existing",
                source="hotfix_promote",
                dedupe_key=alert_key,
            )
        )
    db_session.add(version)
    await db_session.commit()

    repaired = await rq._reconcile_hotfix_audit_trail(
        version.id, session_factory=_shared_session_factory(db_session)
    )
    assert repaired is True

    debt_rows = list(
        (
            await db_session.execute(
                select(CompletionDebtEntry).where(
                    CompletionDebtEntry.dedupe_key == debt_key
                )
            )
        ).scalars()
    )
    alert_rows = list(
        (await db_session.execute(select(Alert).where(Alert.dedupe_key == alert_key)))
        .scalars()
    )
    assert len(debt_rows) == 1
    assert debt_rows[0].status == expected_debt_status
    if debt_status in {"resolved", "dismissed"}:
        assert debt_rows[0].resolution_reason == "normal gate already passed"
    assert len(alert_rows) == 1


@pytest.mark.asyncio
async def test_hotfix_sweep_prefilters_normal_and_complete_release_history(
    db_session, mocker
):
    """Only a hotfix with a missing artifact enters a dedicated repair session."""
    _normal_project, normal = await _make_project_and_version(db_session)
    complete_project, complete = await _make_project_and_version(db_session)
    missing_project, missing = await _make_project_and_version(db_session)
    complete_sha = "c0mplete" * 5
    missing_sha = "m1ssing0" * 5

    normal.status = "promoted"
    normal.last_qualification = {"status": "promoted", "candidate_sha": "normal00" * 5}
    complete.status = "promoted"
    complete.last_qualification = {
        "status": "promoted",
        "candidate_sha": complete_sha,
        "hotfix_promote": True,
    }
    missing.status = "promoted"
    missing.last_qualification = {
        "status": "promoted",
        "candidate_sha": missing_sha,
        "hotfix_promote": True,
    }
    db_session.add(
        CompletionDebtEntry(
            project_id=complete_project.id,
            kind="hotfix_deploy_pending_backfill",
            dedupe_key=f"hotfix_deploy:{complete_project.id}:{complete_sha}",
            status="open",
            title="complete debt",
            source="hotfix_promote",
        )
    )
    db_session.add(
        Alert(
            project_id=complete_project.id,
            severity="WARNING",
            title="complete alert",
            message="complete",
            source="hotfix_promote",
            dedupe_key=f"hotfix_deploy_alert:{complete_project.id}:{complete_sha}",
        )
    )
    db_session.add_all([normal, complete, missing])
    await db_session.commit()

    reconcile = mocker.patch(
        "app.release_qualifier_sweep._reconcile_hotfix_audit_trail",
        new=AsyncMock(return_value=False),
    )
    await rq.sweep_stuck_qualifications(
        db_session, session_factory=_shared_session_factory(db_session)
    )

    reconcile.assert_awaited_once()
    assert reconcile.await_args.args[0] == missing.id
    assert complete_project.id != missing_project.id


@pytest.mark.asyncio
async def test_hotfix_request_and_sweep_writers_serialize_on_version_lock(
    db_session,
):
    """Concurrent request/reaper repairs create one debt and one Alert."""
    if db_session.bind.dialect.name == "sqlite":
        pytest.skip("requires independent transactions and row locks; runs on Postgres")

    from sqlalchemy.ext.asyncio import AsyncSession as _AsyncSession

    engine = db_session.bind

    @contextlib.asynccontextmanager
    async def _dedicated_factory():
        async with _AsyncSession(engine, expire_on_commit=False) as dedicated:
            yield dedicated

    project, version = await _make_project_and_version(db_session)
    candidate_sha = "c0ncur00" * 5
    version.status = "promoted"
    version.last_qualification = {
        "status": "promoted",
        "candidate_sha": candidate_sha,
        "hotfix_promote": True,
    }
    db_session.add(version)
    await db_session.commit()

    outcomes = await asyncio.gather(
        rq._reconcile_hotfix_audit_trail(
            version.id, session_factory=_dedicated_factory
        ),
        rq._reconcile_hotfix_audit_trail(
            version.id, session_factory=_dedicated_factory
        ),
    )
    assert sorted(outcomes) == [False, True]

    debt_key = f"hotfix_deploy:{project.id}:{candidate_sha}"
    alert_key = f"hotfix_deploy_alert:{project.id}:{candidate_sha}"
    assert len(
        list(
            (
                await db_session.execute(
                    select(CompletionDebtEntry).where(
                        CompletionDebtEntry.dedupe_key == debt_key
                    )
                )
            ).scalars()
        )
    ) == 1
    assert len(
        list(
            (
                await db_session.execute(
                    select(Alert).where(Alert.dedupe_key == alert_key)
                )
            ).scalars()
        )
    ) == 1



@pytest.mark.asyncio
async def test_force_promote_refuses_when_promotion_check_wait(db_session, mocker):
    """Force-promote refuses when a promotion is already in flight (an audit-passed
    PR open, or the Fix-3 claim) — mirrors /qualify + the version-cut lever, so all
    levers over an in-flight promotion agree. No second PR is opened."""
    project, version = await _make_project_and_version(db_session, phase="stabilize")
    version.last_qualification = {
        "status": "pending",
        "candidate_sha": "abcd" * 10,
        "promotion_pending": {
            "pr_number": 42,
            "pr_url": "http://pr/42",
            "enabled_at": "2026-07-02T00:00:00",
        },
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
        new=_branch_sha_mock(automain="facec0de" * 5),
    )
    mocker.patch(
        "app.github.list_repo_tree_paths_at_ref",
        new=AsyncMock(
            return_value=[{"path": "backend/app/main.py", "size": 10, "sha": "b1"}]
        ),
    )
    create_mock = mocker.patch("app.github.create_pull_request", new=AsyncMock())
    mocker.patch.object(
        rq,
        "_resolve_ci_at_finalize",
        new=AsyncMock(return_value=(True, {"state": "green", "resolved_sha": "facec0de" * 5, "detail": "ci green for test"})),
    )

    result = await rq.force_promote_current_version(
        db_session,
        project.id,
        actor="op",
        session_factory=_shared_session_factory(db_session),
    )

    assert result["status"] == "promotion_check_wait"
    create_mock.assert_not_called()  # no second promotion PR opened
    await db_session.refresh(version)
    # The in-flight promotion record is untouched.
    assert version.last_qualification["promotion_pending"]["pr_number"] == 42


@pytest.mark.asyncio
async def test_force_promote_proceeds_when_verdict_finalized(db_session, mocker):
    """Stale-pending window: last_qualification has promotion_pending but the
    newest QualificationVerdict row already carries terminal_status='promoted'.
    force_promote must not refuse with promotion_check_wait — the verdict resolved."""
    project, version = await _make_project_and_version(db_session, phase="stabilize")
    version.last_qualification = {
        "status": "pending",
        "candidate_sha": "abcd" * 10,
        "promotion_pending": {
            "pr_number": 77,
            "pr_url": "http://pr/77",
            "enabled_at": "2026-07-02T00:00:00",
        },
    }
    db_session.add(version)

    # Finalized verdict already exists — promotion resolved.
    verdict = QualificationVerdict(
        project_id=project.id,
        release_version_id=version.id,
        candidate_sha="abcd" * 10,
        terminal_status="promoted",
    )
    db_session.add(verdict)
    await db_session.commit()
    await db_session.refresh(version)

    mocker.patch(
        "app.github.get_repo_installation_token",
        new=AsyncMock(return_value="gh-token"),
    )
    mocker.patch(
        "app.github.get_branch_sha",
        new=_branch_sha_mock(automain="facec0de" * 5),
    )
    mocker.patch(
        "app.github.list_repo_tree_paths_at_ref",
        new=AsyncMock(
            return_value=[{"path": "backend/app/main.py", "size": 10, "sha": "b1"}]
        ),
    )
    mocker.patch(
        "app.github.create_pull_request",
        new=AsyncMock(
            return_value={
                "number": 77,
                "html_url": "http://pr/77",
                "empty_diff": True,
                "merged": True,
            }
        ),
    )
    mocker.patch.object(
        rq,
        "_resolve_ci_at_finalize",
        new=AsyncMock(return_value=(True, {"state": "green", "resolved_sha": "facec0de" * 5, "detail": "ci green for test"})),
    )

    result = await rq.force_promote_current_version(
        db_session,
        project.id,
        actor="op",
        session_factory=_shared_session_factory(db_session),
    )

    # Must NOT refuse with promotion_check_wait — verdict already resolved.
    assert result["status"] != "promotion_check_wait", (
        "stale-pending+finalized-verdict should not block force_promote"
    )


@pytest.mark.asyncio
async def test_operator_bypasses_require_candidate_ci(db_session, mocker):
    """force_promote and hotfix_promote both gate on CI being green for the
    candidate commit.

    Both bypass channels skip the AUDIT/scoring gate but must not skip CI: when
    CI is not green the bypass returns ``{"status": "ci_not_green"}`` without
    cutting any branch or opening a PR.  This is the seam the acceptance criterion
    for direction d-ae60ffe3 requires: operator bypasses must resolve exact-candidate
    CI rather than relying on branch protection alone.
    """
    project, version = await _make_project_and_version(db_session, phase="stabilize")
    candidate = "facec0de" * 5

    # Baseline GitHub mocks shared by both bypass paths.
    mocker.patch(
        "app.github.get_repo_installation_token",
        new=AsyncMock(return_value="gh-token"),
    )
    mocker.patch(
        "app.github.get_branch_sha",
        new=_branch_sha_mock(automain=candidate, main="mainnew" * 5 + "00000"),
    )
    mocker.patch(
        "app.github.list_repo_tree_paths_at_ref",
        new=AsyncMock(
            return_value=[{"path": "backend/app/main.py", "size": 10, "sha": "b1"}]
        ),
    )
    promote_mock = mocker.patch(
        "app.release_qualifier.promote",
        new=AsyncMock(return_value={"status": "promoted"}),
    )

    # --- CI not green: force_promote is blocked ---
    mocker.patch.object(
        rq,
        "_resolve_ci_at_finalize",
        new=AsyncMock(
            return_value=(False, {"state": rq._CI_RED, "resolved_sha": candidate, "detail": "ci red for test"})
        ),
    )

    force_result = await rq.force_promote_current_version(
        db_session,
        project.id,
        actor="op",
        reason="prod incident — must ship",
        session_factory=_shared_session_factory(db_session),
    )

    assert force_result["status"] == "ci_not_green", (
        f"force_promote must return ci_not_green when CI is red; got {force_result['status']!r}"
    )
    promote_mock.assert_not_called()

    # --- CI not green: hotfix_promote is blocked (even after scope passes) ---
    mocker.patch(
        "app.github_refs.get_changed_files_with_patches_between_refs",
        new=_hotfix_changed_files_mock(
            [("backend/app/release_scoring.py", "@@ -1 +1 @@\n-a\n+b\n")]
        ),
    )

    hotfix_result = await rq.hotfix_promote_current_version(
        db_session,
        project.id,
        actor="op",
        reason="machinery fix",
        scope_modules=["backend/app/release_scoring.py"],
        session_factory=_shared_session_factory(db_session),
    )

    assert hotfix_result["status"] == "ci_not_green", (
        f"hotfix_promote must return ci_not_green when CI is red; got {hotfix_result['status']!r}"
    )
    promote_mock.assert_not_called()

    # Version remains open — no branch was cut, no PR was opened.
    await db_session.refresh(version)
    assert version.status == "open", (
        "version must remain open when operator bypass is blocked by CI"
    )


@pytest.mark.asyncio
async def test_promote_writes_claim_before_pr_io(db_session, mocker):
    """promote() must persist a promotion CLAIM under the row lock BEFORE it
    releases the lock for its GitHub I/O.

    Without the claim, a concurrent ``run_qualification`` on a newer HEAD
    acquires the freed lock during the 30-120s I/O window, sees no
    ``promotion_pending``, sails past Guard 2b, re-cuts, and clobbers the
    in-flight promotion's record (the PR #12492 incident). This asserts the fix
    by observing the COMMITTED DB row at the moment ``create_pull_request`` runs:
    the claim (``promotion_pending.claiming``) must already be durable then, so a
    concurrent qualifier in that window would see it and refuse."""
    _project, version = await _make_project_and_version(db_session)

    mocker.patch(
        "app.github.get_repo_installation_token",
        new=AsyncMock(return_value="gh-token"),
    )
    mocker.patch(
        "app.github.get_branch_sha",
        new=_branch_sha_mock(main="mainnew" * 5 + "00000"),
    )
    _patch_promote_github_defaults(mocker)
    mocker.patch("app.github.create_branch", new=_create_branch_mock())

    captured: dict = {}

    async def _capture_claim_then_open_pr(*args, **kwargs):
        # Re-read the version row from the DB (populate_existing bypasses the
        # identity-map cache) to observe the COMMITTED state at PR-open time —
        # this is exactly what a concurrent qualifier's FOR UPDATE read sees.
        obj = (
            (
                await db_session.execute(
                    select(rv.ReleaseVersion)
                    .where(rv.ReleaseVersion.id == version.id)
                    .execution_options(populate_existing=True)
                )
            )
            .scalars()
            .first()
        )
        captured["promotion_pending"] = (obj.last_qualification or {}).get(
            "promotion_pending"
        )
        return {"number": 5, "html_url": "http://pr/5"}

    mocker.patch(
        "app.github.create_pull_request",
        new=AsyncMock(side_effect=_capture_claim_then_open_pr),
    )
    mocker.patch(
        "app.github.merge_pull_request",
        new=AsyncMock(return_value={"merged": True, "message": "Merged"}),
    )

    result = await rq.promote(
        db_session, version, "1234" * 10, composite={"confidence": 0.88}
    )
    await db_session.commit()

    assert result["status"] == "promoted"
    # The claim was durable in the committed DB row when the PR I/O ran, so a
    # concurrent re-qualify in that window would have seen it and refused.
    assert captured["promotion_pending"] is not None, (
        "no promotion_pending was committed before create_pull_request ran — "
        "the claim must be written under-lock before the I/O"
    )
    assert captured["promotion_pending"]["claiming"] is True
    assert captured["promotion_pending"]["candidate_sha"] == "1234" * 10


@pytest.mark.asyncio
async def test_promote_blocks_on_dual_alembic_head(db_session, mocker):
    """The pre-merge Alembic dual-head guard HOLDS a promotion whose added
    migration reuses a revision id already on the promotion target.

    A hotfix migration that landed on ``main`` (the promotion target) AFTER
    qualification but BEFORE this merge, plus a feature migration on the
    integration branch that independently reused the same ``NNNN`` slot, would
    produce a dual head on ``main``. promote() must NOT call the merge API; it
    records a retryable ``migration_guard_blocked`` failure (version stays
    open + stabilize, main untouched, no v2 opened) and best-effort deletes the
    release branch."""
    project, version = await _make_project_and_version(db_session)

    mocker.patch(
        "app.github.get_repo_installation_token",
        new=AsyncMock(return_value="gh-token"),
    )
    mocker.patch("app.github.get_branch_sha", new=_branch_sha_mock())
    _is_anc, delete_branch_mock = _patch_promote_github_defaults(mocker)
    mocker.patch("app.github.create_branch", new=_create_branch_mock())
    mocker.patch(
        "app.github.create_pull_request",
        new=AsyncMock(return_value={"number": 77, "html_url": "http://pr/77"}),
    )
    merge_mock = mocker.patch(
        "app.github.merge_pull_request",
        new=AsyncMock(return_value={"merged": True}),
    )

    # Drive the REAL combined-chain validator: the promotion PR adds migration
    # revision 0042, which already exists on the promotion target → collision.
    # The per-file patch is included inline so _collect_added_migration_sources
    # can reconstruct the source without a fallback API call.
    migration_path = "backend/alembic/versions/0042_feature.py"
    mocker.patch(
        "app.github.get_pr_files",
        new=AsyncMock(
            return_value=[
                {
                    "filename": migration_path,
                    "patch": "+revision = '0042'\n+down_revision = '0041'\n",
                }
            ]
        ),
    )
    mocker.patch(
        "app.github.list_versions_at_ref",
        new=AsyncMock(return_value=["0041_base.py", "0042_hotfix.py"]),
    )

    result = await rq.promote(
        db_session,
        version,
        "abcd" * 10,
        composite={"confidence": 0.95},
        base_record={"candidate_sha": "abcd" * 10},
    )
    await db_session.commit()

    # The merge API was NEVER called — the guard held the promotion.
    merge_mock.assert_not_called()
    assert result["status"] == "promotion_failed"
    assert result["error"] == "migration_guard_blocked"

    await db_session.refresh(version)
    # Retryable hold: version stays open + stabilize, main untouched.
    assert version.status == "open"
    assert version.phase == "stabilize"
    assert version.last_qualification["status"] == "promotion_failed"
    assert version.last_qualification["error"] == "migration_guard_blocked"
    # The reason names the colliding revision.
    assert "0042" in version.last_qualification["detail"]
    # The release branch was cleaned up since the merge never ran.
    delete_branch_mock.assert_awaited()

    # No next version was opened — the qualified version was NOT discarded.
    open_version = await rv.current_open_version(db_session, project.id)
    assert open_version is not None
    assert open_version.version_number == 1


@pytest.mark.asyncio
async def test_promote_guard_block_skips_merge(db_session, mocker):
    """A blocking guard verdict prevents promote() from reaching the merge."""
    _project, version = await _make_project_and_version(db_session)

    mocker.patch(
        "app.github.get_repo_installation_token",
        new=AsyncMock(return_value="gh-token"),
    )
    mocker.patch("app.github.get_branch_sha", new=_branch_sha_mock())
    _patch_promote_github_defaults(mocker)
    mocker.patch("app.github.create_branch", new=_create_branch_mock())
    mocker.patch(
        "app.github.create_pull_request",
        new=AsyncMock(return_value={"number": 77, "html_url": "http://pr/77"}),
    )
    merge_mock = mocker.patch(
        "app.github.merge_pull_request",
        new=AsyncMock(return_value={"merged": True}),
    )
    mocker.patch(
        "app.rollup_migration_validation._validate_rollup_migration_chain",
        new=AsyncMock(return_value=("block", "dual-head: duplicate revision")),
    )

    result = await rq.promote(
        db_session,
        version,
        "abcd" * 10,
        composite={"confidence": 0.95},
        base_record={"candidate_sha": "abcd" * 10},
    )
    await db_session.commit()

    merge_mock.assert_not_called()
    assert result["status"] == "promotion_failed"
    assert result["error"] == "migration_guard_blocked"

    await db_session.refresh(version)
    assert version.status == "open"
    assert version.phase == "stabilize"
    assert version.last_qualification["status"] == "promotion_failed"
    assert version.last_qualification["error"] == "migration_guard_blocked"


@pytest.mark.asyncio
async def test_promote_defers_to_managed_check_wait_on_migration_metadata_unfetchable(
    db_session, mocker
):
    """A transient inability to fetch the migration metadata HOLDS the promotion
    in ``managed_check_wait`` (retryable) rather than recording a terminal
    failure or merging blind OR fail-safe.

    The bool wrapper used to collapse ``defer`` → ``migration_guard_blocked``,
    which for a release rollup means a spurious terminal failure that bounces
    the version to stabilize and never lands. With the 3-way verdict, ``defer``
    enters managed_check_wait: the release branch is KEPT so the reconciler can
    re-validate and retry the merge once metadata is available."""
    _project, version = await _make_project_and_version(db_session)

    mocker.patch(
        "app.github.get_repo_installation_token",
        new=AsyncMock(return_value="gh-token"),
    )
    mocker.patch("app.github.get_branch_sha", new=_branch_sha_mock())
    _is_anc, delete_branch_mock = _patch_promote_github_defaults(mocker)
    mocker.patch("app.github.create_branch", new=_create_branch_mock())
    mocker.patch(
        "app.github.create_pull_request",
        new=AsyncMock(return_value={"number": 78, "html_url": "http://pr/78"}),
    )
    merge_mock = mocker.patch(
        "app.github.merge_pull_request",
        new=AsyncMock(return_value={"merged": True}),
    )
    # The guard returns a defer verdict — metadata temporarily unavailable.
    mocker.patch(
        "app.rollup_migration_validation._validate_rollup_migration_chain",
        new=AsyncMock(return_value=("defer", "502 from GitHub")),
    )

    result = await rq.promote(
        db_session,
        version,
        "abcd" * 10,
        composite={"confidence": 0.91},
        base_record={"candidate_sha": "abcd" * 10},
    )
    await db_session.commit()

    # The merge never ran, the branch was NOT deleted (reconciler needs it),
    # and NO terminal failure was recorded — the version is held pending.
    merge_mock.assert_not_called()
    delete_branch_mock.assert_not_awaited()
    assert result["status"] == "pending_managed_check_wait"
    assert result["managed_check_wait"] is True

    await db_session.refresh(version)
    # Version stays open; no terminal failure recorded.
    assert version.status == "open"
    assert version.last_qualification["status"] == "pending"
    pending = version.last_qualification["promotion_pending"]
    assert pending["managed_check_wait"] is True
    assert pending["pr_number"] == 78
    # The release branch was NOT deleted — the reconciler needs it.
    delete_branch_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_promote_defer_migration_verdict_enters_managed_check_wait(
    db_session, mocker
):
    """promote() with a 'defer' migration guard verdict enters managed_check_wait
    with the release branch KEPT and NO terminal failure recorded.

    A 'defer' means migration safety is temporarily unknown (e.g. the PR file
    list could not be fetched). The correct response is to hold in the durable
    managed_check_wait sub-state so the reconciler re-runs the guard on the next
    sweep — NOT to record a terminal promotion_failed. The release branch must
    stay open (not deleted) because the reconciler needs it to retry the merge.
    """
    _project, version = await _make_project_and_version(db_session)
    candidate = "d3fer001" * 5

    mocker.patch(
        "app.github.get_repo_installation_token",
        new=AsyncMock(return_value="gh-token"),
    )
    mocker.patch("app.github.is_ancestor_of", new=AsyncMock(return_value=False))
    mocker.patch("app.github.get_branch_sha", new=_branch_sha_mock())
    mocker.patch("app.github.create_branch", new=_create_branch_mock())
    mocker.patch(
        "app.github.create_pull_request",
        new=AsyncMock(return_value={"number": 91, "html_url": "http://pr/91"}),
    )
    merge_mock = mocker.patch(
        "app.github.merge_pull_request",
        new=AsyncMock(return_value={"merged": True}),
    )
    delete_branch_mock = mocker.patch(
        "app.github.delete_branch", new=AsyncMock(return_value=True)
    )
    # Guard returns defer — metadata temporarily unavailable (not a hard block).
    mocker.patch(
        "app.rollup_migration_validation._validate_rollup_migration_chain",
        new=AsyncMock(
            return_value=("defer", "could not list PR files: 503 Service Unavailable")
        ),
    )

    result = await rq.promote(
        db_session,
        version,
        candidate,
        composite={"confidence": 0.88},
        base_record={"candidate_sha": candidate},
    )
    await db_session.commit()

    # The merge API was never called.
    merge_mock.assert_not_called()
    # Result is a pending managed_check_wait, NOT a terminal failure.
    assert result["status"] == "pending_managed_check_wait"
    assert result["managed_check_wait"] is True
    assert result["pr_number"] == 91
    assert result["pr_url"] == "http://pr/91"

    await db_session.refresh(version)
    # Version stays open; last_qualification is pending (not promotion_failed).
    assert version.status == "open"
    assert version.last_qualification["status"] == "pending"
    pending = version.last_qualification["promotion_pending"]
    assert pending["managed_check_wait"] is True
    assert pending["pr_number"] == 91
    # No terminal failure record.
    assert "error" not in version.last_qualification
    # Release branch NOT deleted — the reconciler needs it.
    delete_branch_mock.assert_not_called()

    # No next version was opened — the version is still pending, not resolved.
    open_version = await rv.current_open_version(db_session, version.project_id)
    assert open_version is not None
    assert open_version.version_number == version.version_number


@pytest.mark.asyncio
async def test_promote_proceeds_when_no_migrations_touched(db_session, mocker):
    """A healthy promotion whose PR touches NO migration files passes the guard
    (the default ``get_pr_files`` → []) and merges exactly as before."""
    _project, version = await _make_project_and_version(db_session)

    mocker.patch(
        "app.github.get_repo_installation_token",
        new=AsyncMock(return_value="gh-token"),
    )
    merge_sha = "mainnew" * 5 + "00000"
    mocker.patch("app.github.get_branch_sha", new=_branch_sha_mock(main=merge_sha))
    _patch_promote_github_defaults(mocker)
    mocker.patch("app.github.create_branch", new=_create_branch_mock())
    mocker.patch(
        "app.github.create_pull_request",
        new=AsyncMock(return_value={"number": 79, "html_url": "http://pr/79"}),
    )
    merge_mock = mocker.patch(
        "app.github.merge_pull_request",
        new=AsyncMock(return_value={"merged": True, "message": "Merged"}),
    )

    await rq.promote(
        db_session,
        version,
        "1234" * 10,
        composite={"confidence": 0.9},
    )
    await db_session.commit()

    # The guard passed (no migrations) — the merge ran and the promotion landed.
    merge_mock.assert_awaited_once()


@pytest.mark.asyncio
async def test_promote_target_change_after_evidence_defers_without_merge(
    db_session, mocker
):
    """Base movement between migration evidence and PUT re-enters managed wait."""

    _project, version = await _make_project_and_version(db_session)
    candidate = "c" * 40
    initial_pr = {
        "number": 71,
        "state": "open",
        "merged": False,
        "head": {"ref": "release-v1", "sha": candidate},
        "base": {"ref": "main", "sha": "base0001"},
    }
    changed_pr = {
        **initial_pr,
        "base": {"ref": "main", "sha": "base0002"},
    }
    mocker.patch(
        "app.github.get_repo_installation_token",
        new=AsyncMock(return_value="gh-token"),
    )
    mocker.patch("app.github.get_branch_sha", new=_branch_sha_mock())
    _patch_promote_github_defaults(mocker)
    mocker.patch("app.github.create_branch", new=_create_branch_mock())
    mocker.patch(
        "app.github.create_pull_request",
        new=AsyncMock(return_value={"number": 71, "html_url": "http://pr/71"}),
    )
    mocker.patch(
        "app.github.get_pull_request",
        new=AsyncMock(side_effect=[initial_pr, changed_pr]),
    )
    mocker.patch(
        "app.github.get_pr_files",
        new=AsyncMock(return_value=[{"filename": "backend/app/foo.py"}]),
    )
    merge = mocker.patch("app.github.merge_pull_request", new=AsyncMock())

    result = await rq.promote(
        db_session,
        version,
        candidate,
        composite={"confidence": 0.9},
        base_record={"candidate_sha": candidate, "confidence": 0.9},
    )
    await db_session.commit()

    assert result["status"] == "pending_managed_check_wait"
    merge.assert_not_awaited()
    await db_session.refresh(version)
    assert version.last_qualification["status"] == "pending"
    assert version.last_qualification["promotion_pending"]["pr_number"] == 71


@pytest.mark.asyncio
async def test_promote_single_branch_repo_is_inert_lts_inactive(db_session, mocker):
    """On a single-branch repo (integration branch == promotion target) promote()
    must NOT open or merge a release PR into the very branch work lands on. It
    mirrors the back-merge ``lts_inactive`` guard: record an inert, already-landed
    success (the candidate is already live) and advance the version, with zero
    GitHub I/O."""
    project = Project(
        name="RQ-SingleBranch",
        user_id="rq-user",
        github_owner="acme",
        github_repo="solo",
        # Single-branch model: the integration branch IS the promotion target.
        integration_branch="trunk",
        promotion_target="trunk",
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)
    version = await rv.open_version(db_session, project.id, version_number=1)
    await db_session.commit()
    await db_session.refresh(version)

    branch_mock = mocker.patch("app.github.create_branch", new=AsyncMock())
    pr_mock = mocker.patch("app.github.create_pull_request", new=AsyncMock())
    merge_mock = mocker.patch("app.github.merge_pull_request", new=AsyncMock())

    result = await rq.promote(
        db_session,
        version,
        "1234" * 10,
        composite={"confidence": 0.91},
    )
    await db_session.commit()

    assert result["status"] == "promoted"
    assert result["next_version_number"] == 2
    # No GitHub I/O whatsoever — the guard short-circuits before any branch/PR work.
    branch_mock.assert_not_called()
    pr_mock.assert_not_called()
    merge_mock.assert_not_called()

    await db_session.refresh(version)
    assert version.status == "promoted"
    assert version.promoted_sha == "1234" * 10
    assert version.last_qualification.get("lts_inactive") is True
    assert version.last_qualification.get("already_landed") is True


@pytest.mark.asyncio
async def test_promote_end_to_end_over_custom_branch_pair(db_session, mocker):
    """End-to-end qualify->promote->merge for a NON-self project whose
    integration/promotion branches are a CUSTOM pair (trunk -> release), not the
    self-repo automain/main fixture every other promote success test uses.

    Per-project branch resolution (audit C4): the release PR must open
    release-v{N} -> the project's promotion_target ('release', NOT 'main'); the
    idempotency ancestry check and the post-merge HEAD re-resolution must both
    use 'release'; and the release tag message must reference the custom
    integration -> promotion pair.  This proves the promote tail honours the
    per-project branch resolver rather than hardcoded literals."""
    project = Project(
        name="RQ-CustomPair",
        user_id="rq-user",
        github_owner="acme",
        github_repo="ext",
        # Custom multi-branch pair — distinct from the automain/main fixture.
        integration_branch="trunk",
        promotion_target="release",
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)
    version = await rv.open_version(db_session, project.id, version_number=1)
    await db_session.commit()
    await db_session.refresh(version)

    mocker.patch(
        "app.github.get_repo_installation_token",
        new=AsyncMock(return_value="gh-token"),
    )
    merge_sha = "release" * 5 + "00000"

    async def _branch_sha(token, owner, repo, branch, **kwargs):
        # promote() re-resolves the promotion target's HEAD after the merge —
        # it MUST ask for 'release', never the hardcoded 'main'.
        assert branch == "release", f"unexpected branch {branch!r}"
        return merge_sha

    mocker.patch("app.github.get_branch_sha", new=AsyncMock(side_effect=_branch_sha))
    is_ancestor_mock, _del_mock = _patch_promote_github_defaults(mocker)
    mocker.patch("app.github.create_branch", new=_create_branch_mock())
    create_mock = mocker.patch(
        "app.github.create_pull_request",
        new=AsyncMock(return_value={"number": 9, "html_url": "http://pr/9"}),
    )
    mocker.patch(
        "app.github.merge_pull_request",
        new=AsyncMock(
            return_value={"merged": True, "message": "Merged", "sha": merge_sha}
        ),
    )
    tag_mock = mocker.patch("app.github.create_tag", new=AsyncMock(return_value={}))

    result = await rq.promote(
        db_session, version, "abcd" * 10, composite={"confidence": 0.93}
    )
    await db_session.commit()

    assert result["status"] == "promoted"
    assert result["next_version_number"] == 2
    # The PR opens release-v1 -> the per-project promotion_target, NOT 'main'.
    assert create_mock.call_args.kwargs["head"] == "release-v1"
    assert create_mock.call_args.kwargs["base"] == "release"
    # The idempotency ancestry pre-check used the custom promotion target
    # (is_ancestor_of(token, owner, repo, candidate_sha, promotion_target, ...)).
    assert is_ancestor_mock.call_args.args[4] == "release"

    await db_session.refresh(version)
    assert version.status == "promoted"
    # promoted_sha is the immutable merge receipt returned by GitHub.
    assert version.promoted_sha == merge_sha
    # The release tag message references the custom integration -> promotion pair.
    assert "trunk → release" in tag_mock.call_args.kwargs["message"]


@pytest.mark.asyncio
async def test_promote_opens_next_version_with_null_goal(db_session, mocker):
    """The next version (N+1) opened by a successful promotion starts with goal=None.

    The strategic agent will author the objective in its first cycle after
    version-open (via new_version_goal output field), replacing the old
    auto-copy of current_focus.
    """
    project, version = await _make_project_and_version(db_session)
    # current_focus is set but should NOT be copied to the next version's goal.
    focus = "Promote v1 and stabilize the qualifier loop."
    project.problem_model = {"intent": {"current_focus": focus}}
    db_session.add(project)
    await db_session.commit()

    mocker.patch(
        "app.github.get_repo_installation_token",
        new=AsyncMock(return_value="gh-token"),
    )
    mocker.patch("app.github.get_branch_sha", new=_branch_sha_mock())
    _patch_promote_github_defaults(mocker)
    mocker.patch("app.github.create_branch", new=_create_branch_mock())
    mocker.patch(
        "app.github.create_pull_request",
        new=AsyncMock(return_value={"number": 7, "html_url": "http://pr/7"}),
    )
    mocker.patch(
        "app.github.merge_pull_request",
        new=AsyncMock(return_value={"merged": True, "message": "Merged"}),
    )

    result = await rq.promote(
        db_session, version, "1234" * 10, composite={"confidence": 0.95}
    )
    await db_session.commit()

    assert result["status"] == "promoted"
    next_version = await rv.current_open_version(db_session, project.id)
    assert next_version is not None
    assert next_version.version_number == 2
    # goal is None — strategist authors it in the first cycle after version-open.
    assert next_version.goal is None


@pytest.mark.asyncio
async def test_promote_empty_diff_is_noop_success(db_session, mocker):
    """An empty diff (release branch == main) is a degenerate but valid success
    — create_pull_request returns {empty_diff: True}; the version still
    advances and the candidate SHA is recorded (no new merge commit exists)."""
    _project, version = await _make_project_and_version(db_session)

    mocker.patch(
        "app.github.get_repo_installation_token",
        new=AsyncMock(return_value="gh-token"),
    )
    mocker.patch(
        "app.github.get_branch_sha",
        new=_branch_sha_mock(),
    )
    _patch_promote_github_defaults(mocker)
    mocker.patch(
        "app.github.create_branch",
        new=_create_branch_mock(),
    )
    mocker.patch(
        "app.github.create_pull_request",
        new=AsyncMock(
            return_value={"empty_diff": True, "merged": True, "html_url": ""}
        ),
    )
    merge_mock = mocker.patch(
        "app.github.merge_pull_request",
        new=AsyncMock(return_value={"merged": True}),
    )

    result = await rq.promote(db_session, version, "0000" * 10)
    await db_session.commit()

    assert result["status"] == "promoted"
    # No merge attempted on an empty diff.
    merge_mock.assert_not_called()
    await db_session.refresh(version)
    assert version.status == "promoted"
    # Empty-diff path keeps the candidate SHA (automain == main).
    assert version.promoted_sha == "0000" * 10


@pytest.mark.asyncio
async def test_promote_succeeds_even_when_automain_advanced(db_session, mocker):
    """Audit C4: automain advancing after qualification is now HARMLESS — there
    is no staleness guard.  promote() pins the PR to a release-v{N} branch
    created at the exact candidate SHA, so even with automain far ahead the
    promotion still succeeds and the qualified commit (not live automain) is
    what lands."""
    _project, version = await _make_project_and_version(db_session)

    candidate = "qua11f1e" * 5  # the commit the children actually scored
    merge_sha = "mergedup" * 5

    mocker.patch(
        "app.github.get_repo_installation_token",
        new=AsyncMock(return_value="gh-token"),
    )
    # automain HEAD has moved far past the candidate — irrelevant now, since
    # promote() never reads automain (the staleness guard is gone).  The mock
    # answers automain only to prove promote() does not consult it.
    automain_mock = _branch_sha_mock(automain="movedon0" * 5, main=merge_sha)
    mocker.patch("app.github.get_branch_sha", new=automain_mock)
    _patch_promote_github_defaults(mocker)
    branch_mock = mocker.patch(
        "app.github.create_branch",
        new=_create_branch_mock(),
    )
    create_mock = mocker.patch(
        "app.github.create_pull_request",
        new=AsyncMock(return_value={"number": 1, "html_url": "http://pr/1"}),
    )
    merge_mock = mocker.patch(
        "app.github.merge_pull_request",
        new=AsyncMock(
            return_value={"merged": True, "message": "Merged", "sha": merge_sha}
        ),
    )

    result = await rq.promote(
        db_session,
        version,
        candidate,  # the candidate snapshot — automain has since moved
        composite={"confidence": 0.95},
    )
    await db_session.commit()

    # Promotion SUCCEEDS — automain moving is expected and harmless.
    assert result["status"] == "promoted"

    # The release branch was created at the EXACT candidate SHA.
    branch_mock.assert_called_once()
    assert branch_mock.call_args.args[4] == candidate
    # The PR head is the pinned release branch, not automain.
    assert create_mock.call_args.kwargs["head"] == "release-v1"
    merge_mock.assert_called_once()

    # promote() never resolves the automain branch — only main (post-merge).
    resolved_branches = {call.args[3] for call in automain_mock.call_args_list}
    assert "automain" not in resolved_branches

    await db_session.refresh(version)
    assert version.status == "promoted"
    # last_qualification records the scored candidate; promoted_sha is the
    # immutable merge receipt.
    assert version.last_qualification["candidate_sha"] == candidate
    assert version.promoted_sha == merge_sha


@pytest.mark.asyncio
async def test_promote_recreates_stale_release_branch(db_session, mocker):
    """Audit C4: if the release-v{N} branch already exists from a prior failed
    attempt and points at a DIFFERENT (stale) SHA, promote() deletes and
    recreates it at the current candidate SHA so the PR always carries exactly
    the qualified commit."""
    _project, version = await _make_project_and_version(db_session)

    candidate = "freshca0" * 5  # the commit we want to promote now
    stale_sha = "0ldbra0c" * 5  # what an existing release branch points at

    mocker.patch(
        "app.github.get_repo_installation_token",
        new=AsyncMock(return_value="gh-token"),
    )
    mocker.patch(
        "app.github.get_branch_sha",
        new=_branch_sha_mock(main="merged00" * 5),
    )
    # Candidate not yet on main — a real promotion runs.
    mocker.patch("app.github.is_ancestor_of", new=AsyncMock(return_value=False))
    mocker.patch(
        "app.github.get_pull_requests_for_head_branch",
        new=AsyncMock(return_value=[]),
    )
    mocker.patch(
        "app.github.get_pull_request",
        new=AsyncMock(
            return_value={
                "number": 3,
                "state": "open",
                "merged": False,
                "head": {"ref": "release-v1", "sha": candidate},
                "base": {"ref": "main", "sha": "base0001"},
            }
        ),
    )
    mocker.patch(
        "app.github.get_pr_files",
        new=AsyncMock(return_value=[{"filename": "backend/app/foo.py"}]),
    )
    mocker.patch("app.github.create_tag", new=AsyncMock(return_value={}))

    # First create_branch call: the branch already exists at the STALE sha
    # (GitHub's 422 idempotent path returns the existing ref).  Second call
    # (after delete) creates it cleanly at the candidate.
    create_calls: list[str] = []

    async def _create_branch(token, owner, repo, branch_name, sha):
        create_calls.append(sha)
        if len(create_calls) == 1:
            # Simulate "branch already exists, pointing somewhere stale".
            return {
                "ref": f"refs/heads/{branch_name}",
                "object": {"sha": stale_sha},
            }
        return {"ref": f"refs/heads/{branch_name}", "object": {"sha": sha}}

    branch_mock = mocker.patch(
        "app.github.create_branch", new=AsyncMock(side_effect=_create_branch)
    )
    delete_mock = mocker.patch(
        "app.github.delete_branch", new=AsyncMock(return_value=True)
    )
    mocker.patch(
        "app.github.create_pull_request",
        new=AsyncMock(return_value={"number": 3, "html_url": "http://pr/3"}),
    )
    mocker.patch(
        "app.github.merge_pull_request",
        new=AsyncMock(
            return_value={
                "merged": True,
                "message": "Merged",
                "sha": "merged00" * 5,
            }
        ),
    )

    result = await rq.promote(
        db_session, version, candidate, composite={"confidence": 0.9}
    )
    await db_session.commit()

    assert result["status"] == "promoted"

    # delete_branch is called twice on this path: once to drop the STALE branch
    # before recreate, and once as the best-effort post-merge cleanup (R4-H2).
    assert delete_mock.call_count == 2
    # Every delete targeted the release-v1 branch.
    assert all(c.args[3] == "release-v1" for c in delete_mock.call_args_list)
    assert branch_mock.call_count == 2
    # Both create attempts targeted the candidate SHA — never the stale one.
    assert create_calls == [candidate, candidate]


@pytest.mark.asyncio
async def test_promote_release_branch_creation_failure_is_blocked(db_session, mocker):
    """Audit C4: a failure creating the release branch blocks the promotion —
    no PR is opened, the version returns to stabilize, and main is untouched."""
    project, version = await _make_project_and_version(db_session)

    from fastapi import HTTPException

    mocker.patch(
        "app.github.get_repo_installation_token",
        new=AsyncMock(return_value="gh-token"),
    )
    # Candidate not on main (idempotency pre-check returns False) so promote()
    # proceeds to the branch-create step, which fails.
    _patch_promote_github_defaults(mocker)
    mocker.patch(
        "app.github.create_branch",
        new=AsyncMock(side_effect=HTTPException(status_code=502, detail="GitHub 502")),
    )
    create_mock = mocker.patch(
        "app.github.create_pull_request",
        new=AsyncMock(return_value={"number": 1, "html_url": "http://pr/1"}),
    )
    merge_mock = mocker.patch(
        "app.github.merge_pull_request",
        new=AsyncMock(return_value={"merged": True}),
    )

    result = await rq.promote(
        db_session, version, "abcd" * 10, composite={"confidence": 0.9}
    )
    await db_session.commit()

    assert result["status"] == "promotion_failed"
    assert result["error"] == "release_branch_failed"

    # No PR opened, no merge — the branch failure blocks before either.
    create_mock.assert_not_called()
    merge_mock.assert_not_called()

    await db_session.refresh(version)
    # Version stays open and returns to stabilize — a failed promotion blocks.
    assert version.status == "open"
    assert version.phase == "stabilize"
    assert version.last_qualification["status"] == "promotion_failed"
    assert version.last_qualification["error"] == "release_branch_failed"

    # No next version opened.
    open_v = await rv.current_open_version(db_session, project.id)
    assert open_v is not None
    assert open_v.version_number == 1


# ---------------------------------------------------------------------------
# INCIDENT 2026-08-23 — "the engine never asks me for something it already has"
#
# `force-promote --candidate-sha 4b6681871` (9 chars) returned
#   {"status": "promotion_failed", "error": "release_branch_failed", "conflict": false}
# and `./blueprint status` showed  "detail": null.
#
# GitHub had answered "Object does not exist" and the engine held that string
# the whole time.  Three defects compounded; these tests pin the two that are
# observable end-to-end from promote() and force_promote_current_version().
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_promotion_failure_detail_reaches_the_operator_surface(
    db_session, mocker
):
    """A branch-creation failure must carry GitHub's OWN reason all the way to
    ``./blueprint status``.

    The recorded detail used to be ``str(exc)[:300]``, which is the EMPTY STRING
    for a keyword-constructed ``HTTPException`` — so ``_record_promotion_failure``'s
    ``if detail:`` guard dropped the key and ``summarize_qualification_state``
    reported ``detail: None``.  See testing/test_failure_reason.py for the
    dependency-level pin of that behaviour.
    """
    from fastapi import HTTPException

    _project, version = await _make_project_and_version(db_session)

    mocker.patch(
        "app.github.get_repo_installation_token",
        new=AsyncMock(return_value="gh-token"),
    )
    _patch_promote_github_defaults(mocker)
    # EXACTLY what github_refs.create_branch now raises for GitHub's
    # "Object does not exist" 422 — keyword-constructed, like every raise site.
    mocker.patch(
        "app.github.create_branch",
        new=AsyncMock(
            side_effect=HTTPException(
                status_code=500,
                detail=(
                    "Failed to create branch 'release-v1' at '4b6681871' on "
                    "GitHub (GitHub 422): Object does not exist"
                ),
            )
        ),
    )

    result = await rq.promote(
        db_session, version, "abcd" * 10, composite={"confidence": 0.9}
    )
    await db_session.commit()

    assert result["status"] == "promotion_failed"
    assert result["error"] == "release_branch_failed"
    # 1. The operator lever prints this dict straight back — it must not be a
    #    state with no reason attached.
    assert "Object does not exist" in result["detail"]

    # 2. The persisted record carries it.
    await db_session.refresh(version)
    lq = version.last_qualification
    assert lq["error"] == "release_branch_failed"
    assert "Object does not exist" in lq["detail"]

    # 3. ...and it survives the summarizer that `./blueprint status` reads.
    summary = rv.summarize_qualification_state(lq)
    assert summary["state"] == "promotion_failed"
    assert summary["detail"] is not None, (
        "a blocked promotion that reports no reason is the whole defect"
    )
    assert "Object does not exist" in summary["detail"]


@pytest.mark.asyncio
async def test_force_promote_resolves_an_abbreviated_candidate_sha(
    db_session, mocker
):
    """REGRESSION for the incident: a 9-character ``--candidate-sha`` promotes.

    ``git log`` prints abbreviated shas, so that is what an operator types.  The
    engine holds the token, owner and repo, so it resolves the ref itself
    instead of failing four layers deeper inside GitHub's ref-create API.
    """
    project, version = await _make_project_and_version(db_session, phase="stabilize")

    full = "4b6681871a41b1b8955fc66b67f6b2790f692715"
    abbreviated = "4b6681871"
    merge_sha = "mainnew" * 5 + "00000"

    mocker.patch(
        "app.github.get_repo_installation_token",
        new=AsyncMock(return_value="gh-token"),
    )
    mocker.patch(
        "app.github.get_branch_sha",
        new=_branch_sha_mock(automain=full, main=merge_sha),
    )
    resolve_mock = mocker.patch(
        "app.github.resolve_commit_sha", new=AsyncMock(return_value=full)
    )
    tree_mock = mocker.patch(
        "app.github.list_repo_tree_paths_at_ref",
        new=AsyncMock(
            return_value=[{"path": "backend/app/main.py", "size": 10, "sha": "b1"}]
        ),
    )
    _patch_promote_github_defaults(mocker)
    branch_mock = mocker.patch("app.github.create_branch", new=_create_branch_mock())
    mocker.patch(
        "app.github.create_pull_request",
        new=AsyncMock(return_value={"number": 7, "html_url": "http://pr/7"}),
    )
    mocker.patch(
        "app.github.merge_pull_request",
        new=AsyncMock(
            return_value={"merged": True, "message": "Merged", "sha": merge_sha}
        ),
    )
    ci_mock = mocker.patch.object(
        rq,
        "_resolve_ci_at_finalize",
        new=AsyncMock(
            return_value=(
                True,
                {"state": "green", "resolved_sha": full, "detail": "ci green"},
            )
        ),
    )

    result = await rq.force_promote_current_version(
        db_session,
        project.id,
        candidate_sha=abbreviated,
        actor="operator-1",
        session_factory=_shared_session_factory(db_session),
    )

    assert result["status"] == "promoted", result

    # The operator's ref was resolved ONCE, at the entry point...
    assert resolve_mock.await_count == 1
    assert resolve_mock.call_args.args[3] == abbreviated
    # ...so every downstream consumer keyed off the FULL sha, never the
    # abbreviation that GitHub's ref-create API rejects.
    assert tree_mock.call_args.args[3] == full
    assert ci_mock.call_args.args[2] == full
    assert branch_mock.call_args.args[4] == full

    await db_session.refresh(version)
    assert version.status == "promoted"


@pytest.mark.asyncio
async def test_force_promote_reports_an_unresolvable_candidate_sha_precisely(
    db_session, mocker
):
    """A ref the engine genuinely cannot resolve is an operator error — and it
    is reported HERE, with GitHub's own message, before any branch is cut."""
    from fastapi import HTTPException

    project, _version = await _make_project_and_version(db_session, phase="stabilize")

    mocker.patch(
        "app.github.get_repo_installation_token",
        new=AsyncMock(return_value="gh-token"),
    )
    mocker.patch(
        "app.github.resolve_commit_sha",
        new=AsyncMock(
            side_effect=HTTPException(
                status_code=400,
                detail=(
                    "Could not resolve 'nope' to a full 40-character commit sha "
                    "(GitHub 422: No commit found for SHA: nope)."
                ),
            )
        ),
    )
    branch_mock = mocker.patch("app.github.create_branch", new=_create_branch_mock())

    result = await rq.force_promote_current_version(
        db_session,
        project.id,
        candidate_sha="nope",
        actor="operator-1",
        session_factory=_shared_session_factory(db_session),
    )

    assert result["status"] == "sha_error"
    assert "No commit found for SHA" in result["detail"], (
        "the operator must be told WHY, not just that it failed"
    )
    branch_mock.assert_not_called()


@pytest.mark.asyncio
async def test_force_promote_does_not_re_resolve_a_full_candidate_sha(
    db_session, mocker
):
    """Guard on the fix's own regression risk: a full 40-hex sha must behave
    exactly as it did before — no extra GitHub round-trip."""
    project, _version = await _make_project_and_version(db_session, phase="stabilize")

    full = "facec0de" * 5
    merge_sha = "mainnew" * 5 + "00000"

    mocker.patch(
        "app.github.get_repo_installation_token",
        new=AsyncMock(return_value="gh-token"),
    )
    mocker.patch(
        "app.github.get_branch_sha",
        new=_branch_sha_mock(automain=full, main=merge_sha),
    )
    request_mock = mocker.patch("app.github._github_request", new=AsyncMock())
    mocker.patch(
        "app.github.list_repo_tree_paths_at_ref",
        new=AsyncMock(
            return_value=[{"path": "backend/app/main.py", "size": 10, "sha": "b1"}]
        ),
    )
    _patch_promote_github_defaults(mocker)
    mocker.patch("app.github.create_branch", new=_create_branch_mock())
    mocker.patch(
        "app.github.create_pull_request",
        new=AsyncMock(return_value={"number": 7, "html_url": "http://pr/7"}),
    )
    mocker.patch(
        "app.github.merge_pull_request",
        new=AsyncMock(
            return_value={"merged": True, "message": "Merged", "sha": merge_sha}
        ),
    )
    mocker.patch.object(
        rq,
        "_resolve_ci_at_finalize",
        new=AsyncMock(
            return_value=(
                True,
                {"state": "green", "resolved_sha": full, "detail": "ci green"},
            )
        ),
    )

    result = await rq.force_promote_current_version(
        db_session,
        project.id,
        candidate_sha=full,
        actor="operator-1",
        session_factory=_shared_session_factory(db_session),
    )

    assert result["status"] == "promoted", result
    # resolve_commit_sha short-circuits on a canonical sha, so the commits
    # endpoint is never called.
    for call in request_mock.call_args_list:
        assert "/commits/" not in str(call), call


@pytest.mark.asyncio
async def test_promote_records_real_merge_sha_not_candidate(db_session, mocker):
    """The merge response SHA wins even if the target advances immediately."""
    _project, version = await _make_project_and_version(db_session)

    candidate = "cand0000" * 5  # 40-char placeholder SHA
    merge_commit = "mergecmt" * 5  # 40-char placeholder SHA
    later_target_tip = "later000" * 5

    mocker.patch(
        "app.github.get_repo_installation_token",
        new=AsyncMock(return_value="gh-token"),
    )
    mocker.patch(
        "app.github.get_branch_sha",
        new=_branch_sha_mock(main=later_target_tip),
    )
    _patch_promote_github_defaults(mocker)
    mocker.patch(
        "app.github.create_branch",
        new=_create_branch_mock(),
    )
    mocker.patch(
        "app.github.create_pull_request",
        new=AsyncMock(return_value={"number": 8, "html_url": "http://pr/8"}),
    )
    mocker.patch(
        "app.github.merge_pull_request",
        new=AsyncMock(
            return_value={
                "merged": True,
                "message": "Merged",
                "sha": merge_commit,
            }
        ),
    )
    tag_mock = mocker.patch(
        "app.github.create_tag",
        new=AsyncMock(return_value={"ref": "refs/tags/v1"}),
    )

    result = await rq.promote(
        db_session, version, candidate, composite={"confidence": 0.9}
    )
    await db_session.commit()

    assert result["status"] == "promoted"
    assert result["promoted_sha"] == merge_commit

    await db_session.refresh(version)
    assert version.promoted_sha == merge_commit
    # last_qualification keeps BOTH: the candidate for traceability and the
    # real promoted_sha.
    assert version.last_qualification["candidate_sha"] == candidate
    assert version.last_qualification["promoted_sha"] == merge_commit

    # The v1 tag is created on the merge commit, NOT the candidate.
    tag_mock.assert_called_once()
    assert tag_mock.call_args.args[3] == "v1"  # tag name
    assert tag_mock.call_args.args[4] == merge_commit  # sha — the merge commit

    # The next version's base_sha is the merge commit too.
    next_version = await rv.current_open_version(db_session, version.project_id)
    assert next_version is not None
    assert next_version.base_sha == merge_commit


@pytest.mark.asyncio
async def test_promote_merged_without_exact_receipt_defers_reconciliation(
    db_session, mocker
):
    """A successful PUT without a merge SHA never guesses from a moving ref."""
    _project, version = await _make_project_and_version(db_session)
    candidate = "fallback" * 5
    initial_pr = {
        "number": 9,
        "state": "open",
        "merged": False,
        "head": {"ref": "release-v1", "sha": candidate},
        "base": {"ref": "main", "sha": "base0001"},
    }
    merged_without_receipt = {
        **initial_pr,
        "state": "closed",
        "merged": True,
        "merge_commit_sha": None,
    }

    mocker.patch(
        "app.github.get_repo_installation_token",
        new=AsyncMock(return_value="gh-token"),
    )
    mocker.patch(
        "app.github.get_branch_sha", new=AsyncMock(return_value="target00" * 5)
    )
    _patch_promote_github_defaults(mocker)
    mocker.patch(
        "app.github.get_pull_request",
        new=AsyncMock(
            side_effect=[initial_pr, initial_pr, merged_without_receipt]
        ),
    )
    mocker.patch(
        "app.github.create_branch",
        new=_create_branch_mock(),
    )
    mocker.patch(
        "app.github.create_pull_request",
        new=AsyncMock(return_value={"number": 9, "html_url": "http://pr/9"}),
    )
    mocker.patch(
        "app.github.merge_pull_request",
        new=AsyncMock(return_value={"merged": True, "message": "Merged"}),
    )
    tag = mocker.patch(
        "app.github.create_tag",
        new=AsyncMock(return_value={"ref": "refs/tags/v1"}),
    )

    result = await rq.promote(
        db_session, version, candidate, composite={"confidence": 0.9}
    )
    await db_session.commit()

    assert result["status"] == "pending_managed_check_wait"
    tag.assert_not_awaited()

    await db_session.refresh(version)
    assert version.status == "open"
    assert version.last_qualification["status"] == "pending"
    assert version.last_qualification["promotion_pending"]["pr_number"] == 9


@pytest.mark.parametrize("merge_reachable", [False, None])
@pytest.mark.asyncio
async def test_exact_promotion_receipt_requires_merge_commit_reachable_from_target(
    db_session, mocker, merge_reachable
):
    """A matching merged PR is not a receipt until its merge SHA is reachable."""

    project, version = await _make_project_and_version(db_session)
    candidate = "candidate" * 5
    merge_sha = "merge-sh" * 5
    target_tip = "later-tip" * 5
    mocker.patch(
        "app.github.get_pull_request",
        new=AsyncMock(
            return_value={
                "number": 9,
                "state": "closed",
                "merged": True,
                "merge_commit_sha": merge_sha,
                "head": {"ref": "release-v1", "sha": candidate},
                "base": {"ref": "main", "sha": "base0001"},
            }
        ),
    )
    mocker.patch("app.github.get_branch_sha", new=AsyncMock(return_value=target_tip))

    async def reachability(_token, _owner, _repo, ancestor, target, **_kwargs):
        assert target == "main"
        if ancestor == merge_sha:
            return merge_reachable
        assert ancestor == candidate
        return True  # candidate ancestry remains corroboration only

    ancestry = mocker.patch(
        "app.github.is_ancestor_of", new=AsyncMock(side_effect=reachability)
    )

    receipt = await rq._resolve_exact_promotion_receipt(
        "tok",
        "acme",
        "widget",
        candidate,
        version,
        project=project,
        pr_number=9,
    )

    assert receipt is None
    assert ancestry.call_args_list[0].args[3] == merge_sha
    assert ancestry.call_args_list[-1].args[3] == candidate


@pytest.mark.asyncio
async def test_promote_project_not_found_records_failure(db_session, mocker):
    """When promote() cannot load the Project row, it records a promotion
    failure (NOT a bare error return) so last_qualification reaches a terminal
    stabilize state — otherwise it would stay 'pending' and re-loop forever."""
    _project, version = await _make_project_and_version(db_session)

    # Force session.get(Project, ...) to return None even though the row
    # exists — simulates a project deleted between finalize and promote.
    real_get = db_session.get

    async def _fake_get(model, ident, *args, **kwargs):
        if model is Project:
            return None
        return await real_get(model, ident, *args, **kwargs)

    mocker.patch.object(db_session, "get", new=AsyncMock(side_effect=_fake_get))

    result = await rq.promote(
        db_session,
        version,
        "abcd" * 10,
        composite={"confidence": 0.9},
        base_record={"candidate_sha": "abcd" * 10},
    )
    await db_session.commit()

    # A real promotion-failure record, not a bare {status: error}.
    assert result["status"] == "promotion_failed"
    assert result["error"] == "project_not_found"

    await db_session.refresh(version)
    # The version reached a terminal stabilize state — NOT stuck pending.
    assert version.status == "open"
    assert version.phase == "stabilize"
    assert version.last_qualification is not None
    assert version.last_qualification["status"] == "promotion_failed"
    assert version.last_qualification["error"] == "project_not_found"
    # Crucially the record is no longer 'pending' — the re-loop is broken.
    assert version.last_qualification["status"] != "pending"


@pytest.mark.asyncio
async def test_finalize_project_not_found_breaks_pending_loop(db_session, mocker):
    """End-to-end: when _finalize_qualification cannot load the Project row,
    last_qualification must NOT be left 'pending' — a later finalize would
    otherwise re-loop forever.

    _finalize_qualification re-resolves CI before scoring; that re-resolution
    needs the Project row to resolve credentials.  With the project gone CI
    cannot be re-resolved, so it degrades to not-green (graceful — never
    crashes), the composite hard-blocks on ci_not_green, and the version is
    finalized as not_promoted.  The pending loop is still broken: the record
    reaches a terminal status rather than staying 'pending'."""
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

    # Make every Project lookup return None — simulates the project deleted
    # between qualification spawn and finalize.
    real_get = db_session.get

    async def _fake_get(model, ident, *args, **kwargs):
        if model is Project:
            return None
        return await real_get(model, ident, *args, **kwargs)

    mocker.patch.object(db_session, "get", new=AsyncMock(side_effect=_fake_get))

    result = await rq._finalize_qualification(db_session, version)
    await db_session.commit()

    # _resolve_ci_at_finalize degrades to not-green + state=_CI_PENDING when the
    # project is gone, so the composite hard-blocks on ci_unresolved (not
    # ci_not_green — the build was never evaluated, CI merely could not be read).
    assert result["status"] == "not_promoted"
    assert result["blocker"] == "ci_unresolved"

    await db_session.refresh(version)
    # The pending loop is broken — status is terminal.
    assert version.last_qualification["status"] == "not_promoted"
    assert version.last_qualification["status"] != "pending"


# ---------------------------------------------------------------------------
# Walkthrough finalize gate: OPEN-rubric report keys (not the retired v26 trio)
# ---------------------------------------------------------------------------
# Regression for the finalize mirror-image of PR #16719.  The request-time gate
# was re-pointed to resolve the walkthrough_e2e evidence contract semantically
# from the OPEN version's ACTIVE criteria; the finalize-time gate still pinned
# the report to the retired v26 key set (frozen raw-set equality in
# _evaluate_walkthrough_e2e_report and frozen canonical-trio equality in
# _walkthrough_e2e_canonical_evidence_keys), so a report authored against the
# OPEN rubric was refused as malformed infra BEFORE the active-rubric check ran.
# These tests pin the re-pointed finalize contract: a report whose keys resolve
# to the open version's ACTIVE criteria passes the gate and is classified as a
# QUALITY verdict, while a genuine canonical-collision report still infra-fails
# with the accurate duplicate_criteria reason.


@pytest.mark.asyncio
async def test_finalize_walkthrough_report_against_open_rubric_is_quality_not_infra(
    db_session, mocker
):
    """A walkthrough report authored against the OPEN version's rubric — fresh
    criterion keys, none of the retired v26 trio — passes the finalize gate and
    is classified as a QUALITY verdict (walkthrough_e2e_failed) rather than an
    infra_failure preempted by the retired frozen-trio schema check."""
    from app import shared_state

    shared_state.runtime_config["promotion_enabled"] = True

    project, version = await _make_project_and_version(db_session, phase="stabilize")
    # Open-version rubric: FRESH walkthrough-provable criteria — none of the
    # retired v26 trio.  The texts name the flows the live harness drives so the
    # request-time contract also resolves (finalize only needs the active keys).
    live_walkthrough_keys = []
    for _text in (
        "Prove runner enrollment on a live local runner",
        "Project tools iframe subresource token flow works",
        "Conversational Workday session lifecycle end to end",
    ):
        key = f"c-{uuid.uuid4().hex[:8]}"
        live_walkthrough_keys.append(key)
        await _add_version_criterion(
            db_session, project, version, key=key, text=_text
        )

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
            # The fixed audits.ts emits the acceptance-category contract whenever
            # an active rubric exists — independent of a version goal string — so
            # a goal-less version's report still carries per-category verdicts.
            "category_verdicts": [
                {
                    "category_key": key,
                    "status": "met",
                    "citations": [{"kind": "diff", "ref": "ui.tsx:10"}],
                }
                for key in live_walkthrough_keys
            ],
            "residual_uncaptured_gap": None,
        },
        smoke_logs={
            "passed": True,
            "tests": [{"name": "a", "status": "pass"}],
            "suites": {"startup": {"passed": True}},
        },
    )

    candidate_sha = version.last_qualification["candidate_sha"]
    walk_task = Task(
        project_id=project.id,
        title="walkthrough",
        payloads=[{"type": "walkthrough_e2e", "base_branch": "automain"}],
        vm_instance_id="vm-walkthrough",
        status=TaskStatus.COMPLETED,
        context={"candidate_sha": candidate_sha},
    )
    db_session.add(walk_task)
    await db_session.flush()
    db_session.add(
        Artifact(
            task_id=walk_task.id,
            artifact_type=ArtifactType.WALKTHROUGH_E2E_REPORT,
            logs=json.dumps(
                {
                    "git_sha": candidate_sha,
                    "exercised_engine_sha": candidate_sha,
                    "results": [
                        {
                            "criterion_key": live_walkthrough_keys[0],
                            "passed": True,
                        },
                        {
                            "criterion_key": live_walkthrough_keys[1],
                            "passed": False,
                        },
                        {
                            "criterion_key": live_walkthrough_keys[2],
                            "passed": True,
                        },
                    ],
                }
            ),
        )
    )
    await db_session.commit()
    await db_session.refresh(walk_task)

    qual = {
        **(version.last_qualification or {}),
        "requested_probes": ["walkthrough_e2e"],
        "walkthrough_e2e_task_id": str(walk_task.id),
    }
    version.last_qualification = _with_current_qualification_contract(
        project, version, qual
    )
    db_session.add(version)
    await db_session.commit()
    await db_session.refresh(version)

    result = await rq._finalize_qualification(db_session, version)
    await db_session.commit()

    # The report reached the QUALITY classification — the live-rubric check ran
    # and found a genuine criterion failure.  NOT an infra_failure that the
    # retired frozen-trio schema assertion used to preempt.
    assert result["status"] == "not_promoted"
    assert result["blocker"] == "walkthrough_e2e_failed"

    await db_session.refresh(version)
    lq = version.last_qualification
    assert lq["status"] == "not_promoted"
    assert lq["walkthrough_e2e_result"]["status"] == "failed"
    assert lq["walkthrough_e2e_result"]["failed_criterion_keys"] == [
        live_walkthrough_keys[1]
    ]
    assert lq["sub_scores"]["walkthrough_e2e"] == 0.0


@pytest.mark.asyncio
async def test_finalize_walkthrough_report_open_rubric_all_passed_records_passed_dimension(
    db_session, mocker
):
    """A fully-passing walkthrough report against the OPEN version's rubric —
    with covered-tier evidence ingested for every resolved key — records the
    walkthrough_e2e dimension as passed (sub_score 1.0) instead of infra-failing
    on the retired frozen-trio schema check.  The goal-less version's active
    rubric is graded per-category by the fixed audits.ts contract (all ``met``,
    so the coverage gate stays inert); the point is that the walkthrough probe
    itself is no longer refused as malformed."""
    from app import shared_state

    shared_state.runtime_config["promotion_enabled"] = False

    project, version = await _make_project_and_version(db_session, phase="stabilize")
    live_criteria = []
    for _text in (
        "Prove runner enrollment on a live local runner",
        "Project tools iframe subresource token flow works",
        "Conversational Workday session lifecycle end to end",
    ):
        key = f"c-{uuid.uuid4().hex[:8]}"
        live_criteria.append(
            await _add_version_criterion(
                db_session, project, version, key=key, text=_text
            )
        )
    live_walkthrough_keys = [c.criterion_key for c in live_criteria]

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
            # The fixed audits.ts emits the acceptance-category contract whenever
            # an active rubric exists — independent of a version goal string — so
            # a goal-less version's report still carries per-category verdicts.
            "category_verdicts": [
                {
                    "category_key": key,
                    "status": "met",
                    "citations": [{"kind": "diff", "ref": "ui.tsx:10"}],
                }
                for key in live_walkthrough_keys
            ],
            "residual_uncaptured_gap": None,
        },
        smoke_logs={
            "passed": True,
            "tests": [{"name": "a", "status": "pass"}],
            "suites": {"startup": {"passed": True}},
        },
    )

    candidate_sha = version.last_qualification["candidate_sha"]
    walk_task = Task(
        project_id=project.id,
        title="walkthrough",
        payloads=[{"type": "walkthrough_e2e", "base_branch": "automain"}],
        vm_instance_id="vm-walkthrough",
        status=TaskStatus.COMPLETED,
        context={"candidate_sha": candidate_sha},
    )
    db_session.add(walk_task)
    await db_session.flush()

    # The probe passed every criterion; the finalize gate's passed-probe path
    # verifies covered-tier evidence was ingested for each resolved key.
    attempt_id = uuid.uuid4()
    for _crit in live_criteria:
        db_session.add(
            CriterionEvidence(
                project_id=project.id,
                criterion_id=_crit.id,
                source_kind="walkthrough_e2e",
                source_id=f"walkthrough_e2e:{attempt_id}:{_crit.criterion_key}",
                detail={"candidate_sha": candidate_sha},
            )
        )
    db_session.add(
        Artifact(
            task_id=walk_task.id,
            artifact_type=ArtifactType.WALKTHROUGH_E2E_REPORT,
            logs=json.dumps(
                {
                    "git_sha": candidate_sha,
                    "exercised_engine_sha": candidate_sha,
                    "results": [
                        {"criterion_key": k, "passed": True}
                        for k in live_walkthrough_keys
                    ],
                }
            ),
        )
    )
    await db_session.commit()
    await db_session.refresh(walk_task)

    qual = {
        **(version.last_qualification or {}),
        "qualification_attempt_id": str(attempt_id),
        "requested_probes": ["walkthrough_e2e"],
        "walkthrough_e2e_task_id": str(walk_task.id),
    }
    version.last_qualification = _with_current_qualification_contract(
        project, version, qual
    )
    db_session.add(version)
    await db_session.commit()
    await db_session.refresh(version)

    result = await rq._finalize_qualification(db_session, version)
    await db_session.commit()

    assert result["status"] != "infra_failure"
    await db_session.refresh(version)
    lq = version.last_qualification
    assert lq["walkthrough_e2e_result"]["status"] == "passed"
    assert lq["sub_scores"]["walkthrough_e2e"] == 1.0


@pytest.mark.asyncio
async def test_finalize_walkthrough_report_unexpected_canonical_collision_is_contract_blocker(
    db_session, mocker, monkeypatch
):
    """A report that maps two DISTINCT raw keys onto one canonical evidence key
    (beyond the sole allowed workday-slug/meta pair) is still real malformed
    evidence — but it is a REPORT-CONTRACT defect resolved by the DEPLOYED
    qualifier, so it now books a durable not_promoted verdict with
    blocker=walkthrough_e2e_contract_failed / execution_site=deployed instead
    of a transient infra hold.  No fix on automain changes how the serving
    validator parses the report until this version itself promotes."""
    from app import shared_state
    from app import acceptance_criteria

    shared_state.runtime_config["promotion_enabled"] = True

    project, version = await _make_project_and_version(db_session, phase="stabilize")
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

    candidate_sha = version.last_qualification["candidate_sha"]
    # A second alias onto an existing canonical key makes two distinct FRESH
    # raw keys collide — the malformed shape the structural check must reject.
    # The patched alias is test-only; it proves the collision gate still fires
    # now that the frozen-trio equality check is gone.  The keys are fresh (not
    # the retired v26 set) so the report passes the report validator's legacy
    # subset check and reaches the canonical-collision check.
    monkeypatch.setitem(
        acceptance_criteria.WALKTHROUGH_LEGACY_CRITERION_ALIASES,
        "c-open-slug-b",
        "c-open-slug-a",
    )

    walk_task = Task(
        project_id=project.id,
        title="walkthrough",
        payloads=[{"type": "walkthrough_e2e", "base_branch": "automain"}],
        vm_instance_id="vm-walkthrough",
        status=TaskStatus.COMPLETED,
        context={"candidate_sha": candidate_sha},
    )
    db_session.add(walk_task)
    await db_session.flush()
    db_session.add(
        Artifact(
            task_id=walk_task.id,
            artifact_type=ArtifactType.WALKTHROUGH_E2E_REPORT,
            logs=json.dumps(
                {
                    "git_sha": candidate_sha,
                    "exercised_engine_sha": candidate_sha,
                    "results": [
                        {"criterion_key": "c-open-slug-a", "passed": True},
                        {"criterion_key": "c-open-slug-b", "passed": True},
                    ],
                }
            ),
        )
    )
    await db_session.commit()
    await db_session.refresh(walk_task)

    qual = {
        **(version.last_qualification or {}),
        "requested_probes": ["walkthrough_e2e"],
        "walkthrough_e2e_task_id": str(walk_task.id),
    }
    version.last_qualification = _with_current_qualification_contract(
        project, version, qual
    )
    db_session.add(version)
    await db_session.commit()
    await db_session.refresh(version)

    result = await rq._finalize_qualification(db_session, version)
    await db_session.commit()

    # Structural malformed evidence → DEPLOYED-side hard blocker with the
    # accurate reason (NOT a transient infra hold and NOT the misleading
    # active-rubric classification).
    assert result["status"] == "not_promoted"
    assert result["blocker"] == "walkthrough_e2e_contract_failed"
    assert result["execution_site"] == rs.EXECUTION_SITE_DEPLOYED
    assert result["error"] == "walkthrough_e2e_report_duplicate_criteria"

    await db_session.refresh(version)
    lq = version.last_qualification
    assert lq["status"] == "not_promoted"
    assert lq["blocker"] == "walkthrough_e2e_contract_failed"
    assert lq["execution_site"] == rs.EXECUTION_SITE_DEPLOYED
    assert lq["error"] == "walkthrough_e2e_report_duplicate_criteria"
    assert lq["sub_scores"]["walkthrough_e2e"] == 0.0
    assert version.phase == "stabilize"


@pytest.mark.asyncio
async def test_finalize_walkthrough_report_unknown_criteria_is_contract_blocker(
    db_session, mocker
):
    """A report whose keys do not resolve to the open version's ACTIVE rubric
    (the harness posted against criteria this version does not carry) is a
    REPORT-CONTRACT defect resolved by the DEPLOYED qualifier: it books a
    not_promoted verdict with blocker=walkthrough_e2e_contract_failed /
    execution_site=deployed rather than a transient infra hold."""
    from app import shared_state

    shared_state.runtime_config["promotion_enabled"] = True

    project, version = await _make_project_and_version(db_session, phase="stabilize")
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

    candidate_sha = version.last_qualification["candidate_sha"]
    walk_task = Task(
        project_id=project.id,
        title="walkthrough",
        payloads=[{"type": "walkthrough_e2e", "base_branch": "automain"}],
        vm_instance_id="vm-walkthrough",
        status=TaskStatus.COMPLETED,
        context={"candidate_sha": candidate_sha},
    )
    db_session.add(walk_task)
    await db_session.flush()
    # Fresh keys the open rubric does NOT carry — the validator accepts them
    # (no legacy keys used), then the active-rubric gate rejects them as
    # unknown criteria.
    db_session.add(
        Artifact(
            task_id=walk_task.id,
            artifact_type=ArtifactType.WALKTHROUGH_E2E_REPORT,
            logs=json.dumps(
                {
                    "git_sha": candidate_sha,
                    "exercised_engine_sha": candidate_sha,
                    "results": [
                        {"criterion_key": "c-unknown-aaa", "passed": True},
                        {"criterion_key": "c-unknown-bbb", "passed": False},
                        {"criterion_key": "c-unknown-ccc", "passed": True},
                    ],
                }
            ),
        )
    )
    await db_session.commit()
    await db_session.refresh(walk_task)

    qual = {
        **(version.last_qualification or {}),
        "requested_probes": ["walkthrough_e2e"],
        "walkthrough_e2e_task_id": str(walk_task.id),
    }
    version.last_qualification = _with_current_qualification_contract(
        project, version, qual
    )
    db_session.add(version)
    await db_session.commit()
    await db_session.refresh(version)

    result = await rq._finalize_qualification(db_session, version)
    await db_session.commit()

    assert result["status"] == "not_promoted"
    assert result["blocker"] == "walkthrough_e2e_contract_failed"
    assert result["execution_site"] == rs.EXECUTION_SITE_DEPLOYED
    assert result["error"] == "walkthrough_e2e_report_unknown_criteria"

    await db_session.refresh(version)
    lq = version.last_qualification
    assert lq["status"] == "not_promoted"
    assert lq["blocker"] == "walkthrough_e2e_contract_failed"
    assert lq["execution_site"] == rs.EXECUTION_SITE_DEPLOYED
    assert lq["error"] == "walkthrough_e2e_report_unknown_criteria"
    assert lq["sub_scores"]["walkthrough_e2e"] == 0.0
    assert version.phase == "stabilize"


@pytest.mark.asyncio
async def test_finalize_walkthrough_report_partial_legacy_schema_is_contract_blocker(
    db_session, mocker
):
    """A report that carries SOME but not ALL of the legacy harness keys is a
    truncated report — the report validator classifies it invalid with reason
    result_schema_mismatch.  That is a REPORT-CONTRACT defect resolved by the
    DEPLOYED qualifier, so it books a not_promoted verdict with
    blocker=walkthrough_e2e_contract_failed / execution_site=deployed."""
    from app import shared_state

    shared_state.runtime_config["promotion_enabled"] = True

    project, version = await _make_project_and_version(db_session, phase="stabilize")
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

    candidate_sha = version.last_qualification["candidate_sha"]
    walk_task = Task(
        project_id=project.id,
        title="walkthrough",
        payloads=[{"type": "walkthrough_e2e", "base_branch": "automain"}],
        vm_instance_id="vm-walkthrough",
        status=TaskStatus.COMPLETED,
        context={"candidate_sha": candidate_sha},
    )
    db_session.add(walk_task)
    await db_session.flush()
    # Partial legacy set: only two of the four v26 raw keys → the report
    # validator's legacy-subset check refuses it as result_schema_mismatch.
    db_session.add(
        Artifact(
            task_id=walk_task.id,
            artifact_type=ArtifactType.WALKTHROUGH_E2E_REPORT,
            logs=json.dumps(
                {
                    "git_sha": candidate_sha,
                    "exercised_engine_sha": candidate_sha,
                    "results": [
                        {"criterion_key": "c-dfdc9d82", "passed": True},
                        {"criterion_key": "c-f52fd975", "passed": True},
                    ],
                }
            ),
        )
    )
    await db_session.commit()
    await db_session.refresh(walk_task)

    qual = {
        **(version.last_qualification or {}),
        "requested_probes": ["walkthrough_e2e"],
        "walkthrough_e2e_task_id": str(walk_task.id),
    }
    version.last_qualification = _with_current_qualification_contract(
        project, version, qual
    )
    db_session.add(version)
    await db_session.commit()
    await db_session.refresh(version)

    result = await rq._finalize_qualification(db_session, version)
    await db_session.commit()

    assert result["status"] == "not_promoted"
    assert result["blocker"] == "walkthrough_e2e_contract_failed"
    assert result["execution_site"] == rs.EXECUTION_SITE_DEPLOYED
    assert result["error"] == "walkthrough_e2e_report_result_schema_mismatch"

    await db_session.refresh(version)
    lq = version.last_qualification
    assert lq["status"] == "not_promoted"
    assert lq["blocker"] == "walkthrough_e2e_contract_failed"
    assert lq["execution_site"] == rs.EXECUTION_SITE_DEPLOYED
    assert lq["error"] == "walkthrough_e2e_report_result_schema_mismatch"
    assert lq["sub_scores"]["walkthrough_e2e"] == 0.0
    assert version.phase == "stabilize"


@pytest.mark.asyncio
async def test_release_notification_idempotency(db_session):
    """Retried release notifications for the same (version, event_type) are
    deduplicated via the idempotency key; distinct event types for the same
    version still produce distinct messages."""
    from app.models import ProjectMessage

    project, version = await _make_project_and_version(db_session)

    await rq._notify_operator(db_session, version, "release_promoted", "v1 promoted")
    await db_session.commit()
    # A retried qualification / promotion re-posts the same outcome — the prior
    # committed row must be found via the idempotency key and nothing new
    # inserted.
    await rq._notify_operator(db_session, version, "release_promoted", "v1 promoted")
    await db_session.commit()

    promoted_rows = (
        (
            await db_session.execute(
                select(ProjectMessage).where(
                    ProjectMessage.project_id == project.id,
                    ProjectMessage.idempotency_key
                    == f"release:{version.id}:release_promoted",
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(promoted_rows) == 1
    assert promoted_rows[0].idempotency_key == f"release:{version.id}:release_promoted"
    assert promoted_rows[0].context["event_type"] == "release_promoted"

    # A different event type for the same version is a distinct notification
    # (keys are scoped per event_type, so one outcome never masks another).
    await rq._notify_operator(db_session, version, "release_blocked", "v1 blocked")
    await db_session.commit()

    all_rows = (
        (
            await db_session.execute(
                select(ProjectMessage).where(ProjectMessage.project_id == project.id)
            )
        )
        .scalars()
        .all()
    )
    assert len(all_rows) == 2
