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
when it is False (the ships-dark default)."""
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
    """With walkthrough_e2e_enabled=False (the ships-dark default), no
    walkthrough_e2e child is spawned and last_qualification records a None
    walkthrough_e2e_task_id."""
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
