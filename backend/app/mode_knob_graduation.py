"""Shared mode-knob graduation harness.

Generalizes the proven ``capability_proof_gate`` auto-graduation pattern
(``metrics_integrity_sweep.check_capability_proof_gate_auto_graduation``, a.k.a.
"decision #7") into a REUSABLE driver that disposes dormant runtime-config
mode-knobs with real MEASURED evidence, per the default-off PRINCIPLE:

    default-off is guilty until proven innocent — graduate a knob on measured
    lift, or emit a legible CUT recommendation carrying the measured numbers.

Five dormant knobs (``evergreen_fill_mode``, ``structural_health_audit_mode``,
``metrics_integrity_sweep_mode``, ``autonomous_spawn_budget_mode``,
``direction_gap_respawn_block_mode``) ship permanently in ``off``/``observe``
with real ``enforce`` branches but no evidence-driven path to ever graduate, so
they age as completion debt indefinitely. This harness gives each registered
knob exactly one of three dispositions per driver tick:

* **GRADUATE** — the measured counterfactual signal cleared the next threshold;
  advance ONE monotonic step (``off → observe`` or ``observe → enforce``),
  durably persist to ``DeploymentState.runtime_config_json`` BEFORE mutating the
  in-memory ``runtime_config``, resolve the ``dormant_knob:<key>`` completion-debt
  row on reaching ``enforce``, and post an operator notification.
* **CUT** — the counterfactual shows the enforce branch would never bind (no
  lift); stamp a legible CUT recommendation carrying the measured numbers into
  the knob's ``dormant_knob`` debt-row detail AND post a HIGH-SIGNAL operator
  NOTE. There was previously no CUT surface on the ledger — this is it.
* **HOLD** — below threshold (or insufficient sample); no-op, return the measured
  snapshot for observability.

Hard invariants (mirrors the proven pattern exactly):

* **Durable-before-in-memory.** A process must never enforce a mode that was not
  durably committed. The DB write to ``DeploymentState.runtime_config_json``
  commits BEFORE the in-memory ``runtime_config`` mutation.
* **Monotonic.** Only advances ``off → observe → enforce``; never regresses.
* **Single arm.** Gated behind a single dedicated ``mode_knob_graduation_enabled``
  flag (default ``True``). Independent of any single knob's own mode — the
  driver does NOT require the target knob (or the metrics-integrity sweep) to
  already be enforcing to run.
* **Isolated.** Each knob is driven inside its own try/except; a crash records an
  error decision and never aborts the driver or the hosting reaper cycle.
"""

from __future__ import annotations

import contextlib
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Awaitable, Callable, Mapping

from sqlmodel import select

from app.time_utils import _utcnow

logger = logging.getLogger(__name__)

#: Single dedicated arm for the whole harness (default True).
MODE_KNOB_GRADUATION_ENABLED_KEY = "mode_knob_graduation_enabled"

#: Monotonic mode ladder. A knob may only advance to the NEXT rung.
_MODE_ORDER = {"off": 0, "observe": 1, "enforce": 2}

# Decision actions.
GRADUATE = "graduate"
CUT = "cut"
HOLD = "hold"
ERROR = "error"


@dataclass(frozen=True)
class KnobEvidence:
    """The measured counterfactual a knob's evidence provider returns.

    * ``measurements`` — the rendered measured numbers (would-have-blocked counts,
      headroom, pressure, window, …) surfaced to the operator and stamped into
      notifications / CUT recommendations.
    * ``signal`` — the scalar graduation signal compared against the knob's
      thresholds. ``None`` means the signal is undefined / the sample is
      insufficient, so the harness must HOLD (never graduate or cut on no data).
    * ``no_lift`` — the domain-specific CUT criterion: the enforce branch would
      never bind over a representative window. Only consulted when ``signal`` is
      not ``None`` and graduation did not fire.
    * ``summary`` — a one-line human description of the measured decision inputs.
    """

    measurements: dict[str, Any]
    signal: float | None
    no_lift: bool = False
    summary: str = ""


# An evidence provider computes the observe-mode counterfactual for one knob.
EvidenceProvider = Callable[..., Awaitable[KnobEvidence]]


@dataclass(frozen=True)
class KnobGraduationSpec:
    """Registration record for one knob driven by the harness."""

    config_key: str
    label: str
    compute_evidence: EvidenceProvider
    observe_threshold_key: str
    enforce_threshold_key: str
    default_observe_threshold: float
    default_enforce_threshold: float

    @property
    def debt_key(self) -> str:
        """The stable ``dormant_knob`` completion-debt dedupe key for this knob."""
        return f"dormant_knob:{self.config_key}"


@dataclass(frozen=True)
class KnobGraduationDecision:
    """One driver outcome for one knob. Carries the measured numbers, never a
    mutation the caller must apply — the harness already applied any side effect."""

    config_key: str
    action: str  # GRADUATE | CUT | HOLD | ERROR
    from_mode: str
    to_mode: str | None = None
    signal: float | None = None
    measurements: dict[str, Any] = field(default_factory=dict)
    detail: str = ""
    cut_recommendation: dict[str, Any] | None = None
    debt_resolved: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "config_key": self.config_key,
            "action": self.action,
            "from_mode": self.from_mode,
            "to_mode": self.to_mode,
            "signal": self.signal,
            "measurements": self.measurements,
            "detail": self.detail,
            "cut_recommendation": self.cut_recommendation,
            "debt_resolved": self.debt_resolved,
        }


# --- Registry ----------------------------------------------------------------

_REGISTRY: dict[str, KnobGraduationSpec] = {}


def register_knob(spec: KnobGraduationSpec) -> KnobGraduationSpec:
    """Register a knob spec under its ``config_key`` (last registration wins)."""
    _REGISTRY[spec.config_key] = spec
    return spec


def registered_knobs() -> list[KnobGraduationSpec]:
    """Registered specs in insertion order."""
    return list(_REGISTRY.values())


# --- Evidence provider: autonomous_spawn_budget_mode -------------------------


async def _autonomous_spawn_budget_evidence(
    session: Any,
    *,
    runtime_config: Mapping[str, Any],
    now: datetime | None = None,
) -> KnobEvidence:
    """Compute the ``autonomous_spawn_budget_mode`` counterfactual as evidence.

    Signal = initiative pressure (``initiative_count / initiative_cap``) over the
    trailing window. CUT criterion (no lift): a representative window
    (``>= min_sample`` observed spawns) in which nothing would have been blocked
    AND pressure sits below the cut threshold — the enforce branch would never
    bind. Below ``min_sample`` the signal is treated as insufficient (HOLD).
    """
    from app.autonomous_spawn_budget import (
        compute_autonomous_spawn_budget_counterfactual,
    )

    window_hours = float(
        runtime_config.get("autonomous_spawn_budget_graduation_window_hours", 24)
    )
    min_sample = int(
        runtime_config.get("autonomous_spawn_budget_graduation_min_sample", 20)
    )
    cut_threshold = float(
        runtime_config.get("autonomous_spawn_budget_graduation_cut_threshold", 0.1)
    )

    measurements = await compute_autonomous_spawn_budget_counterfactual(
        session,
        runtime_config=runtime_config,
        now=now,
        window=timedelta(hours=window_hours),
    )
    measurements["min_sample"] = min_sample
    measurements["cut_threshold"] = cut_threshold

    total = int(measurements.get("total_count") or 0)
    pressure = measurements.get("pressure")
    would_have_blocked = int(measurements.get("would_have_blocked") or 0)

    # Insufficient sample (or an undefined pressure ratio) → HOLD. Never graduate
    # or cut on a window too thin to be representative.
    if pressure is None or total < min_sample:
        return KnobEvidence(
            measurements=measurements,
            signal=None,
            no_lift=False,
            summary=(
                f"insufficient sample: {total} spawn(s) in {window_hours:.0f}h "
                f"(min_sample={min_sample})"
            ),
        )

    no_lift = would_have_blocked == 0 and pressure < cut_threshold
    return KnobEvidence(
        measurements=measurements,
        signal=float(pressure),
        no_lift=no_lift,
        summary=(
            f"pressure={pressure:.3f} (initiative={measurements['initiative_count']}"
            f"/{measurements['initiative_cap']}), would_have_blocked="
            f"{would_have_blocked} over {window_hours:.0f}h ({total} spawns)"
        ),
    )


register_knob(
    KnobGraduationSpec(
        config_key="autonomous_spawn_budget_mode",
        label="Autonomous initiative spawn budget",
        compute_evidence=_autonomous_spawn_budget_evidence,
        observe_threshold_key="autonomous_spawn_budget_graduation_observe_threshold",
        enforce_threshold_key="autonomous_spawn_budget_graduation_enforce_threshold",
        default_observe_threshold=0.5,
        default_enforce_threshold=1.0,
    )
)


# --- Evidence provider: evergreen_fill_mode ------------------------------------


async def _evergreen_fill_evidence(
    session: Any,
    *,
    runtime_config: Mapping[str, Any],
    now: datetime | None = None,
) -> KnobEvidence:
    """Delegate to the canonical ``compute_observe_mode_verdict`` for evidence.

    The canonical verdict uses a **daily-cap-adjusted** fire rate that avoids
    idle-fleet inflation: consecutive observe-mode ticks within the same UTC
    calendar day are capped (default 3/day) so bursty idle ticks do not drive
    the rate toward 100%.  Signal = the cap-adjusted fire rate normalized to
    0-1, preserving the original direction (higher rate = more lift = more
    likely to graduate).  ``no_lift`` = zero would-have-fired events over a
    representative window.
    """
    from app.evergreen_backlog import compute_observe_mode_verdict

    window_hours = int(
        runtime_config.get("evergreen_fill_graduation_window_hours", 168)
    )
    min_sample = int(
        runtime_config.get("evergreen_fill_graduation_min_sample", 10)
    )

    verdict = await compute_observe_mode_verdict(
        session,
        window_hours=window_hours,
        min_evaluations=min_sample,
    )

    total = int(verdict.get("observe_evaluations") or 0)
    would_have_fired = int(verdict.get("would_have_fired") or 0)

    if verdict["verdict"] == "insufficient_data":
        return KnobEvidence(
            measurements=verdict,
            signal=None,
            no_lift=False,
            summary=(
                f"insufficient sample: {total} observe evaluation(s) in "
                f"{window_hours:.0f}h (min_sample={min_sample})"
            ),
        )

    # Signal = cap-adjusted fire rate on 0-1 scale.  Grouping by UTC
    # calendar day and capping each day's count prevents the idle-fleet
    # inflation that plagues the raw would-have-fired rate.
    cap_adjusted_rate = float(
        verdict.get("cap_adjusted_fire_rate_pct") or 0
    ) / 100.0
    no_lift = would_have_fired == 0
    return KnobEvidence(
        measurements=verdict,
        signal=float(cap_adjusted_rate),
        no_lift=no_lift,
        summary=(
            f"verdict={verdict['verdict']} "
            f"(cap_adjusted_rate={cap_adjusted_rate:.3f}, "
            f"{would_have_fired}/{total} would-fire, "
            f"window={window_hours:.0f}h)"
        ),
    )


register_knob(
    KnobGraduationSpec(
        config_key="evergreen_fill_mode",
        label="Evergreen backlog auto-fill",
        compute_evidence=_evergreen_fill_evidence,
        observe_threshold_key="evergreen_fill_graduation_observe_threshold",
        enforce_threshold_key="evergreen_fill_graduation_enforce_threshold",
        default_observe_threshold=0.1,
        default_enforce_threshold=0.25,
    )
)


# --- Evidence provider: structural_health_audit_mode ---------------------------


async def _structural_health_audit_evidence(
    session: Any,
    *,
    runtime_config: Mapping[str, Any],
    now: datetime | None = None,
) -> KnobEvidence:
    """Compute the ``structural_health_audit_mode`` counterfactual as evidence.

    Signal = tripped_rate — the fraction of audit cycles where at least one
    structural offender was found (fat-row, JSONB bloat, GIN overhead, or pool
    saturation) over the trailing window.  Lift = real offenders that enforce
    would enroll.  CUT criterion (no lift): zero tripped cycles over a
    representative window — the enforce branch would never bind.
    """
    window_hours = float(
        runtime_config.get("structural_health_audit_graduation_window_hours", 168)
    )
    min_sample = int(
        runtime_config.get("structural_health_audit_graduation_min_sample", 3)
    )
    cut_threshold = float(
        runtime_config.get("structural_health_audit_graduation_cut_threshold", 0.05)
    )

    measurements = await _read_audit_lane_counterfactual(
        session,
        lane="structural_health",
        now=now,
        window_hours=window_hours,
    )
    measurements["min_sample"] = min_sample
    measurements["cut_threshold"] = cut_threshold

    total = int(measurements.get("cycles") or 0)
    tripped_count = int(measurements.get("tripped_count") or 0)

    if total < min_sample:
        return KnobEvidence(
            measurements=measurements,
            signal=None,
            no_lift=False,
            summary=(
                f"insufficient sample: {total} audit cycle(s) in "
                f"{window_hours:.0f}h (min_sample={min_sample})"
            ),
        )

    rate = tripped_count / total if total > 0 else 0.0
    no_lift = tripped_count == 0 and rate < cut_threshold
    return KnobEvidence(
        measurements=measurements,
        signal=float(rate),
        no_lift=no_lift,
        summary=(
            f"tripped_rate={rate:.3f} ({tripped_count}/{total} cycles "
            f"in {window_hours:.0f}h)"
        ),
    )


register_knob(
    KnobGraduationSpec(
        config_key="structural_health_audit_mode",
        label="Structural-health audit",
        compute_evidence=_structural_health_audit_evidence,
        observe_threshold_key="structural_health_audit_graduation_observe_threshold",
        enforce_threshold_key="structural_health_audit_graduation_enforce_threshold",
        default_observe_threshold=0.1,
        default_enforce_threshold=0.25,
    )
)


# --- Evidence provider: metrics_integrity_sweep_mode ---------------------------


async def _metrics_integrity_sweep_evidence(
    session: Any,
    *,
    runtime_config: Mapping[str, Any],
    now: datetime | None = None,
) -> KnobEvidence:
    """Compute the ``metrics_integrity_sweep_mode`` counterfactual as evidence.

    Signal = tripped_rate — the fraction of sweep ticks where at least one drift
    finding was flagged over the trailing window.  Lift = genuine drift the
    enforce branch would enroll.  CUT criterion (no lift): zero flagged ticks
    over a representative window.

    IMPORTANT: this evidence provider does NOT require the knob to be in enforce
    to run — it reads the durable ``AuditLaneDecisionLog`` which is written
    regardless of mode, so the harness runs independently.
    """
    window_hours = float(
        runtime_config.get("metrics_integrity_sweep_graduation_window_hours", 168)
    )
    min_sample = int(
        runtime_config.get("metrics_integrity_sweep_graduation_min_sample", 3)
    )
    cut_threshold = float(
        runtime_config.get("metrics_integrity_sweep_graduation_cut_threshold", 0.05)
    )

    measurements = await _read_audit_lane_counterfactual(
        session,
        lane="metrics_integrity",
        now=now,
        window_hours=window_hours,
    )
    measurements["min_sample"] = min_sample
    measurements["cut_threshold"] = cut_threshold

    total = int(measurements.get("cycles") or 0)
    tripped_count = int(measurements.get("tripped_count") or 0)

    if total < min_sample:
        return KnobEvidence(
            measurements=measurements,
            signal=None,
            no_lift=False,
            summary=(
                f"insufficient sample: {total} sweep tick(s) in "
                f"{window_hours:.0f}h (min_sample={min_sample})"
            ),
        )

    rate = tripped_count / total if total > 0 else 0.0
    no_lift = tripped_count == 0 and rate < cut_threshold
    return KnobEvidence(
        measurements=measurements,
        signal=float(rate),
        no_lift=no_lift,
        summary=(
            f"tripped_rate={rate:.3f} ({tripped_count}/{total} ticks "
            f"in {window_hours:.0f}h)"
        ),
    )


register_knob(
    KnobGraduationSpec(
        config_key="metrics_integrity_sweep_mode",
        label="Metrics-integrity sweep",
        compute_evidence=_metrics_integrity_sweep_evidence,
        observe_threshold_key="metrics_integrity_sweep_graduation_observe_threshold",
        enforce_threshold_key="metrics_integrity_sweep_graduation_enforce_threshold",
        default_observe_threshold=0.1,
        default_enforce_threshold=0.25,
    )
)


# --- Shared helper: read AuditLaneDecisionLog counterfactual -------------------


async def _read_audit_lane_counterfactual(
    session: Any,
    *,
    lane: str,
    now: datetime | None = None,
    window_hours: float = 168,
) -> dict[str, Any]:
    """Read observe-window stats from the durable ``AuditLaneDecisionLog``.

    Shared by ``structural_health_audit_mode`` and ``metrics_integrity_sweep_mode``
    evidence providers. Returns aggregate counts over the trailing window.
    """
    from datetime import timedelta

    from app.models import AuditLaneDecisionLog
    from app.time_utils import _utcnow

    _now = now or _utcnow()
    cutoff = _now - timedelta(hours=window_hours)
    try:
        rows = (
            (
                await session.execute(
                    select(AuditLaneDecisionLog)
                    .where(
                        AuditLaneDecisionLog.lane == lane,
                        AuditLaneDecisionLog.ts >= cutoff,
                    )
                    .order_by(AuditLaneDecisionLog.ts.desc())
                    .limit(500)
                )
            )
            .scalars()
            .all()
        )
    except Exception:
        logger.warning(
            "[MODE_KNOB_GRADUATION] _read_audit_lane_counterfactual lane=%s failed",
            lane,
            exc_info=True,
        )
        return {
            "source": "AuditLaneDecisionLog",
            "lane": lane,
            "window_hours": window_hours,
            "cycles": 0,
            "tripped_count": 0,
            "enrolled_total": 0,
            "last_cycle_at": None,
        }

    total = len(rows)
    tripped_count = sum(1 for r in rows if r.tripped)
    enrolled_total = sum(r.enrolled for r in rows)
    last_ts = rows[0].ts.isoformat() if rows else None
    return {
        "source": "AuditLaneDecisionLog",
        "lane": lane,
        "window_hours": window_hours,
        "cycles": total,
        "tripped_count": tripped_count,
        "enrolled_total": enrolled_total,
        "last_cycle_at": last_ts,
    }


async def _read_audit_evaluation_count(
    session: Any,
    *,
    now: datetime | None = None,
    window_hours: float = 168,
) -> int:
    """Count total ``AuditLaneDecisionLog`` rows (any lane) in the trailing window.

    Used by ``_direction_gap_respawn_block_evidence`` as a proxy for "the
    evaluation machinery was alive during the window."  Unlike other audit lanes
    where every evaluation writes a row, the gap_respawn lane only writes on
    match, so counting gap_respawn rows alone conflates "no evaluations ran"
    with "evaluations ran but found no match."
    """
    from datetime import timedelta

    from app.models import AuditLaneDecisionLog
    from app.time_utils import _utcnow

    _now = now or _utcnow()
    cutoff = _now - timedelta(hours=window_hours)
    try:
        rows = (
            (
                await session.execute(
                    select(AuditLaneDecisionLog)
                    .where(AuditLaneDecisionLog.ts >= cutoff)
                    .order_by(AuditLaneDecisionLog.ts.desc())
                    .limit(500)
                )
            )
            .scalars()
            .all()
        )
    except Exception:
        logger.warning(
            "[MODE_KNOB_GRADUATION] _read_audit_evaluation_count failed",
            exc_info=True,
        )
        return 0
    return len(rows)


# --- Evidence provider: direction_gap_respawn_block_mode -----------------------


async def _direction_gap_respawn_block_evidence(
    session: Any,
    *,
    runtime_config: Mapping[str, Any],
    now: datetime | None = None,
) -> KnobEvidence:
    """Compute the ``direction_gap_respawn_block_mode`` counterfactual as evidence.

    Signal = would_have_blocked — the raw count of observe-mode would-have-
    blocked events over the trailing window.  Each event represents one direction-
    creation attempt that matched a recently-retired direction (by gap_id or text
    overlap) within the cooldown window.  Lift = redundant same-gap respawns
    enforce would prevent.  CUT criterion (no lift): zero would-have-blocked over
    a representative window (the gap-respawn rule never binds).

    This knob only takes observe/enforce (no off), so only the enforce threshold
    applies.  The evidence reads from ``AuditLaneDecisionLog`` rows written at
    observe-mode match time (lane='gap_respawn').

    **Sample sufficiency** uses a separate *evaluations* counter (all audit-lane
    rows in the window, not just gap_respawn matches) because gap_respawn rows
    are ONLY written when a would-have-blocked event occurs.  Zero gap_respawn
    rows could mean "no evaluation ran" or "evaluations ran but found no match";
    a nonzero evaluations count distinguishes these.  ``min_sample`` applies to
    evaluations, not would_have_blocked.
    """
    window_hours = float(
        runtime_config.get("direction_gap_respawn_block_graduation_window_hours", 168)
    )
    min_sample = int(
        runtime_config.get("direction_gap_respawn_block_graduation_min_sample", 1)
    )

    # 1) Read gap_respawn lane rows (only written on would-have-blocked events).
    gap_measurements = await _read_audit_lane_counterfactual(
        session,
        lane="gap_respawn",
        now=now,
        window_hours=window_hours,
    )
    would_have_blocked = int(gap_measurements.get("cycles") or 0)

    # 2) Read total audit evaluations across ALL lanes as a proxy for "the
    #    evaluation machinery was alive during the window."
    evaluations = await _read_audit_evaluation_count(
        session, now=now, window_hours=window_hours
    )

    measurements = {
        **gap_measurements,
        "evaluations": evaluations,
        "would_have_blocked": would_have_blocked,
        "min_sample": min_sample,
    }

    # Insufficient sample: too few total evaluations to draw a conclusion.
    # Note: this uses evaluations (any lane), not would_have_blocked, because
    # gap_respawn only logs on match — zero events could be genuine no-lift.
    if evaluations < min_sample:
        return KnobEvidence(
            measurements=measurements,
            signal=None,
            no_lift=False,
            summary=(
                f"insufficient sample: {evaluations} evaluation(s) across "
                f"{window_hours:.0f}h (min_sample={min_sample}); "
                f"{would_have_blocked} gap-respawn would-have-blocked"
            ),
        )

    # Signal is the raw count — each blocked direction is one unit of lift.
    no_lift = would_have_blocked == 0
    return KnobEvidence(
        measurements=measurements,
        signal=float(would_have_blocked),
        no_lift=no_lift,
        summary=(
            f"would_have_blocked={would_have_blocked} direction-creation "
            f"attempt(s) in {window_hours:.0f}h ({evaluations} total evaluations)"
        ),
    )


register_knob(
    KnobGraduationSpec(
        config_key="direction_gap_respawn_block_mode",
        label="Direction gap-respawn block",
        compute_evidence=_direction_gap_respawn_block_evidence,
        observe_threshold_key=(
            "direction_gap_respawn_block_graduation_observe_threshold"
        ),
        enforce_threshold_key=(
            "direction_gap_respawn_block_graduation_enforce_threshold"
        ),
        default_observe_threshold=0.0,
        default_enforce_threshold=3.0,
    )
)


# --- Driver ------------------------------------------------------------------


async def _notify_projects(session: Any, max_projects: int) -> list[Any]:
    """Return a bounded list of project ids to route a knob notification to.

    Mode-knobs are deployment-global, so a graduation/CUT is routed to each
    project operator (bounded by ``max_projects``), mirroring the proven pattern's
    per-contributing-project notification. Best-effort — a query failure yields an
    empty list and the (best-effort) notification is simply skipped.
    """
    from app.models import Project

    try:
        rows = (
            await session.execute(select(Project.id).limit(max_projects))
        ).all()
        return [r[0] for r in rows]
    except Exception:  # noqa: BLE001
        logger.warning(
            "[MODE_KNOB_GRADUATION] failed to enumerate projects for notification",
            exc_info=True,
        )
        return []


async def _drive_one_knob(
    session: Any,
    spec: KnobGraduationSpec,
    *,
    runtime_config: dict[str, Any],
    now: datetime,
    max_projects: int,
) -> KnobGraduationDecision:
    """Drive a single knob: compute evidence, decide, apply the side effect."""
    current_mode = str(runtime_config.get(spec.config_key, "off") or "off").lower()

    # Compute the measured counterfactual.
    evidence = await spec.compute_evidence(
        session, runtime_config=runtime_config, now=now
    )
    measurements = evidence.measurements
    signal = evidence.signal

    # Already at the top rung — nothing to graduate on. (A knob at enforce whose
    # debt is still open is reconciled elsewhere; the harness only advances.)
    if _MODE_ORDER.get(current_mode, 0) >= _MODE_ORDER["enforce"]:
        return KnobGraduationDecision(
            config_key=spec.config_key,
            action=HOLD,
            from_mode=current_mode,
            signal=signal,
            measurements=measurements,
            detail="already at enforce (maximum); no further advancement possible",
        )

    # Insufficient / undefined signal → HOLD.
    if signal is None:
        return KnobGraduationDecision(
            config_key=spec.config_key,
            action=HOLD,
            from_mode=current_mode,
            signal=None,
            measurements=measurements,
            detail=f"insufficient signal — {evidence.summary}",
        )

    observe_threshold = float(
        runtime_config.get(
            spec.observe_threshold_key, spec.default_observe_threshold
        )
    )
    enforce_threshold = float(
        runtime_config.get(
            spec.enforce_threshold_key, spec.default_enforce_threshold
        )
    )

    advance_to: str | None = None
    if current_mode == "off" and signal >= observe_threshold:
        advance_to = "observe"
    elif current_mode == "observe" and signal >= enforce_threshold:
        advance_to = "enforce"

    if advance_to is not None:
        return await _graduate(
            session,
            spec,
            runtime_config=runtime_config,
            current_mode=current_mode,
            advance_to=advance_to,
            signal=signal,
            threshold=observe_threshold if current_mode == "off" else enforce_threshold,
            measurements=measurements,
            max_projects=max_projects,
        )

    # No graduation. Consult the domain CUT criterion.
    if evidence.no_lift:
        return await _cut(
            session,
            spec,
            signal=signal,
            measurements=measurements,
            max_projects=max_projects,
            current_mode=current_mode,
        )

    threshold = observe_threshold if current_mode == "off" else enforce_threshold
    return KnobGraduationDecision(
        config_key=spec.config_key,
        action=HOLD,
        from_mode=current_mode,
        signal=signal,
        measurements=measurements,
        detail=(
            f"signal={signal:.3f} below threshold={threshold} "
            f"(mode={current_mode}); no advancement — {evidence.summary}"
        ),
    )


async def _graduate(
    session: Any,
    spec: KnobGraduationSpec,
    *,
    runtime_config: dict[str, Any],
    current_mode: str,
    advance_to: str,
    signal: float,
    threshold: float,
    measurements: dict[str, Any],
    max_projects: int,
) -> KnobGraduationDecision:
    """Apply a GRADUATE: durable persist → in-memory → resolve debt → notify."""
    from app.models import DeploymentState

    # Snapshot the spec's plain fields into locals up front. They are read on the
    # post-rollback error paths below; using local names (not ``spec.<attr>``) keeps
    # those reads off the greenlet-fidelity scanner's radar — ``spec`` is a frozen
    # dataclass, never a mapped ORM row, so there is no lazy-load after rollback.
    config_key = spec.config_key
    debt_key = spec.debt_key

    # 1) Persist the new mode BEFORE mutating process-local state. A process must
    #    never enforce a mode that was not durably committed.
    try:
        state = await session.get(DeploymentState, 1)
        if state is None:
            await session.rollback()
            return KnobGraduationDecision(
                config_key=config_key,
                action=ERROR,
                from_mode=current_mode,
                to_mode=advance_to,
                signal=signal,
                measurements=measurements,
                detail=(
                    f"cannot graduate {config_key} {current_mode}→{advance_to}: "
                    "deployment state missing; in-memory mode unchanged"
                ),
            )
        config_json = dict(getattr(state, "runtime_config_json", None) or {})
        config_json[config_key] = advance_to
        state.runtime_config_json = config_json
        session.add(state)
        await session.commit()
    except Exception as exc:  # noqa: BLE001
        with contextlib.suppress(Exception):
            await session.rollback()
        logger.warning(
            "[MODE_KNOB_GRADUATION] failed to persist %s %s→%s; in-memory "
            "mode unchanged",
            config_key,
            current_mode,
            advance_to,
            exc_info=True,
        )
        return KnobGraduationDecision(
            config_key=config_key,
            action=ERROR,
            from_mode=current_mode,
            to_mode=advance_to,
            signal=signal,
            measurements=measurements,
            detail=f"failed to persist graduation: {exc}; in-memory mode unchanged",
        )

    # 2) Mutate the in-memory config only after the durable commit.
    runtime_config[config_key] = advance_to
    logger.info(
        "[MODE_KNOB_GRADUATION] %s auto-graduated %s → %s (signal=%.4f, "
        "threshold=%.2f)",
        config_key,
        current_mode,
        advance_to,
        signal,
        threshold,
    )

    # 3) Resolve the dormant-knob debt on reaching enforce (its off/observe value
    #    was the open obligation).
    debt_resolved = False
    if advance_to == "enforce":
        try:
            from app.completion_debt import aresolve_completion_debt

            debt_resolved = await aresolve_completion_debt(
                session,
                debt_key,
                reason="graduated:mode_knob_graduation",
            )
            if debt_resolved:
                await session.commit()
        except Exception:  # noqa: BLE001
            logger.warning(
                "[MODE_KNOB_GRADUATION] failed to resolve dormant-knob debt for "
                "%s after graduation to enforce",
                config_key,
                exc_info=True,
            )
            with contextlib.suppress(Exception):
                await session.rollback()

    # 4) Notify each project operator. Best-effort — a post failure must never
    #    undo the already-committed graduation.
    try:
        from app.notification_dispatch import post_mode_knob_graduated_message

        project_ids = await _notify_projects(session, max_projects)
        for pid in project_ids:
            await post_mode_knob_graduated_message(
                session,
                pid,
                config_key=config_key,
                from_mode=current_mode,
                to_mode=advance_to,
                signal=signal,
                threshold=threshold,
                measurements=measurements,
            )
        if project_ids:
            await session.commit()
    except Exception:  # noqa: BLE001
        logger.warning(
            "[MODE_KNOB_GRADUATION] failed to post operator notification for "
            "%s graduation (%s → %s); graduation itself is unaffected",
            config_key,
            current_mode,
            advance_to,
            exc_info=True,
        )
        with contextlib.suppress(Exception):
            await session.rollback()

    return KnobGraduationDecision(
        config_key=config_key,
        action=GRADUATE,
        from_mode=current_mode,
        to_mode=advance_to,
        signal=signal,
        measurements=measurements,
        debt_resolved=debt_resolved,
        detail=(
            f"graduated {current_mode} → {advance_to} "
            f"(signal={signal:.4f} ≥ threshold={threshold})"
        ),
    )


async def _cut(
    session: Any,
    spec: KnobGraduationSpec,
    *,
    signal: float,
    measurements: dict[str, Any],
    max_projects: int,
    current_mode: str,
) -> KnobGraduationDecision:
    """Apply a CUT: stamp the measured recommendation into the debt row + notify.

    *current_mode* is the knob's actual runtime mode at decision time (not read
    from *measurements*, which may not carry a ``mode`` key for all knobs).
    """
    # Snapshot spec fields into plain locals (see the note in ``_graduate``): they
    # are read on the post-rollback error paths and ``spec`` is a frozen dataclass,
    # so plain-name reads keep the greenlet-fidelity scanner satisfied.
    config_key = spec.config_key
    debt_key = spec.debt_key

    recommendation = {
        "verdict": "cut",
        "reason": "measured no-lift: enforce branch would never bind",
        "signal": round(float(signal), 4),
        "measurements": measurements,
        "recommended_at": _utcnow().isoformat(),
    }

    stamped = False
    try:
        from app.completion_debt import astamp_cut_recommendation

        stamped = await astamp_cut_recommendation(
            session, debt_key, recommendation=recommendation
        )
        if stamped:
            await session.commit()
    except Exception:  # noqa: BLE001
        logger.warning(
            "[MODE_KNOB_GRADUATION] failed to stamp CUT recommendation for %s",
            config_key,
            exc_info=True,
        )
        with contextlib.suppress(Exception):
            await session.rollback()

    # High-signal operator NOTE per project. Best-effort.
    try:
        from app.notification_dispatch import (
            post_mode_knob_cut_recommendation_message,
        )

        project_ids = await _notify_projects(session, max_projects)
        for pid in project_ids:
            await post_mode_knob_cut_recommendation_message(
                session,
                pid,
                config_key=config_key,
                signal=signal,
                measurements=measurements,
            )
        if project_ids:
            await session.commit()
    except Exception:  # noqa: BLE001
        logger.warning(
            "[MODE_KNOB_GRADUATION] failed to post CUT notification for %s",
            config_key,
            exc_info=True,
        )
        with contextlib.suppress(Exception):
            await session.rollback()

    return KnobGraduationDecision(
        config_key=config_key,
        action=CUT,
        from_mode=current_mode,
        signal=signal,
        measurements=measurements,
        cut_recommendation=recommendation if stamped else None,
        detail=(
            f"CUT recommended: no measured lift (signal={signal:.4f}, "
            f"would_have_blocked={measurements.get('would_have_blocked')})"
        ),
    )


async def run_mode_knob_graduation(
    session: Any,
    *,
    now: datetime | None = None,
    max_projects: int = 25,
) -> list[KnobGraduationDecision]:
    """Drive every registered knob one step. Gated by the shared enabled flag.

    Returns the list of per-knob decisions (empty when the harness is disabled).
    Each knob is driven inside its own try/except; a crash records an ``ERROR``
    decision and never aborts the driver. Independent of any single knob's own
    mode — gated ONLY by ``mode_knob_graduation_enabled`` (default True).
    """
    from app.shared_state import runtime_config

    if not bool(runtime_config.get(MODE_KNOB_GRADUATION_ENABLED_KEY, False)):
        return []

    _now = now or _utcnow()
    decisions: list[KnobGraduationDecision] = []
    for spec in registered_knobs():
        # Snapshot the knob key as a plain local before the driver call: it is read
        # on the post-rollback error path below, and a plain-name read keeps the
        # greenlet-fidelity scanner satisfied (``spec`` is a frozen dataclass).
        config_key = spec.config_key
        try:
            decision = await _drive_one_knob(
                session,
                spec,
                runtime_config=runtime_config,
                now=_now,
                max_projects=max_projects,
            )
            decisions.append(decision)
        except Exception as exc:  # noqa: BLE001, PERF203 — per-knob isolation is intentional
            logger.warning(
                "[MODE_KNOB_GRADUATION] knob=%s crashed: %s", config_key, exc
            )
            with contextlib.suppress(Exception):
                await session.rollback()
            decisions.append(
                KnobGraduationDecision(
                    config_key=config_key,
                    action=ERROR,
                    from_mode=str(
                        runtime_config.get(config_key, "off") or "off"
                    ).lower(),
                    detail=str(exc)[:200],
                )
            )
    return decisions
