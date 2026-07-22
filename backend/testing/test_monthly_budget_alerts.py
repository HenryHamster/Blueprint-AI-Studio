"""
Tests for monthly budget alert logic in backend/app/monthly_budget_alerts.py.

Covers:
  1. Disabled check (target = 0) produces no alert
  2. 50% threshold alert is created at WARNING severity
  3. 90% threshold alert is created at ERROR severity
  4. 100% threshold alert is created at ERROR severity
  5. Deduplication prevents duplicate alerts for the same threshold
  6. Alert is correctly scoped to top-spending project

Uses an isolated in-memory SQLite DB (same pattern as test_services.py cost_ceiling tests)
and patches ``_compute_month_to_date_spend`` to return controlled values.
"""

import uuid
import pytest
from unittest.mock import AsyncMock, patch

from sqlalchemy.dialects.sqlite.base import SQLiteTypeCompiler as _SQLiteTypeCompiler
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from sqlmodel import SQLModel, select

import app.shared_state as _shared_state
from app.monthly_budget_alerts import check_monthly_budget, check_daily_budget, _BUDGET_THRESHOLDS
from app.models import Alert, AlertSeverity, Project


class _BudgetSQLiteTypeCompiler(_SQLiteTypeCompiler):
    """Compile PostgreSQL JSONB as JSON only for this fixture's SQLite engine."""

    def visit_JSONB(self, type_, **kw):
        return self.visit_JSON(type_, **kw)


@pytest.fixture
async def budget_db():
    """Isolated in-memory SQLite DB for check_monthly_budget tests."""
    _engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    # This file is part of the qualifier's ``--noconftest`` regression batch,
    # so it cannot rely on testing/conftest.py's JSONB compatibility hook.
    # Keep the adapter engine-local to avoid changing later tests by import order.
    dialect = _engine.sync_engine.dialect
    type_compiler = _BudgetSQLiteTypeCompiler(dialect)
    dialect.type_compiler = type_compiler
    dialect.type_compiler_instance = type_compiler
    async with _engine.begin() as conn:
        await conn.run_sync(SQLModel.metadata.create_all)

    _Session = sessionmaker(
        bind=_engine,
        class_=AsyncSession,
        autocommit=False,
        autoflush=False,
        expire_on_commit=False,
    )

    # Suppress broadcasts
    patch_broadcast = patch(
        "app.monthly_budget_alerts.broadcast_to_project",
        new_callable=AsyncMock,
    )
    patch_broadcast.start()

    async with _Session() as session:
        yield session

    patch_broadcast.stop()
    async with _engine.begin() as conn:
        await conn.run_sync(SQLModel.metadata.drop_all)
    await _engine.dispose()


@pytest.fixture
async def project_with_id(budget_db) -> uuid.UUID:
    """Create a real project in the test DB and return its id."""
    project = Project(name="Budget Test Project", user_id="user1")
    budget_db.add(project)
    await budget_db.commit()
    await budget_db.refresh(project)
    return project.id


def _mock_spend(total: float, top_project_id: str) -> dict:
    """Return a canned return value that matches _compute_month_to_date_spend's shape.

    Returns (total, top_project_id, {top_project_id: total}).
    """
    return (total, top_project_id, {top_project_id: total})


@pytest.mark.asyncio
async def test_monthly_budget_disabled_produces_no_alert(
    budget_db, project_with_id
):
    """When monthly_budget_target_usd == 0 (disabled), no alert should be created."""
    pid_str = str(project_with_id)
    with patch.dict(
        _shared_state.runtime_config,
        {"monthly_budget_target_usd": 0},
    ), patch(
        "app.monthly_budget_alerts._compute_month_to_date_spend",
        new_callable=AsyncMock,
        return_value=_mock_spend(500.0, pid_str),
    ):
        await check_monthly_budget(budget_db)

    alerts = (
        (await budget_db.execute(select(Alert).where(Alert.project_id == project_with_id)))
        .scalars()
        .all()
    )
    assert len(alerts) == 0, "No alerts expected when target is 0"


@pytest.mark.asyncio
async def test_monthly_budget_50pct_alert_created(
    budget_db, project_with_id
):
    """When month-to-date spend reaches 50% of target, a WARNING alert should fire."""
    pid_str = str(project_with_id)
    target = 1000.0
    spend = 500.0  # exactly 50%

    with patch.dict(
        _shared_state.runtime_config,
        {"monthly_budget_target_usd": target},
    ), patch(
        "app.monthly_budget_alerts._compute_month_to_date_spend",
        new_callable=AsyncMock,
        return_value=_mock_spend(spend, pid_str),
    ):
        await check_monthly_budget(budget_db)

    await budget_db.flush()
    alerts = (
        (await budget_db.execute(select(Alert).where(Alert.project_id == project_with_id)))
        .scalars()
        .all()
    )
    assert len(alerts) == 1
    assert alerts[0].severity == AlertSeverity.WARNING
    assert "50%" in alerts[0].title
    assert alerts[0].dedupe_key == "monthly_budget_50pct"
    assert alerts[0].domain == "billing"
    assert alerts[0].event_type == "MONTHLY_BUDGET_ALERT"


@pytest.mark.asyncio
async def test_monthly_budget_90pct_alert_created(
    budget_db, project_with_id
):
    """When month-to-date spend reaches 90% of target, an ERROR alert should fire."""
    pid_str = str(project_with_id)
    target = 1000.0
    spend = 900.0  # exactly 90%

    with patch.dict(
        _shared_state.runtime_config,
        {"monthly_budget_target_usd": target},
    ), patch(
        "app.monthly_budget_alerts._compute_month_to_date_spend",
        new_callable=AsyncMock,
        return_value=_mock_spend(spend, pid_str),
    ):
        await check_monthly_budget(budget_db)

    await budget_db.flush()
    alerts = (
        (await budget_db.execute(select(Alert).where(Alert.project_id == project_with_id)))
        .scalars()
        .all()
    )
    # At 90%, both 50% and 90% alerts fire.
    assert len(alerts) == 2
    titles = [a.title for a in alerts]
    assert any("90%" in t for t in titles)
    assert any("50%" in t for t in titles)
    error_alerts = [a for a in alerts if a.severity == AlertSeverity.ERROR]
    assert any("90%" in a.title for a in error_alerts)
    dedupe_keys = [a.dedupe_key for a in alerts]
    assert "monthly_budget_90pct" in dedupe_keys


@pytest.mark.asyncio
async def test_monthly_budget_100pct_alert_created(
    budget_db, project_with_id
):
    """When month-to-date spend reaches/exceeds 100% of target, an ERROR alert should fire."""
    pid_str = str(project_with_id)
    target = 1000.0
    spend = 1000.0  # exactly 100%

    with patch.dict(
        _shared_state.runtime_config,
        {"monthly_budget_target_usd": target},
    ), patch(
        "app.monthly_budget_alerts._compute_month_to_date_spend",
        new_callable=AsyncMock,
        return_value=_mock_spend(spend, pid_str),
    ):
        await check_monthly_budget(budget_db)

    await budget_db.flush()
    alerts = (
        (await budget_db.execute(select(Alert).where(Alert.project_id == project_with_id)))
        .scalars()
        .all()
    )
    # At 100%, all three thresholds (50%, 90%, 100%) fire.
    assert len(alerts) == 3
    titles = [a.title for a in alerts]
    assert any("100%" in t for t in titles)
    assert any("90%" in t for t in titles)
    assert any("50%" in t for t in titles)
    error_alerts = [a for a in alerts if a.severity == AlertSeverity.ERROR]
    assert any("100%" in a.title for a in error_alerts)
    dedupe_keys = [a.dedupe_key for a in alerts]
    assert "monthly_budget_100pct" in dedupe_keys


@pytest.mark.asyncio
async def test_monthly_budget_deduplicates_per_threshold(
    budget_db, project_with_id
):
    """Multiple calls for the same threshold should not create duplicate alerts."""
    pid_str = str(project_with_id)
    target = 1000.0
    spend = 600.0  # above 50% but below 90%

    with patch.dict(
        _shared_state.runtime_config,
        {"monthly_budget_target_usd": target},
    ), patch(
        "app.monthly_budget_alerts._compute_month_to_date_spend",
        new_callable=AsyncMock,
        return_value=_mock_spend(spend, pid_str),
    ):
        await check_monthly_budget(budget_db)  # first call — creates alert

    await budget_db.flush()

    with patch.dict(
        _shared_state.runtime_config,
        {"monthly_budget_target_usd": target},
    ), patch(
        "app.monthly_budget_alerts._compute_month_to_date_spend",
        new_callable=AsyncMock,
        return_value=_mock_spend(spend, pid_str),
    ):
        await check_monthly_budget(budget_db)  # second call — should dedupe

    await budget_db.flush()
    alerts = (
        (await budget_db.execute(select(Alert).where(Alert.project_id == project_with_id)))
        .scalars()
        .all()
    )
    # Only one alert for 50% (spend at 60% crosses 50% threshold but deduped)
    assert len(alerts) == 1


@pytest.mark.asyncio
async def test_monthly_budget_scoped_to_top_spending_project(
    budget_db,
):
    """The alert should be attached to the top-spending project."""
    # Create two projects
    project_a = Project(name="Project A", user_id="user1")
    project_b = Project(name="Project B", user_id="user1")
    budget_db.add(project_a)
    budget_db.add(project_b)
    await budget_db.commit()
    await budget_db.refresh(project_a)
    await budget_db.refresh(project_b)

    pid_a = str(project_a.id)
    pid_b = str(project_b.id)

    target = 1000.0
    # Project B has higher spend
    project_spend_map = {pid_a: 200.0, pid_b: 400.0}
    total = 600.0
    top_pid = pid_b

    with patch.dict(
        _shared_state.runtime_config,
        {"monthly_budget_target_usd": target},
    ), patch(
        "app.monthly_budget_alerts._compute_month_to_date_spend",
        new_callable=AsyncMock,
        return_value=(total, top_pid, project_spend_map),
    ):
        await check_monthly_budget(budget_db)

    await budget_db.flush()
    # Alert should be on project_b (top spender)
    alerts_b = (
        (await budget_db.execute(select(Alert).where(Alert.project_id == project_b.id)))
        .scalars()
        .all()
    )
    assert len(alerts_b) == 1
    assert "Project B spend" not in alerts_b[0].message  # just checking it's right project

    # No alert on project_a
    alerts_a = (
        (await budget_db.execute(select(Alert).where(Alert.project_id == project_a.id)))
        .scalars()
        .all()
    )
    assert len(alerts_a) == 0


@pytest.mark.asyncio
async def test_monthly_budget_below_50pct_no_alert(
    budget_db, project_with_id
):
    """When month-to-date spend is below 50% of target, no alert should fire."""
    pid_str = str(project_with_id)
    target = 1000.0
    spend = 400.0  # 40% — below 50% threshold

    with patch.dict(
        _shared_state.runtime_config,
        {"monthly_budget_target_usd": target},
    ), patch(
        "app.monthly_budget_alerts._compute_month_to_date_spend",
        new_callable=AsyncMock,
        return_value=_mock_spend(spend, pid_str),
    ):
        await check_monthly_budget(budget_db)

    alerts = (
        (await budget_db.execute(select(Alert).where(Alert.project_id == project_with_id)))
        .scalars()
        .all()
    )
    assert len(alerts) == 0


@pytest.mark.asyncio
async def test_monthly_budget_zero_spend_no_alert(
    budget_db, project_with_id
):
    """When month-to-date spend is zero, no alert should fire even if target is set."""
    pid_str = str(project_with_id)
    target = 1000.0
    spend = 0.0

    with patch.dict(
        _shared_state.runtime_config,
        {"monthly_budget_target_usd": target},
    ), patch(
        "app.monthly_budget_alerts._compute_month_to_date_spend",
        new_callable=AsyncMock,
        return_value=_mock_spend(spend, pid_str),
    ):
        await check_monthly_budget(budget_db)

    alerts = (
        (await budget_db.execute(select(Alert).where(Alert.project_id == project_with_id)))
        .scalars()
        .all()
    )
    assert len(alerts) == 0


@pytest.mark.asyncio
async def test_threshold_constants_are_correct():
    """Verify _BUDGET_THRESHOLDS matches the documented 50/90/100% shape."""
    assert _BUDGET_THRESHOLDS == (0.50, 0.90, 1.00)


# ---------------------------------------------------------------------------
# Return-value contract tests (dispatch-gate interface)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_check_monthly_budget_returns_exhausted_true_at_100pct(
    budget_db, project_with_id
):
    """When MTD spend >= target, check_monthly_budget returns (True, >= 1.0)."""
    pid_str = str(project_with_id)
    target = 1000.0
    spend = 1000.0  # exactly 100%

    with patch.dict(
        _shared_state.runtime_config,
        {"monthly_budget_target_usd": target},
    ), patch(
        "app.monthly_budget_alerts._compute_month_to_date_spend",
        new_callable=AsyncMock,
        return_value=_mock_spend(spend, pid_str),
    ):
        exhausted, ratio = await check_monthly_budget(budget_db)

    assert exhausted is True
    assert ratio == 1.0


@pytest.mark.asyncio
async def test_check_monthly_budget_returns_exhausted_false_below_100pct(
    budget_db, project_with_id
):
    """When MTD spend < target, check_monthly_budget returns (False, < 1.0)."""
    pid_str = str(project_with_id)
    target = 1000.0
    spend = 500.0  # exactly 50%

    with patch.dict(
        _shared_state.runtime_config,
        {"monthly_budget_target_usd": target},
    ), patch(
        "app.monthly_budget_alerts._compute_month_to_date_spend",
        new_callable=AsyncMock,
        return_value=_mock_spend(spend, pid_str),
    ):
        exhausted, ratio = await check_monthly_budget(budget_db)

    assert exhausted is False
    assert ratio == 0.5


@pytest.mark.asyncio
async def test_check_monthly_budget_returns_none_when_disabled(
    budget_db, project_with_id
):
    """When monthly_budget_target_usd is 0 (disabled), returns (False, None)."""
    pid_str = str(project_with_id)
    with patch.dict(
        _shared_state.runtime_config,
        {"monthly_budget_target_usd": 0},
    ), patch(
        "app.monthly_budget_alerts._compute_month_to_date_spend",
        new_callable=AsyncMock,
        return_value=_mock_spend(500.0, pid_str),
    ):
        exhausted, ratio = await check_monthly_budget(budget_db)

    assert exhausted is False
    assert ratio is None


@pytest.mark.asyncio
async def test_check_monthly_budget_returns_none_when_zero_spend(
    budget_db, project_with_id
):
    """When month-to-date spend is zero, returns (False, None)."""
    pid_str = str(project_with_id)
    target = 1000.0
    spend = 0.0

    with patch.dict(
        _shared_state.runtime_config,
        {"monthly_budget_target_usd": target},
    ), patch(
        "app.monthly_budget_alerts._compute_month_to_date_spend",
        new_callable=AsyncMock,
        return_value=_mock_spend(spend, pid_str),
    ):
        exhausted, ratio = await check_monthly_budget(budget_db)

    assert exhausted is False
    assert ratio is None


# ---------------------------------------------------------------------------
# check_daily_budget tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_daily_budget_disabled_when_key_unset(
    budget_db, project_with_id
):
    """When daily_spend_budget_usd is absent/0, gate is disabled — no alert, returns (False, None)."""
    pid_str = str(project_with_id)
    with patch.dict(
        _shared_state.runtime_config,
        {"daily_spend_budget_usd": 0},
    ), patch(
        "app.monthly_budget_alerts._compute_month_to_date_spend",
        new_callable=AsyncMock,
        return_value=_mock_spend(999.0, pid_str),
    ):
        exhausted, ratio = await check_daily_budget(budget_db)

    assert exhausted is False
    assert ratio is None
    alerts = (
        (await budget_db.execute(select(Alert).where(Alert.project_id == project_with_id)))
        .scalars()
        .all()
    )
    assert len(alerts) == 0


@pytest.mark.asyncio
async def test_daily_budget_blocks_when_spend_reaches_target(
    budget_db, project_with_id
):
    """When today's spend >= daily_spend_budget_usd, gate fires alert and returns (True, ratio)."""
    pid_str = str(project_with_id)
    target = 50.0
    spend = 50.0  # exactly 100%

    with patch.dict(
        _shared_state.runtime_config,
        {"daily_spend_budget_usd": target},
    ), patch(
        "app.monthly_budget_alerts._compute_month_to_date_spend",
        new_callable=AsyncMock,
        return_value=_mock_spend(spend, pid_str),
    ):
        exhausted, ratio = await check_daily_budget(budget_db)

    assert exhausted is True
    assert ratio == 1.0

    await budget_db.flush()
    alerts = (
        (await budget_db.execute(
            select(Alert).where(
                Alert.project_id == project_with_id,
                Alert.event_type == "DAILY_BUDGET_ALERT",
            )
        ))
        .scalars()
        .all()
    )
    assert len(alerts) == 1
    assert alerts[0].severity == AlertSeverity.ERROR
    assert "daily" in alerts[0].title.lower()


@pytest.mark.asyncio
async def test_daily_budget_inactive_when_spend_below_target(
    budget_db, project_with_id
):
    """When today's spend < daily_spend_budget_usd, gate is inactive (False, ratio < 1.0)."""
    pid_str = str(project_with_id)
    target = 100.0
    spend = 40.0  # 40%

    with patch.dict(
        _shared_state.runtime_config,
        {"daily_spend_budget_usd": target},
    ), patch(
        "app.monthly_budget_alerts._compute_month_to_date_spend",
        new_callable=AsyncMock,
        return_value=_mock_spend(spend, pid_str),
    ):
        exhausted, ratio = await check_daily_budget(budget_db)

    assert exhausted is False
    assert ratio == 0.4

    alerts = (
        (await budget_db.execute(select(Alert).where(Alert.project_id == project_with_id)))
        .scalars()
        .all()
    )
    assert len(alerts) == 0


@pytest.mark.asyncio
async def test_daily_budget_deduplicates_alert(
    budget_db, project_with_id
):
    """Calling check_daily_budget twice at 100% only creates one alert (dedup by dedupe_key)."""
    pid_str = str(project_with_id)
    target = 50.0
    spend = 60.0  # over 100%

    with patch.dict(
        _shared_state.runtime_config,
        {"daily_spend_budget_usd": target},
    ), patch(
        "app.monthly_budget_alerts._compute_month_to_date_spend",
        new_callable=AsyncMock,
        return_value=_mock_spend(spend, pid_str),
    ):
        await check_daily_budget(budget_db)
        await budget_db.flush()
        await check_daily_budget(budget_db)

    await budget_db.flush()
    alerts = (
        (await budget_db.execute(
            select(Alert).where(
                Alert.project_id == project_with_id,
                Alert.event_type == "DAILY_BUDGET_ALERT",
            )
        ))
        .scalars()
        .all()
    )
    assert len(alerts) == 1


@pytest.mark.asyncio
async def test_monthly_and_daily_compose_either_blocks(
    budget_db, project_with_id
):
    """Monthly exhausted -> gate active; daily exhausted -> gate active; both compose correctly."""
    pid_str = str(project_with_id)

    # Case 1: monthly exhausted, daily disabled (key=0)
    with patch.dict(
        _shared_state.runtime_config,
        {"monthly_budget_target_usd": 100.0, "daily_spend_budget_usd": 0},
    ), patch(
        "app.monthly_budget_alerts._compute_month_to_date_spend",
        new_callable=AsyncMock,
        return_value=_mock_spend(100.0, pid_str),
    ):
        monthly_exhausted, _ = await check_monthly_budget(budget_db)
        daily_exhausted, _ = await check_daily_budget(budget_db)

    assert monthly_exhausted is True
    assert daily_exhausted is False

    # Case 2: daily exhausted, monthly disabled (key=0)
    with patch.dict(
        _shared_state.runtime_config,
        {"monthly_budget_target_usd": 0, "daily_spend_budget_usd": 10.0},
    ), patch(
        "app.monthly_budget_alerts._compute_month_to_date_spend",
        new_callable=AsyncMock,
        return_value=_mock_spend(15.0, pid_str),
    ):
        monthly_exhausted2, _ = await check_monthly_budget(budget_db)
        daily_exhausted2, _ = await check_daily_budget(budget_db)

    assert monthly_exhausted2 is False
    assert daily_exhausted2 is True
