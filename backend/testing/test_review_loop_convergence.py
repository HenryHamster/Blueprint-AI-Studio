"""Tests for review_loop_convergence cap-exhaustion preservation.

Covers FIX #3b (preserve mergeable work on cap exhaustion) and its extension to
back-merge conflict-resolver PRs: a task carrying
``context['backmerge_conflict'] == True`` is a ``create`` task (NOT a rollup),
but its PR re-applies expensive main→automain sync work, so a mergeable,
CI-green back-merge resolver PR must be PRESERVED on cap exhaustion exactly like
a rollup — left open + requires_approval for hand-merge, NOT auto-closed.

These import the handler directly from ``app.review_loop_convergence`` (rather
than the ``app.review_pipeline`` re-export) so the module owns its own tests.
"""

import uuid
from unittest.mock import AsyncMock

import pytest

from app.models import Project, TaskStatus
from testing.factories import make_project, make_task


async def _make_db_project(db_session, **project_kwargs) -> Project:
    """Persist a project and return the refreshed instance."""
    project = make_project(**project_kwargs)
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)
    return project


def _mock_preservable_pr(mocker, *, changed_files: int, mergeable_state: str):
    """Mock GitHub side effects + get_pull_request for the preserve-work path.

    Patches at the source modules because ``_handle_cap_exhaustion`` /
    ``_pr_is_preservable_on_cap`` import these lazily inside the function body.
    Returns the ``close_pull_request`` mock so callers can assert the PR was
    (not) closed.
    """
    mocker.patch(
        "app.routers.agent.get_github_token_for_task",
        new_callable=AsyncMock,
        return_value=("tok", "o", "r"),
    )
    close_mock = mocker.patch(
        "app.github.close_pull_request", new_callable=AsyncMock, return_value=True
    )
    mocker.patch("app.github.create_pr_comment", new_callable=AsyncMock)
    mocker.patch(
        "app.github.get_pull_request",
        new_callable=AsyncMock,
        return_value={
            "state": "open",
            "changed_files": changed_files,
            "mergeable_state": mergeable_state,
        },
    )
    return close_mock


def _mock_pr_lookup_fails(mocker):
    """Mock the preserve path so the GitHub PR lookup raises (unknown state)."""
    mocker.patch(
        "app.routers.agent.get_github_token_for_task",
        new_callable=AsyncMock,
        return_value=("tok", "o", "r"),
    )
    close_mock = mocker.patch(
        "app.github.close_pull_request", new_callable=AsyncMock, return_value=True
    )
    mocker.patch("app.github.create_pr_comment", new_callable=AsyncMock)
    mocker.patch(
        "app.github.get_pull_request",
        new_callable=AsyncMock,
        side_effect=RuntimeError("GitHub down"),
    )
    return close_mock


# ── _pr_is_preservable_on_cap: back-merge resolver branch ────────────────────


@pytest.mark.asyncio
async def test_backmerge_resolver_mergeable_is_preservable(db_session, mocker):
    """A back-merge resolver PR (context.backmerge_conflict=True), non-empty +
    mergeable_state 'clean', is preservable even though it is NOT a rollup."""
    _mock_preservable_pr(mocker, changed_files=3, mergeable_state="clean")
    from app.review_loop_convergence import _pr_is_preservable_on_cap

    project = await _make_db_project(db_session)
    orig = make_task(
        project_id=project.id,
        status=TaskStatus.EVALUATING,
        payloads=[{"type": "create", "prompt": "resolve", "base_branch": "automain"}],
        context={"backmerge_conflict": True, "backmerge_pr_number": 700},
    )
    db_session.add(orig)
    await db_session.commit()
    await db_session.refresh(orig)

    assert await _pr_is_preservable_on_cap(orig, 700, db_session) is True


@pytest.mark.asyncio
async def test_backmerge_resolver_dirty_is_not_preservable(db_session, mocker):
    """A back-merge resolver PR with a genuine merge conflict
    (mergeable_state 'dirty') cannot be hand-merged → NOT preservable."""
    _mock_preservable_pr(mocker, changed_files=3, mergeable_state="dirty")
    from app.review_loop_convergence import _pr_is_preservable_on_cap

    project = await _make_db_project(db_session)
    orig = make_task(
        project_id=project.id,
        status=TaskStatus.EVALUATING,
        payloads=[{"type": "create", "prompt": "resolve", "base_branch": "automain"}],
        context={"backmerge_conflict": True},
    )
    db_session.add(orig)
    await db_session.commit()
    await db_session.refresh(orig)

    assert await _pr_is_preservable_on_cap(orig, 700, db_session) is False


@pytest.mark.asyncio
async def test_backmerge_resolver_ci_red_unstable_is_not_preservable(
    db_session, mocker
):
    """A back-merge resolver PR that is mergeable but CI-red
    (mergeable_state 'unstable') is NOT preservable — it is not green."""
    _mock_preservable_pr(mocker, changed_files=3, mergeable_state="unstable")
    from app.review_loop_convergence import _pr_is_preservable_on_cap

    project = await _make_db_project(db_session)
    orig = make_task(
        project_id=project.id,
        status=TaskStatus.EVALUATING,
        payloads=[{"type": "create", "prompt": "resolve", "base_branch": "automain"}],
        context={"backmerge_conflict": True},
    )
    db_session.add(orig)
    await db_session.commit()
    await db_session.refresh(orig)

    assert await _pr_is_preservable_on_cap(orig, 700, db_session) is False


@pytest.mark.asyncio
async def test_backmerge_resolver_empty_is_not_preservable(db_session, mocker):
    """A confirmed-empty back-merge resolver PR (changed_files == 0) has nothing
    to hand-merge → NOT preservable, even though closing a resolver is normally
    the worse outcome."""
    _mock_preservable_pr(mocker, changed_files=0, mergeable_state="clean")
    from app.review_loop_convergence import _pr_is_preservable_on_cap

    project = await _make_db_project(db_session)
    orig = make_task(
        project_id=project.id,
        status=TaskStatus.EVALUATING,
        payloads=[{"type": "create", "prompt": "resolve", "base_branch": "automain"}],
        context={"backmerge_conflict": True},
    )
    db_session.add(orig)
    await db_session.commit()
    await db_session.refresh(orig)

    assert await _pr_is_preservable_on_cap(orig, 700, db_session) is False


@pytest.mark.asyncio
async def test_backmerge_resolver_unknown_state_preserves(db_session, mocker):
    """CONSERVATIVE: a back-merge resolver whose mergeable_state is 'unknown'
    (GitHub hasn't computed it) is PRESERVED — closing a resolver is the worse
    outcome since it can always be hand-merged. (A rollup would NOT preserve
    here.)"""
    _mock_preservable_pr(mocker, changed_files=3, mergeable_state="unknown")
    from app.review_loop_convergence import _pr_is_preservable_on_cap

    project = await _make_db_project(db_session)
    orig = make_task(
        project_id=project.id,
        status=TaskStatus.EVALUATING,
        payloads=[{"type": "create", "prompt": "resolve", "base_branch": "automain"}],
        context={"backmerge_conflict": True},
    )
    db_session.add(orig)
    await db_session.commit()
    await db_session.refresh(orig)

    assert await _pr_is_preservable_on_cap(orig, 700, db_session) is True


@pytest.mark.asyncio
async def test_backmerge_resolver_lookup_failure_preserves(db_session, mocker):
    """CONSERVATIVE: when the GitHub PR lookup FAILS for a back-merge resolver,
    PRESERVE (a rollup would fall back to close+FAIL)."""
    _mock_pr_lookup_fails(mocker)
    from app.review_loop_convergence import _pr_is_preservable_on_cap

    project = await _make_db_project(db_session)
    orig = make_task(
        project_id=project.id,
        status=TaskStatus.EVALUATING,
        payloads=[{"type": "create", "prompt": "resolve", "base_branch": "automain"}],
        context={"backmerge_conflict": True},
    )
    db_session.add(orig)
    await db_session.commit()
    await db_session.refresh(orig)

    assert await _pr_is_preservable_on_cap(orig, 700, db_session) is True


@pytest.mark.asyncio
async def test_rollup_lookup_failure_still_closes(db_session, mocker):
    """GUARDRAIL: rollup behavior unchanged — a rollup PR whose GitHub lookup
    FAILS is NOT preservable (falls back to the safe destructive path)."""
    _mock_pr_lookup_fails(mocker)
    from app.review_loop_convergence import _pr_is_preservable_on_cap

    project = await _make_db_project(db_session)
    orig = make_task(
        project_id=project.id,
        status=TaskStatus.EVALUATING,
        payloads=[{"type": "create", "prompt": "Build X", "base_branch": "main"}],
        context={"_is_rollup_review": True, "rollup_pr_number": 800},
    )
    db_session.add(orig)
    await db_session.commit()
    await db_session.refresh(orig)

    assert await _pr_is_preservable_on_cap(orig, 800, db_session) is False


@pytest.mark.asyncio
async def test_rollup_unknown_state_still_closes(db_session, mocker):
    """GUARDRAIL: rollup behavior unchanged — a rollup PR with an 'unknown'
    mergeable_state is NOT preservable."""
    _mock_preservable_pr(mocker, changed_files=4, mergeable_state="unknown")
    from app.review_loop_convergence import _pr_is_preservable_on_cap

    project = await _make_db_project(db_session)
    orig = make_task(
        project_id=project.id,
        status=TaskStatus.EVALUATING,
        payloads=[{"type": "create", "prompt": "Build X", "base_branch": "main"}],
        context={"_is_rollup_review": True, "rollup_pr_number": 800},
    )
    db_session.add(orig)
    await db_session.commit()
    await db_session.refresh(orig)

    assert await _pr_is_preservable_on_cap(orig, 800, db_session) is False


@pytest.mark.asyncio
async def test_rollup_clean_state_is_preservable(db_session, mocker):
    """GUARDRAIL: rollup behavior unchanged — a non-empty, clean rollup PR is
    still preservable."""
    _mock_preservable_pr(mocker, changed_files=4, mergeable_state="clean")
    from app.review_loop_convergence import _pr_is_preservable_on_cap

    project = await _make_db_project(db_session)
    orig = make_task(
        project_id=project.id,
        status=TaskStatus.EVALUATING,
        payloads=[{"type": "create", "prompt": "Build X", "base_branch": "main"}],
        context={"_is_rollup_review": True, "rollup_pr_number": 800},
    )
    db_session.add(orig)
    await db_session.commit()
    await db_session.refresh(orig)

    assert await _pr_is_preservable_on_cap(orig, 800, db_session) is True


@pytest.mark.asyncio
async def test_plain_create_task_is_not_preservable(db_session, mocker):
    """GUARDRAIL: a plain create task (no rollup markers, no backmerge_conflict)
    is NOT preservable even when its PR is non-empty + clean."""
    _mock_preservable_pr(mocker, changed_files=4, mergeable_state="clean")
    from app.review_loop_convergence import _pr_is_preservable_on_cap

    project = await _make_db_project(db_session)
    orig = make_task(
        project_id=project.id,
        status=TaskStatus.EVALUATING,
        payloads=[{"type": "create", "prompt": "Build X", "base_branch": "main"}],
        context={"scope": "medium"},
    )
    db_session.add(orig)
    await db_session.commit()
    await db_session.refresh(orig)

    assert await _pr_is_preservable_on_cap(orig, 900, db_session) is False


# ── _handle_cap_exhaustion: end-to-end preservation behavior ─────────────────


@pytest.mark.asyncio
async def test_cap_preserves_backmerge_resolver_pr(db_session, mocker):
    """A back-merge conflict-resolver PR (context.backmerge_conflict=True),
    non-empty + CI-green at cap exhaustion, is PRESERVED: left OPEN, flagged
    requires_approval, NO CODE_ERROR, NO pr_rejected, NO PR close — mirroring
    the rollup-preservation behavior."""
    close_mock = _mock_preservable_pr(mocker, changed_files=3, mergeable_state="clean")
    alert_mock = mocker.patch(
        "app.alert_dispatch._maybe_create_failure_alert", new_callable=AsyncMock
    )
    rec_task = mocker.patch("app.routers.agent_status.record_task_result")
    rec_dir = mocker.patch("app.shared_state.record_direction_result")
    mocker.patch(
        "app.task_completion._record_remediation_if_terminal", new_callable=AsyncMock
    )

    from app.review_loop_convergence import _handle_cap_exhaustion

    project = await _make_db_project(db_session)
    orig = make_task(
        project_id=project.id,
        status=TaskStatus.EVALUATING,
        payloads=[
            {"type": "create", "prompt": "resolve sync", "base_branch": "automain"}
        ],
        context={
            "backmerge_conflict": True,
            "backmerge_pr_number": 710,
            # Genuine non-convergence: latest round has outstanding changes.
            "review_loop": {
                "pr_number": 710,
                "review_history": [
                    {"verdict": "CHANGES_REQUESTED", "issues": ["x"]},
                ],
            },
        },
    )
    db_session.add(orig)
    source_task = make_task(
        project_id=project.id,
        status=TaskStatus.IN_PROGRESS,
        payloads=[{"type": "fix", "pr_number": 710, "original_task_id": str(orig.id)}],
        context={"review_loop": {"pr_number": 710}},
    )
    db_session.add(source_task)
    await db_session.commit()
    await db_session.refresh(orig)
    await db_session.refresh(source_task)

    await _handle_cap_exhaustion(
        source_task, str(orig.id), 710, 16, db_session, scope="per-chain"
    )

    await db_session.refresh(orig)
    assert (orig.context or {}).get("failure_detail") == (
        "review_cap_preserved_for_handmerge"
    ), "Preservable back-merge resolver PR must be tagged for hand-merge"
    assert (orig.context or {}).get("pr_preserved_for_handmerge") is True
    assert orig.requires_approval is True, "Preserved PR must be flagged for operator"
    assert (orig.context or {}).get("failure_category") != "CODE_ERROR", (
        "Preserving salvageable back-merge work must NOT record CODE_ERROR"
    )
    assert (orig.context or {}).get("pr_rejected") is not True
    close_mock.assert_not_called()  # PR LEFT OPEN
    rec_task.assert_not_called()
    rec_dir.assert_not_called()
    alert_mock.assert_awaited()  # operator still alerted


@pytest.mark.asyncio
async def test_cap_closes_nonrollup_nonbackmerge_pr(db_session, mocker):
    """GUARDRAIL: a plain create task (no rollup markers, no backmerge_conflict)
    at cap exhaustion still takes the DESTRUCTIVE path — FAILED + CODE_ERROR +
    pr_rejected + PR closed — even when its PR is non-empty + clean."""
    close_mock = _mock_preservable_pr(mocker, changed_files=4, mergeable_state="clean")
    mocker.patch(
        "app.alert_dispatch._maybe_create_failure_alert", new_callable=AsyncMock
    )
    mocker.patch("app.routers.agent_status.record_task_result")
    mocker.patch("app.shared_state.record_direction_result")
    mocker.patch(
        "app.task_completion._record_remediation_if_terminal", new_callable=AsyncMock
    )

    from app.review_loop_convergence import _handle_cap_exhaustion

    project = await _make_db_project(db_session)
    orig = make_task(
        project_id=project.id,
        status=TaskStatus.EVALUATING,
        payloads=[{"type": "create", "prompt": "Build X", "base_branch": "main"}],
        context={
            "scope": "medium",
            # No rollup markers, no backmerge_conflict.
            "review_loop": {
                "pr_number": 720,
                "review_history": [
                    {"verdict": "CHANGES_REQUESTED", "issues": ["x"]},
                ],
            },
        },
    )
    db_session.add(orig)
    source_task = make_task(
        project_id=project.id,
        status=TaskStatus.IN_PROGRESS,
        payloads=[{"type": "fix", "pr_number": 720, "original_task_id": str(orig.id)}],
        context={"review_loop": {"pr_number": 720}},
    )
    db_session.add(source_task)
    await db_session.commit()
    await db_session.refresh(orig)
    await db_session.refresh(source_task)

    await _handle_cap_exhaustion(
        source_task, str(orig.id), 720, 16, db_session, scope="per-chain"
    )

    await db_session.refresh(orig)
    assert orig.status == TaskStatus.FAILED
    assert (orig.context or {}).get("failure_detail") == "review_cap_exhausted"
    assert (orig.context or {}).get("failure_category") == "CODE_ERROR"
    assert (orig.context or {}).get("pr_rejected") is True
    close_mock.assert_awaited()  # destructive path closes the PR


@pytest.mark.asyncio
async def test_cap_preserves_rollup_pr_unchanged(db_session, mocker):
    """GUARDRAIL: existing rollup preservation still works — a non-empty, clean
    rollup PR at cap exhaustion is left OPEN + requires_approval, no CODE_ERROR,
    no close."""
    close_mock = _mock_preservable_pr(mocker, changed_files=4, mergeable_state="clean")
    alert_mock = mocker.patch(
        "app.alert_dispatch._maybe_create_failure_alert", new_callable=AsyncMock
    )
    rec_task = mocker.patch("app.routers.agent_status.record_task_result")
    rec_dir = mocker.patch("app.shared_state.record_direction_result")
    mocker.patch(
        "app.task_completion._record_remediation_if_terminal", new_callable=AsyncMock
    )

    from app.review_loop_convergence import _handle_cap_exhaustion

    project = await _make_db_project(db_session)
    orig = make_task(
        project_id=project.id,
        status=TaskStatus.EVALUATING,
        payloads=[{"type": "create", "prompt": "Build X", "base_branch": "main"}],
        context={
            "scope": "medium",
            "_is_rollup_review": True,
            "rollup_pr_number": 730,
            "review_loop": {
                "pr_number": 730,
                "review_history": [
                    {"verdict": "CHANGES_REQUESTED", "issues": ["x"]},
                ],
            },
        },
    )
    db_session.add(orig)
    source_task = make_task(
        project_id=project.id,
        status=TaskStatus.IN_PROGRESS,
        payloads=[{"type": "fix", "pr_number": 730, "original_task_id": str(orig.id)}],
        context={"review_loop": {"pr_number": 730}},
    )
    db_session.add(source_task)
    await db_session.commit()
    await db_session.refresh(orig)
    await db_session.refresh(source_task)

    await _handle_cap_exhaustion(
        source_task, str(orig.id), 730, 16, db_session, scope="per-chain"
    )

    await db_session.refresh(orig)
    assert (orig.context or {}).get("failure_detail") == (
        "review_cap_preserved_for_handmerge"
    )
    assert (orig.context or {}).get("pr_preserved_for_handmerge") is True
    assert orig.requires_approval is True
    assert (orig.context or {}).get("failure_category") != "CODE_ERROR"
    assert (orig.context or {}).get("pr_rejected") is not True
    close_mock.assert_not_called()
    rec_task.assert_not_called()
    rec_dir.assert_not_called()
    alert_mock.assert_awaited()


@pytest.mark.asyncio
async def test_is_backmerge_resolver_helper(db_session):
    """The _is_backmerge_resolver predicate is True only for
    context.backmerge_conflict == True."""
    from app.review_loop_convergence import _is_backmerge_resolver

    yes = make_task(project_id=uuid.uuid4(), context={"backmerge_conflict": True})
    no_flag = make_task(project_id=uuid.uuid4(), context={"backmerge_conflict": False})
    no_key = make_task(project_id=uuid.uuid4(), context={})
    none_ctx = make_task(project_id=uuid.uuid4())
    none_ctx.context = None

    assert _is_backmerge_resolver(yes) is True
    assert _is_backmerge_resolver(no_flag) is False
    assert _is_backmerge_resolver(no_key) is False
    assert _is_backmerge_resolver(none_ctx) is False
