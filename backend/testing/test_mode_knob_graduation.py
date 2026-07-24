"""Tests for the shared mode-knob graduation harness (``app.mode_knob_graduation``).

The harness generalizes the proven capability_proof_gate auto-graduation pattern
("decision #7") into a reusable driver that disposes dormant runtime-config
mode-knobs with MEASURED evidence. These tests exercise the GRADUATION DECISION
for the first wired knob (``autonomous_spawn_budget_mode``):

* GRADUATE — counterfactual signal above the enforce threshold advances the mode,
  durably persists to ``DeploymentState.runtime_config_json``, and resolves the
  ``dormant_knob:<key>`` completion-debt row;
* CUT — measured no-lift emits the legible CUT recommendation with numbers;
* HOLD — below threshold / insufficient sample is a no-op;
* the shared enabled-flag gate defaults OFF.

Run: bash scripts/test-backend.sh testing/test_mode_knob_graduation.py \
     testing/test_completion_debt.py
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from sqlmodel import SQLModel

import app.mode_knob_graduation as mkg
from app.completion_debt import KIND_DORMANT_KNOB
from app.models import (
    AuditLaneDecisionLog,
    CompletionDebtEntry,
    DeploymentState,
    EvergreenFillDecisionLog,
    Project,
    Task,
    TaskStatus,
)
from app.shared_state import runtime_config as _rc
from app.time_utils import _utcnow

CONFIG_KEY = "autonomous_spawn_budget_mode"
DEBT_KEY = "dormant_knob:autonomous_spawn_budget_mode"

# Every runtime_config key the harness / providers read.
_HARNESS_RC_KEYS = (
    mkg.MODE_KNOB_GRADUATION_ENABLED_KEY,
    CONFIG_KEY,
    "max_global_autonomous_initiative_tasks_per_hour",
    "max_global_autonomous_tasks_per_hour",
    "autonomous_spawn_budget_graduation_observe_threshold",
    "autonomous_spawn_budget_graduation_enforce_threshold",
    "autonomous_spawn_budget_graduation_cut_threshold",
    "autonomous_spawn_budget_graduation_min_sample",
    "autonomous_spawn_budget_graduation_window_hours",
    # --- Evergreen fill graduation config keys ---
    "evergreen_fill_mode",
    "evergreen_fill_graduation_observe_threshold",
    "evergreen_fill_graduation_enforce_threshold",
    "evergreen_fill_graduation_cut_threshold",
    "evergreen_fill_graduation_min_sample",
    "evergreen_fill_graduation_window_hours",
    # --- Structural health audit graduation config keys ---
    "structural_health_audit_mode",
    "structural_health_audit_graduation_observe_threshold",
    "structural_health_audit_graduation_enforce_threshold",
    "structural_health_audit_graduation_cut_threshold",
    "structural_health_audit_graduation_min_sample",
    "structural_health_audit_graduation_window_hours",
    # --- Metrics integrity sweep graduation config keys ---
    "metrics_integrity_sweep_mode",
    "metrics_integrity_sweep_graduation_observe_threshold",
    "metrics_integrity_sweep_graduation_enforce_threshold",
    "metrics_integrity_sweep_graduation_cut_threshold",
    "metrics_integrity_sweep_graduation_min_sample",
    "metrics_integrity_sweep_graduation_window_hours",
    # --- Direction gap respawn block graduation config keys ---
    "direction_gap_respawn_block_mode",
    "direction_gap_respawn_block_graduation_observe_threshold",
    "direction_gap_respawn_block_graduation_enforce_threshold",
    "direction_gap_respawn_block_graduation_cut_threshold",
    "direction_gap_respawn_block_graduation_min_sample",
    "direction_gap_respawn_block_graduation_window_hours",
)


@pytest_asyncio.fixture()
async def db():
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


@pytest.fixture(autouse=True)
def _restore_config():
    """Snapshot + restore every harness runtime_config key around each test."""
    saved = {k: _rc[k] for k in _HARNESS_RC_KEYS if k in _rc}
    present = set(saved)
    yield
    for k in _HARNESS_RC_KEYS:
        if k in present:
            _rc[k] = saved[k]
        else:
            _rc.pop(k, None)


def _configure(
    *,
    enabled: bool = True,
    mode: str = "observe",
    initiative_cap: int = 2,
    global_cap: int = 500,
    observe_threshold: float = 0.5,
    enforce_threshold: float = 1.0,
    cut_threshold: float = 0.1,
    min_sample: int = 3,
    window_hours: float = 24,
) -> None:
    _rc[mkg.MODE_KNOB_GRADUATION_ENABLED_KEY] = enabled
    _rc[CONFIG_KEY] = mode
    _rc["max_global_autonomous_initiative_tasks_per_hour"] = initiative_cap
    _rc["max_global_autonomous_tasks_per_hour"] = global_cap
    _rc["autonomous_spawn_budget_graduation_observe_threshold"] = observe_threshold
    _rc["autonomous_spawn_budget_graduation_enforce_threshold"] = enforce_threshold
    _rc["autonomous_spawn_budget_graduation_cut_threshold"] = cut_threshold
    _rc["autonomous_spawn_budget_graduation_min_sample"] = min_sample
    _rc["autonomous_spawn_budget_graduation_window_hours"] = window_hours


async def _project(db) -> Project:
    p = Project(name="mkg-unit", user_id="u1")
    db.add(p)
    await db.commit()
    await db.refresh(p)
    return p


async def _spawn_tasks(db, project, task_type: str, n: int) -> None:
    """Create ``n`` ORCHESTRATOR tasks of ``task_type`` (classified by the budget)."""
    for _ in range(n):
        db.add(
            Task(
                project_id=project.id,
                payloads=[{"type": task_type}],
                status=TaskStatus.COMPLETED,
                created_by="ORCHESTRATOR",
            )
        )
    await db.commit()


async def _seed_deployment_state(db) -> None:
    if await db.get(DeploymentState, 1) is None:
        db.add(DeploymentState())
        await db.commit()


async def _seed_dormant_debt(db, project) -> CompletionDebtEntry:
    entry = CompletionDebtEntry(
        project_id=None,
        kind=KIND_DORMANT_KNOB,
        dedupe_key=DEBT_KEY,
        title="Autonomous spawn-budget mode observe-only pending dedicated rollout",
        detail={"knob": CONFIG_KEY, "value": "observe"},
    )
    db.add(entry)
    await db.commit()
    await db.refresh(entry)
    return entry


# --- Registry + enabled gate --------------------------------------------------


def test_registry_wires_autonomous_spawn_budget_knob():
    keys = {s.config_key for s in mkg.registered_knobs()}
    assert CONFIG_KEY in keys
    spec = next(s for s in mkg.registered_knobs() if s.config_key == CONFIG_KEY)
    assert spec.debt_key == DEBT_KEY


def test_enabled_flag_defaults_off():
    """The shared harness enabled flag ships default False.

    ``shared_state.runtime_config`` IS the module-level defaults dict from
    ``runtime_config_state``; the autouse fixture restores it around every test,
    so at test entry the key holds its shipped default.
    """
    assert _rc.get(mkg.MODE_KNOB_GRADUATION_ENABLED_KEY) is False
    # And the key is genuinely seeded (not merely absent → falsy via .get()).
    assert mkg.MODE_KNOB_GRADUATION_ENABLED_KEY in _rc


@pytest.mark.asyncio
async def test_driver_noop_when_disabled(db):
    """When the harness is disabled, the driver runs nothing and mutates nothing."""
    project = await _project(db)
    await _spawn_tasks(db, project, "create", 5)  # would-be graduation signal
    await _seed_deployment_state(db)
    _configure(enabled=False, mode="observe", initiative_cap=2)

    decisions = await mkg.run_mode_knob_graduation(db, now=_utcnow())
    assert decisions == []
    # Mode untouched in memory and in the durable state.
    assert _rc[CONFIG_KEY] == "observe"
    state = await db.get(DeploymentState, 1)
    assert (state.runtime_config_json or {}).get(CONFIG_KEY) is None


# --- GRADUATE (acceptance evidence) ------------------------------------------


@pytest.mark.asyncio
async def test_autonomous_spawn_budget_graduates_and_resolves_debt(db):
    """GRADUATE observe→enforce: pressure over cap advances the mode, durably
    persists to DeploymentState.runtime_config_json, and resolves the dormant_knob
    debt row.

    initiative_cap=2 with 3 initiative ('create') spawns → pressure=1.5 ≥
    enforce_threshold=1.0 and would_have_blocked=1.
    """
    project = await _project(db)
    await _spawn_tasks(db, project, "create", 3)  # initiative
    await _spawn_tasks(db, project, "review", 1)  # servicing (noise)
    await _seed_deployment_state(db)
    debt = await _seed_dormant_debt(db, project)
    _configure(
        enabled=True,
        mode="observe",
        initiative_cap=2,
        enforce_threshold=1.0,
        min_sample=3,
    )

    decisions = await mkg.run_mode_knob_graduation(db, now=_utcnow())

    grad = next(d for d in decisions if d.config_key == CONFIG_KEY)
    assert grad.action == mkg.GRADUATE
    assert grad.from_mode == "observe"
    assert grad.to_mode == "enforce"
    assert grad.signal == pytest.approx(1.5)
    assert grad.measurements["would_have_blocked"] == 1
    assert grad.debt_resolved is True

    # In-memory config advanced.
    assert _rc[CONFIG_KEY] == "enforce"

    # Durable persist to DeploymentState.runtime_config_json.
    state = await db.get(DeploymentState, 1)
    await db.refresh(state)
    assert (state.runtime_config_json or {}).get(CONFIG_KEY) == "enforce"

    # dormant_knob debt row resolved.
    await db.refresh(debt)
    assert debt.status == "resolved"
    assert debt.resolved_at is not None


@pytest.mark.asyncio
async def test_off_to_observe_graduation_leaves_debt_open(db):
    """GRADUATE off→observe: crosses the observe threshold but the debt row stays
    OPEN (only reaching enforce resolves the dormant-knob obligation)."""
    project = await _project(db)
    await _spawn_tasks(db, project, "create", 3)  # initiative, cap=4 → pressure 0.75
    await _seed_deployment_state(db)
    debt = await _seed_dormant_debt(db, project)
    _configure(
        enabled=True,
        mode="off",
        initiative_cap=4,
        observe_threshold=0.5,
        enforce_threshold=1.0,
        min_sample=3,
    )

    decisions = await mkg.run_mode_knob_graduation(db, now=_utcnow())
    grad = next(d for d in decisions if d.config_key == CONFIG_KEY)
    assert grad.action == mkg.GRADUATE
    assert grad.from_mode == "off"
    assert grad.to_mode == "observe"
    assert grad.debt_resolved is False

    assert _rc[CONFIG_KEY] == "observe"
    state = await db.get(DeploymentState, 1)
    assert (state.runtime_config_json or {}).get(CONFIG_KEY) == "observe"
    await db.refresh(debt)
    assert debt.status == "open"


# --- CUT ---------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cut_recommendation_on_measured_no_lift(db):
    """CUT: a representative window with zero initiative pressure and nothing that
    would have blocked stamps a legible CUT recommendation with numbers into the
    dormant_knob debt row, without changing the mode."""
    project = await _project(db)
    await _spawn_tasks(db, project, "review", 5)  # all servicing → 0 initiative
    await _seed_deployment_state(db)
    debt = await _seed_dormant_debt(db, project)
    _configure(
        enabled=True,
        mode="observe",
        initiative_cap=10,
        cut_threshold=0.1,
        min_sample=3,
    )

    decisions = await mkg.run_mode_knob_graduation(db, now=_utcnow())
    cut = next(d for d in decisions if d.config_key == CONFIG_KEY)
    assert cut.action == mkg.CUT
    assert cut.signal == pytest.approx(0.0)
    assert cut.measurements["would_have_blocked"] == 0
    assert cut.cut_recommendation is not None
    assert cut.cut_recommendation["verdict"] == "cut"

    # Mode is NOT changed by a CUT.
    assert _rc[CONFIG_KEY] == "observe"
    state = await db.get(DeploymentState, 1)
    assert (state.runtime_config_json or {}).get(CONFIG_KEY) is None

    # The measured recommendation is stamped into the debt row's detail; the row
    # stays OPEN (advisory signal, not a disposition).
    await db.refresh(debt)
    assert debt.status == "open"
    assert isinstance(debt.detail, dict)
    rec = debt.detail.get("cut_recommendation")
    assert rec is not None
    assert rec["verdict"] == "cut"
    assert rec["measurements"]["initiative_count"] == 0
    assert rec["measurements"]["would_have_blocked"] == 0


# --- HOLD --------------------------------------------------------------------


@pytest.mark.asyncio
async def test_hold_below_threshold_is_noop(db):
    """HOLD: pressure defined but below the observe threshold (and above the cut
    threshold) is a no-op — no mode change, no debt mutation."""
    project = await _project(db)
    await _spawn_tasks(db, project, "create", 3)  # initiative, cap=10 → pressure 0.3
    await _seed_deployment_state(db)
    debt = await _seed_dormant_debt(db, project)
    _configure(
        enabled=True,
        mode="off",
        initiative_cap=10,
        observe_threshold=0.5,
        cut_threshold=0.1,
        min_sample=3,
    )

    decisions = await mkg.run_mode_knob_graduation(db, now=_utcnow())
    hold = next(d for d in decisions if d.config_key == CONFIG_KEY)
    assert hold.action == mkg.HOLD
    assert hold.signal == pytest.approx(0.3)

    assert _rc[CONFIG_KEY] == "off"
    state = await db.get(DeploymentState, 1)
    assert (state.runtime_config_json or {}).get(CONFIG_KEY) is None
    await db.refresh(debt)
    assert debt.status == "open"
    assert "cut_recommendation" not in (debt.detail or {})


@pytest.mark.asyncio
async def test_hold_insufficient_sample_is_noop(db):
    """HOLD: below min_sample the signal is insufficient — never graduate or cut on
    a window too thin to be representative."""
    project = await _project(db)
    await _spawn_tasks(db, project, "create", 1)  # one spawn only
    await _seed_deployment_state(db)
    _configure(
        enabled=True,
        mode="observe",
        initiative_cap=2,
        enforce_threshold=1.0,
        min_sample=5,  # 1 < 5 → insufficient
    )

    decisions = await mkg.run_mode_knob_graduation(db, now=_utcnow())
    hold = next(d for d in decisions if d.config_key == CONFIG_KEY)
    assert hold.action == mkg.HOLD
    assert hold.signal is None
    assert "insufficient" in hold.detail
    assert _rc[CONFIG_KEY] == "observe"


@pytest.mark.asyncio
async def test_already_at_enforce_holds(db):
    """A knob already at enforce is at the top rung — HOLD, no further advance."""
    project = await _project(db)
    await _spawn_tasks(db, project, "create", 5)
    await _seed_deployment_state(db)
    _configure(enabled=True, mode="enforce", initiative_cap=2, min_sample=3)

    decisions = await mkg.run_mode_knob_graduation(db, now=_utcnow())
    hold = next(d for d in decisions if d.config_key == CONFIG_KEY)
    assert hold.action == mkg.HOLD
    assert "already at enforce" in hold.detail
    assert _rc[CONFIG_KEY] == "enforce"


# --- Helpers for the four newly-registered knobs -------------------------------


async def _seed_evergreen_fill_decisions(
    db,
    *,
    n_observe_mode: int = 10,
    n_other: int = 5,
    start_hours_ago: float = 100,
    spacing_hours: float = 2,
) -> None:
    """Seed ``EvergreenFillDecisionLog`` rows for the evergreen-fill evidence provider.

    ``n_observe_mode`` rows with reason='observe_mode' (would-have-fired),
    ``n_other`` rows with other noop reasons (e.g. busy_queue).
    ``spacing_hours`` controls the interval between consecutive rows.
    """
    now = _utcnow()
    def _ts(i: int) -> datetime:
        return now - timedelta(hours=start_hours_ago - i * spacing_hours)

    for i in range(n_observe_mode):
        db.add(
            EvergreenFillDecisionLog(
                ts=_ts(i),
                action="noop",
                reason="observe_mode",
                trigger="low_utilization",
                mode="observe",
                util_threshold=0.4,
                dispatchable_queue_depth=0,
                active_evergreen_probes=0,
                daily_spawn_count=i,
                has_candidate=True,
            )
        )
    for i in range(n_other):
        db.add(
            EvergreenFillDecisionLog(
                ts=_ts(n_observe_mode + i),
                action="noop",
                reason="busy_queue",
                trigger=None,
                mode="observe",
                util_threshold=0.4,
                dispatchable_queue_depth=50,
                active_evergreen_probes=0,
                daily_spawn_count=0,
                has_candidate=True,
            )
        )
    await db.commit()


async def _seed_audit_lane_decisions(
    db,
    *,
    lane: str,
    n_tripped: int = 3,
    n_ok: int = 5,
    start_hours_ago: float = 100,
) -> None:
    """Seed ``AuditLaneDecisionLog`` rows for structural-health / metrics-integrity /
    gap-respawn evidence providers.

    ``n_tripped`` rows with ``tripped=True`` (would-have-fired), ``n_ok`` rows with
    ``tripped=False`` (no offenders).
    """
    now = _utcnow()
    for i in range(n_tripped):
        db.add(
            AuditLaneDecisionLog(
                ts=now - timedelta(hours=start_hours_ago - i * 8),
                lane=lane,
                mode="observe",
                tripped=True,
                enrolled=0,
                payload={"fat_row_offenders": 2} if "structural" in lane else {},
            )
        )
    for i in range(n_ok):
        db.add(
            AuditLaneDecisionLog(
                ts=now - timedelta(hours=start_hours_ago - (n_tripped + i) * 8),
                lane=lane,
                mode="observe",
                tripped=False,
                enrolled=0,
                payload={},
            )
        )
    await db.commit()


def _configure_evergreen_fill(
    *,
    enabled: bool = True,
    mode: str = "observe",
    observe_threshold: float = 0.1,
    enforce_threshold: float = 0.25,
    cut_threshold: float = 0.05,
    min_sample: int = 3,
    window_hours: float = 168,
) -> None:
    _rc[mkg.MODE_KNOB_GRADUATION_ENABLED_KEY] = enabled
    _rc["evergreen_fill_mode"] = mode
    _rc["evergreen_fill_graduation_observe_threshold"] = observe_threshold
    _rc["evergreen_fill_graduation_enforce_threshold"] = enforce_threshold
    _rc["evergreen_fill_graduation_cut_threshold"] = cut_threshold
    _rc["evergreen_fill_graduation_min_sample"] = min_sample
    _rc["evergreen_fill_graduation_window_hours"] = window_hours


def _configure_structural_health(
    *,
    enabled: bool = True,
    mode: str = "observe",
    observe_threshold: float = 0.1,
    enforce_threshold: float = 0.25,
    cut_threshold: float = 0.05,
    min_sample: int = 3,
    window_hours: float = 168,
) -> None:
    _rc[mkg.MODE_KNOB_GRADUATION_ENABLED_KEY] = enabled
    _rc["structural_health_audit_mode"] = mode
    _rc["structural_health_audit_graduation_observe_threshold"] = observe_threshold
    _rc["structural_health_audit_graduation_enforce_threshold"] = enforce_threshold
    _rc["structural_health_audit_graduation_cut_threshold"] = cut_threshold
    _rc["structural_health_audit_graduation_min_sample"] = min_sample
    _rc["structural_health_audit_graduation_window_hours"] = window_hours


def _configure_metrics_integrity(
    *,
    enabled: bool = True,
    mode: str = "observe",
    observe_threshold: float = 0.1,
    enforce_threshold: float = 0.25,
    cut_threshold: float = 0.05,
    min_sample: int = 3,
    window_hours: float = 168,
) -> None:
    _rc[mkg.MODE_KNOB_GRADUATION_ENABLED_KEY] = enabled
    _rc["metrics_integrity_sweep_mode"] = mode
    _rc["metrics_integrity_sweep_graduation_observe_threshold"] = observe_threshold
    _rc["metrics_integrity_sweep_graduation_enforce_threshold"] = enforce_threshold
    _rc["metrics_integrity_sweep_graduation_cut_threshold"] = cut_threshold
    _rc["metrics_integrity_sweep_graduation_min_sample"] = min_sample
    _rc["metrics_integrity_sweep_graduation_window_hours"] = window_hours


def _configure_gap_respawn(
    *,
    enabled: bool = True,
    mode: str = "observe",
    enforce_threshold: float = 3.0,
    min_sample: int = 1,
    window_hours: float = 168,
) -> None:
    _rc[mkg.MODE_KNOB_GRADUATION_ENABLED_KEY] = enabled
    _rc["direction_gap_respawn_block_mode"] = mode
    _rc["direction_gap_respawn_block_graduation_enforce_threshold"] = enforce_threshold
    _rc["direction_gap_respawn_block_graduation_min_sample"] = min_sample
    _rc["direction_gap_respawn_block_graduation_window_hours"] = window_hours


# --- Registry: all five knobs (acceptance evidence) ---------------------------


ALL_FIVE_CONFIG_KEYS = {
    "autonomous_spawn_budget_mode",
    "evergreen_fill_mode",
    "structural_health_audit_mode",
    "metrics_integrity_sweep_mode",
    "direction_gap_respawn_block_mode",
}


def test_all_five_dormant_knobs_registered_in_harness():
    """Acceptance evidence: every dormant knob is registered in the harness."""
    keys = {s.config_key for s in mkg.registered_knobs()}
    missing = ALL_FIVE_CONFIG_KEYS - keys
    assert not missing, f"Missing from registry: {missing}"
    assert keys == ALL_FIVE_CONFIG_KEYS, (
        f"Unexpected extra keys registered: {keys - ALL_FIVE_CONFIG_KEYS}"
    )
    # Verify debt_keys match the expected pattern for each.
    for spec in mkg.registered_knobs():
        assert spec.debt_key == f"dormant_knob:{spec.config_key}"
        assert callable(spec.compute_evidence)
        assert spec.label


# --- Evergreen fill: GRADUATE -------------------------------------------------


@pytest.mark.asyncio
async def test_evergreen_fill_graduates_observe_to_enforce(db):
    """GRADUATE observe→enforce: cap-adjusted fire rate clears the enforce threshold
    and the mode is durably persisted."""
    # Seed 5 would-have-fired events 24h apart across ~5 UTC days
    # so the daily-cap adjustment (cap=3) does not constrain and the
    # cap-adjusted rate is deterministic (5/10 = 0.50).
    await _seed_evergreen_fill_decisions(
        db, n_observe_mode=5, n_other=5, spacing_hours=24
    )
    await _seed_deployment_state(db)
    _configure_evergreen_fill(
        enabled=True, mode="observe", enforce_threshold=0.25, min_sample=3
    )
    # cap_adjusted_fire_rate_pct = 5 would-fire / 10 total = 50%
    # signal = 0.50 >= 0.25 enforce threshold → GRADUATE

    decisions = await mkg.run_mode_knob_graduation(db, now=_utcnow())
    grad = next(d for d in decisions if d.config_key == "evergreen_fill_mode")
    assert grad.action == mkg.GRADUATE
    assert grad.from_mode == "observe"
    assert grad.to_mode == "enforce"
    assert grad.signal == pytest.approx(0.50, abs=0.05)
    assert grad.measurements["would_have_fired"] == 5

    assert _rc["evergreen_fill_mode"] == "enforce"
    state = await db.get(DeploymentState, 1)
    assert (state.runtime_config_json or {}).get("evergreen_fill_mode") == "enforce"


# --- Evergreen fill: CUT ------------------------------------------------------


@pytest.mark.asyncio
async def test_evergreen_fill_cut_on_zero_lift(db):
    """CUT: a representative window with zero would-have-fired events stamps a
    legible CUT recommendation without changing the mode."""
    # All rows have non-observe_mode reasons (busy_queue).
    await _seed_evergreen_fill_decisions(db, n_observe_mode=0, n_other=10)
    await _seed_deployment_state(db)
    project = await _project(db)
    debt = CompletionDebtEntry(
        project_id=project.id,
        kind=KIND_DORMANT_KNOB,
        dedupe_key="dormant_knob:evergreen_fill_mode",
        title="Evergreen fill mode observe-only",
        detail={"knob": "evergreen_fill_mode", "value": "observe"},
    )
    db.add(debt)
    await db.commit()
    await db.refresh(debt)
    _configure_evergreen_fill(
        enabled=True, mode="observe", cut_threshold=0.05, min_sample=3
    )
    # 0 would-have-fired out of 10 evaluations → no_lift = True

    decisions = await mkg.run_mode_knob_graduation(db, now=_utcnow())
    cut = next(d for d in decisions if d.config_key == "evergreen_fill_mode")
    assert cut.action == mkg.CUT
    assert cut.signal == pytest.approx(0.0)
    assert cut.measurements["would_have_fired"] == 0
    assert cut.cut_recommendation is not None
    assert cut.cut_recommendation["verdict"] == "cut"

    assert _rc["evergreen_fill_mode"] == "observe"


# --- Evergreen fill: HOLD (insufficient sample) -------------------------------


@pytest.mark.asyncio
async def test_evergreen_fill_hold_insufficient_sample(db):
    """HOLD: fewer than min_sample observe-mode evaluations → undefined signal."""
    await _seed_evergreen_fill_decisions(db, n_observe_mode=1, n_other=1)
    await _seed_deployment_state(db)
    _configure_evergreen_fill(
        enabled=True, mode="observe", min_sample=10, window_hours=168
    )
    # 2 evaluations < 10 min_sample → HOLD

    decisions = await mkg.run_mode_knob_graduation(db, now=_utcnow())
    hold = next(d for d in decisions if d.config_key == "evergreen_fill_mode")
    assert hold.action == mkg.HOLD
    assert hold.signal is None
    assert "insufficient" in hold.detail
    assert _rc["evergreen_fill_mode"] == "observe"


# --- Structural health audit: GRADUATE ----------------------------------------


@pytest.mark.asyncio
async def test_structural_health_graduates_off_to_observe(db):
    """GRADUATE off→observe: tripped_rate clears the observe threshold."""
    await _seed_audit_lane_decisions(
        db, lane="structural_health", n_tripped=3, n_ok=5
    )
    await _seed_deployment_state(db)
    _configure_structural_health(
        enabled=True, mode="off", observe_threshold=0.1, min_sample=3
    )
    # 3/(3+5) = 0.375 >= 0.1 observe threshold → GRADUATE off→observe

    decisions = await mkg.run_mode_knob_graduation(db, now=_utcnow())
    grad = next(d for d in decisions if d.config_key == "structural_health_audit_mode")
    assert grad.action == mkg.GRADUATE
    assert grad.from_mode == "off"
    assert grad.to_mode == "observe"
    assert grad.signal == pytest.approx(3.0 / 8.0)
    assert grad.measurements["tripped_count"] == 3
    assert grad.debt_resolved is False  # stays open at observe

    assert _rc["structural_health_audit_mode"] == "observe"
    state = await db.get(DeploymentState, 1)
    assert (
        state.runtime_config_json or {}
    ).get("structural_health_audit_mode") == "observe"


# --- Structural health audit: CUT ---------------------------------------------


@pytest.mark.asyncio
async def test_structural_health_cut_on_zero_offenders(db):
    """CUT: a representative window with zero tripped cycles — enforce would never
    bind because there are no structural offenders to enroll."""
    await _seed_audit_lane_decisions(
        db, lane="structural_health", n_tripped=0, n_ok=8
    )
    await _seed_deployment_state(db)
    project = await _project(db)
    debt = CompletionDebtEntry(
        project_id=project.id,
        kind=KIND_DORMANT_KNOB,
        dedupe_key="dormant_knob:structural_health_audit_mode",
        title="Structural-health audit observe-only",
        detail={"knob": "structural_health_audit_mode", "value": "observe"},
    )
    db.add(debt)
    await db.commit()
    await db.refresh(debt)
    _configure_structural_health(
        enabled=True, mode="observe", cut_threshold=0.05, min_sample=3
    )
    # 0 tripped out of 8 cycles → no_lift

    decisions = await mkg.run_mode_knob_graduation(db, now=_utcnow())
    cut = next(d for d in decisions if d.config_key == "structural_health_audit_mode")
    assert cut.action == mkg.CUT
    assert cut.signal == pytest.approx(0.0)
    assert cut.measurements["tripped_count"] == 0
    assert cut.cut_recommendation is not None
    assert cut.cut_recommendation["verdict"] == "cut"

    assert _rc["structural_health_audit_mode"] == "observe"


# --- Metrics integrity sweep: GRADUATE ----------------------------------------


@pytest.mark.asyncio
async def test_metrics_integrity_graduates_observe_to_enforce(db):
    """GRADUATE observe→enforce: tripped_rate clears the enforce threshold."""
    await _seed_audit_lane_decisions(
        db, lane="metrics_integrity", n_tripped=3, n_ok=2
    )
    await _seed_deployment_state(db)
    # Seed dormant debt for the knob to verify debt resolution.
    debt = CompletionDebtEntry(
        project_id=None,
        kind=KIND_DORMANT_KNOB,
        dedupe_key="dormant_knob:metrics_integrity_sweep_mode",
        title="Metrics-integrity sweep observe-only",
        detail={"knob": "metrics_integrity_sweep_mode", "value": "observe"},
    )
    db.add(debt)
    await db.commit()
    await db.refresh(debt)
    _configure_metrics_integrity(
        enabled=True, mode="observe", enforce_threshold=0.25, min_sample=3
    )
    # 3/(3+2) = 0.6 >= 0.25 enforce threshold → GRADUATE

    decisions = await mkg.run_mode_knob_graduation(db, now=_utcnow())
    grad = next(
        d for d in decisions if d.config_key == "metrics_integrity_sweep_mode"
    )
    assert grad.action == mkg.GRADUATE
    assert grad.from_mode == "observe"
    assert grad.to_mode == "enforce"
    assert grad.signal == pytest.approx(3.0 / 5.0)
    assert grad.measurements["tripped_count"] == 3
    assert grad.debt_resolved is True

    assert _rc["metrics_integrity_sweep_mode"] == "enforce"
    state = await db.get(DeploymentState, 1)
    assert (
        state.runtime_config_json or {}
    ).get("metrics_integrity_sweep_mode") == "enforce"

    await db.refresh(debt)
    assert debt.status == "resolved"


# --- Metrics integrity sweep: CUT ---------------------------------------------


@pytest.mark.asyncio
async def test_metrics_integrity_cut_on_zero_drift(db):
    """CUT: zero flagged ticks over a representative window — enforce would never
    enroll drift findings."""
    await _seed_audit_lane_decisions(
        db, lane="metrics_integrity", n_tripped=0, n_ok=6
    )
    await _seed_deployment_state(db)
    project = await _project(db)
    debt = CompletionDebtEntry(
        project_id=project.id,
        kind=KIND_DORMANT_KNOB,
        dedupe_key="dormant_knob:metrics_integrity_sweep_mode",
        title="Metrics-integrity sweep observe-only",
        detail={"knob": "metrics_integrity_sweep_mode", "value": "observe"},
    )
    db.add(debt)
    await db.commit()
    await db.refresh(debt)
    _configure_metrics_integrity(
        enabled=True, mode="observe", cut_threshold=0.05, min_sample=3
    )

    decisions = await mkg.run_mode_knob_graduation(db, now=_utcnow())
    cut = next(
        d for d in decisions if d.config_key == "metrics_integrity_sweep_mode"
    )
    assert cut.action == mkg.CUT
    assert cut.signal == pytest.approx(0.0)
    assert cut.measurements["tripped_count"] == 0
    assert cut.cut_recommendation is not None

    assert _rc["metrics_integrity_sweep_mode"] == "observe"


# --- Direction gap respawn block: GRADUATE ------------------------------------


@pytest.mark.asyncio
async def test_gap_respawn_graduates_observe_to_enforce(db):
    """GRADUATE observe→enforce: would_have_blocked count clears the enforce
    threshold (3 events ≥ 3). This knob only takes observe/enforce, no off."""
    await _seed_audit_lane_decisions(
        db, lane="gap_respawn", n_tripped=3, n_ok=0
    )
    await _seed_deployment_state(db)
    debt = CompletionDebtEntry(
        project_id=None,
        kind=KIND_DORMANT_KNOB,
        dedupe_key="dormant_knob:direction_gap_respawn_block_mode",
        title="Direction gap-respawn block observe-only",
        detail={"knob": "direction_gap_respawn_block_mode", "value": "observe"},
    )
    db.add(debt)
    await db.commit()
    await db.refresh(debt)
    _configure_gap_respawn(
        enabled=True, mode="observe", enforce_threshold=3.0, min_sample=1
    )
    # 3 would-have-blocked events ≥ 3 enforce threshold → GRADUATE

    decisions = await mkg.run_mode_knob_graduation(db, now=_utcnow())
    grad = next(
        d for d in decisions if d.config_key == "direction_gap_respawn_block_mode"
    )
    assert grad.action == mkg.GRADUATE
    assert grad.from_mode == "observe"
    assert grad.to_mode == "enforce"
    assert grad.signal == 3.0
    assert grad.measurements["would_have_blocked"] == 3
    assert grad.debt_resolved is True

    assert _rc["direction_gap_respawn_block_mode"] == "enforce"
    state = await db.get(DeploymentState, 1)
    assert (
        state.runtime_config_json or {}
    ).get("direction_gap_respawn_block_mode") == "enforce"

    await db.refresh(debt)
    assert debt.status == "resolved"


# --- Direction gap respawn block: CUT -----------------------------------------


@pytest.mark.asyncio
async def test_gap_respawn_cut_on_zero_blocks(db):
    """CUT: zero would-have-blocked events over a representative window — the
    gap-respawn rule never binds, enforce would prevent nothing."""
    # Seed gap_respawn with 0 rows (no matches).
    await _seed_audit_lane_decisions(
        db, lane="gap_respawn", n_tripped=0, n_ok=0
    )
    # Seed other audit lanes so the evaluations counter shows the audit
    # machinery was alive (gap_respawn only logs on match).
    await _seed_audit_lane_decisions(
        db, lane="structural_health", n_tripped=0, n_ok=5
    )
    await _seed_deployment_state(db)
    project = await _project(db)
    debt = CompletionDebtEntry(
        project_id=project.id,
        kind=KIND_DORMANT_KNOB,
        dedupe_key="dormant_knob:direction_gap_respawn_block_mode",
        title="Direction gap-respawn block observe-only",
        detail={"knob": "direction_gap_respawn_block_mode", "value": "observe"},
    )
    db.add(debt)
    await db.commit()
    await db.refresh(debt)
    _configure_gap_respawn(
        enabled=True, mode="observe", window_hours=168
    )
    # 0 would-have-blocked out of 5 total evaluations → no_lift

    decisions = await mkg.run_mode_knob_graduation(db, now=_utcnow())
    cut = next(
        d for d in decisions if d.config_key == "direction_gap_respawn_block_mode"
    )
    assert cut.action == mkg.CUT
    assert cut.signal == pytest.approx(0.0)
    assert cut.measurements["would_have_blocked"] == 0
    assert cut.cut_recommendation is not None

    assert _rc["direction_gap_respawn_block_mode"] == "observe"


# --- Direction gap respawn block: HOLD (insufficient sample) ------------------


@pytest.mark.asyncio
async def test_gap_respawn_hold_insufficient_sample(db):
    """HOLD: fewer than min_sample would-have-blocked events."""
    # Empty gap_respawn lane yields 0 cycles = 0 would_have_blocked.
    _configure_gap_respawn(
        enabled=True, mode="observe", min_sample=2, window_hours=168
    )
    await _seed_deployment_state(db)

    decisions = await mkg.run_mode_knob_graduation(db, now=_utcnow())
    hold = next(
        d for d in decisions if d.config_key == "direction_gap_respawn_block_mode"
    )
    assert hold.action == mkg.HOLD
    assert hold.signal is None
    assert "insufficient" in hold.detail
    assert _rc["direction_gap_respawn_block_mode"] == "observe"
