"""Tests for reaper bug-message sweep helpers (Step 18e: unlinked BLOCKER decay)."""

from unittest.mock import AsyncMock
from uuid import UUID

import pytest
import pytest_asyncio
from datetime import timedelta
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from sqlmodel import SQLModel, select

from app.models import (
    AcceptanceHealingSweepRun,
    MessageType,
    Project,
    ProjectMessage,
    Task,
    TaskStatus,
)
from app.reaper_bug_message_sweeps import (
    STALE_OPEN_UNLINKED_BLOCKER_DECAY_DAYS,
    sweep_decay_stale_unlinked_blocker_messages,
)
from app.time_utils import _utcnow


@pytest_asyncio.fixture()
async def sweep_db():
    """Isolated in-memory SQLite DB for sweep unit tests."""
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    async with engine.begin() as conn:
        await conn.run_sync(SQLModel.metadata.create_all)

    Session = sessionmaker(
        bind=engine,
        class_=AsyncSession,
        autocommit=False,
        autoflush=False,
        expire_on_commit=False,
    )
    async with Session() as s:
        yield s

    async with engine.begin() as conn:
        await conn.run_sync(SQLModel.metadata.drop_all)
    await engine.dispose()


async def _make_project(db: AsyncSession) -> Project:
    project = Project(name="sweep-unit", user_id="user1")
    db.add(project)
    await db.commit()
    await db.refresh(project)
    return project


@pytest.mark.asyncio
async def test_blocker_message_auto_decay(sweep_db):
    """An unlinked, unowned OPEN BLOCKER older than the threshold is auto-decayed.

    This is the primary acceptance test for Step 18e. It verifies that a
    user-authored BLOCKER with no _convergence_link and no ownership context
    is resolved with 'auto-decayed (unlinked dead thread)' after the configured
    decay window.
    """
    project = await _make_project(sweep_db)
    old = _utcnow() - timedelta(days=STALE_OPEN_UNLINKED_BLOCKER_DECAY_DAYS + 1)
    msg = ProjectMessage(
        project_id=project.id,
        message_type=MessageType.BLOCKER,
        from_type="user",
        content="stale operator-filed blocker with no follow-up",
        status="open",
        context={},
        created_at=old,
    )
    sweep_db.add(msg)
    await sweep_db.commit()
    await sweep_db.refresh(msg)

    acted = await sweep_decay_stale_unlinked_blocker_messages(sweep_db, _utcnow())

    assert acted == 1
    await sweep_db.refresh(msg)
    assert msg.status == "resolved"
    assert msg.resolution == "auto-decayed (unlinked dead thread)"
    assert msg.resolved_at is not None


@pytest.mark.asyncio
async def test_blocker_message_auto_decay_system_authored(sweep_db):
    """A system-authored BLOCKER with no link/ownership also decays via Step 18e."""
    project = await _make_project(sweep_db)
    old = _utcnow() - timedelta(days=STALE_OPEN_UNLINKED_BLOCKER_DECAY_DAYS + 5)
    msg = ProjectMessage(
        project_id=project.id,
        message_type=MessageType.BLOCKER,
        from_type="system",
        content="old system blocker with no owner",
        status="open",
        context={},
        created_at=old,
    )
    sweep_db.add(msg)
    await sweep_db.commit()
    await sweep_db.refresh(msg)

    acted = await sweep_decay_stale_unlinked_blocker_messages(sweep_db, _utcnow())

    assert acted == 1
    await sweep_db.refresh(msg)
    assert msg.status == "resolved"
    assert msg.resolution == "auto-decayed (unlinked dead thread)"


@pytest.mark.asyncio
async def test_blocker_auto_decay_skips_fresh(sweep_db):
    """A BLOCKER newer than the decay threshold is left open."""
    project = await _make_project(sweep_db)
    recent = _utcnow() - timedelta(days=STALE_OPEN_UNLINKED_BLOCKER_DECAY_DAYS - 1)
    msg = ProjectMessage(
        project_id=project.id,
        message_type=MessageType.BLOCKER,
        from_type="user",
        content="recent blocker still within window",
        status="open",
        context={},
        created_at=recent,
    )
    sweep_db.add(msg)
    await sweep_db.commit()
    await sweep_db.refresh(msg)

    acted = await sweep_decay_stale_unlinked_blocker_messages(sweep_db, _utcnow())

    assert acted == 0
    await sweep_db.refresh(msg)
    assert msg.status == "open"


@pytest.mark.asyncio
async def test_blocker_auto_decay_skips_linked(sweep_db):
    """A BLOCKER with a live _convergence_link is skipped (Step 15 owns it)."""
    project = await _make_project(sweep_db)
    old = _utcnow() - timedelta(days=STALE_OPEN_UNLINKED_BLOCKER_DECAY_DAYS + 10)
    msg = ProjectMessage(
        project_id=project.id,
        message_type=MessageType.BLOCKER,
        from_type="user",
        content="blocker linked to a direction",
        status="open",
        context={"_convergence_link": {"kind": "direction", "id": "d-some-dir"}},
        created_at=old,
    )
    sweep_db.add(msg)
    await sweep_db.commit()
    await sweep_db.refresh(msg)

    acted = await sweep_decay_stale_unlinked_blocker_messages(sweep_db, _utcnow())

    assert acted == 0
    await sweep_db.refresh(msg)
    assert msg.status == "open"


@pytest.mark.asyncio
async def test_blocker_auto_decay_skips_direction_owned(sweep_db):
    """A BLOCKER with direction_id ownership is skipped (Step 17 owns it)."""
    project = await _make_project(sweep_db)
    old = _utcnow() - timedelta(days=STALE_OPEN_UNLINKED_BLOCKER_DECAY_DAYS + 10)
    msg = ProjectMessage(
        project_id=project.id,
        message_type=MessageType.BLOCKER,
        from_type="system",
        content="system blocker tied to an active direction",
        status="open",
        context={"direction_id": "d-still-active"},
        created_at=old,
    )
    sweep_db.add(msg)
    await sweep_db.commit()
    await sweep_db.refresh(msg)

    acted = await sweep_decay_stale_unlinked_blocker_messages(sweep_db, _utcnow())

    assert acted == 0
    await sweep_db.refresh(msg)
    assert msg.status == "open"


@pytest.mark.asyncio
async def test_blocker_auto_decay_skips_task_owned(sweep_db):
    """A BLOCKER with pr_number+source_task_id ownership is skipped (Step 17 owns it)."""
    project = await _make_project(sweep_db)
    old = _utcnow() - timedelta(days=STALE_OPEN_UNLINKED_BLOCKER_DECAY_DAYS + 20)
    msg = ProjectMessage(
        project_id=project.id,
        message_type=MessageType.BLOCKER,
        from_type="agent",
        content="agent blocker linked to a specific task PR",
        status="open",
        context={
            "pr_number": 9999,
            "source_task_id": "00000000-0000-0000-0000-000000000002",
        },
        created_at=old,
    )
    sweep_db.add(msg)
    await sweep_db.commit()
    await sweep_db.refresh(msg)

    acted = await sweep_decay_stale_unlinked_blocker_messages(sweep_db, _utcnow())

    assert acted == 0
    await sweep_db.refresh(msg)
    assert msg.status == "open"


@pytest.mark.asyncio
async def test_blocker_auto_decay_does_not_touch_bug_report(sweep_db):
    """Step 18e targets BLOCKER only; BUG_REPORT is untouched (Step 18d covers it)."""
    project = await _make_project(sweep_db)
    old = _utcnow() - timedelta(days=STALE_OPEN_UNLINKED_BLOCKER_DECAY_DAYS + 5)
    msg = ProjectMessage(
        project_id=project.id,
        message_type=MessageType.BUG_REPORT,
        from_type="user",
        content="user-authored bug report — not a blocker",
        status="open",
        context={},
        created_at=old,
    )
    sweep_db.add(msg)
    await sweep_db.commit()
    await sweep_db.refresh(msg)

    acted = await sweep_decay_stale_unlinked_blocker_messages(sweep_db, _utcnow())

    assert acted == 0
    await sweep_db.refresh(msg)
    assert msg.status == "open"


# ---------------------------------------------------------------------------
# Step 21b: acceptance-verdict healing backfill sweep
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_acceptance_healing_backfill_cadence_gated(sweep_db):
    """Step 21b runs on its own cadence timer and is suppressed within the interval.

    The timer is independent of every other sweep's stamp, so an earlier sweep in
    the same reaper cycle cannot accidentally suppress the healing pass.

    A cadence-skipped invocation must ALSO write no sweep-run row: the reaper
    cycles roughly once a minute against an hourly sweep, so persisting the skip
    would append ~1.4k rows/day recording only that the timer was not due.
    """
    import app.reaper_sweeps as rsw

    _prev = rsw._last_acceptance_healing_backfill_sweep
    rsw._last_acceptance_healing_backfill_sweep = None
    try:
        now = _utcnow()
        first = await rsw.sweep_acceptance_verdict_healing_backfills(sweep_db, now=now)
        assert first["outcome"] == "no_open_version"
        assert first["evidence_rows_recorded"] == 0
        assert rsw._last_acceptance_healing_backfill_sweep == now
        # The run that actually examined the fleet IS recorded.
        assert first["record_persisted"] is True
        rows_after_first = (
            await sweep_db.execute(select(AcceptanceHealingSweepRun))
        ).scalars().all()
        assert len(rows_after_first) == 1

        # Many suppressed calls within the interval: none re-stamp the timer and
        # none append a row.
        for _ in range(5):
            second = await rsw.sweep_acceptance_verdict_healing_backfills(
                sweep_db, now=now
            )
            assert second["outcome"] == "cadence_skipped"
            assert second["record_persisted"] is False
            assert "run_id" not in second
        assert rsw._last_acceptance_healing_backfill_sweep == now

        rows_after_skips = (
            await sweep_db.execute(select(AcceptanceHealingSweepRun))
        ).scalars().all()
        assert [r.id for r in rows_after_skips] == [r.id for r in rows_after_first]
        assert all(r.outcome != "cadence_skipped" for r in rows_after_skips)
    finally:
        rsw._last_acceptance_healing_backfill_sweep = _prev


@pytest.mark.asyncio
async def test_acceptance_healing_sweep_run_retention_purges_old_rows(sweep_db):
    """Step 21c purges sweep-run rows older than the retention window only."""
    import app.reaper_sweeps as rsw

    now = _utcnow()
    stale = AcceptanceHealingSweepRun(
        executed_at=now
        - timedelta(days=rsw.ACCEPTANCE_HEALING_SWEEP_RUN_RETENTION_DAYS + 1),
        outcome="no_op",
    )
    boundary = AcceptanceHealingSweepRun(
        executed_at=now
        - timedelta(days=rsw.ACCEPTANCE_HEALING_SWEEP_RUN_RETENTION_DAYS - 1),
        outcome="changed",
    )
    fresh = AcceptanceHealingSweepRun(executed_at=now, outcome="changed")
    sweep_db.add_all([stale, boundary, fresh])
    await sweep_db.commit()

    _prev = rsw._last_acceptance_healing_retention_sweep
    rsw._last_acceptance_healing_retention_sweep = None
    try:
        deleted = await rsw.sweep_acceptance_healing_sweep_run_retention(
            sweep_db, now=now
        )
    finally:
        rsw._last_acceptance_healing_retention_sweep = _prev

    assert deleted == 1
    remaining = (
        await sweep_db.execute(select(AcceptanceHealingSweepRun))
    ).scalars().all()
    assert {r.id for r in remaining} == {boundary.id, fresh.id}


@pytest.mark.asyncio
async def test_acceptance_healing_sweep_run_retention_cadence_gated(sweep_db):
    """The purge runs at most daily so it is not re-issued every reaper cycle."""
    import app.reaper_sweeps as rsw

    now = _utcnow()
    old_ts = now - timedelta(
        days=rsw.ACCEPTANCE_HEALING_SWEEP_RUN_RETENTION_DAYS + 1
    )
    sweep_db.add(AcceptanceHealingSweepRun(executed_at=old_ts, outcome="no_op"))
    await sweep_db.commit()

    _prev = rsw._last_acceptance_healing_retention_sweep
    rsw._last_acceptance_healing_retention_sweep = None
    try:
        assert (
            await rsw.sweep_acceptance_healing_sweep_run_retention(sweep_db, now=now)
            == 1
        )
        assert rsw._last_acceptance_healing_retention_sweep == now

        # A second stale row inside the same day is left alone until the cadence
        # comes due again.
        sweep_db.add(AcceptanceHealingSweepRun(executed_at=old_ts, outcome="no_op"))
        await sweep_db.commit()
        assert (
            await rsw.sweep_acceptance_healing_sweep_run_retention(sweep_db, now=now)
            == 0
        )
        assert (
            len(
                (await sweep_db.execute(select(AcceptanceHealingSweepRun)))
                .scalars()
                .all()
            )
            == 1
        )

        # Once a day has elapsed the purge runs again.
        later = now + timedelta(
            seconds=rsw._ACCEPTANCE_HEALING_RETENTION_INTERVAL_SECONDS
        )
        assert (
            await rsw.sweep_acceptance_healing_sweep_run_retention(sweep_db, now=later)
            == 1
        )
    finally:
        rsw._last_acceptance_healing_retention_sweep = _prev


@pytest.mark.asyncio
async def test_acceptance_healing_sweep_run_retention_wired_into_reaper():
    """The purge is imported and invoked by the reaper cycle, not just defined."""
    import inspect

    import app.reaper as reaper

    assert hasattr(reaper, "sweep_acceptance_healing_sweep_run_retention")
    src = inspect.getsource(reaper)
    assert "await sweep_acceptance_healing_sweep_run_retention(session, now)" in src


@pytest.mark.asyncio
async def test_acceptance_healing_backfill_skips_closed_projects(sweep_db, mocker):
    """Step 21b invokes the healing passes only for projects with an open version.

    A project with no open release version has no active acceptance criteria to
    heal, so none of the reconcile passes (rollup stamping, unverified-merge
    backfill, window minter) are invoked for it.
    """
    import app.reaper_sweeps as rsw

    await _make_project(sweep_db)  # project with NO open version

    rollup = mocker.patch(
        "app.acceptance_criteria.backfill_rollup_child_changed_files",
        new=AsyncMock(return_value=0),
    )
    unverified = mocker.patch(
        "app.acceptance_criteria.backfill_unverified_merge_evidence",
        new=AsyncMock(return_value=0),
    )
    window = mocker.patch(
        "app.acceptance_criteria.record_acceptance_window_coverage_evidence",
        new=AsyncMock(return_value=0),
    )

    _prev = rsw._last_acceptance_healing_backfill_sweep
    rsw._last_acceptance_healing_backfill_sweep = None
    try:
        total = await rsw.sweep_acceptance_verdict_healing_backfills(
            sweep_db, now=_utcnow()
        )
    finally:
        rsw._last_acceptance_healing_backfill_sweep = _prev

    assert total["outcome"] == "no_open_version"
    rollup.assert_not_called()
    unverified.assert_not_called()
    window.assert_not_called()


@pytest.mark.asyncio
async def test_acceptance_healing_backfill_records_stamped_only_change(sweep_db, mocker):
    """A rollup-context repair is a changed run even when it mints no evidence."""
    import app.reaper_sweeps as rsw
    from app import release_version as rv

    project = await _make_project(sweep_db)
    version = await rv.open_version(sweep_db, project.id, version_number=1)
    await sweep_db.commit()

    mocker.patch(
        "app.acceptance_criteria.backfill_rollup_child_changed_files",
        new=AsyncMock(return_value=1),
    )
    mocker.patch(
        "app.acceptance_criteria.backfill_unverified_merge_evidence",
        new=AsyncMock(return_value=0),
    )
    mocker.patch(
        "app.acceptance_criteria.record_acceptance_window_coverage_evidence",
        new=AsyncMock(return_value=0),
    )

    previous = rsw._last_acceptance_healing_backfill_sweep
    rsw._last_acceptance_healing_backfill_sweep = None
    try:
        result = await rsw.sweep_acceptance_verdict_healing_backfills(
            sweep_db, now=_utcnow()
        )
    finally:
        rsw._last_acceptance_healing_backfill_sweep = previous

    assert result["outcome"] == "changed"
    assert result["evidence_rows_recorded"] == 0
    run = await sweep_db.get(AcceptanceHealingSweepRun, UUID(result["run_id"]))
    assert run is not None
    assert run.outcome == "changed"
    assert run.project_pass_counts[str(project.id)]["rollup_children_stamped"] == 1
    assert run.open_versions_examined == [
        {"project_id": str(project.id), "release_version_id": str(version.id)}
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("helper_name", "pass_name", "error_code"),
    [
        (
            "backfill_rollup_child_changed_files",
            "rollup_child_changed_files",
            "backfill_rollup_child_changed_files_failed",
        ),
        (
            "backfill_unverified_merge_evidence",
            "unverified_merge_evidence",
            "backfill_unverified_merge_evidence_failed",
        ),
    ],
)
async def test_acceptance_healing_backfill_persists_reconcile_pass_error(
    sweep_db, mocker, real_orm_expiry, helper_name, pass_name, error_code
):
    """Reconcile errors remain visible without poisoning the independent window pass."""
    import app.reaper_sweeps as rsw
    from app import release_version as rv
    from sqlalchemy import inspect as sa_inspect

    project = await _make_project(sweep_db)
    await rv.open_version(sweep_db, project.id, version_number=1)
    await sweep_db.commit()

    async def failed_reconcile_pass(_session, _project_id, *, diagnostic):
        diagnostic["error"] = error_code
        diagnostic["error_type"] = "DatabaseError"
        return 0

    mocker.patch(
        f"app.acceptance_criteria.{helper_name}",
        new=failed_reconcile_pass,
    )
    other_helper = (
        "backfill_unverified_merge_evidence"
        if helper_name == "backfill_rollup_child_changed_files"
        else "backfill_rollup_child_changed_files"
    )
    other_pass = mocker.patch(
        f"app.acceptance_criteria.{other_helper}", new=AsyncMock(return_value=0)
    )
    window_attribute_reads = False

    async def record_window_evidence(_session, version, *, diagnostic):
        nonlocal window_attribute_reads
        # This is deliberately an async-side-effect read of the fields used by
        # the real minter.  An expired instance raises MissingGreenlet here; a
        # refreshed instance proves the independent pass stayed usable.
        assert not sa_inspect(version).expired
        _ = version.id
        _ = version.project_id
        window_attribute_reads = True
        diagnostic["branch"] = "no_targets"
        return 0

    window_pass = mocker.patch(
        "app.acceptance_criteria.record_acceptance_window_coverage_evidence",
        new=AsyncMock(side_effect=record_window_evidence),
    )

    previous = rsw._last_acceptance_healing_backfill_sweep
    rsw._last_acceptance_healing_backfill_sweep = None
    try:
        result = await rsw.sweep_acceptance_verdict_healing_backfills(
            sweep_db, now=_utcnow()
        )
    finally:
        rsw._last_acceptance_healing_backfill_sweep = previous

    assert result["outcome"] == "error"
    other_pass.assert_awaited_once()
    window_pass.assert_awaited_once()
    assert window_attribute_reads
    run = await sweep_db.get(AcceptanceHealingSweepRun, UUID(result["run_id"]))
    assert run is not None
    assert run.outcome == "error"
    project_result = run.project_results[0]
    assert project_result["pass_errors"] == {
        pass_name: {
            "error": error_code,
            "error_type": "DatabaseError",
        }
    }


@pytest.mark.asyncio
async def test_acceptance_verdict_healing_sweep_records_skip_reasons(sweep_db):
    """Step 21b records both a false-zero skip and a real evidence-producing run.

    The durable row is intentionally observational: it identifies the open
    versions examined and the exact acceptance-window branch, but cannot make
    the false-zero version covered.  The second project demonstrates that the
    normal healing path still writes only ordinary criterion evidence.
    """
    from app import acceptance_criteria as ac
    from app import release_version as rv
    import app.reaper_sweeps as rsw
    from app.acceptance_gate import CODE_ACCEPTANCE_VERDICT_KEY

    false_zero_project = await _make_project(sweep_db)
    healed_project = Project(name="sweep-healed", user_id="user1")
    sweep_db.add(healed_project)
    await sweep_db.commit()
    await sweep_db.refresh(healed_project)

    false_zero_version = await rv.open_version(
        sweep_db, false_zero_project.id, version_number=1
    )
    healed_version = await rv.open_version(
        sweep_db, healed_project.id, version_number=1
    )
    await sweep_db.commit()

    for project, version in (
        (false_zero_project, false_zero_version),
        (healed_project, healed_version),
    ):
        await ac.create_criterion(
            sweep_db,
            project.id,
            version.id,
            "Acceptance window fraction becomes non-zero",
        )

    # This is a genuinely landed write task with a pre-existing soft acceptance
    # verdict.  The sweep minter may record a probe row, but it does not alter
    # the verdict or satisfy the false-zero project's criterion.
    landed = Task(
        project_id=healed_project.id,
        title="landed write with accepted test",
        payloads=[{"type": "create", "prompt": "normal healing"}],
        status=TaskStatus.COMPLETED,
        created_by="ORCHESTRATOR",
        completed_at=healed_version.opened_at + timedelta(seconds=1),
        context={
            "pr_merged": True,
            "landed_on_integration": True,
            CODE_ACCEPTANCE_VERDICT_KEY: {
                "kind": "named_test",
                "satisfied": True,
                "proven": False,
                "reason": "named_test_present_in_diff",
            },
        },
    )
    sweep_db.add(landed)
    await sweep_db.commit()

    previous = rsw._last_acceptance_healing_backfill_sweep
    rsw._last_acceptance_healing_backfill_sweep = None
    try:
        result = await rsw.sweep_acceptance_verdict_healing_backfills(
            sweep_db, now=_utcnow()
        )
    finally:
        rsw._last_acceptance_healing_backfill_sweep = previous

    assert result["outcome"] == "changed"
    assert result["record_persisted"] is True
    assert result["evidence_rows_recorded"] >= 1

    run = await sweep_db.get(AcceptanceHealingSweepRun, UUID(result["run_id"]))
    assert run is not None
    assert run.outcome == "changed"
    assert set(run.projects_examined) == {
        str(false_zero_project.id),
        str(healed_project.id),
    }
    assert {entry["release_version_id"] for entry in run.open_versions_examined} == {
        str(false_zero_version.id),
        str(healed_version.id),
    }
    by_project = {entry["project_id"]: entry for entry in run.project_results}
    assert by_project[str(false_zero_project.id)]["acceptance_window_branch"] == (
        "false_zero_window"
    )
    healed = by_project[str(healed_project.id)]
    assert healed["acceptance_window_branch"] == "evidence_recorded"
    assert healed["pass_counts"]["acceptance_window_evidence"] >= 1
    assert run.project_pass_counts[str(healed_project.id)] == healed["pass_counts"]
