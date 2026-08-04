# AUTO-SPLIT from test_release_qualifier.py — see refactor: decompose the
# 45K-line release-qualifier test monolith.
# The bulk of this suite was decomposed into focused domain slices
# (test_release_qualifier__*.py). Their shared preamble — imports, fixtures,
# and seed helpers — lives in _release_qualifier_testhelpers.py. Every prior
# test class/function in this file was byte-identical to a copy already carried
# by one of those slices, so it ran twice in the full suite; those duplicates
# are removed here. This file now holds only the run_qualification
# walkthrough_e2e spawn slice — the two tests that had no home in an existing
# sub-module.
"""Domain slice: run_qualification — walkthrough_e2e evidence-child spawn.

Split from test_release_qualifier.py. Covers run_qualification's optional
walkthrough_e2e child: exactly one PROVISIONING decision-bearing child pinned
at birth to the verified container-capable runner + candidate SHA + attempt id."""
from _release_qualifier_testhelpers import *  # noqa: F403

from app.models import LocalRunner
from app.time_utils import _utcnow


def test_walkthrough_execution_site_is_registry_declared():
    from app.task_capabilities import (
        DIRECT_PACKER_GCE_EXECUTION_SITE,
        TaskType,
        execution_site_for_task_type,
    )

    assert (
        execution_site_for_task_type(TaskType.walkthrough_e2e)
        == DIRECT_PACKER_GCE_EXECUTION_SITE
    )


@pytest.mark.asyncio
async def test_walkthrough_spawn_fails_closed_on_execution_site_drift(mocker):
    mocker.patch(
        "app.release_qualifier_audit.execution_site_for_task_type",
        return_value="default",
    )

    with pytest.raises(RuntimeError, match="requires the registry-declared"):
        await rq._spawn_qualifier_walkthrough_task(
            object(),
            uuid.uuid4(),
            version_id=uuid.uuid4(),
            candidate_sha="a" * 40,
            qualification_attempt_id=uuid.uuid4(),
        )


def test_qualification_contract_fingerprint_tracks_goal_and_categories():
    empty_rubric = rq._rubric_fingerprint([])
    category_rubric = rq._rubric_fingerprint(
        [{"criterion_key": "ship", "text": "Ship the feature"}]
    )
    baseline = rq._qualification_contract_fingerprint("Ship safely", empty_rubric)

    assert baseline == rq._qualification_contract_fingerprint(
        "  Ship   safely  ", empty_rubric
    )
    assert baseline != rq._qualification_contract_fingerprint(
        "Ship quickly", empty_rubric
    )
    assert baseline != rq._qualification_contract_fingerprint(
        "Ship safely", category_rubric
    )
    assert baseline != rq._qualification_contract_fingerprint(
        "Ship safely",
        empty_rubric,
        promotion_policy={"threshold": 0.91, "weights": {"audit": 1.0}},
    )
    assert baseline != rq._qualification_contract_fingerprint(
        "Ship safely", empty_rubric, capability_proof_mode="enforce"
    )
    assert rq._qualification_contract_fingerprint(
        "Ship safely", empty_rubric, requested_probes=["walkthrough", "smoke"]
    ) == rq._qualification_contract_fingerprint(
        "Ship safely", empty_rubric, requested_probes=["smoke", "walkthrough"]
    )


@pytest.mark.asyncio
async def test_promotion_disabled_does_not_overwrite_pending_qualification(
    db_session, mocker
):
    project, version = await _make_project_and_version(db_session, phase="stabilize")
    pending = {
        "status": "pending",
        "qualification_attempt_id": str(uuid.uuid4()),
        "candidate_sha": "pending0" * 5,
    }
    version.last_qualification = pending
    db_session.add(version)
    await db_session.commit()

    mocker.patch(
        "app.release_qualifier.resolve_project_promotion_enabled",
        return_value=False,
    )
    mocker.patch(
        "app.release_qualifier._qualification_in_flight",
        new=AsyncMock(return_value=True),
    )

    result = await rq.run_qualification(
        db_session, project.id, version, explicit_request=True
    )

    assert result == {"status": "already_pending"}
    await db_session.refresh(version)
    assert version.last_qualification == pending


@pytest.mark.asyncio
async def test_pending_goal_change_supersedes_attempt_before_promotion(
    db_session, mocker
):
    from app import shared_state

    shared_state.runtime_config["promotion_enabled"] = True
    project, version = await _make_project_and_version(db_session, phase="stabilize")
    launched_goal = "Ship the original goal"
    launched_rubric = []
    rubric_fingerprint = rq._rubric_fingerprint(launched_rubric)
    policy_snapshot = {"threshold": 0.8, "weights": {}}
    version.goal = "Ship the revised goal"
    version.last_qualification = {
        "status": "pending",
        "qualification_attempt_id": str(uuid.uuid4()),
        "candidate_sha": "contract" * 5,
        "version_goal": launched_goal,
        "rubric_snapshot": launched_rubric,
        "rubric_fingerprint": rubric_fingerprint,
        "rubric_lifecycle_snapshot": [],
        "rubric_provenance": rq._rubric_provenance(
            launched_rubric,
            source="qualification_launch",
            qualification_attempt_id=None,
        ),
        "contract_fingerprint": rq._qualification_contract_fingerprint(
            launched_goal,
            rubric_fingerprint,
            promotion_policy=policy_snapshot,
        ),
        "promotion_policy_snapshot": policy_snapshot,
        "capability_proof_mode": "off",
        "env_preflight_enabled": False,
        "requested_probes": [],
        "started_at": "2026-08-03T00:00:00+00:00",
    }
    db_session.add(version)
    await db_session.commit()

    promote_spy = mocker.patch("app.release_qualifier.promote", new=AsyncMock())
    result = await rq._finalize_qualification(
        db_session,
        version,
        _pre_gates={
            "ci_green": True,
            "ci_resolution": {"state": "green", "resolved_sha": "contract" * 5},
        },
    )

    assert result["status"] == "rubric_changed"
    assert result["blocker"] == "qualification_contract_superseded"
    promote_spy.assert_not_called()


@pytest.mark.parametrize(
    "contract_change", ["goal", "category", "policy_threshold", "capability_mode"]
)
@pytest.mark.asyncio
async def test_same_sha_contract_change_spawns_fresh_holistic_review(
    db_session, mocker, contract_change
):
    from app import shared_state

    mocker.patch.dict(
        shared_state.runtime_config,
        {
            "promotion_enabled": True,
            "capability_proof_gate_mode": "off",
            "env_preflight_enabled": False,
        },
        clear=False,
    )
    project, version = await _make_project_and_version(db_session, phase="stabilize")
    candidate = "newcontract" * 4
    launched_goal = "Ship the original outcome"
    launched_rubric = []
    launched_rubric_fingerprint = rq._rubric_fingerprint(launched_rubric)
    launched_policy = rq._load_policy(project)
    launched_contract = rq._qualification_contract_fingerprint(
        launched_goal,
        launched_rubric_fingerprint,
        promotion_policy=launched_policy,
        capability_proof_mode="off",
        env_preflight_enabled=False,
        requested_probes=[],
    )
    version.goal = launched_goal
    version.last_qualification = {
        "status": "dry_run",
        "candidate_sha": candidate,
        "requested_probes": [],
        "version_goal": launched_goal,
        "rubric_snapshot": launched_rubric,
        "rubric_fingerprint": launched_rubric_fingerprint,
        "contract_fingerprint": launched_contract,
    }
    db_session.add(version)
    await db_session.commit()

    if contract_change == "goal":
        version.goal = "Ship the revised outcome"
        db_session.add(version)
        await db_session.commit()
    elif contract_change == "category":
        await _add_version_criterion(
            db_session,
            project,
            version,
            key="new-category",
            text="Prove the newly required outcome",
        )
    elif contract_change == "policy_threshold":
        old_threshold = float(launched_policy["threshold"])
        changed_threshold = 0.95 if old_threshold != 0.95 else 0.9
        project.build_config = {
            **(project.build_config or {}),
            "promotion_threshold": changed_threshold,
        }
        db_session.add(project)
        await db_session.commit()
    else:
        shared_state.runtime_config["capability_proof_gate_mode"] = "enforce"

    mocker.patch(
        "app.github.get_repo_installation_token", new=AsyncMock(return_value="gh-token")
    )
    mocker.patch("app.github.get_branch_sha", new=AsyncMock(return_value=candidate))
    mocker.patch(
        "app.github.get_check_runs_cached",
        new=AsyncMock(return_value={"conclusion": "success"}),
    )

    result = await rq.run_qualification(
        db_session, project.id, version, explicit_request=True
    )
    await db_session.commit()

    assert result["status"] == "spawned"
    assert result["audit_task_id"] is not None
    await db_session.refresh(version)
    assert version.last_qualification["contract_fingerprint"] != launched_contract


@pytest.mark.asyncio
async def test_run_qualification_spawns_walkthrough_e2e_when_enabled(
    db_session, mocker
):
    """An explicit walkthrough probe request spawns exactly ONE PROVISIONING
    walkthrough_e2e child pinned AT BIRTH to the verified container-capable
    project runner (candidate SHA + attempt id); a same-attempt re-drive does
    NOT spawn a duplicate."""
    from app import shared_state

    shared_state.runtime_config["smoketest_enabled"] = False
    shared_state.runtime_config["env_preflight_enabled"] = False
    shared_state.runtime_config["promotion_enabled"] = True

    project, version = await _make_project_and_version(db_session, phase="stabilize")

    # A live runner proving the candidate-stack site (docker info + Compose +
    # Chromium) so run_qualification pins the walkthrough child to it.
    capable_runner = LocalRunner(
        project_id=project.id, token_hash="x" * 64, status="active",
        last_seen_at=_utcnow(),
        capabilities={"task_types": "all", "max_concurrent": 2,
                      "isolation_backends": {"docker_usable": True},
                      "playwright_chromium_usable": True},
    )
    db_session.add(capable_runner)
    await db_session.commit()

    candidate = "wa1c0e2e" * 5  # 40-char placeholder SHA
    mocker.patch("app.github.get_repo_installation_token", new=AsyncMock(return_value="gh-token"))
    mocker.patch("app.github.get_branch_sha", new=AsyncMock(return_value=candidate))
    mocker.patch("app.github.get_check_runs_cached", new=AsyncMock(return_value={"conclusion": "success"}))

    result = await rq.run_qualification(
        db_session,
        project.id,
        version,
        requested_probes=["walkthrough_e2e"],
        explicit_request=True,
    )
    await db_session.commit()

    assert result["status"] == "spawned"
    assert result["requested_probes"] == ["walkthrough_e2e"]
    walk_task_id = result["walkthrough_e2e_task_id"]
    assert walk_task_id is not None

    await db_session.refresh(version)
    attempt_id = version.last_qualification["qualification_attempt_id"]
    assert version.last_qualification["requested_probes"] == ["walkthrough_e2e"]
    assert version.last_qualification["walkthrough_e2e_task_id"] == walk_task_id

    from sqlmodel import select as _sqlmodel_select

    project_tasks = (
        (
            await db_session.execute(
                _sqlmodel_select(Task).where(Task.project_id == project.id)
            )
        )
        .scalars()
        .all()
    )
    walk_tasks = [
        task
        for task in project_tasks
        if (task.context or {}).get("qualifier_walkthrough_e2e") is True
    ]
    assert len(walk_tasks) == 1
    walk = walk_tasks[0]
    assert str(walk.id) == walk_task_id
    assert walk.status == TaskStatus.PROVISIONING
    assert walk.vm_instance_id == f"runner:{capable_runner.id}"
    assert walk.vm_provisioned_at is not None
    assert walk.started_at is None
    assert walk.created_by == "ORCHESTRATOR"
    assert walk.first_payload["type"] == "walkthrough_e2e"
    assert walk.first_payload["candidate_sha"] == candidate
    assert walk.first_payload["qualification_attempt_id"] == attempt_id
    assert walk.first_payload["base_branch"] == "automain"
    # Qualifier marker prevents ordinary task-fix cascades.
    assert walk.context["qualifier_walkthrough_e2e"] is True
    assert walk.context["runner_target_id"] == str(capable_runner.id)
    assert walk.context["candidate_sha"] == candidate
    assert walk.context["qualification_attempt_id"] == attempt_id
    assert walk.context["release_version_id"] == str(version.id)
    assert walk.context["execution_site"] == "direct_packer_gce"


@pytest.mark.asyncio
async def test_run_qualification_skips_walkthrough_e2e_when_disabled(
    db_session, mocker
):
    """Without an explicit probe request, no walkthrough_e2e child is spawned
    and last_qualification records a None walkthrough_e2e_task_id."""
    from app import shared_state

    shared_state.runtime_config["smoketest_enabled"] = False
    shared_state.runtime_config["env_preflight_enabled"] = False
    shared_state.runtime_config["promotion_enabled"] = True

    project, version = await _make_project_and_version(db_session, phase="stabilize")

    candidate = "de1e7ed0" * 5  # 40-char placeholder SHA
    mocker.patch("app.github.get_repo_installation_token", new=AsyncMock(return_value="gh-token"))
    mocker.patch("app.github.get_branch_sha", new=AsyncMock(return_value=candidate))
    mocker.patch("app.github.get_check_runs_cached", new=AsyncMock(return_value={"conclusion": "success"}))

    result = await rq.run_qualification(
        db_session, project.id, version, explicit_request=True
    )
    await db_session.commit()

    assert result["status"] == "spawned"
    assert result["requested_probes"] == []
    assert result["walkthrough_e2e_task_id"] is None

    await db_session.refresh(version)
    assert version.last_qualification["requested_probes"] == []
    assert version.last_qualification["walkthrough_e2e_task_id"] is None

    from sqlmodel import select as _sqlmodel_select

    project_tasks = (
        (
            await db_session.execute(
                _sqlmodel_select(Task).where(Task.project_id == project.id)
            )
        )
        .scalars()
        .all()
    )
    walk_tasks = [
        task
        for task in project_tasks
        if (task.context or {}).get("qualifier_walkthrough_e2e") is True
    ]
    assert walk_tasks == []


def test_walkthrough_probe_contract_is_strict_and_cache_safe():
    with pytest.raises(ValueError, match="unsupported"):
        rq._normalize_qualification_probes(["made_up_probe"])
    with pytest.raises(ValueError, match="duplicate"):
        rq._normalize_qualification_probes(
            ["walkthrough_e2e", "walkthrough_e2e"]
        )
    with pytest.raises(ValueError, match="collection"):
        rq._normalize_qualification_probes("walkthrough_e2e")

    candidate = "a" * 40
    assert not rq._qualification_probe_contract_is_reusable(
        {"candidate_sha": candidate, "requested_probes": []},
        frozenset({"walkthrough_e2e"}),
    )
    assert not rq._qualification_probe_contract_is_reusable(
        {
            "candidate_sha": candidate,
            "requested_probes": ["walkthrough_e2e"],
            "walkthrough_e2e_result": {
                "status": "passed",
                "candidate_sha": candidate,
            },
        },
        frozenset({"walkthrough_e2e"}),
    )
    assert not rq._qualification_probe_contract_is_reusable(
        {
            "candidate_sha": candidate,
            "requested_probes": ["walkthrough_e2e"],
            "walkthrough_e2e_result": {
                "status": "passed",
                "candidate_sha": candidate,
                "exercised_engine_sha": "b" * 40,
            },
        },
        frozenset({"walkthrough_e2e"}),
    )
    assert rq._qualification_probe_contract_is_reusable(
        {
            "candidate_sha": candidate,
            "requested_probes": ["walkthrough_e2e"],
            "walkthrough_e2e_result": {
                "status": "passed",
                "candidate_sha": candidate,
                "exercised_engine_sha": candidate,
            },
        },
        frozenset({"walkthrough_e2e"}),
    )


def test_walkthrough_report_requires_exact_exercised_sha_and_classifies_failure():
    candidate = "b" * 40
    full_results = [
        {"criterion_key": "c-dfdc9d82", "passed": True},
        {"criterion_key": "c-f52fd975", "passed": True},
        {"criterion_key": "c-workday-session-lifecycle", "passed": True},
        {"criterion_key": "c-5214996f", "passed": True},
    ]
    missing_engine = rq._evaluate_walkthrough_e2e_report(
        {
            "git_sha": candidate,
            "results": full_results,
        },
        candidate,
    )
    assert missing_engine == {
        "status": "invalid",
        "reason": "exercised_engine_sha_mismatch",
        "candidate_sha": candidate,
        "exercised_engine_sha": None,
    }

    failed = rq._evaluate_walkthrough_e2e_report(
        {
            "git_sha": candidate,
            "exercised_engine_sha": candidate,
            "results": [
                {"criterion_key": "c-dfdc9d82", "passed": True},
                {"criterion_key": "c-f52fd975", "passed": False},
                {
                    "criterion_key": "c-workday-session-lifecycle",
                    "passed": True,
                },
                {"criterion_key": "c-5214996f", "passed": False},
            ],
        },
        candidate,
    )
    assert failed["status"] == "failed"
    assert failed["failed_criterion_keys"] == ["c-f52fd975", "c-5214996f"]
    assert rq._walkthrough_e2e_canonical_evidence_keys(failed["results"]) == {
        "c-dfdc9d82",
        "c-f52fd975",
        "c-5214996f",
    }
    decision = rq._apply_walkthrough_decision(
        {"promote": True, "blocker": None, "sub_scores": {"audit": 1.0}},
        failed,
    )
    assert decision["promote"] is False
    assert decision["blocker"] == "walkthrough_e2e_failed"
    assert decision["sub_scores"]["walkthrough_e2e"] == 0.0


def test_walkthrough_report_rejects_subset_duplicate_and_malformed_rows():
    candidate = "c" * 40
    full_results = [
        {"criterion_key": "c-dfdc9d82", "passed": True},
        {"criterion_key": "c-f52fd975", "passed": True},
        {"criterion_key": "c-workday-session-lifecycle", "passed": True},
        {"criterion_key": "c-5214996f", "passed": True},
    ]

    def evaluate(results):
        return rq._evaluate_walkthrough_e2e_report(
            {
                "git_sha": candidate,
                "exercised_engine_sha": candidate,
                "results": results,
            },
            candidate,
        )

    subset = evaluate(full_results[:-1])
    assert subset["status"] == "invalid"
    assert subset["reason"] == "result_schema_mismatch"
    assert subset["missing_criterion_keys"] == ["c-5214996f"]

    duplicate = evaluate(
        [*full_results[:-1], {"criterion_key": "c-dfdc9d82", "passed": True}]
    )
    assert duplicate["status"] == "invalid"
    assert duplicate["reason"] == "result_key_duplicate"

    malformed = evaluate(
        [
            full_results[0],
            {"criterion_key": "c-f52fd975", "passed": 1},
            *full_results[2:],
        ]
    )
    assert malformed["status"] == "invalid"
    assert malformed["reason"] == "result_entry_invalid"


@pytest.mark.asyncio
async def test_requested_walkthrough_is_a_required_terminal_child():
    import uuid
    from types import SimpleNamespace

    attempt_id = uuid.uuid4()
    audit_id = uuid.uuid4()
    walkthrough_id = uuid.uuid4()
    version = SimpleNamespace(
        version_number=1,
        last_qualification={
            "status": "pending",
            "qualification_attempt_id": str(attempt_id),
            "audit_task_id": str(audit_id),
            "requested_probes": ["walkthrough_e2e"],
            "walkthrough_e2e_task_id": str(walkthrough_id),
        },
    )
    tasks = {
        audit_id: SimpleNamespace(
            id=audit_id,
            status=TaskStatus.COMPLETED,
            context={"qualification_attempt_id": str(attempt_id)},
        ),
        walkthrough_id: SimpleNamespace(
            id=walkthrough_id,
            status=TaskStatus.QUEUED,
            context={"qualification_attempt_id": str(attempt_id)},
        ),
    }
    session = MagicMock()
    session.get = AsyncMock(side_effect=lambda _model, task_id: tasks.get(task_id))

    assert not await rq._required_children_terminal(session, version)
    tasks[walkthrough_id].status = TaskStatus.COMPLETED
    assert await rq._required_children_terminal(session, version)


@pytest.mark.asyncio
async def test_walkthrough_e2e_lane_records_registered_criterion_evidence(
    db_session, mocker
):
    """End-to-end: the walkthrough_e2e lane spawns a PROVISIONING child pinned to
    the candidate SHA, ingests PASS results as covered-tier CriterionEvidence rows
    for all three registered keys (c-dfdc9d82, c-f52fd975, c-5214996f), and
    surfaces a loud Alert for any phantom/drifted key while writing no evidence
    for it.

    Properties verified:
      1. ACTIVE VersionAcceptanceCriterion rows for the three registered keys.
      2. run_qualification spawns exactly one PROVISIONING walkthrough_e2e child
         pinned to the verified runner + candidate SHA + qualification attempt id.
      3-4. record_walkthrough_e2e_evidence writes covered-tier CriterionEvidence
           rows (source_kind=walkthrough_e2e) for each registered key; no unmatched
           alert when all keys resolve.
      5. A phantom/drifted key in the result batch surfaces in unmatched_keys AND
         triggers a project Alert (event_type=release.walkthrough_key_unmatched)
         while writing no evidence row.
    """
    from app import shared_state
    from app.acceptance_criteria import (
        WALKTHROUGH_E2E_EVIDENCE_KIND,
        compute_criterion_coverage,
        get_evidence_for_criterion,
        record_walkthrough_e2e_evidence,
    )
    from sqlmodel import select as _sel

    shared_state.runtime_config["smoketest_enabled"] = False
    shared_state.runtime_config["env_preflight_enabled"] = False
    shared_state.runtime_config["promotion_enabled"] = True

    # PROPERTY 1: project + open version; register the three emitted keys as ACTIVE criteria.
    project, version = await _make_project_and_version(db_session, phase="stabilize")

    capable_runner = LocalRunner(
        project_id=project.id, token_hash="x" * 64, status="active",
        last_seen_at=_utcnow(),
        capabilities={"task_types": "all", "max_concurrent": 2,
                      "isolation_backends": {"docker_usable": True},
                      "playwright_chromium_usable": True},
    )
    db_session.add(capable_runner)
    await db_session.commit()

    REGISTERED_KEYS = ["c-dfdc9d82", "c-f52fd975", "c-5214996f"]
    for key in REGISTERED_KEYS:
        await _add_version_criterion(
            db_session,
            project,
            version,
            key=key,
            text=f"Walkthrough criterion {key}",
            sync_pending_rubric=False,
        )
    await db_session.refresh(version)

    # Fetch created criteria so we can query evidence by id later.
    all_criteria = (
        (
            await db_session.execute(
                _sel(VersionAcceptanceCriterion).where(
                    VersionAcceptanceCriterion.project_id == project.id,
                    VersionAcceptanceCriterion.release_version_id == version.id,
                )
            )
        )
        .scalars()
        .all()
    )
    criteria_by_key = {c.criterion_key: c for c in all_criteria}
    for key in REGISTERED_KEYS:
        assert key in criteria_by_key, f"Criterion {key} not registered"

    candidate = "c0de1234" * 5  # deterministic 40-char SHA

    mocker.patch("app.github.get_repo_installation_token", new=AsyncMock(return_value="gh-token"))
    mocker.patch("app.github.get_branch_sha", new=AsyncMock(return_value=candidate))
    mocker.patch("app.github.get_check_runs_cached", new=AsyncMock(return_value={"conclusion": "success"}))

    # PROPERTY 2: run_qualification spawns exactly one PROVISIONING walkthrough_e2e child.
    result = await rq.run_qualification(
        db_session,
        project.id,
        version,
        requested_probes=["walkthrough_e2e"],
        explicit_request=True,
    )
    await db_session.commit()

    assert result["status"] == "spawned"
    assert result["requested_probes"] == ["walkthrough_e2e"]
    walk_task_id = result["walkthrough_e2e_task_id"]
    assert walk_task_id is not None

    await db_session.refresh(version)
    attempt_id = version.last_qualification["qualification_attempt_id"]
    assert version.last_qualification["candidate_sha"] == candidate

    project_tasks = (
        (
            await db_session.execute(
                _sel(Task).where(Task.project_id == project.id)
            )
        )
        .scalars()
        .all()
    )
    walk_tasks = [
        task
        for task in project_tasks
        if (task.context or {}).get("qualifier_walkthrough_e2e") is True
    ]
    assert len(walk_tasks) == 1
    walk = walk_tasks[0]
    assert str(walk.id) == walk_task_id
    assert walk.status == TaskStatus.PROVISIONING
    assert walk.vm_instance_id == f"runner:{capable_runner.id}"
    assert walk.context["runner_target_id"] == str(capable_runner.id)
    assert walk.first_payload["candidate_sha"] == candidate
    assert walk.first_payload["qualification_attempt_id"] == attempt_id

    # PROPERTIES 3-4: Feed PASS results for all three registered keys plus the
    # FLOW 5 placeholder key (c-workday-session-lifecycle), required by the meta
    # gate and routed to c-5214996f via the alias table.
    # exercised_engine_sha must equal candidate_sha to satisfy the SHA-match guard.
    results_batch = [
        {"criterion_key": key, "passed": True, "notes": f"green-{key}"}
        for key in REGISTERED_KEYS
    ]
    results_batch.append({"criterion_key": "c-workday-session-lifecycle", "passed": True, "notes": "green-flow5"})
    summary = await record_walkthrough_e2e_evidence(
        db_session,
        version=version,
        candidate_sha=candidate,
        results=results_batch,
        qualification_attempt_id=attempt_id,
        exercised_engine_sha=candidate,
    )
    await db_session.commit()

    assert summary["status"] == "recorded"
    # All three registered keys must be written (meta gate passes because
    # c-dfdc9d82, c-f52fd975, and c-workday-session-lifecycle are all present
    # and passed in the same batch; the alias routes c-workday-session-lifecycle
    # to c-5214996f, so recorded=3 not 4).
    assert summary["recorded"] == 3
    assert summary["unmatched_keys"] == []

    # Each registered criterion has exactly one covered-tier walkthrough_e2e row.
    for key in REGISTERED_KEYS:
        criterion = criteria_by_key[key]
        ev_list = await get_evidence_for_criterion(db_session, criterion.id)
        walk_ev = [e for e in ev_list if e.source_kind == WALKTHROUGH_E2E_EVIDENCE_KIND]
        assert walk_ev, f"No walkthrough_e2e evidence row for registered key {key!r}"
        assert walk_ev[0].detail["candidate_sha"] == candidate
        assert (
            compute_criterion_coverage(ev_list) == "covered"
        ), f"Expected covered tier for {key!r}, got {compute_criterion_coverage(ev_list)!r}"

    # No unmatched-key alert when all keys resolved.
    no_alerts = (
        (
            await db_session.execute(
                _sel(Alert).where(
                    Alert.project_id == project.id,
                    Alert.event_type == "release.walkthrough_key_unmatched",
                )
            )
        )
        .scalars()
        .all()
    )
    assert no_alerts == [], "No unmatched-key alert expected when all keys resolve"

    # PROPERTY 5: A phantom/drifted key must appear in unmatched_keys, trigger a
    # loud Alert, and write NO evidence row.
    # NOTE: this must be a key that is neither registered NOR a recognized legacy
    # alias. 'c-workday-session-lifecycle' is intentionally NOT used here — it is a
    # live legacy alias (WALKTHROUGH_LEGACY_CRITERION_ALIASES) that resolves to the
    # active meta criterion c-5214996f, so it is not a phantom. A truly-drifted key
    # exercises the unmatched-key alert path.
    phantom_key = "c-workday-phantom-drift"
    summary2 = await record_walkthrough_e2e_evidence(
        db_session,
        version=version,
        candidate_sha=candidate,
        results=[
            {"criterion_key": REGISTERED_KEYS[0], "passed": True},
            {"criterion_key": phantom_key, "passed": True, "notes": "phantom"},
        ],
        qualification_attempt_id=f"{attempt_id}-b",
        exercised_engine_sha=candidate,
    )
    await db_session.commit()

    assert phantom_key in summary2["unmatched_keys"]
    # The real key still wrote evidence (best-effort per-row recording continues).
    assert summary2["recorded"] >= 1

    unmatched_alerts = (
        (
            await db_session.execute(
                _sel(Alert).where(
                    Alert.project_id == project.id,
                    Alert.event_type == "release.walkthrough_key_unmatched",
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(unmatched_alerts) >= 1, "Expected at least one release.walkthrough_key_unmatched Alert"
    assert any(phantom_key in a.message for a in unmatched_alerts), (
        f"Alert message must mention the phantom key; got: {[a.message for a in unmatched_alerts]}"
    )

    # The phantom key must have written zero evidence rows.
    phantom_ev = (
        (
            await db_session.execute(
                _sel(CriterionEvidence).where(
                    CriterionEvidence.project_id == project.id,
                )
            )
        )
        .scalars()
        .all()
    )
    phantom_source_ids = [
        e.source_id for e in phantom_ev if phantom_key in (e.source_id or "")
    ]
    assert phantom_source_ids == [], (
        f"Phantom key must not write any CriterionEvidence rows; found: {phantom_source_ids}"
    )


@pytest.mark.asyncio
async def test_walkthrough_e2e_site_gate_rejects_unproven_runners(db_session):
    """Fail-closed site gate + deterministic low-id pin for the walkthrough child."""
    from app.release_qualifier_audit import (
        QualifierWalkthroughSiteUnavailableError,
        _spawn_qualifier_walkthrough_task,
    )

    project, version = await _make_project_and_version(db_session, phase="stabilize")

    def add_runner(runner_id, caps):
        db_session.add(LocalRunner(
            id=runner_id, project_id=project.id, token_hash="x" * 64,
            status="active", last_seen_at=_utcnow(), capabilities=caps))

    base = {"task_types": "all", "max_concurrent": 2}
    add_runner(uuid.uuid4(), {**base, "isolation_backends": {"bwrap_usable": True}})
    add_runner(uuid.uuid4(), {**base, "isolation_backends": {"docker_usable": True}})
    await db_session.commit()

    with pytest.raises(QualifierWalkthroughSiteUnavailableError,
                       match="candidate-stack site"):
        await _spawn_qualifier_walkthrough_task(
            db_session, project.id, version_id=version.id,
            candidate_sha="f011c10se" * 5,
            qualification_attempt_id=uuid.uuid4(),
        )

    low_id = uuid.UUID("00000000-0000-0000-0000-0000000000aa")
    site_caps = {**base, "isolation_backends": {"docker_usable": True},
                 "playwright_chromium_usable": True}
    add_runner(low_id, site_caps)
    add_runner(uuid.UUID("00000000-0000-0000-0000-0000000000bb"), site_caps)
    await db_session.commit()

    pinned = await _spawn_qualifier_walkthrough_task(
        db_session, project.id, version_id=version.id,
        candidate_sha="f011c10se" * 5,
        qualification_attempt_id=uuid.uuid4(),
    )
    assert pinned.status == TaskStatus.PROVISIONING
    assert pinned.vm_instance_id == f"runner:{low_id}"
    assert pinned.context["runner_target_id"] == str(low_id)
    assert pinned.vm_provisioned_at is not None
    assert pinned.started_at is None
    assert pinned.context["qualifier_walkthrough_e2e"] is True
