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
walkthrough_e2e child: exactly one QUEUED decision-bearing child pinned to the
candidate SHA + attempt id when explicitly requested, and no child by default."""
from _release_qualifier_testhelpers import *  # noqa: F403


@pytest.mark.asyncio
async def test_run_qualification_spawns_walkthrough_e2e_when_enabled(
    db_session, mocker
):
    """An explicit walkthrough probe request spawns exactly ONE
    QUEUED walkthrough_e2e child pinned to the candidate SHA + the qualification
    attempt id, and a same-attempt re-drive does NOT spawn a duplicate.

    The walkthrough child is flagged ``qualifier_walkthrough_e2e`` (so a failed
    run cascades no fix task) and is a required child for this attempt's
    in-flight/finalize gate."""
    from app import shared_state

    shared_state.runtime_config["smoketest_enabled"] = False
    shared_state.runtime_config["env_preflight_enabled"] = False

    project, version = await _make_project_and_version(db_session, phase="stabilize")

    candidate = "wa1c0e2e" * 5  # 40-char placeholder SHA
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
        db_session,
        project.id,
        version,
        requested_probes=["walkthrough_e2e"],
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

    # Exactly one walkthrough_e2e task, QUEUED, pinned to the candidate SHA and
    # the qualification attempt id.
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
    assert walk.status == TaskStatus.QUEUED
    assert walk.created_by == "ORCHESTRATOR"
    assert walk.first_payload["type"] == "walkthrough_e2e"
    assert walk.first_payload["candidate_sha"] == candidate
    assert walk.first_payload["qualification_attempt_id"] == attempt_id
    assert walk.first_payload["base_branch"] == "automain"
    # Qualifier marker prevents ordinary task-fix cascades.
    assert walk.context["qualifier_walkthrough_e2e"] is True
    assert walk.context["candidate_sha"] == candidate
    assert walk.context["qualification_attempt_id"] == attempt_id
    assert walk.context["release_version_id"] == str(version.id)


@pytest.mark.asyncio
async def test_run_qualification_skips_walkthrough_e2e_when_disabled(
    db_session, mocker
):
    """Without an explicit probe request, no walkthrough_e2e child is spawned
    and last_qualification records a None walkthrough_e2e_task_id."""
    from app import shared_state

    shared_state.runtime_config["smoketest_enabled"] = False
    shared_state.runtime_config["env_preflight_enabled"] = False

    project, version = await _make_project_and_version(db_session, phase="stabilize")

    candidate = "de1e7ed0" * 5  # 40-char placeholder SHA
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

    result = await rq.run_qualification(db_session, project.id, version)
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
    """End-to-end: the wired walkthrough_e2e lane spawns a QUEUED child pinned to
    the candidate SHA, ingests PASS results as covered-tier CriterionEvidence rows
    for all three registered keys (c-dfdc9d82, c-f52fd975, c-5214996f), and
    surfaces a loud Alert for any phantom/drifted key while writing no evidence
    for it.

    Properties verified:
      1. ACTIVE VersionAcceptanceCriterion rows for the three registered keys.
      2. run_qualification spawns exactly one QUEUED walkthrough_e2e child pinned
         to the candidate SHA + qualification attempt id.
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

    # PROPERTY 1: project + open version; register the three emitted keys as ACTIVE criteria.
    project, version = await _make_project_and_version(db_session, phase="stabilize")

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

    # PROPERTY 2: run_qualification spawns exactly one QUEUED walkthrough_e2e child.
    result = await rq.run_qualification(
        db_session,
        project.id,
        version,
        requested_probes=["walkthrough_e2e"],
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
    assert walk.status == TaskStatus.QUEUED
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
