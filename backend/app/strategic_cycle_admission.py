"""Project-owned admission policy for automatic Strategic cycles.

``Project.autonomous_enabled`` authorizes the broader autonomous research loop,
which contains two distinct authoring surfaces: tactical direction research and
periodic/forced Strategic cycles.  Operators sometimes need to canary the first
surface without releasing the second.  Keep that distinction in one leaf module
so the scheduler and its debug projections cannot disagree about the
``build_config`` default or malformed-value behavior.

Explicit/manual Strategic launches and the conversational Resident do not
consult this policy.  Every automatic Strategic producer does, including the
architecture-to-strategy continuation and qualification-triggered refresh.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Project
from app.task_locking import (
    AUTOMATIC_TASK_CREATION_ALLOWED,
    AUTOMATIC_TASK_CREATION_PROJECT_UNAVAILABLE,
    automatic_task_creation_decision,
    project_lock,
)
from app.time_utils import _coerce_naive_utc, _utcnow, parse_retry_after


logger = logging.getLogger(__name__)

AUTOMATIC_STRATEGIC_CYCLES_ENABLED_BUILD_CONFIG_KEY = (
    "automatic_strategic_cycles_enabled"
)
AUTOMATIC_STRATEGIC_CYCLES_DISABLED = "automatic_strategic_cycles_disabled"

#: Sub-keys of the richer mapping form of the build_config value.  The bare
#: boolean remains valid and means exactly what it always meant.
STRATEGIC_LANE_ENABLED_KEY = "enabled"
STRATEGIC_LANE_UNTIL_KEY = "until"
STRATEGIC_LANE_REASON_KEY = "reason"
STRATEGIC_LANE_DISABLED_BY_KEY = "disabled_by"
STRATEGIC_LANE_DISABLED_AT_KEY = "disabled_at"

#: How often one project may re-emit the "lane suppressed" notice.  The
#: autonomous loop makes ~290 passes/day per project; the notice must be loud
#: on the first suppressed pass and then throttled, never silent.
STRATEGIC_LANE_SUPPRESSION_NOTICE_INTERVAL_SECONDS = 3600.0

#: Trigger labels for :func:`note_automatic_strategic_cycles_suppressed`.
SUPPRESSION_TRIGGER_LOOP_PASS = "autonomous_loop_pass"
SUPPRESSION_TRIGGER_CADENCE_DUE = "strategic_cadence_due"


@dataclass(frozen=True)
class StrategicLaneAdmission:
    """Decoded state of ``build_config.automatic_strategic_cycles_enabled``.

    ``enabled`` is the only thing the scheduler needs.  Every other field
    exists so the suppressed path can say *why* the lane is off, which is the
    whole point: a bare stored ``False`` carries no bound, no expiry, no
    reason and no actor, so a wedged project and a deliberately quiet one used
    to produce byte-identical operator signal.
    """

    enabled: bool
    configured: bool = False
    explicitly_disabled: bool = False
    legacy_bare_boolean: bool = False
    expired: bool = False
    until: datetime | None = None
    until_raw: str | None = None
    malformed_until: bool = False
    reason: str | None = None
    disabled_by: str | None = None
    disabled_at_raw: str | None = None

    @property
    def bounded(self) -> bool:
        """True when the disable carries a parseable expiry."""
        return self.until is not None

    def describe(self) -> str:
        """One-line, operator-readable summary of the disable."""
        parts = [
            f"reason={self.reason!r}" if self.reason else "reason=<none recorded>",
            (
                f"disabled_by={self.disabled_by!r}"
                if self.disabled_by
                else "disabled_by=<unknown>"
            ),
        ]
        if self.until is not None:
            parts.append(f"expires={self.until.isoformat()}Z")
        elif self.malformed_until:
            parts.append(f"expires=<unparseable until={self.until_raw!r}>")
        else:
            parts.append("expires=never (no `until` bound)")
        if self.legacy_bare_boolean:
            parts.append("form=legacy-bare-boolean")
        if self.disabled_at_raw:
            parts.append(f"disabled_at={self.disabled_at_raw}")
        return "; ".join(parts)

    def as_projection(self) -> dict[str, Any]:
        """Bounded, JSON-safe projection for operator read surfaces."""
        return {
            "enabled": self.enabled,
            "configured": self.configured,
            "explicitly_disabled": self.explicitly_disabled,
            "legacy_bare_boolean": self.legacy_bare_boolean,
            "bounded": self.bounded,
            "expired": self.expired,
            "until": self.until.isoformat() if self.until is not None else None,
            "malformed_until": self.malformed_until,
            "reason": self.reason,
            "disabled_by": self.disabled_by,
            "disabled_at": self.disabled_at_raw,
            "summary": self.describe(),
        }


def _coerce_text(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    stripped = value.strip()
    return stripped or None


def describe_automatic_strategic_cycles(
    project: Any, *, now: datetime | None = None
) -> StrategicLaneAdmission:
    """Decode the stored lane value into an explicit, self-describing record.

    Accepted stored forms:

    * key absent -> enabled (the historical compatibility default);
    * ``True`` / ``False`` -> the legacy bare boolean, unbounded;
    * ``{"enabled": false, "until": <iso8601>, "reason": ..., "disabled_by": ...}``
      -> a bounded disable that lapses on its own once ``until`` passes.

    Malformed *values* keep failing open to enabled, exactly as before.  A
    mapping that says ``enabled: false`` is unambiguous, so a malformed or
    absent ``until`` inside it leaves the lane disabled (unbounded) rather than
    silently re-enabling an operator's explicit choice -- and both the
    build-config lint and the suppression notice name that unbounded state, so
    it can never be silent.
    """

    build_config = getattr(project, "build_config", None)
    if not isinstance(build_config, Mapping):
        return StrategicLaneAdmission(enabled=True)
    if AUTOMATIC_STRATEGIC_CYCLES_ENABLED_BUILD_CONFIG_KEY not in build_config:
        return StrategicLaneAdmission(enabled=True)

    configured = build_config[AUTOMATIC_STRATEGIC_CYCLES_ENABLED_BUILD_CONFIG_KEY]

    if isinstance(configured, bool):
        return StrategicLaneAdmission(
            enabled=configured,
            configured=True,
            explicitly_disabled=not configured,
            legacy_bare_boolean=True,
        )

    if not isinstance(configured, Mapping):
        # Unknown scalar shape (string, int, list, ...): historical fail-open.
        return StrategicLaneAdmission(enabled=True, configured=True)

    raw_enabled = configured.get(STRATEGIC_LANE_ENABLED_KEY)
    if not isinstance(raw_enabled, bool):
        # A mapping that never states ``enabled`` is malformed; fail open.
        return StrategicLaneAdmission(enabled=True, configured=True)
    if raw_enabled:
        return StrategicLaneAdmission(enabled=True, configured=True)

    reason = _coerce_text(configured.get(STRATEGIC_LANE_REASON_KEY))
    disabled_by = _coerce_text(configured.get(STRATEGIC_LANE_DISABLED_BY_KEY))
    disabled_at_raw = _coerce_text(configured.get(STRATEGIC_LANE_DISABLED_AT_KEY))
    raw_until = configured.get(STRATEGIC_LANE_UNTIL_KEY)
    until_raw = _coerce_text(raw_until)

    if raw_until is None or (isinstance(raw_until, str) and until_raw is None):
        return StrategicLaneAdmission(
            enabled=False,
            configured=True,
            explicitly_disabled=True,
            reason=reason,
            disabled_by=disabled_by,
            disabled_at_raw=disabled_at_raw,
        )

    until = parse_retry_after(until_raw) if until_raw is not None else None
    if until is None:
        return StrategicLaneAdmission(
            enabled=False,
            configured=True,
            explicitly_disabled=True,
            malformed_until=True,
            until_raw=until_raw if until_raw is not None else str(raw_until),
            reason=reason,
            disabled_by=disabled_by,
            disabled_at_raw=disabled_at_raw,
        )

    reference = _coerce_naive_utc(now) if now is not None else _utcnow()
    lapsed = until <= reference
    return StrategicLaneAdmission(
        enabled=lapsed,
        configured=True,
        explicitly_disabled=True,
        expired=lapsed,
        until=until,
        until_raw=until_raw,
        reason=reason,
        disabled_by=disabled_by,
        disabled_at_raw=disabled_at_raw,
    )


def automatic_strategic_cycles_enabled(project: Any) -> bool:
    """Return whether the autonomous loop may originate Strategic cycles.

    Existing projects have no stored key, so the compatibility default is
    enabled.  A bare stored ``False`` still means "disabled" forever; only the
    richer mapping form can lapse on its own once its ``until`` passes.
    Malformed raw JSON fails back to the historical enabled behavior instead of
    silently turning off Strategic work.
    """

    return describe_automatic_strategic_cycles(project).enabled


@dataclass
class _SuppressionNoticeSlot:
    last_emitted_at: float
    suppressed_since_last: int = 0


_suppression_notice_state: dict[str, _SuppressionNoticeSlot] = {}


def _monotonic() -> float:
    """Indirection so tests can advance the notice throttle deterministically."""
    return time.monotonic()


def reset_strategic_lane_suppression_notices() -> None:
    """Clear the per-process notice throttle (testing/worker-restart hook)."""
    _suppression_notice_state.clear()


def note_automatic_strategic_cycles_suppressed(
    project: Any,
    *,
    trigger: str = SUPPRESSION_TRIGGER_LOOP_PASS,
    admission: StrategicLaneAdmission | None = None,
    interval_seconds: float | None = None,
) -> bool:
    """Emit the deduped "Strategic lane suppressed" reason line.

    The defect this closes: the scheduler short-circuited on a falsy flag with
    no log, no counter and no marker, so a project whose entire Strategic
    authoring engine was off looked byte-identical to a healthy one on every
    liveness surface.  Surface the *reason*, not the state -- name the flag,
    the stored ``reason``, and the ``until`` bound (or its absence).

    Loud on the first suppressed pass per (project, trigger), then throttled to
    at most once per ``interval_seconds``; the throttled passes are counted and
    reported in the next line so the volume stays visible without the spam.

    Returns True when a line was emitted.
    """

    record = admission or describe_automatic_strategic_cycles(project)
    project_id = str(getattr(project, "id", None) or "<unknown>")
    key = f"{project_id}:{trigger}"
    window = (
        float(interval_seconds)
        if interval_seconds is not None
        else STRATEGIC_LANE_SUPPRESSION_NOTICE_INTERVAL_SECONDS
    )
    now = _monotonic()
    slot = _suppression_notice_state.get(key)
    if slot is not None and (now - slot.last_emitted_at) < window:
        slot.suppressed_since_last += 1
        return False

    throttled = slot.suppressed_since_last if slot is not None else 0
    _suppression_notice_state[key] = _SuppressionNoticeSlot(last_emitted_at=now)
    logger.warning(
        "[STRATEGIC] Automatic Strategic cycles SUPPRESSED for project %s (%s) - "
        "build_config.%s is disabled; trigger=%s; %s; "
        "throttled_passes_since_last_notice=%d. Tactical research keeps "
        "spawning, so every liveness surface still looks healthy; no Strategic "
        "cycle will be authored until this flag is re-enabled or its `until` "
        "bound lapses.",
        project_id,
        getattr(project, "name", None) or "<unnamed>",
        AUTOMATIC_STRATEGIC_CYCLES_ENABLED_BUILD_CONFIG_KEY,
        trigger,
        record.describe(),
        throttled,
    )
    return True


async def acquire_automatic_strategic_cycle_creation_authority(
    session: AsyncSession,
    project_id: Any,
    *,
    skip_locked: bool = True,
    timeout: float = 30.0,
) -> str:
    """Linearize an automatic Strategic INSERT against its full authority.

    Repository/config detection and operator PATCHes can change the Strategic
    lane while an automatic producer spends seconds assembling a payload.  Read
    the *current* Project authority columns only after acquiring the canonical
    ``FOR NO KEY UPDATE`` lock, then retain that transaction-scoped lock through
    the caller's Task INSERT and commit.  This gives a concurrent lane-disable
    and spawn one deterministic order:

    * disable wins (or still owns the row) -> the automatic spawn fails closed;
    * spawn wins -> its Task commits before the disable can become visible.

    Manual/directions and Resident callers deliberately never invoke this
    helper.  ``autonomous_enabled`` is also deliberately absent: it controls
    tactical scheduling, while this dedicated flag controls automatic Strategic
    authorship and pause/archive remain the universal task fences.
    """

    async with project_lock(
        session,
        project_id,
        skip_locked=skip_locked,
        timeout=timeout,
        load_columns=[
            Project.id,
            Project.paused,
            Project.archived,
            Project.build_config,
        ],
    ) as locked_project:
        decision = automatic_task_creation_decision(locked_project)
        if decision != AUTOMATIC_TASK_CREATION_ALLOWED:
            return decision
        if not automatic_strategic_cycles_enabled(locked_project):
            return AUTOMATIC_STRATEGIC_CYCLES_DISABLED
        return AUTOMATIC_TASK_CREATION_ALLOWED

    # The context manager always returns from inside the block.  Keep an
    # explicit fail-closed fallback for static analyzers and future refactors.
    return AUTOMATIC_TASK_CREATION_PROJECT_UNAVAILABLE
