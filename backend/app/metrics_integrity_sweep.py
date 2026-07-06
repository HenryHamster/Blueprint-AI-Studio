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

Registered checks: ``landed_vs_reported_merges``, ``playbook_confirmation_integrity``,
``direction_acceptance_metric_invariant``, ``noncode_outcome_window_accounting``,
``completion_debt_completeness`` (recomputes the currently-expected
completion-debt obligations — seed/dormant-knob inventory, per-project
continue/trial capability verdicts, per-project no-CI gate fall-opens — and flags
ledger under-counts, without ever mutating a ``CompletionDebtEntry`` row),
``rollup_child_landing_credit``, ``near_cap_size_warning`` (scans capped
backend modules for files at >=95% of their size cap, pre-classified with the
chronic-offender policy, so decomposition can be staged before a hard breach),
and ``problem_model_duplicate_id_sweep`` (scans id-keyed problem_model JSONB
collections for duplicate ids, flagging per-project when
``len(items) > len({item["id"] for item in items})``).

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
import os
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum
from typing import Any, Awaitable, Callable, Deque, Dict, List, cast

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
        self.last_tick_at = str(tick["at"])
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


# Near-cap warning constants — patchable in tests.
_NEAR_CAP_RATIO: float = 0.95
_NEAR_CAP_LIMITS_PATH: str = os.path.normpath(
    os.path.join(os.path.dirname(__file__), "..", "testing", "module_size_limits.json")
)
_NEAR_CAP_BACKEND_ROOT: str = os.path.dirname(__file__)  # backend/app/


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
    "criterion_merge_evidence_gap",
    "Recompute landed, proven task coverage against active version acceptance "
    "criteria and flag tasks that recorded no task_merge evidence.",
)
async def check_criterion_merge_evidence_gap(
    ctx: SweepContext,
) -> List[CheckResult]:
    """Ground truth: a COMPLETED task that truly landed, has a proven code-
    acceptance verdict, and maps to active criteria should also have a
    recorded ``task_merge`` evidence row for those criteria.

    The check is bounded in two dimensions:
      * at most ``max_projects`` projects are scanned;
      * within each project, at most ``sample_limit`` completed tasks are
        considered.

    A task is counted in ``self_reported`` when it satisfies all preconditions
    but only in ``ground_truth`` when at least one mapped active criterion has
    a proven ``task_merge`` evidence row for that task. The delta is the count
    of silent recording failures in the sampled window.
    """

    from app.acceptance_criteria import (
        get_criteria_for_version,
        get_evidence_for_criterion,
    )
    from app.acceptance_gate import CODE_ACCEPTANCE_VERDICT_KEY, acceptance_is_proven
    from app.landed_signal import has_landed_signal
    from app.models import Project, Task, TaskStatus
    from app.release_version import current_open_version

    projects = (
        await ctx.session.execute(select(Project.id).limit(ctx.max_projects))
    ).all()
    if not projects:
        return [
            CheckResult(
                check="criterion_merge_evidence_gap",
                status=CheckStatus.SKIPPED,
                subject="global",
                detail="no projects to audit",
            )
        ]

    results: List[CheckResult] = []
    any_reported = False
    for (project_id,) in projects:
        pid = str(project_id)
        open_version = await current_open_version(ctx.session, project_id)
        if open_version is None:
            results.append(
                CheckResult(
                    check="criterion_merge_evidence_gap",
                    status=CheckStatus.SKIPPED,
                    subject=pid,
                    detail="no open version",
                )
            )
            continue

        criteria = await get_criteria_for_version(ctx.session, open_version.id)
        active_criteria = [c for c in criteria if c.status == "active"]
        if not active_criteria:
            results.append(
                CheckResult(
                    check="criterion_merge_evidence_gap",
                    status=CheckStatus.SKIPPED,
                    subject=pid,
                    detail="no active criteria on open version",
                )
            )
            continue

        active_keys = {c.criterion_key for c in active_criteria}
        task_ids_with_task_merge_evidence: set[str] = set()
        for criterion in active_criteria:
            evidence = await get_evidence_for_criterion(ctx.session, criterion.id)
            for row in evidence:
                if (
                    row.source_kind == "task_merge"
                    and row.source_id
                    and isinstance(row.detail, dict)
                    and row.detail.get("proven") is True
                ):
                    task_ids_with_task_merge_evidence.add(str(row.source_id))

        tasks = (
            await ctx.session.execute(
                select(Task)
                .where(
                    Task.project_id == project_id,
                    Task.status == TaskStatus.COMPLETED,
                )
                .order_by(Task.created_at.desc())
                .limit(ctx.sample_limit)
            )
        ).scalars().all()

        reported = 0
        ground_truth = 0
        missing_task_ids: List[str] = []
        for task in tasks:
            if not has_landed_signal(task):
                continue
            verdict = (task.context or {}).get(CODE_ACCEPTANCE_VERDICT_KEY)
            if not acceptance_is_proven(verdict):
                continue
            task_criterion_ids = [
                criterion_key
                for criterion_key in (task.criterion_ids or [])
                if criterion_key in active_keys
            ]
            if not task_criterion_ids:
                continue

            reported += 1
            task_id = str(task.id)
            if task_id in task_ids_with_task_merge_evidence:
                ground_truth += 1
            else:
                missing_task_ids.append(task_id)

        if reported == 0:
            results.append(
                CheckResult(
                    check="criterion_merge_evidence_gap",
                    status=CheckStatus.SKIPPED,
                    subject=pid,
                    detail="no landed, proven tasks mapped to active criteria in sample",
                )
            )
            continue

        any_reported = True
        if ground_truth < reported:
            gap = reported - ground_truth
            results.append(
                CheckResult(
                    check="criterion_merge_evidence_gap",
                    status=CheckStatus.FLAGGED,
                    self_reported=reported,
                    ground_truth=ground_truth,
                    delta=gap,
                    severity="warn",
                    subject=pid,
                    detail=(
                        f"{gap} landed, proven task(s) mapped to active criteria "
                        "recorded no task_merge evidence"
                        + (
                            f" (e.g. task {missing_task_ids[0]})"
                            if missing_task_ids
                            else ""
                        )
                    ),
                )
            )
        else:
            results.append(
                CheckResult(
                    check="criterion_merge_evidence_gap",
                    status=CheckStatus.OK,
                    self_reported=reported,
                    ground_truth=ground_truth,
                    delta=0,
                    subject=pid,
                    detail="all sampled landed, proven tasks recorded task_merge evidence",
                )
            )

    if not any_reported and not results:
        return [
            CheckResult(
                check="criterion_merge_evidence_gap",
                status=CheckStatus.SKIPPED,
                subject="global",
                detail="no qualifying tasks in sample window",
            )
        ]
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
        over_followed_keys: List[str] = []
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
            # confirmed can never exceed the number of times the entry was followed.
            elif followed > 0 and confirmed > followed:
                over_followed_keys.append(key)

        if negative_keys or over_credit_keys or over_followed_keys:
            bad = len(negative_keys) + len(over_credit_keys) + len(over_followed_keys)
            note_parts = []
            if over_credit_keys:
                note_parts.append(
                    f"{len(over_credit_keys)} entry(ies) confirmed more than referenced "
                    f"(e.g. {over_credit_keys[0]})"
                )
            if over_followed_keys:
                note_parts.append(
                    f"{len(over_followed_keys)} entry(ies) confirmed more than followed "
                    f"(e.g. {over_followed_keys[0]})"
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
        created_at = t.created_at
        if created_at is None:
            return None
        if created_at.tzinfo is not None:
            return cast(datetime, created_at.replace(tzinfo=None))
        return cast(datetime, created_at)

    # Per-project counters: pid -> {naive, terminal, mismatches}
    per_project: Dict[str, Dict[str, int]] = {}
    considered = 0
    for t in tasks:
        # Non-code == not part of a PR review chain (mirrors _compute_non_code_outcomes).
        if isinstance((t.context or {}).get("review_loop"), dict):
            continue
        considered += 1
        pid = str(t.project_id)
        if pid not in per_project:
            per_project[pid] = {"naive": 0, "terminal": 0, "mismatches": 0}
        created = _created_naive(t)
        completed = _derive_completed_at(t)
        in_naive = created is not None and created >= window_start
        in_terminal = completed >= window_start
        per_project[pid]["naive"] += 1 if in_naive else 0
        per_project[pid]["terminal"] += 1 if in_terminal else 0
        if in_naive != in_terminal:
            per_project[pid]["mismatches"] += 1

    if considered == 0:
        return [
            CheckResult(
                check="noncode_outcome_window_accounting",
                status=CheckStatus.SKIPPED,
                subject="global",
                detail="no non-code terminal tasks in load window",
            )
        ]

    results: List[CheckResult] = []
    any_mismatch = False
    for pid, counts in per_project.items():
        if counts["mismatches"] > 0:
            any_mismatch = True
            results.append(
                CheckResult(
                    check="noncode_outcome_window_accounting",
                    status=CheckStatus.FLAGGED,
                    self_reported=counts["naive"],
                    ground_truth=counts["terminal"],
                    delta=counts["mismatches"],
                    severity="warn",
                    subject=pid,
                    detail=(
                        f"{counts['mismatches']} non-code terminal task(s) fall in a "
                        "different reporting window under created-at vs terminal-time "
                        "accounting"
                    ),
                )
            )
    if not any_mismatch:
        results.append(
            CheckResult(
                check="noncode_outcome_window_accounting",
                status=CheckStatus.OK,
                self_reported=considered,
                ground_truth=considered,
                delta=0,
                subject="global",
                detail=f"{considered} non-code terminal task(s) window-consistent",
            )
        )
    return results


@register_check(
    "completion_debt_completeness",
    "Independently recompute the currently-expected completion-debt obligations "
    "(global seed/dormant-knob inventory, per-project continue/trial capability "
    "verdicts, per-project no-CI gate fall-opens) and flag ledger under-counts "
    "where an expected-active obligation has no open row.",
)
async def check_completion_debt_completeness(
    ctx: SweepContext,
) -> List[CheckResult]:
    """Ground truth: every obligation the recorders SHOULD have opened must carry
    an OPEN completion-debt row. The recorders are best-effort / non-fatal, so a
    silently-failed recorder under-counts debt forever unless an independent check
    recomputes the expected set. This recomputes the set of currently-expected
    ACTIVE obligations from the SAME canonical dedupe keys the recorder paths mint,
    then flags any expected obligation whose ledger row is absent or non-open.

    A DISMISSED row is an explicit operator disposition and is treated as
    satisfied (never as missing debt); an absent row — or one in any non-open,
    non-dismissed state (e.g. ``resolved`` while the obligation is live again) — is
    an under-count and is flagged.

    Flag-only: performs ZERO writes to ``CompletionDebtEntry`` in any mode — the
    only side effect (enforce mode) is the shared deduped ``EvergreenBacklogItem``
    enrollment path used by every other flagged check. Bounded by
    ``ctx.max_projects`` (projects scanned) and ``ctx.sample_limit`` (expected
    keys); when either bound is exceeded it returns ``inconclusive`` rather than
    claiming a partial ``ok``.
    """
    from app.capability_trial_verdict import _CONTINUE_VERDICTS
    from app.completion_debt import (
        STATUS_DISMISSED,
        STATUS_OPEN,
        _iter_seed_debts,
    )
    from app.models import CompletionDebtEntry, Project

    # (subject, dedupe_key) for every currently-expected ACTIVE obligation.
    expected: List[tuple[str, str]] = []

    # 1) Deployment-global seed inventory + dormant runtime knobs. These reuse the
    #    exact kwargs bundles the startup seed upserts, so their dedupe_key IS the
    #    canonical one. Best-effort enumeration — a failure must not abort the check.
    try:
        for item in _iter_seed_debts():
            key = item.get("dedupe_key")
            if key:
                expected.append(("global", str(key)))
    except Exception:  # noqa: BLE001 — seed enumeration is best-effort
        logger.warning(
            "[METRICS_INTEGRITY] completion_debt_completeness: seed enumeration failed",
            exc_info=True,
        )

    # 2) Per-project obligations. Bounded to max_projects; load one extra so a
    #    project count OVER the bound can be reported as inconclusive rather than
    #    silently truncating (which would let an unscanned project's missing debt
    #    masquerade as fully audited).
    project_rows = (
        await ctx.session.execute(
            select(
                Project.id,
                Project.capability_adoption_verdicts,
                Project.merge_gate_runtime,
            ).limit(ctx.max_projects + 1)
        )
    ).all()
    if len(project_rows) > ctx.max_projects:
        return [
            CheckResult(
                check="completion_debt_completeness",
                status=CheckStatus.INCONCLUSIVE,
                subject="global",
                detail=(
                    f"project volume exceeds max_projects={ctx.max_projects}; "
                    "cannot recompute expected per-project obligations within bounds"
                ),
            )
        ]

    for project_id, verdicts, merge_gate_runtime in project_rows:
        pid = str(project_id)
        # Capability-trial obligation: a capability still in continue/trial (no
        # decided enforce verdict) owes a standing debt row. Canonical key mirrors
        # the recorder in
        # capability_trial_verdict.persist_trial_verdicts_in_transaction:
        #     f"capability_trial:{project_id}:{cap_name}"
        if isinstance(verdicts, dict):
            for cap_name, entry in verdicts.items():
                verdict = entry.get("verdict") if isinstance(entry, dict) else None
                if verdict in _CONTINUE_VERDICTS:
                    expected.append((pid, f"capability_trial:{pid}:{cap_name}"))
        # No-CI gate obligation: a live fall-open to an UNVERIFIED merge
        # (``merge_gate_runtime.last_unverified_reason`` set) owes a debt row.
        # Canonical key mirrors the recorder in
        # ci_resolution.record_no_ci_gate_runtime:  f"no_ci_gate:{project_id}"
        if isinstance(merge_gate_runtime, dict) and merge_gate_runtime.get(
            "last_unverified_reason"
        ):
            expected.append((pid, f"no_ci_gate:{pid}"))

    if not expected:
        return [
            CheckResult(
                check="completion_debt_completeness",
                status=CheckStatus.SKIPPED,
                subject="global",
                detail="no currently-expected completion-debt obligations to audit",
            )
        ]

    all_keys = sorted({key for _subject, key in expected})
    if len(all_keys) > ctx.sample_limit:
        return [
            CheckResult(
                check="completion_debt_completeness",
                status=CheckStatus.INCONCLUSIVE,
                subject="global",
                detail=(
                    f"expected-obligation volume ({len(all_keys)}) exceeds "
                    f"sample_limit={ctx.sample_limit}; cannot recompute within bounds"
                ),
            )
        ]

    # Single bounded query: dedupe_key -> status for every expected obligation.
    status_rows = (
        await ctx.session.execute(
            select(
                CompletionDebtEntry.dedupe_key, CompletionDebtEntry.status
            ).where(CompletionDebtEntry.dedupe_key.in_(all_keys))
        )
    ).all()
    status_by_key = {row[0]: row[1] for row in status_rows}

    # Group expected obligations by subject and classify. A subject with any
    # missing / non-open (and non-dismissed) obligation is FLAGGED; a subject whose
    # obligations are all satisfied (open, or operator-dismissed) is OK. A subject
    # whose every obligation was operator-dismissed produces NO result — there is
    # nothing owed and nothing drifting.
    per_subject: Dict[str, set] = {}
    for subject, key in expected:
        per_subject.setdefault(subject, set()).add(key)

    results: List[CheckResult] = []
    for subject in sorted(per_subject):
        open_count = 0
        missing: List[str] = []
        for key in sorted(per_subject[subject]):
            status = status_by_key.get(key)
            if status == STATUS_DISMISSED:
                # Operator explicitly disposed of this obligation — not missing debt.
                continue
            if status == STATUS_OPEN:
                open_count += 1
                continue
            # Absent, resolved, or any other non-open/non-dismissed state while the
            # obligation is currently expected active == an under-count.
            missing.append(key)

        expected_active = open_count + len(missing)
        if missing:
            preview = ", ".join(missing[:3])
            if len(missing) > 3:
                preview += f" (+{len(missing) - 3} more)"
            results.append(
                CheckResult(
                    check="completion_debt_completeness",
                    status=CheckStatus.FLAGGED,
                    self_reported=open_count,
                    ground_truth=expected_active,
                    delta=len(missing),
                    severity="warn",
                    subject=subject,
                    detail=(
                        f"{len(missing)} currently-expected completion-debt "
                        f"obligation(s) have no open ledger row (under-count): "
                        f"{preview}"
                    ),
                )
            )
        elif expected_active > 0:
            results.append(
                CheckResult(
                    check="completion_debt_completeness",
                    status=CheckStatus.OK,
                    self_reported=open_count,
                    ground_truth=expected_active,
                    delta=0,
                    subject=subject,
                    detail=(
                        f"{open_count} expected obligation(s) each have an open "
                        "ledger row"
                    ),
                )
            )
    return results


@register_check(
    "rollup_child_landing_credit",
    "Recompute integration-landing credit for rollup constituent children and flag "
    "children whose rollup parent landed but whose own landed_on_integration stamp "
    "is absent (under-credit).",
)
async def check_rollup_child_landing_credit(ctx: SweepContext) -> List[CheckResult]:
    """Ground truth: if a rollup parent has the canonical integration-landing signal
    (``has_landed_signal``), every constituent child that carries a merge-unblock
    signal (``has_merge_unblock_signal`` / ``pr_merged=True``) and is NOT
    force-dropped (lacks ``NON_MERGE_UNBLOCK_REASON_KEY``) SHOULD also carry
    ``has_landed_signal``.  A child that is missing the landing stamp is an
    under-credited unit: the direction reads ``prs_merged=0`` / all-zero
    ``merge_credit`` despite the rollup having provably landed on the integration
    branch.

    Rollup parent identification mirrors ``rollup_state._is_rollup_parent_awaiting_merge``:
    a task whose context carries ``_is_rollup_review=True`` or has
    ``rollup_pr_number`` set (and whose own ``landed_on_integration`` is not False)
    is treated as a rollup parent candidate.

    Constituent child identification: context carries ``rollup_pr_number`` (the
    staging PR that was merged into the rollup), the task is NOT itself a rollup
    parent by the criteria above, and it has ``has_merge_unblock_signal``.

    Bounded to the most-recent ``sample_limit`` COMPLETED tasks; groups per project.
    Flag-only in every mode — never writes to the tasks it audits.
    """
    from app.landed_signal import (
        NON_MERGE_UNBLOCK_REASON_KEY,
        has_landed_signal,
        has_merge_unblock_signal,
    )
    from app.models import Task, TaskStatus

    tasks = (
        await ctx.session.execute(
            select(Task)
            .where(Task.status == TaskStatus.COMPLETED)
            .order_by(Task.id.desc())
            .limit(ctx.sample_limit)
        )
    ).scalars().all()

    if not tasks:
        return [
            CheckResult(
                check="rollup_child_landing_credit",
                status=CheckStatus.SKIPPED,
                subject="global",
                detail="no completed tasks in sample window",
            )
        ]

    def _is_rollup_parent(task: Task) -> bool:
        # Among completed tasks the reliable parent signal is _is_rollup_review=True.
        # Children also carry rollup_pr_number (linking them to the parent) but do
        # NOT carry _is_rollup_review, so using rollup_pr_number alone would
        # mis-classify children as parents.
        return (task.context or {}).get("_is_rollup_review") is True

    def _is_rollup_child(task: Task) -> bool:
        ctx_map = task.context or {}
        return bool(ctx_map.get("rollup_pr_number")) and not _is_rollup_parent(task)

    # Collect landed rollup parents: set of rollup_pr_number values (or task ids)
    # that carry the integration landing signal, keyed by project_id.
    # We identify parents by their context["rollup_pr_number"] when acting as parent
    # (i.e. the PR number of the rollup itself), or by task.id as a fallback.
    # Children reference the same rollup_pr_number to link to their parent.
    # Strategy: group parents by (project_id, rollup_pr_number); a parent is "landed"
    # if has_landed_signal(parent) is True.

    # First pass: classify every task.
    parents: Dict[str, List[Task]] = {}  # key: str(project_id)
    children: Dict[str, List[Task]] = {}

    for task in tasks:
        pid = str(task.project_id)
        if _is_rollup_parent(task):
            parents.setdefault(pid, []).append(task)
        elif _is_rollup_child(task):
            children.setdefault(pid, []).append(task)

    has_any_rollup_data = bool(parents or children)
    if not has_any_rollup_data:
        return [
            CheckResult(
                check="rollup_child_landing_credit",
                status=CheckStatus.SKIPPED,
                subject="global",
                detail="no rollup parents or children in sample window",
            )
        ]

    results: List[CheckResult] = []
    all_pids = set(parents.keys()) | set(children.keys())

    for pid in sorted(all_pids):
        project_parents = parents.get(pid, [])
        project_children = children.get(pid, [])

        if not project_parents or not project_children:
            # No children to audit OR no parents to anchor credit to.
            continue

        # Build the set of rollup_pr_number values for LANDED parents in this project.
        landed_rollup_pr_numbers: set = set()
        for parent in project_parents:
            ctx_map = parent.context or {}
            pr_num = ctx_map.get("rollup_pr_number")
            if pr_num and has_landed_signal(parent):
                landed_rollup_pr_numbers.add(pr_num)

        if not landed_rollup_pr_numbers:
            # No landed rollup parents in this project's sample — nothing to flag.
            continue

        # Count children whose rollup parent landed but who lack the landed signal.
        under_credited = 0
        for child in project_children:
            ctx_map = child.context or {}
            # Must have a merge-unblock signal (pr_merged=True).
            if not has_merge_unblock_signal(child):
                continue
            # Must NOT be force-dropped.
            if NON_MERGE_UNBLOCK_REASON_KEY in ctx_map:
                continue
            # Must link to a landed rollup parent.
            child_rollup_pr = ctx_map.get("rollup_pr_number")
            if child_rollup_pr not in landed_rollup_pr_numbers:
                continue
            # The child should have the landed signal but doesn't.
            if not has_landed_signal(child):
                under_credited += 1

        if under_credited > 0:
            results.append(
                CheckResult(
                    check="rollup_child_landing_credit",
                    status=CheckStatus.FLAGGED,
                    self_reported=0,
                    ground_truth=under_credited,
                    delta=under_credited,
                    severity="warn",
                    subject=pid,
                    detail=(
                        f"{under_credited} rollup constituent child(ren) whose rollup "
                        "parent landed on the integration branch but whose own "
                        "landed_on_integration stamp is absent (under-credited merge credit)"
                    ),
                )
            )
        else:
            results.append(
                CheckResult(
                    check="rollup_child_landing_credit",
                    status=CheckStatus.OK,
                    self_reported=0,
                    ground_truth=0,
                    delta=0,
                    subject=pid,
                    detail="all rollup children of landed parents carry the landing signal",
                )
            )

    if not results:
        return [
            CheckResult(
                check="rollup_child_landing_credit",
                status=CheckStatus.SKIPPED,
                subject="global",
                detail="no rollup children to audit against landed parents in sample",
            )
        ]
    return results


@register_check(
    "near_cap_size_warning",
    "Scan capped backend modules for files at >=95% of their size cap and flag "
    "them pre-classified with the chronic-offender policy, so decomposition can "
    "be staged before a hard breach.",
)
async def check_near_cap_size_warning(ctx: SweepContext) -> List[CheckResult]:
    """Ground truth: disk-resident line count of each capped backend module vs
    its cap in ``module_size_limits.json``.  Modules at >= ``_NEAR_CAP_RATIO`` of
    their cap (but not yet breached) are flagged; chronic offenders escalate to
    ``severity=error``.  No DB interaction — reads disk only.

    Returns SKIPPED when the limits JSON is absent/unreadable OR when the
    backend source tree is not present in the current environment (e.g., a test
    VM that only has partial tree layout).  Already-breached files (count > cap)
    are skipped — the hard-breach guardrail already catches those.
    """
    import json as _json

    from app.size_limit_policy import _is_chronic_offender, parse_bump_history

    # 1. Load caps from the tracked JSON.
    try:
        with open(_NEAR_CAP_LIMITS_PATH) as fh:
            all_limits = _json.load(fh)
    except (OSError, ValueError):
        return [
            CheckResult(
                check="near_cap_size_warning",
                status=CheckStatus.SKIPPED,
                subject="global",
                detail="module_size_limits.json absent or unreadable",
            )
        ]

    backend_caps = all_limits.get("backend")
    if not isinstance(backend_caps, dict) or not backend_caps:
        return [
            CheckResult(
                check="near_cap_size_warning",
                status=CheckStatus.SKIPPED,
                subject="global",
                detail="no backend section in module_size_limits.json",
            )
        ]

    # 2. Verify the backend source tree exists.
    if not os.path.isdir(_NEAR_CAP_BACKEND_ROOT):
        return [
            CheckResult(
                check="near_cap_size_warning",
                status=CheckStatus.SKIPPED,
                subject="global",
                detail="backend source tree absent in current environment",
            )
        ]

    # 3. Line-count each capped file and classify.
    results: List[CheckResult] = []
    ok_count = 0

    for rel, cap in backend_caps.items():
        if not isinstance(cap, int) or cap <= 0:
            continue
        path = os.path.join(_NEAR_CAP_BACKEND_ROOT, rel)
        if not os.path.isfile(path):
            continue
        try:
            with open(path, encoding="utf-8", errors="replace") as fh:
                count = len(fh.readlines())
        except OSError:
            continue

        if count > cap:
            # Already breached — the hard-breach guardrail owns these.
            continue

        ratio = count / cap
        if ratio < _NEAR_CAP_RATIO:
            ok_count += 1
            continue

        # Near-cap: apply chronic classification.
        try:
            history = parse_bump_history(rel, "backend")
            is_chronic = _is_chronic_offender(history)
        except Exception:  # noqa: BLE001 — classification is advisory; never propagate
            is_chronic = False

        severity = "error" if is_chronic else "warn"
        pct = int(ratio * 100)
        results.append(
            CheckResult(
                check="near_cap_size_warning",
                status=CheckStatus.FLAGGED,
                self_reported=count,
                ground_truth=cap,
                delta=cap - count,
                severity=severity,
                subject=rel,
                detail=(
                    f"{rel}: {count}/{cap} lines ({pct}% of cap)"
                    + (" [chronic offender]" if is_chronic else "")
                ),
            )
        )

    if not results and ok_count == 0:
        return [
            CheckResult(
                check="near_cap_size_warning",
                status=CheckStatus.SKIPPED,
                subject="global",
                detail="no countable capped backend files found",
            )
        ]

    if not results:
        return [
            CheckResult(
                check="near_cap_size_warning",
                status=CheckStatus.OK,
                subject="global",
                detail=(
                    f"all {ok_count} capped backend module(s) are below "
                    f"{int(_NEAR_CAP_RATIO * 100)}% of cap"
                ),
            )
        ]

    return results


@register_check(
    "problem_model_duplicate_id_sweep",
    "Scan id-keyed problem_model JSONB collections for duplicate ids and flag "
    "projects where any collection contains repeated ids (self_reported=raw_len, "
    "ground_truth=distinct_id_count, delta=duplicates).",
)
async def check_problem_model_duplicate_id_sweep(
    ctx: SweepContext,
) -> List[CheckResult]:
    """Ground truth: every id-keyed list in a project's problem_model must contain
    unique ids. Duplicates silently inflate list lengths without adding distinct
    items, and indicate a write path that appended without dedup (e.g.
    ``pending_user_confirmation`` append on replay, ``gaps`` re-open without
    dedup). The check is purely structural — it does NOT parse or validate any
    business rules, only checks ``item["id"]`` uniqueness within each collection.

    Collections audited: ``pending_user_confirmation``, ``gaps``,
    ``archived_gaps``, and ``friction`` (all carry a top-level ``"id"`` field).

    Flag-only: never mutates the audited collections. Bounded to
    ``max_projects`` projects.
    """
    from app.models import Project

    projects = (
        await ctx.session.execute(
            select(Project.id, Project.problem_model).limit(ctx.max_projects)
        )
    ).all()
    if not projects:
        return [
            CheckResult(
                check="problem_model_duplicate_id_sweep",
                status=CheckStatus.SKIPPED,
                subject="global",
                detail="no projects to audit",
            )
        ]

    # Collections within problem_model that carry a top-level "id" on each item.
    _ID_KEYED_COLLECTIONS = (
        "pending_user_confirmation",
        "gaps",
        "archived_gaps",
        "friction",
    )

    results: List[CheckResult] = []
    for project_id, raw_pm in projects:
        pid = str(project_id)
        pm = raw_pm if isinstance(raw_pm, dict) else {}
        if not pm:
            results.append(
                CheckResult(
                    check="problem_model_duplicate_id_sweep",
                    status=CheckStatus.SKIPPED,
                    subject=pid,
                    detail="problem_model absent or empty",
                )
            )
            continue

        total_raw = 0
        total_distinct = 0
        dup_collections: List[str] = []
        for coll_name in _ID_KEYED_COLLECTIONS:
            items = pm.get(coll_name)
            if not isinstance(items, list) or not items:
                continue
            ids = [item.get("id") for item in items if isinstance(item, dict)]
            raw_len = len(ids)
            distinct_len = len({i for i in ids if i is not None})
            total_raw += raw_len
            total_distinct += distinct_len
            if distinct_len < raw_len:
                dup_collections.append(coll_name)

        if total_raw == 0:
            results.append(
                CheckResult(
                    check="problem_model_duplicate_id_sweep",
                    status=CheckStatus.SKIPPED,
                    subject=pid,
                    detail="no id-keyed collections with items found",
                )
            )
            continue

        duplicates = total_raw - total_distinct
        if duplicates > 0:
            results.append(
                CheckResult(
                    check="problem_model_duplicate_id_sweep",
                    status=CheckStatus.FLAGGED,
                    self_reported=total_raw,
                    ground_truth=total_distinct,
                    delta=duplicates,
                    severity="warn",
                    subject=pid,
                    detail=(
                        f"{duplicates} duplicate id(s) across "
                        f"{len(dup_collections)} collection(s): "
                        + ", ".join(dup_collections)
                    ),
                )
            )
        else:
            results.append(
                CheckResult(
                    check="problem_model_duplicate_id_sweep",
                    status=CheckStatus.OK,
                    self_reported=total_raw,
                    ground_truth=total_distinct,
                    delta=0,
                    subject=pid,
                    detail="all id-keyed collections have unique ids",
                )
            )
    return results
