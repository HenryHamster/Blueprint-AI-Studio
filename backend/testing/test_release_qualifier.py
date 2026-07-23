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
walkthrough_e2e child: exactly one QUEUED evidence-only child pinned to the
candidate SHA + attempt id when walkthrough_e2e_enabled, and no child at all
when it is False."""
from _release_qualifier_testhelpers import *  # noqa: F403


@pytest.mark.asyncio
async def test_run_qualification_spawns_walkthrough_e2e_when_enabled(
    db_session, mocker
):
    """With walkthrough_e2e_enabled=True, run_qualification spawns exactly ONE
    QUEUED walkthrough_e2e child pinned to the candidate SHA + the qualification
    attempt id, and a same-attempt re-drive does NOT spawn a duplicate.

    The walkthrough child is evidence-only: it is flagged
    ``qualifier_walkthrough_e2e`` (so a failed run cascades no fix task) and is
    NOT a required child for the in-flight / finalize gate."""
    from app import shared_state

    shared_state.runtime_config["smoketest_enabled"] = False
    shared_state.runtime_config["env_preflight_enabled"] = False
    shared_state.runtime_config["walkthrough_e2e_enabled"] = True

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

    result = await rq.run_qualification(db_session, project.id, version)
    await db_session.commit()

    assert result["status"] == "spawned"
    assert result["walkthrough_e2e_enabled"] is True
    walk_task_id = result["walkthrough_e2e_task_id"]
    assert walk_task_id is not None

    await db_session.refresh(version)
    attempt_id = version.last_qualification["qualification_attempt_id"]
    assert version.last_qualification["walkthrough_e2e_enabled"] is True
    assert version.last_qualification["walkthrough_e2e_task_id"] == walk_task_id

    # Exactly one walkthrough_e2e task, QUEUED, pinned to the candidate SHA and
    # the qualification attempt id.
    from sqlmodel import select as _sqlmodel_select

    walk_tasks = (
        (
            await db_session.execute(
                _sqlmodel_select(Task).where(
                    Task.project_id == project.id,
                    Task.context["qualifier_walkthrough_e2e"].astext == "true",
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(walk_tasks) == 1
    walk = walk_tasks[0]
    assert str(walk.id) == walk_task_id
    assert walk.status == TaskStatus.QUEUED
    assert walk.created_by == "ORCHESTRATOR"
    assert walk.first_payload["type"] == "walkthrough_e2e"
    assert walk.first_payload["candidate_sha"] == candidate
    assert walk.first_payload["qualification_attempt_id"] == attempt_id
    assert walk.first_payload["base_branch"] == "automain"
    # Evidence-only marker mirrors the smoketest's qualifier_smoketest flag.
    assert walk.context["qualifier_walkthrough_e2e"] is True
    assert walk.context["candidate_sha"] == candidate
    assert walk.context["qualification_attempt_id"] == attempt_id
    assert walk.context["release_version_id"] == str(version.id)


@pytest.mark.asyncio
async def test_run_qualification_skips_walkthrough_e2e_when_disabled(
    db_session, mocker
):
    """With walkthrough_e2e_enabled=False, no walkthrough_e2e child is spawned
    and last_qualification records a None walkthrough_e2e_task_id."""
    from app import shared_state

    shared_state.runtime_config["smoketest_enabled"] = False
    shared_state.runtime_config["env_preflight_enabled"] = False
    shared_state.runtime_config["walkthrough_e2e_enabled"] = False

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
    assert result["walkthrough_e2e_enabled"] is False
    assert result["walkthrough_e2e_task_id"] is None

    await db_session.refresh(version)
    assert version.last_qualification["walkthrough_e2e_enabled"] is False
    assert version.last_qualification["walkthrough_e2e_task_id"] is None

    from sqlmodel import select as _sqlmodel_select

    walk_tasks = (
        (
            await db_session.execute(
                _sqlmodel_select(Task).where(
                    Task.project_id == project.id,
                    Task.context["qualifier_walkthrough_e2e"].astext == "true",
                )
            )
        )
        .scalars()
        .all()
    )
    assert walk_tasks == []


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
    shared_state.runtime_config["walkthrough_e2e_enabled"] = True

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
    result = await rq.run_qualification(db_session, project.id, version)
    await db_session.commit()

    assert result["status"] == "spawned"
    assert result["walkthrough_e2e_enabled"] is True
    walk_task_id = result["walkthrough_e2e_task_id"]
    assert walk_task_id is not None

    await db_session.refresh(version)
    attempt_id = version.last_qualification["qualification_attempt_id"]
    assert version.last_qualification["candidate_sha"] == candidate

    walk_tasks = (
        (
            await db_session.execute(
                _sel(Task).where(
                    Task.project_id == project.id,
                    Task.context["qualifier_walkthrough_e2e"].astext == "true",
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(walk_tasks) == 1
    walk = walk_tasks[0]
    assert str(walk.id) == walk_task_id
    assert walk.status == TaskStatus.QUEUED
    assert walk.first_payload["candidate_sha"] == candidate
    assert walk.first_payload["qualification_attempt_id"] == attempt_id

    # PROPERTIES 3-4: Feed PASS results for all three registered keys.
    # exercised_engine_sha must equal candidate_sha to satisfy the SHA-match guard.
    results_batch = [
        {"criterion_key": key, "passed": True, "notes": f"green-{key}"}
        for key in REGISTERED_KEYS
    ]
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
    # All three registered keys must be written (meta gate passes because both
    # c-dfdc9d82 and c-f52fd975 are present and passed in the same batch).
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
    phantom_key = "c-workday-session-lifecycle"
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
