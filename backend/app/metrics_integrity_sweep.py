"""Standing metrics-integrity sweep — a recurring, leader-only reaper sweep that
recomputes selected *self-reported* counters from ground truth and records any
discrepancy as a flag, **without ever mutating the source counters it audits**.

Motivation (parent direction: systematic audit sweep for silent data-integrity
bugs + a standing mechanism that keeps re-checking the system's own numbers
against reality). Several self-reported counters in this codebase are written by
one path and read by another with no post-hoc verification; when the writer
drifts from ground truth the number silently lies. Pure invariant checkers exist
for some of these surfaces but nothing runs them on a schedule against live data.
This module is that standing executor.

Hard invariants (mirrors ``structural_health_audit.py``):

* **Flag-only.** The sweep NEVER writes to the counter it audits. ``observe``
  records to the in-process observability ring only; ``enforce`` additionally
  enrolls a DEDUPLICATED finding into the existing evergreen backlog ledger
  (``EvergreenBacklogItem``) so a drift gets the same triage as a correctness
  finding — but the audited source column is untouched in every mode.
* **No schema change.** Findings reuse ``EvergreenBacklogItem`` (deduped on
  ``(project_id, finding_key)``); recent results live in a bounded in-memory
  singleton — no table, no migration.
* **Bounded.** Every check runs against a BOUNDED result set (``sample_limit``
  rows, ``max_projects`` projects). A check that cannot cheaply recompute ground
  truth within those bounds returns an explicit ``skipped`` / ``inconclusive``
  result instead of scanning unbounded data.
* **Isolated.** Each check runs inside its own try/except; a crashing check
  records an ``error`` result and never aborts the tick or the reaper cycle.
* **Registry.** New checks register via :func:`register_check`; the sweep runs
  the registered checks in order, capped at ``max_checks_per_tick``.

Config knobs (runtime_config, operator-settable via PATCH /api/admin/settings):

* ``metrics_integrity_sweep_mode``            — ``off`` / ``observe`` / ``enforce``
* ``metrics_integrity_sweep_interval_minutes``— cadence gate (default 60)
* ``metrics_integrity_sweep_max_checks``      — checks run per tick (default 25)
* ``metrics_integrity_sweep_max_projects``    — projects scanned per check (25)
* ``metrics_integrity_sweep_sample_limit``    — rows sampled per DB check (200)
"""

from __future__ import annotations

import contextlib
import logging
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum
from typing import Any, Awaitable, Callable, Deque, Dict, List

from sqlmodel import or_, select

from app.time_utils import _utcnow

logger = logging.getLogger(__name__)

# --- Closed vocabularies -----------------------------------------------------

#: Sweep modes (runtime config ``metrics_integrity_sweep_mode``). Mirrors
#: ``structural_health_audit.AUDIT_MODES`` / ``evergreen_backlog.FILL_MODES``.
SWEEP_MODES = frozenset({"off", "observe", "enforce"})

#: Provenance stamped on every metrics-integrity-enrolled ledger row, distinct
#: from other enrollment provenances so drift findings are attributable.
METRICS_INTEGRITY_PROVENANCE = "metrics_integrity"

# --- Defaults (operator-overridable via runtime_config / PATCH settings) ------

DEFAULT_MODE = "observe"
DEFAULT_INTERVAL_MINUTES = 60
DEFAULT_MAX_CHECKS_PER_TICK = 25
DEFAULT_MAX_PROJECTS_PER_CHECK = 25
DEFAULT_SAMPLE_LIMIT = 200
#: How far back a windowed check looks when recomputing ground truth. Keeps the
#: recompute bounded in time as well as in row count.
DEFAULT_WINDOW_DAYS = 14


class CheckStatus(str, Enum):
    """Terminal status of a single metrics-integrity check result."""

    OK = "ok"  # recomputed ground truth agrees with the self-reported counter
    FLAGGED = "flagged"  # a real drift: self-reported diverged from ground truth
    SKIPPED = "skipped"  # no data / not applicable (nothing to audit)
    INCONCLUSIVE = "inconclusive"  # ground truth not cheaply recomputable in bounds
    ERROR = "error"  # the check itself raised


@dataclass(frozen=True)
class CheckResult:
    """One recompute outcome. Carries the compared numbers, never a mutation."""

    check: str
    status: CheckStatus
    self_reported: int | None = None
    ground_truth: int | None = None
    delta: int | None = None
    severity: str = "info"  # info | warn | error
    subject: str | None = None  # e.g. project_id / direction_id / "global"
    detail: str = ""

    @property
    def flagged(self) -> bool:
        return self.status == CheckStatus.FLAGGED

    def as_dict(self) -> Dict[str, Any]:
        return {
            "check": self.check,
            "status": self.status.value,
            "self_reported": self.self_reported,
            "ground_truth": self.ground_truth,
            "delta": self.delta,
            "severity": self.severity,
            "subject": self.subject,
            "detail": self.detail,
        }

    def finding_key(self) -> str:
        """Stable dedup key for evergreen enrollment (per check + subject)."""
        return f"metrics-integrity:{self.check}:{self.subject or 'global'}"


@dataclass
class SweepContext:
    """Read-only-intent execution context handed to every check."""

    session: Any
    now: datetime
    mode: str
    max_projects: int
    sample_limit: int
    window_days: int


CheckFn = Callable[[SweepContext], Awaitable[List[CheckResult]]]


@dataclass(frozen=True)
class MetricsCheck:
    name: str
    description: str
    run: CheckFn


# --- Registry ----------------------------------------------------------------

_REGISTRY: Dict[str, MetricsCheck] = {}


def register_check(name: str, description: str) -> Callable[[CheckFn], CheckFn]:
    """Decorator registering a check function under a stable ``name``.

    Adding a check is a two-line change: decorate an ``async def`` taking a
    :class:`SweepContext` and returning a list of :class:`CheckResult`. Duplicate
    names overwrite (last registration wins) so a test can re-register safely.
    """

    def _wrap(fn: CheckFn) -> CheckFn:
        _REGISTRY[name] = MetricsCheck(name=name, description=description, run=fn)
        return fn

    return _wrap


def registered_checks() -> List[MetricsCheck]:
    """Registered checks in insertion order."""
    return list(_REGISTRY.values())


# --- Bounded in-memory observability -----------------------------------------

_RECENT_TICKS_MAX = 20
_RECENT_FLAGS_MAX = 50


@dataclass
class _MetricsIntegrityObservability:
    """Process-local, bounded record of recent ticks + flags for operators/tests."""

    ticks: int = 0
    checks_run: int = 0
    flags_total: int = 0
    errors_total: int = 0
    enrolled_total: int = 0
    last_tick_at: str | None = None
    last_tick: Dict[str, Any] | None = None
    recent_ticks: Deque[Dict[str, Any]] = field(
        default_factory=lambda: deque(maxlen=_RECENT_TICKS_MAX)
    )
    recent_flags: Deque[Dict[str, Any]] = field(
        default_factory=lambda: deque(maxlen=_RECENT_FLAGS_MAX)
    )

    def record_tick(
        self,
        *,
        now: datetime,
        mode: str,
        results: List[CheckResult],
        enrolled: int,
    ) -> Dict[str, Any]:
        flagged = [r for r in results if r.flagged]
        errored = [r for r in results if r.status == CheckStatus.ERROR]
        self.ticks += 1
        self.checks_run += len(results)
        self.flags_total += len(flagged)
        self.errors_total += len(errored)
        self.enrolled_total += enrolled
        tick = {
            "at": now.isoformat(),
            "mode": mode,
            "results": len(results),
            "flagged": len(flagged),
            "errors": len(errored),
            "enrolled": enrolled,
            "flags": [r.as_dict() for r in flagged],
        }
        self.last_tick_at = tick["at"]
        self.last_tick = tick
        self.recent_ticks.append(tick)
        for r in flagged:
            entry = r.as_dict()
            entry["at"] = tick["at"]
            self.recent_flags.append(entry)
        return tick

    def snapshot(self) -> Dict[str, Any]:
        return {
            "ticks": self.ticks,
            "checks_run": self.checks_run,
            "flags_total": self.flags_total,
            "errors_total": self.errors_total,
            "enrolled_total": self.enrolled_total,
            "last_tick_at": self.last_tick_at,
            "last_tick": dict(self.last_tick) if self.last_tick else None,
            "recent_ticks": list(self.recent_ticks),
            "recent_flags": list(self.recent_flags),
        }


_OBSERVABILITY = _MetricsIntegrityObservability()


def get_metrics_integrity_observability() -> Dict[str, Any]:
    """Bounded in-memory recent-results snapshot for operators / tests."""
    return _OBSERVABILITY.snapshot()


def _reset_observability_for_tests() -> None:
    """Test helper — clear the process-local counters and cadence gate."""
    global _OBSERVABILITY, _last_sweep_scan
    _OBSERVABILITY = _MetricsIntegrityObservability()
    _last_sweep_scan = None


# --- Config resolution -------------------------------------------------------


def _resolve_config() -> Dict[str, Any]:
    """Resolve the runtime-config-driven sweep knobs (mode / cadence / bounds)."""
    from app.shared_state import runtime_config as _runtime_config

    mode = str(
        _runtime_config.get("metrics_integrity_sweep_mode", DEFAULT_MODE)
    ).lower()
    if mode not in SWEEP_MODES:
        mode = DEFAULT_MODE

    def _pos_int(key: str, default: int) -> int:
        try:
            val = int(_runtime_config.get(key, default))
        except (TypeError, ValueError):
            return default
        return val if val > 0 else default

    return {
        "mode": mode,
        "interval_minutes": _pos_int(
            "metrics_integrity_sweep_interval_minutes", DEFAULT_INTERVAL_MINUTES
        ),
        "max_checks_per_tick": _pos_int(
            "metrics_integrity_sweep_max_checks", DEFAULT_MAX_CHECKS_PER_TICK
        ),
        "max_projects": _pos_int(
            "metrics_integrity_sweep_max_projects", DEFAULT_MAX_PROJECTS_PER_CHECK
        ),
        "sample_limit": _pos_int(
            "metrics_integrity_sweep_sample_limit", DEFAULT_SAMPLE_LIMIT
        ),
        "window_days": _pos_int(
            "metrics_integrity_sweep_window_days", DEFAULT_WINDOW_DAYS
        ),
    }


# --- Enforce-mode enrollment (deduped; never touches the audited counter) ----


async def _enroll_flag_finding(session, result: CheckResult) -> bool:
    """Enroll a deduped evergreen finding for a flagged drift (enforce mode only).

    Returns True when a NEW row was created, False when a live row already tracks
    this finding_key (re-detection → enroll nothing) or the subject is not a
    project id. Reuses the existing evergreen seam so a drift flows through the
    same triage as a correctness finding. This writes to the ledger, NOT to the
    audited source counter — the flag-only guarantee is preserved.
    """
    import uuid as _uuid

    from app.evergreen_backlog import enroll_backlog_item
    from app.models import EvergreenBacklogItem

    subject = result.subject
    if not subject or subject == "global":
        # Deployment-global drift (no owning project) — recorded to observability
        # only. The evergreen ledger is per-project; enrolling a null project row
        # would break its triage invariants.
        return False
    try:
        project_id = _uuid.UUID(str(subject))
    except (TypeError, ValueError):
        return False

    finding_key = result.finding_key()
    existing = (
        await session.execute(
            select(EvergreenBacklogItem.status).where(
                EvergreenBacklogItem.project_id == project_id,
                EvergreenBacklogItem.finding_key == finding_key,
            )
        )
    ).scalar_one_or_none()
    if existing is not None and existing != "retired":
        return False

    title = f"Metrics-integrity drift: {result.check}"
    detail = (
        f"{result.detail} (self_reported={result.self_reported}, "
        f"ground_truth={result.ground_truth}, delta={result.delta}). "
        "A self-reported counter diverged from an independent recompute of "
        "ground truth. Investigate the writer and, if the drift is real, "
        "correct the counter at its source (this audit never mutates it)."
    )
    await enroll_backlog_item(
        session,
        project_id=project_id,
        finding_key=finding_key,
        title=title,
        detail=detail,
        provenance=METRICS_INTEGRITY_PROVENANCE,
    )
    return True


# --- Leader-only recurring sweep (called from the reaper cycle) --------------

# Module-level cadence gate, exactly like structural_health_audit._last_audit_scan.
_last_sweep_scan: datetime | None = None


async def run_metrics_integrity_sweep(
    session, now: datetime | None = None
) -> Dict[str, Any] | None:
    """Leader-only recurring metrics-integrity sweep (mode + cadence gated).

    Called from the reaper cycle (already leader-only) inside its
    try/except+rollback idiom, so it can never crash the cycle. Returns the tick
    summary dict when it ran, or ``None`` when gated out (mode ``off`` or the
    cadence interval has not elapsed).

    Runs each registered check under isolated error handling, records the
    results to the bounded observability ring, and — in ``enforce`` mode only —
    enrolls a deduped evergreen finding per flagged drift. It NEVER writes to the
    counter it audits in any mode.
    """
    global _last_sweep_scan
    cfg = _resolve_config()
    if cfg["mode"] == "off":
        return None

    _now = now or _utcnow()
    if _last_sweep_scan is not None and (_now - _last_sweep_scan) < timedelta(
        minutes=cfg["interval_minutes"]
    ):
        return None
    _last_sweep_scan = _now

    ctx = SweepContext(
        session=session,
        now=_now,
        mode=cfg["mode"],
        max_projects=cfg["max_projects"],
        sample_limit=cfg["sample_limit"],
        window_days=cfg["window_days"],
    )

    checks = registered_checks()[: cfg["max_checks_per_tick"]]
    results: List[CheckResult] = []
    for check in checks:
        try:
            produced = await check.run(ctx)
            if produced:
                results.extend(produced)
        except Exception as exc:  # noqa: BLE001, PERF203 — per-check isolation is intentional
            logger.warning(
                "[METRICS_INTEGRITY] check=%s crashed: %s", check.name, exc
            )
            results.append(
                CheckResult(
                    check=check.name,
                    status=CheckStatus.ERROR,
                    severity="error",
                    detail=str(exc)[:200],
                )
            )
            # A failed check may have left the session mid-statement; roll back so
            # the next check starts clean. Never raises.
            with contextlib.suppress(Exception):
                await session.rollback()

    enrolled = 0
    if cfg["mode"] == "enforce":
        for r in results:
            if not r.flagged:
                continue
            try:
                if await _enroll_flag_finding(session, r):
                    enrolled += 1
            except Exception as exc:  # noqa: BLE001 — enrollment must not abort the tick
                logger.warning(
                    "[METRICS_INTEGRITY] enroll failed for %s: %s",
                    r.finding_key(),
                    exc,
                )
                with contextlib.suppress(Exception):
                    await session.rollback()
        if enrolled:
            with contextlib.suppress(Exception):
                await session.commit()

    tick = _OBSERVABILITY.record_tick(
        now=_now, mode=cfg["mode"], results=results, enrolled=enrolled
    )
    if tick["flagged"]:
        logger.info(
            "[METRICS_INTEGRITY] tick (mode=%s) flagged %d drift(s), enrolled %d: %s",
            cfg["mode"],
            tick["flagged"],
            enrolled,
            ", ".join(
                f"{f['check']}[{f['subject']}] {f['self_reported']}!={f['ground_truth']}"
                for f in tick["flags"]
            ),
        )
    return tick


# =============================================================================
# Initial checks
# =============================================================================


@register_check(
    "landed_vs_reported_merges",
    "Recompute merged-PR counts with the canonical landed-signal predicate and "
    "flag ghost merges (PR artifact present but no integration landing).",
)
async def check_landed_vs_reported_merges(ctx: SweepContext) -> List[CheckResult]:
    """Ground truth: a COMPLETED task with a PR artifact counts as merged ONLY
    when it carries the canonical integration landing signal (``has_landed_signal``).

    The legacy/naive predicate ("has a PULL_REQUEST artifact") over-counts:
    stranded rollups and merge-equivalent unblocks have a PR artifact but never
    landed. The delta between the two is the ghost-merge count — a self-reported
    merge number that silently exceeds ground truth. Bounded to the most recent
    ``sample_limit`` PR-artifact tasks; groups the flag per project.
    """
    from app.landed_signal import has_landed_signal
    from app.models import Artifact, ArtifactType, Task, TaskStatus

    # Bounded: newest PR-artifact task ids first, capped at the sample limit.
    pr_task_id_rows = (
        await ctx.session.execute(
            select(Artifact.task_id)
            .where(Artifact.artifact_type == ArtifactType.PULL_REQUEST)
            .order_by(Artifact.id.desc())
            .limit(ctx.sample_limit)
        )
    ).all()
    pr_task_ids = [row[0] for row in pr_task_id_rows]
    if not pr_task_ids:
        return [
            CheckResult(
                check="landed_vs_reported_merges",
                status=CheckStatus.SKIPPED,
                subject="global",
                detail="no PR-artifact tasks in sample window",
            )
        ]

    tasks = (
        await ctx.session.execute(
            select(Task).where(
                Task.id.in_(pr_task_ids),
                Task.status == TaskStatus.COMPLETED,
            )
        )
    ).scalars().all()

    # reported = completed tasks with a PR artifact (the naive merge signal);
    # ground truth = subset that actually landed on integration. Per project.
    per_project_reported: Dict[str, int] = {}
    per_project_landed: Dict[str, int] = {}
    for t in tasks:
        pid = str(t.project_id)
        per_project_reported[pid] = per_project_reported.get(pid, 0) + 1
        if has_landed_signal(t):
            per_project_landed[pid] = per_project_landed.get(pid, 0) + 1

    results: List[CheckResult] = []
    for pid, reported in per_project_reported.items():
        landed = per_project_landed.get(pid, 0)
        ghosts = reported - landed
        if ghosts > 0:
            results.append(
                CheckResult(
                    check="landed_vs_reported_merges",
                    status=CheckStatus.FLAGGED,
                    self_reported=reported,
                    ground_truth=landed,
                    delta=ghosts,
                    severity="warn",
                    subject=pid,
                    detail=(
                        f"{ghosts} completed PR-artifact task(s) lack the canonical "
                        "integration landing signal (ghost merges): the naive "
                        "merge count over-reports landed work"
                    ),
                )
            )
        else:
            results.append(
                CheckResult(
                    check="landed_vs_reported_merges",
                    status=CheckStatus.OK,
                    self_reported=reported,
                    ground_truth=landed,
                    delta=0,
                    subject=pid,
                    detail="reported merges match canonical landed count",
                )
            )
    return results


@register_check(
    "playbook_confirmation_integrity",
    "Recompute playbook confirmation counters against the reference/followed "
    "ceilings and flag entries confirmed more often than they were ever shown.",
)
async def check_playbook_confirmation_integrity(
    ctx: SweepContext,
) -> List[CheckResult]:
    """Ground truth: a playbook entry cannot be *confirmed* more times than it
    was ever referenced/shown. ``confirmed_count`` is credited only when the
    entry was actually shown to the succeeding task (shown-key gate), and every
    show increments ``reference_count`` — so ``confirmed_count <= reference_count``
    must hold. A negative counter is structurally impossible corruption.

    Pure per-entry invariants over the already-loaded ``design_doc`` — no extra
    query. Bounded to ``max_projects`` projects.
    """
    from app.models import Project

    projects = (
        await ctx.session.execute(
            select(Project.id, Project.design_doc).limit(ctx.max_projects)
        )
    ).all()
    if not projects:
        return [
            CheckResult(
                check="playbook_confirmation_integrity",
                status=CheckStatus.SKIPPED,
                subject="global",
                detail="no projects to audit",
            )
        ]

    results: List[CheckResult] = []
    for project_id, design_doc in projects:
        pid = str(project_id)
        doc = design_doc or {}
        entries = {
            k: v
            for k, v in doc.items()
            if isinstance(k, str) and k.startswith("playbook/") and isinstance(v, dict)
        }
        if not entries:
            results.append(
                CheckResult(
                    check="playbook_confirmation_integrity",
                    status=CheckStatus.SKIPPED,
                    subject=pid,
                    detail="no playbook entries",
                )
            )
            continue

        total_confirmed = 0
        total_reference = 0
        over_credit_keys: List[str] = []
        negative_keys: List[str] = []
        for key, entry in entries.items():
            confirmed = int(entry.get("confirmed_count", 0) or 0)
            reference = int(entry.get("reference_count", 0) or 0)
            followed = int(entry.get("followed_count", 0) or 0)
            total_confirmed += confirmed
            total_reference += reference
            if confirmed < 0 or reference < 0 or followed < 0:
                negative_keys.append(key)
            # confirmed can never exceed the number of times the entry was shown.
            elif confirmed > reference:
                over_credit_keys.append(key)

        if negative_keys or over_credit_keys:
            bad = len(negative_keys) + len(over_credit_keys)
            note_parts = []
            if over_credit_keys:
                note_parts.append(
                    f"{len(over_credit_keys)} entry(ies) confirmed more than referenced "
                    f"(e.g. {over_credit_keys[0]})"
                )
            if negative_keys:
                note_parts.append(
                    f"{len(negative_keys)} entry(ies) with a negative counter "
                    f"(e.g. {negative_keys[0]})"
                )
            results.append(
                CheckResult(
                    check="playbook_confirmation_integrity",
                    status=CheckStatus.FLAGGED,
                    self_reported=total_confirmed,
                    ground_truth=total_reference,
                    delta=bad,
                    severity="warn",
                    subject=pid,
                    detail="; ".join(note_parts),
                )
            )
        else:
            results.append(
                CheckResult(
                    check="playbook_confirmation_integrity",
                    status=CheckStatus.OK,
                    self_reported=total_confirmed,
                    ground_truth=total_reference,
                    delta=0,
                    subject=pid,
                    detail=f"{len(entries)} playbook entry(ies) consistent",
                )
            )
    return results


@register_check(
    "direction_acceptance_metric_invariant",
    "Scan research_history entries across directions and flag any that violate the "
    "semantic invariants between acceptance-evidence metrics (proven ≤ declared ≤ "
    "merged-write, proven ≤ verified ≤ prs_merged, coverage identity).",
)
async def check_direction_acceptance_metric_invariant(
    ctx: SweepContext,
) -> List[CheckResult]:
    """Ground truth: the semantic ordering invariants in
    :func:`app.direction_outcomes.acceptance_metric_invariant_violations` must hold
    for every research_history entry. A violation means a metric counter was written
    inconsistently — proven exceeding declared, verified exceeding raw merged, etc.
    Flag-only: the check never mutates the entry it audits.

    Bounded to ``max_projects`` projects; skips directions without research_history
    and entries without any acceptance-evidence fields (legacy rows).
    """
    from app.direction_outcomes import acceptance_metric_invariant_violations
    from app.models import Project

    projects = (
        await ctx.session.execute(
            select(Project.id, Project.directions).limit(ctx.max_projects)
        )
    ).all()
    if not projects:
        return [
            CheckResult(
                check="direction_acceptance_metric_invariant",
                status=CheckStatus.SKIPPED,
                subject="global",
                detail="no projects to audit",
            )
        ]

    results: List[CheckResult] = []
    any_checked = False
    for project_id, directions in projects:
        pid = str(project_id)
        for direction in directions or []:
            history = direction.get("research_history") or []
            for entry in history:
                # Skip legacy entries with no acceptance fields — they were written
                # before the acceptance-evidence layer existed and the guard
                # function itself handles them safely, but emitting OK for every
                # legacy entry inflates noise without value.
                has_acceptance_fields = any(
                    k in entry
                    for k in (
                        "acceptance_proven_merged",
                        "acceptance_declared_merged",
                        "acceptance_merged_write_total",
                        "verified_prs_merged",
                    )
                )
                if not has_acceptance_fields:
                    continue
                any_checked = True
                violations = acceptance_metric_invariant_violations(entry)
                if violations:
                    direction_id = direction.get("id", "unknown")
                    cycle_id = entry.get("task_id", "unknown")
                    results.append(
                        CheckResult(
                            check="direction_acceptance_metric_invariant",
                            status=CheckStatus.FLAGGED,
                            self_reported=int(entry.get("acceptance_proven_merged") or 0),
                            ground_truth=0,
                            delta=len(violations),
                            severity="warn",
                            subject=pid,
                            detail=(
                                f"direction {direction_id} cycle {cycle_id}: "
                                + "; ".join(violations[:3])
                            ),
                        )
                    )

    if not any_checked:
        return [
            CheckResult(
                check="direction_acceptance_metric_invariant",
                status=CheckStatus.SKIPPED,
                subject="global",
                detail="no research_history entries with acceptance fields found",
            )
        ]

    # Only emit OK results per-project when no flagged results exist for that project.
    flagged_pids = {r.subject for r in results if r.flagged}
    seen_ok: set[str] = set()
    for project_id, directions in projects:
        pid = str(project_id)
        if pid in flagged_pids or pid in seen_ok:
            continue
        # Check if this project had any entries audited.
        for direction in directions or []:
            history = direction.get("research_history") or []
            for entry in history:
                if any(
                    k in entry
                    for k in (
                        "acceptance_proven_merged",
                        "acceptance_declared_merged",
                        "acceptance_merged_write_total",
                        "verified_prs_merged",
                    )
                ):
                    seen_ok.add(pid)
                    results.append(
                        CheckResult(
                            check="direction_acceptance_metric_invariant",
                            status=CheckStatus.OK,
                            delta=0,
                            subject=pid,
                            detail="all audited acceptance-metric entries consistent",
                        )
                    )
                    break
            if pid in seen_ok:
                break

    return results


@register_check(
    "noncode_outcome_window_accounting",
    "Recompute non-code terminal-outcome window membership by terminal time vs "
    "the naive created-at method and flag mis-windowed tasks.",
)
async def check_noncode_outcome_window_accounting(
    ctx: SweepContext,
) -> List[CheckResult]:
    """Ground truth: a non-code task belongs to a reporting window by its
    TERMINAL (completion) time, not ``created_at`` (#12864). The naive created-at
    method mis-classifies tasks created before but completed inside the window
    (and vice-versa). The delta between the two memberships is the count of
    silently mis-windowed outcomes.

    Bounded: only terminal, non-review-chain tasks touched in the last
    ``window_days`` (by created_at OR terminal time) are loaded, capped at
    ``sample_limit``. If the load is truncated by the cap the ground truth cannot
    be computed exactly, so the check returns ``inconclusive`` rather than
    reporting a partial (and therefore untrustworthy) delta.
    """
    from app.routers.admin_analytics import _derive_completed_at
    from app.models import Task, TaskStatus

    window_start = ctx.now.replace(tzinfo=None) - timedelta(days=ctx.window_days)
    # A task can qualify by created_at OR terminal time; load a bounded superset
    # covering both, then classify in Python.
    load_cutoff = window_start - timedelta(days=ctx.window_days)
    terminal = (TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.CANCELLED)
    tasks = (
        await ctx.session.execute(
            select(Task)
            .where(
                Task.status.in_(terminal),
                # Load a bounded superset by created_at or by a terminal-time
                # column source that can still land inside the window. Rows
                # with completed_at missing are kept so _derive_completed_at can
                # fall back to the terminal status_log timestamp.
                or_(
                    Task.created_at >= load_cutoff,
                    Task.completed_at >= window_start,
                    Task.completed_at.is_(None),
                ),
            )
            .order_by(Task.created_at.desc())
            .limit(ctx.sample_limit + 1)
        )
    ).scalars().all()

    if len(tasks) > ctx.sample_limit:
        return [
            CheckResult(
                check="noncode_outcome_window_accounting",
                status=CheckStatus.INCONCLUSIVE,
                subject="global",
                detail=(
                    f"terminal-task volume exceeds sample_limit={ctx.sample_limit}; "
                    "cannot recompute exact window membership within bounds"
                ),
            )
        ]

    def _created_naive(t: Task) -> datetime | None:
        if t.created_at is None:
            return None
        return t.created_at.replace(tzinfo=None) if t.created_at.tzinfo else t.created_at

    naive_in_window = 0
    terminal_in_window = 0
    mismatches = 0
    considered = 0
    for t in tasks:
        # Non-code == not part of a PR review chain (mirrors _compute_non_code_outcomes).
        if isinstance((t.context or {}).get("review_loop"), dict):
            continue
        considered += 1
        created = _created_naive(t)
        completed = _derive_completed_at(t)
        in_naive = created is not None and created >= window_start
        in_terminal = completed >= window_start
        naive_in_window += 1 if in_naive else 0
        terminal_in_window += 1 if in_terminal else 0
        if in_naive != in_terminal:
            mismatches += 1

    if considered == 0:
        return [
            CheckResult(
                check="noncode_outcome_window_accounting",
                status=CheckStatus.SKIPPED,
                subject="global",
                detail="no non-code terminal tasks in load window",
            )
        ]

    if mismatches > 0:
        return [
            CheckResult(
                check="noncode_outcome_window_accounting",
                status=CheckStatus.FLAGGED,
                self_reported=naive_in_window,
                ground_truth=terminal_in_window,
                delta=mismatches,
                severity="warn",
                subject="global",
                detail=(
                    f"{mismatches} non-code terminal task(s) fall in a different "
                    "reporting window under created-at vs terminal-time accounting"
                ),
            )
        ]
    return [
        CheckResult(
            check="noncode_outcome_window_accounting",
            status=CheckStatus.OK,
            self_reported=naive_in_window,
            ground_truth=terminal_in_window,
            delta=0,
            subject="global",
            detail=f"{considered} non-code terminal task(s) window-consistent",
        )
    ]
