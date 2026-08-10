"""Acceptance coverage for periodic terminal rollup PR reconciliation."""

from unittest.mock import AsyncMock

import pytest
from app.models import TaskStatus
from app.operator_attention import _task_attention_item
from app import terminal_rollup_pr_sweep as sweep_module
from app.terminal_rollup_pr_sweep import (
    TERMINAL_ROLLUP_PR_ATTENTION_KEY,
    TERMINAL_ROLLUP_PR_SWEEP_REQUEUES_KEY,
    sweep_terminal_rollup_prs,
)
from testing.factories import make_project, make_task


async def _make_parent(db_session, project, *, pr_number: int, context=None):
    parent = make_task(
        project.id,
        title=f"Rollup {pr_number}",
        payloads=[{"type": "research", "prompt": "reconcile"}],
        status=TaskStatus.FAILED,
        context={
            "research_branch": f"research/parent-{pr_number}",
            "rollup_pr_number": pr_number,
            "rollup_pr_url": f"https://example.test/pulls/{pr_number}",
            **(context or {}),
        },
    )
    db_session.add(parent)
    await db_session.flush()
    child = make_task(
        project.id,
        title=f"Child {pr_number}",
        payloads=[{"type": "create", "prompt": "landed"}],
        parent_task_id=parent.id,
        status=TaskStatus.COMPLETED,
    )
    db_session.add(child)
    await db_session.commit()
    await db_session.refresh(parent)
    return parent


@pytest.mark.asyncio
async def test_merged_pr_auto_requeues_and_ambiguous_outcomes_do_not(
    db_session, mocker
):
    """Merged evidence requeues; dead or uncertain evidence stays FAILED and alerts attention."""

    project = make_project(integration_branch="automain")
    db_session.add(project)
    await db_session.flush()
    merged = await _make_parent(db_session, project, pr_number=17101)
    outcomes = {
        17102: "closed_pr_released",
        17103: "no_pr_cleared",
        17104: "lookup_uncertain_identity_preserved",
        17105: "replacement_lookup_uncertain_identity_preserved",
    }
    ambiguous = {
        number: await _make_parent(db_session, project, pr_number=number)
        for number in outcomes
    }

    async def reconcile(task, session):
        if task.id == merged.id:
            return "merged_pr_rearmed"
        return outcomes[int(task.context["rollup_pr_number"])]

    trigger = mocker.patch(
        "app.terminal_rollup_pr_sweep._trigger_merged_parent_classifier",
        new_callable=AsyncMock,
    )
    mocker.patch(
        "app.terminal_rollup_requeue.reconcile_terminal_rollup_requeue",
        new=AsyncMock(side_effect=reconcile),
    )

    result = await sweep_terminal_rollup_prs(db_session)

    await db_session.refresh(merged)
    assert merged.status == TaskStatus.EVALUATING
    assert merged.context[TERMINAL_ROLLUP_PR_SWEEP_REQUEUES_KEY] == 1
    trigger.assert_awaited_once()
    for task in ambiguous.values():
        await db_session.refresh(task)
        assert task.status == TaskStatus.FAILED
        assert task.context[TERMINAL_ROLLUP_PR_ATTENTION_KEY]["outcome"] == outcomes[
            int(task.context["rollup_pr_number"])
        ]
    assert result["merged_pr_rearmed"] == 1
    assert result["lookup_uncertain_identity_preserved"] == 1

    attention_items = [
        _task_attention_item(
            task,
            reason="terminal_rollup_pr_attention",
            severity="needs_operator",
        )
        for task in ambiguous.values()
    ]
    assert {item["reason"] for item in attention_items} == {
        "terminal_rollup_pr_attention"
    }
    assert all(item["severity"] == "needs_operator" for item in attention_items)
    assert {item["ref"]["id"] for item in attention_items} == {
        str(task.id) for task in ambiguous.values()
    }


@pytest.mark.asyncio
async def test_terminal_rollup_sweep_cap_routes_live_pr_to_attention(
    db_session, mocker
):
    project = make_project()
    db_session.add(project)
    await db_session.flush()
    parent = await _make_parent(
        db_session,
        project,
        pr_number=17201,
        context={TERMINAL_ROLLUP_PR_SWEEP_REQUEUES_KEY: 1},
    )
    reconcile = mocker.patch(
        "app.terminal_rollup_requeue.reconcile_terminal_rollup_requeue",
        new_callable=AsyncMock,
        return_value="open_pr_rearmed",
    )

    await sweep_terminal_rollup_prs(db_session)

    await db_session.refresh(parent)
    assert parent.status == TaskStatus.FAILED
    attention = parent.context[TERMINAL_ROLLUP_PR_ATTENTION_KEY]
    assert attention["outcome"] == "open_pr_rearmed"
    assert attention["cap_exhausted"] is True
    assert attention["marked_at"]
    reconcile.assert_awaited_once()


@pytest.mark.asyncio
async def test_terminal_rollup_sweep_rechecks_under_task_lock(db_session, mocker):
    project = make_project()
    db_session.add(project)
    await db_session.flush()
    parent = await _make_parent(db_session, project, pr_number=17301)
    mocker.patch(
        "app.terminal_rollup_requeue.reconcile_terminal_rollup_requeue",
        new_callable=AsyncMock,
        return_value="no_pr_cleared",
    )
    statements = []
    execute = db_session.execute

    async def recording_execute(statement, *args, **kwargs):
        statements.append(statement)
        return await execute(statement, *args, **kwargs)

    mocker.patch.object(db_session, "execute", side_effect=recording_execute)
    await sweep_terminal_rollup_prs(db_session)

    lock_statements = [
        statement for statement in statements if statement._for_update_arg is not None
    ]
    assert lock_statements
    assert "WHERE task.id" in str(lock_statements[0])
    assert parent.id in lock_statements[0].compile().params.values()


@pytest.mark.asyncio
async def test_terminal_rollup_sweep_is_idempotent_after_attention(db_session, mocker):
    project = make_project()
    db_session.add(project)
    await db_session.flush()
    parent = await _make_parent(db_session, project, pr_number=17401)
    reconcile = mocker.patch(
        "app.terminal_rollup_requeue.reconcile_terminal_rollup_requeue",
        new_callable=AsyncMock,
        return_value="lookup_uncertain_identity_preserved",
    )

    first = await sweep_terminal_rollup_prs(db_session)
    await db_session.refresh(parent)
    context_after_first = dict(parent.context)
    second = await sweep_terminal_rollup_prs(db_session)

    assert first == {"lookup_uncertain_identity_preserved": 1}
    assert second == {}
    reconcile.assert_awaited_once()
    await db_session.refresh(parent)
    assert parent.status == TaskStatus.FAILED
    assert parent.context == context_after_first


@pytest.mark.asyncio
async def test_terminal_rollup_sweep_uses_own_multi_minute_cadence(mocker):
    sweep_module._last_sweep_monotonic = None
    run = mocker.patch(
        "app.terminal_rollup_pr_sweep.sweep_terminal_rollup_prs",
        new_callable=AsyncMock,
        return_value={"merged_pr_rearmed": 1},
    )
    mocker.patch(
        "app.terminal_rollup_pr_sweep._monotonic",
        side_effect=(100.0, 100.0 + 1.0, 100.0 + 301.0),
    )

    assert await sweep_module.maybe_sweep_terminal_rollup_prs(None) == {
        "merged_pr_rearmed": 1
    }
    assert await sweep_module.maybe_sweep_terminal_rollup_prs(None) == {}
    assert await sweep_module.maybe_sweep_terminal_rollup_prs(None) == {
        "merged_pr_rearmed": 1
    }
    assert run.await_count == 2
