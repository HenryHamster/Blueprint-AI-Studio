"""
Tests that verify SR compute functions are called from the agent task-completion path.
"""

import os
from unittest.mock import patch, AsyncMock

import pytest
from httpx import AsyncClient

from app.models import (
    Project,
    Task,
)


@pytest.mark.asyncio
async def test_compute_sr_false_attestation_trends_called_on_claim(
    client: AsyncClient, db_session, mocker
):
    """
    Verify that compute_sr_false_attestation_trends is called when claiming a
    create/fix/refactor task.
    """
    project = Project(name="Test Project", user_id="test-user")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = Task(
        project_id=project.id,
        payloads=[{"type": "create", "description": "test"}],
        vm_instance_id="test-vm",
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    from app.auth import create_agent_token

    token = create_agent_token(str(task.id), "test-vm")
    headers = {"x-agent-token": token}

    mock_fn = AsyncMock()
    with patch(
        "app.sr_analytics.compute_sr_false_attestation_trends",
        new=mock_fn,
    ):
        resp = await client.post("/api/agent/claim", headers=headers)
        assert resp.status_code == 200
        mock_fn.assert_awaited_once()
        call_kwargs = mock_fn.call_args.kwargs
        assert call_kwargs["project_id"] == project.id
        assert call_kwargs["session"] is not None
        assert isinstance(call_kwargs["all_tasks"], list)


@pytest.mark.asyncio
async def test_refresh_sr_catch_rate_computes_on_first_claim(
    client: AsyncClient, db_session, mocker
):
    """The SR catch-rate trend is (re)computed on a claim when this instance
    has no recent recompute recorded for the project (first sight)."""
    import app.shared_state as _shared_state

    project = Project(name="Debounce First Claim", user_id="test-user")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    # Isolate the per-process debounce map from any prior test/claim.
    _shared_state.sr_catch_rate_last_refreshed.pop(str(project.id), None)

    task = Task(
        project_id=project.id,
        payloads=[{"type": "create", "description": "test"}],
        vm_instance_id="debounce-vm-first",
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    from app.auth import create_agent_token

    token = create_agent_token(str(task.id), "debounce-vm-first")
    headers = {"x-agent-token": token}

    mock_fn = AsyncMock(return_value=None)
    with patch("app.sr_analytics.compute_sr_catch_rate_trends", new=mock_fn):
        resp = await client.post("/api/agent/claim", headers=headers)
        assert resp.status_code == 200
        mock_fn.assert_awaited_once()
        call_kwargs = mock_fn.call_args.kwargs
        assert call_kwargs["project_id"] == project.id
        assert call_kwargs["session"] is not None
        assert isinstance(call_kwargs["all_tasks"], list)

    # A successful recompute must record the debounce marker.
    assert str(project.id) in _shared_state.sr_catch_rate_last_refreshed


@pytest.mark.asyncio
async def test_refresh_sr_catch_rate_debounced_on_second_claim_within_window(
    client: AsyncClient, db_session, mocker
):
    """Design-audit #2 (scale): the SR catch-rate recompute is a ~2000-row
    JSONB scan + a project write-lock that previously ran on EVERY claim,
    serializing claims per project at 100-200 VMs.

    This asserts the debounce: a first claim computes the trend (one call into
    the expensive ``compute_sr_catch_rate_trends``); a SECOND claim for the same
    project within the debounce window SKIPS the recompute entirely — the
    expensive function is not invoked a second time. The trend still updates,
    just at a bounded cadence rather than on literally every claim.
    """
    import app.shared_state as _shared_state

    project = Project(name="Debounce Second Claim", user_id="test-user")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    # Isolate the per-process debounce map and force a wide window so the second
    # claim is unambiguously inside it.
    _shared_state.sr_catch_rate_last_refreshed.pop(str(project.id), None)

    # Two QUEUED tasks on the SAME vm + project: each claim picks one.
    task1 = Task(
        project_id=project.id,
        payloads=[{"type": "create", "description": "one"}],
        vm_instance_id="debounce-vm-window",
    )
    task2 = Task(
        project_id=project.id,
        payloads=[{"type": "fix", "description": "two"}],
        vm_instance_id="debounce-vm-window",
    )
    db_session.add(task1)
    db_session.add(task2)
    await db_session.commit()
    await db_session.refresh(task1)
    await db_session.refresh(task2)

    from app.auth import create_agent_token

    headers1 = {"x-agent-token": create_agent_token(str(task1.id), "debounce-vm-window")}
    headers2 = {"x-agent-token": create_agent_token(str(task2.id), "debounce-vm-window")}

    mock_fn = AsyncMock(return_value=None)
    with (
        patch("app.sr_analytics.compute_sr_catch_rate_trends", new=mock_fn),
        patch.object(_shared_state, "SR_CATCH_RATE_DEBOUNCE_SECONDS", 3600),
    ):
        resp1 = await client.post("/api/agent/claim", headers=headers1)
        assert resp1.status_code == 200
        # First claim recomputes.
        assert mock_fn.await_count == 1

        resp2 = await client.post("/api/agent/claim", headers=headers2)
        assert resp2.status_code == 200
        # Second claim within the window is debounced: NO second recompute.
        assert mock_fn.await_count == 1, (
            "second claim within the debounce window must skip the expensive "
            f"SR catch-rate recompute, got {mock_fn.await_count} total calls"
        )


@pytest.mark.asyncio
async def test_should_refresh_sr_catch_rate_window_semantics():
    """Unit-level guard for the debounce gate: first sight refreshes; a fresh
    mark suppresses within the window; an aged mark re-enables refresh."""
    from datetime import timedelta

    import app.shared_state as _shared_state
    from app.time_utils import _utcnow

    pid = "debounce-unit-project"
    _shared_state.sr_catch_rate_last_refreshed.pop(pid, None)

    # First sight (no recorded refresh) → always refresh.
    assert _shared_state.should_refresh_sr_catch_rate(pid, window_seconds=600) is True

    # Just marked → within the window → skip.
    _shared_state.mark_sr_catch_rate_refreshed(pid)
    assert _shared_state.should_refresh_sr_catch_rate(pid, window_seconds=600) is False

    # Marker older than the window → refresh again.
    _shared_state.sr_catch_rate_last_refreshed[pid] = _utcnow() - timedelta(seconds=601)
    assert _shared_state.should_refresh_sr_catch_rate(pid, window_seconds=600) is True

    _shared_state.sr_catch_rate_last_refreshed.pop(pid, None)


@pytest.mark.asyncio
async def test_compute_sr_regression_risk_test_signal_trends_called_on_claim(
    client: AsyncClient, db_session, mocker
):
    """
    Verify that compute_sr_regression_risk_test_signal_trends is called when
    claiming a create/fix/refactor task.
    """
    project = Project(name="Test Project", user_id="test-user")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = Task(
        project_id=project.id,
        payloads=[{"type": "fix", "description": "test"}],
        vm_instance_id="test-vm",
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    from app.auth import create_agent_token

    token = create_agent_token(str(task.id), "test-vm")
    headers = {"x-agent-token": token}

    mock_fn = AsyncMock()
    with patch(
        "app.sr_analytics.compute_sr_regression_risk_test_signal_trends",
        new=mock_fn,
    ):
        resp = await client.post("/api/agent/claim", headers=headers)
        assert resp.status_code == 200
        mock_fn.assert_awaited_once()
        call_kwargs = mock_fn.call_args.kwargs
        assert call_kwargs["project_id"] == project.id
        assert call_kwargs["session"] is not None
        assert isinstance(call_kwargs["all_tasks"], list)


@pytest.mark.asyncio
async def test_compute_fresh_reviewer_trend_called_on_claim(
    client: AsyncClient, db_session, mocker
):
    """
    Verify that compute_fresh_reviewer_trend is called when claiming a
    create/fix/refactor task.
    """
    project = Project(name="Test Project", user_id="test-user")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = Task(
        project_id=project.id,
        payloads=[{"type": "refactor", "description": "test"}],
        vm_instance_id="test-vm",
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    from app.auth import create_agent_token

    token = create_agent_token(str(task.id), "test-vm")
    headers = {"x-agent-token": token}

    mock_fn = AsyncMock()
    with patch(
        "app.sr_analytics.compute_fresh_reviewer_trend",
        new=mock_fn,
    ):
        resp = await client.post("/api/agent/claim", headers=headers)
        assert resp.status_code == 200
        mock_fn.assert_awaited_once()
        call_kwargs = mock_fn.call_args.kwargs
        assert call_kwargs["project_id"] == project.id
        assert call_kwargs["session"] is not None
        assert isinstance(call_kwargs["all_tasks"], list)


@pytest.mark.asyncio
async def test_sr_compute_functions_not_called_for_non_code_change_tasks(
    client: AsyncClient, db_session, mocker
):
    """
    Verify that SR compute functions are only called for create/fix/refactor
    tasks and not for other task types (e.g., research, plan).
    """
    project = Project(name="Test Project", user_id="test-user")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = Task(
        project_id=project.id,
        payloads=[{"type": "research", "description": "test"}],
        vm_instance_id="test-vm",
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    from app.auth import create_agent_token

    token = create_agent_token(str(task.id), "test-vm")
    headers = {"x-agent-token": token}

    mock_false_attestation = AsyncMock()
    mock_regression_risk = AsyncMock()
    mock_fresh_reviewer = AsyncMock()

    with (
        patch(
            "app.sr_analytics.compute_sr_false_attestation_trends",
            new=mock_false_attestation,
        ),
        patch(
            "app.sr_analytics.compute_sr_regression_risk_test_signal_trends",
            new=mock_regression_risk,
        ),
        patch(
            "app.sr_analytics.compute_fresh_reviewer_trend",
            new=mock_fresh_reviewer,
        ),
    ):
        resp = await client.post("/api/agent/claim", headers=headers)
        assert resp.status_code == 200
        # None of these should be called for non-code-change tasks
        mock_false_attestation.assert_not_awaited()
        mock_regression_risk.assert_not_awaited()
        mock_fresh_reviewer.assert_not_awaited()


@pytest.mark.asyncio
async def test_claim_high_iter_loadonly_keeps_sr_trend_columns_eager(db_session):
    """Regression (incident 2026-05-29): claim_task loads its high-iteration
    task rows with ``load_only`` for performance and passes them to the
    SR-trend analytics (``compute_*_trend``). Those functions read
    ``strategic_cycle_id``, ``updated_at`` and ``fix_loop_history`` off the rows
    via ``getattr``. If any is deferred, the access lazy-loads outside the async
    greenlet (``MissingGreenlet``), aborts the request transaction, and 500s the
    claim at the unguarded ``session.get(Project)`` downstream (agent.py:1889).

    This asserts those columns are eager-loaded so the ``getattr`` accesses
    never trigger IO. The load_only set below MIRRORS claim_task's
    ``high_iter_stmt`` — keep them in sync: if an sr_*_trends function starts
    reading another Task attribute off these rows, add the column both here and
    in claim_task.
    """
    from sqlalchemy import inspect as sa_inspect
    from sqlalchemy.orm import load_only
    from sqlmodel import select

    from app.models import TaskStatus

    project = Project(name="LoadOnly Regression", user_id="u")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = Task(
        project_id=project.id,
        payloads=[{"type": "create", "description": "x"}],
        status=TaskStatus.COMPLETED,
        strategic_cycle_id="cycle-abc123",
        fix_loop_history={"iterations": 2},
    )
    db_session.add(task)
    await db_session.commit()

    # Mirror of claim_task's high_iter_stmt load_only (backend/app/routers/agent.py).
    stmt = (
        select(Task)
        .options(
            load_only(
                Task.id,
                Task.title,
                Task.status,
                Task.payloads,
                Task.context,
                Task.latest_status_message,
                Task.created_at,
                Task.updated_at,
                Task.project_id,
                Task.account_id,
                Task.provider,
                Task.model,
                Task.strategic_cycle_id,
                Task.fix_loop_history,
            )
        )
        .where(Task.project_id == project.id)
    )
    rows = (await db_session.execute(stmt)).scalars().all()
    assert rows, "expected the seeded task to load"

    # Every Task attribute the sr_*_trends functions read off these rows must be
    # eager-loaded — otherwise getattr() triggers a MissingGreenlet lazy-load.
    sr_trend_reads = {
        "strategic_cycle_id",
        "updated_at",
        "fix_loop_history",
        "context",
        "created_at",
    }
    for t in rows:
        unloaded = sa_inspect(t).unloaded
        leaked = sr_trend_reads & set(unloaded)
        assert not leaked, (
            f"SR-trend columns deferred by claim_task load_only (would "
            f"MissingGreenlet on access): {sorted(leaked)}"
        )
        # The exact getattr accesses the trend functions perform — must not raise.
        _ = getattr(t, "strategic_cycle_id", None)
        _ = getattr(t, "updated_at", None)
        _ = getattr(t, "fix_loop_history", None)


@pytest.mark.asyncio
async def test_refresh_sr_catch_rate_projection_loads_updated_at(
    client, db_session, mocker
):
    """Regression: ``_refresh_sr_catch_rate`` (routers/agent.py) loads its
    28-day SR rows with ``load_only(..., raiseload=True)``.
    ``sr_analytics_common._updated_naive`` reads ``task.updated_at`` off those
    rows via ``getattr(task, "updated_at", None)`` — getattr does NOT suppress
    the raiseload ``InvalidRequestError`` (it only suppresses AttributeError),
    so with the column deferred the whole SR catch-rate trend update silently
    aborted (swallowed by the claim path's broad except). Assert the rows
    handed to ``compute_sr_catch_rate_trends`` have ``updated_at`` loaded."""
    from sqlalchemy import inspect as sa_inspect

    import app.shared_state as _shared_state
    from app.models import TaskStatus

    project = Project(name="CatchRate Projection", user_id="test-user")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    _shared_state.sr_catch_rate_last_refreshed.pop(str(project.id), None)

    # SR-relevant terminal row: matches the _sr_rows query filters
    # (status, created_at window, context has _self_review_outcome).
    sr_row = Task(
        project_id=project.id,
        status=TaskStatus.COMPLETED,
        payloads=[{"type": "create", "description": "landed"}],
        context={
            "_self_review_outcome": {
                "check_results": {"security": {"passed": True}}
            },
            "_first_attempt_rejection_categories": {"security": 1},
        },
    )
    claimable = Task(
        project_id=project.id,
        payloads=[{"type": "create", "description": "test"}],
        vm_instance_id="projection-vm",
    )
    db_session.add(sr_row)
    db_session.add(claimable)
    await db_session.commit()
    await db_session.refresh(claimable)

    from app.auth import create_agent_token

    headers = {"x-agent-token": create_agent_token(str(claimable.id), "projection-vm")}

    mock_fn = AsyncMock(return_value=None)
    with patch("app.sr_analytics.compute_sr_catch_rate_trends", new=mock_fn):
        resp = await client.post("/api/agent/claim", headers=headers)
        assert resp.status_code == 200
        mock_fn.assert_awaited_once()
        all_tasks = mock_fn.call_args.kwargs["all_tasks"]

    captured = [t for t in all_tasks if t.id == sr_row.id]
    assert captured, "seeded SR row must be in the catch-rate window"
    for t in captured:
        unloaded = sa_inspect(t).unloaded
        assert "updated_at" not in unloaded, (
            "updated_at deferred by the _refresh_sr_catch_rate load_only — "
            "sr_analytics_common._updated_naive would raise raiseload's "
            "InvalidRequestError and silently abort the catch-rate update"
        )
        _ = getattr(t, "updated_at", None)


@pytest.mark.asyncio
async def test_catch_rate_cycle_projection_loads_updated_at(db_session, mocker):
    """Regression: cycle-mode catch-rate rows are loaded inside
    ``compute_sr_catch_rate_trends`` (sr_analytics_catch_rate_impl.py) with
    ``load_only(..., raiseload=True)`` and flow into
    ``sr_analytics_common._compute_miss_latency_stats`` → ``_updated_naive``,
    which reads ``task.updated_at`` (getattr does NOT suppress the raiseload
    error). Assert the cycle-bucketed rows have ``updated_at`` loaded by
    capturing them at the ``_stats_for_tasks`` consumer."""
    from sqlalchemy import inspect as sa_inspect

    from app.models import StrategicCycle, TaskStatus
    from app.sr_analytics_catch_rate import compute_sr_catch_rate_trends
    import app.sr_analytics_catch_rate_impl as _impl
    from conftest import TestingSessionLocal

    project = Project(name="CatchRate Cycle Projection", user_id="test-user")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    from datetime import datetime, timedelta, timezone

    now = datetime.now(timezone.utc).replace(tzinfo=None)
    cycles = [
        StrategicCycle(
            id=f"cycle-proj-{i}",
            project_id=project.id,
            outcome="completed",
            phase1_started_at=now - timedelta(days=i + 1),
        )
        for i in range(2)
    ]
    tasks = [
        Task(
            project_id=project.id,
            status=TaskStatus.COMPLETED,
            strategic_cycle_id=c.id,
            payloads=[{"type": "create", "description": "x"}],
            context={
                "_self_review_outcome": {
                    "check_results": {"security": {"passed": True}}
                }
            },
        )
        for c in cycles
    ]
    db_session.add_all([*cycles, *tasks])
    await db_session.commit()
    seeded_ids = {t.id for t in tasks}

    # Pass-through spy on the first consumer of the cycle-bucketed rows.
    real_stats = _impl._stats_for_tasks
    captured: list = []

    def _spy(rows, *args, **kwargs):
        captured.extend(rows)
        return real_stats(rows, *args, **kwargs)

    mocker.patch.object(_impl, "_stats_for_tasks", side_effect=_spy)

    # Fresh session: an identity-map hit from db_session would bypass the
    # projection and mask the regression.
    async with TestingSessionLocal() as fresh_session:
        await compute_sr_catch_rate_trends(
            project_id=project.id, session=fresh_session, all_tasks=[]
        )

    captured_seeded = [t for t in captured if t.id in seeded_ids]
    assert captured_seeded, "cycle-bucketed rows must reach _stats_for_tasks"
    for t in captured_seeded:
        unloaded = sa_inspect(t).unloaded
        assert "updated_at" not in unloaded, (
            "updated_at deferred by the cycle-tasks load_only — "
            "_compute_miss_latency_stats/_updated_naive would raise raiseload's "
            "InvalidRequestError on the legacy-timestamp fallback path"
        )
        _ = getattr(t, "updated_at", None)


@pytest.mark.asyncio
async def test_sr_trend_cycle_query_narrowed_to_id_rows(db_session):
    """Regression (incident 2026-05-29): the SR-trend cycle query was narrowed
    from ``select(StrategicCycle)`` to ``select(StrategicCycle.id)`` + ``.all()``
    — only ``.id`` is used by the cycle bucketing, and the full-entity fetch
    detoasted JSONB needlessly. Backed by the new
    ix_strategiccycle_project_outcome_phase1 index it becomes an ordered index
    scan that satisfies ``LIMIT 4`` directly (was a full scan + sort of every
    completed cycle → 120s statement_timeout that poisoned the claim session).

    Guards the row-shape change: result rows must still expose ``.id`` (the
    bucketing does ``c_cycle.id`` / ``[r.id for r in cycle_rows]``), exclude
    non-completed cycles, and come back ordered by phase1_started_at DESC.
    """
    from datetime import datetime

    from sqlmodel import select

    from app.models import StrategicCycle

    project = Project(name="SR cycle query", user_id="u")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    db_session.add_all(
        [
            StrategicCycle(
                id="cyc-older",
                project_id=project.id,
                outcome="completed",
                phase1_started_at=datetime(2026, 5, 1, 12, 0, 0),
            ),
            StrategicCycle(
                id="cyc-newer",
                project_id=project.id,
                outcome="completed",
                phase1_started_at=datetime(2026, 5, 20, 12, 0, 0),
            ),
            # Non-completed → must be excluded by the outcome filter.
            StrategicCycle(
                id="cyc-running",
                project_id=project.id,
                outcome="in_progress",
                phase1_started_at=datetime(2026, 5, 25, 12, 0, 0),
            ),
        ]
    )
    await db_session.commit()

    # Mirror of the narrowed cycle_stmt in sr_*_trends.py / sr_catch_rate_trends.py.
    cycle_stmt = (
        select(StrategicCycle.id)
        .where(StrategicCycle.project_id == project.id)
        .where(StrategicCycle.outcome == "completed")
        .order_by(StrategicCycle.phase1_started_at.desc())
        .limit(4)
    )
    cycle_rows = (await db_session.execute(cycle_stmt)).all()

    # DESC by phase1_started_at, non-completed excluded, and .id accessible
    # (the exact attribute access the bucketing performs on each Row).
    assert [r.id for r in cycle_rows] == ["cyc-newer", "cyc-older"]
    assert cycle_rows[0].id == "cyc-newer"


@pytest.mark.skipif(
    os.getenv("USE_TEST_POSTGRES", "").lower() != "true",
    reason="transaction-abort poisoning is PostgreSQL-specific (SQLite tolerates "
    "a failed statement without aborting the surrounding transaction)",
)
@pytest.mark.asyncio
async def test_claim_survives_sr_trend_session_poisoning(
    client: AsyncClient, db_session
):
    """Regression (incident 2026-05-29): a best-effort SR-trend compute that
    aborts the claim session (prod: a 120s statement_timeout → QueryCanceledError)
    must NOT 500 the claim — the claim was already committed before enrichment.
    The pre-fix guard caught only PendingRollbackError and its retry could itself
    fail, so claims 500'd (~44 / 15 min). The broadened guard rolls back and
    re-reads, degrading to no-playbook if even that fails.
    """
    from sqlalchemy import text

    project = Project(name="Poison Project", user_id="test-user")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = Task(
        project_id=project.id,
        payloads=[{"type": "create", "description": "test"}],
        vm_instance_id="test-vm",
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    from app.auth import create_agent_token

    token = create_agent_token(str(task.id), "test-vm")
    headers = {"x-agent-token": token}

    async def _poison(**kwargs):
        # Run a failing statement on the claim session to abort its transaction,
        # reproducing the aborted-transaction state a statement_timeout leaves.
        await kwargs["session"].execute(
            text("SELECT * FROM _no_such_table_sr_trend_poison")
        )

    with patch("app.sr_analytics.compute_edges_trend", new=_poison):
        resp = await client.post("/api/agent/claim", headers=headers)

    assert resp.status_code == 200, (
        "claim must survive a poisoned best-effort enrichment session, got "
        f"{resp.status_code}: {resp.text[:300]}"
    )


@pytest.mark.skipif(
    os.getenv("USE_TEST_POSTGRES", "").lower() != "true",
    reason="transaction-abort poisoning is PostgreSQL-specific (SQLite tolerates "
    "a failed statement without aborting the surrounding transaction)",
)
@pytest.mark.asyncio
async def test_claim_survives_enrichment_poisoning_that_expires_task(
    client: AsyncClient, db_session
):
    """Regression (incident 2026-05-29, round 2): when a best-effort enrichment
    call aborts the claim session, the *subsequent* commit does an internal
    rollback that EXPIRES the ``task`` ORM instance. Every later UNGUARDED
    ``task.<attr>`` read — e.g. ``task.context`` at the direction-signal block
    (agent.py:2243) — then does a sync lazy-load with no greenlet ->
    MissingGreenlet (or PendingRollbackError on the still-aborted txn) and 500s
    the claim, even though the claim was already committed. This is distinct
    from the statement_timeout root cause fixed earlier: in prod ~5 claim 500s /
    15 min persisted on rev 00245 *after* statement_timeout went to zero, all
    tracing to ``task.context`` lazy-loads.

    The fix snapshots the immutable claim-time scalars (``_claimed_*``),
    re-attaches the task after enrichment, and reads from the snapshots in all
    unguarded downstream code. This test poisons the FIRST enrichment call so
    the abort + expire lands before the direction block, and gives the task a
    ``direction_id`` so that block actually executes and dereferences the task.
    Pre-fix this returns 500; post-fix it must return a well-formed 200.
    """
    from sqlalchemy import text

    project = Project(name="Poison Project Expire", user_id="test-user")
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = Task(
        project_id=project.id,
        payloads=[{"type": "create", "description": "test"}],
        context={
            "direction_id": "test-dir-expire",
            "related_files": ["a.py"],
        },
        vm_instance_id="test-vm-expire",
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    from app.auth import create_agent_token

    token = create_agent_token(str(task.id), "test-vm-expire")
    headers = {"x-agent-token": token}

    async def _poison(**kwargs):
        # Abort the claim session's transaction on the FIRST best-effort
        # enrichment call (compute_review_effectiveness, agent.py ~1740),
        # reproducing the aborted-transaction state a DB error (lock/statement
        # timeout) leaves. The subsequent commit then expires ``task``, exposing
        # every unguarded ``task.*`` read further down claim_task.
        await kwargs["session"].execute(
            text("SELECT * FROM _no_such_table_enrichment_expire_poison")
        )

    with patch(
        "app.review_learning.compute_review_effectiveness", new=_poison
    ):
        resp = await client.post("/api/agent/claim", headers=headers)

    assert resp.status_code == 200, (
        "claim must survive enrichment that aborts+expires the session, got "
        f"{resp.status_code}: {resp.text[:400]}"
    )
    body = resp.json()
    assert body.get("task_id") == str(task.id), (
        f"claim response must be well-formed after session recovery, got {body}"
    )
