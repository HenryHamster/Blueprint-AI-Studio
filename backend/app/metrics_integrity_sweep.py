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
``problem_model_duplicate_id_sweep`` (scans id-keyed problem_model JSONB
collections for duplicate ids, flagging per-project when
``len(items) > len({item["id"] for item in items})``),
``direction_cumulative_merge_invariant`` (combines research_history +
history_aggregate lifetime totals and flags structural violations such as
verified > merged or proven > declared), ``first_attempt_rate_denominator_coverage``
(checks that a sufficient fraction of terminal create/refactor tasks carry
``_first_attempt_succeeded`` so the FA-rate denominator is representative), and
``sr_catch_rate_cache_freshness`` (checks that each project's persisted
``sr_catch_rate_trends`` cache is within the staleness window so operators see
up-to-date catch-rate trends), ``evidence_claim_reconciliation``
(re-verifies structured gap ``evidence_refs`` against HEAD via the tri-state
path helper and flags gaps whose path-backed proven evidence was definitely
removed — contradicted — or whose evidence is absent while the gap claims
closure; an indeterminate (None) check NEVER flags), and
``capability_claim_reconciliation`` (re-verifies each working/partial capability's
``backing_paths`` against HEAD via the same tri-state helper and flags capabilities
whose EVERY declared backing path was definitely removed; indeterminate (None)
and planned capabilities are NEVER flagged), and
``merged_pr_count_consistency`` (recomputes merged-PR counts with the canonical
rollup-aware definition — excluding rollup-parent task ids — and flags any project
where the naive count (used by llm_cost_per_merged_pr and merge_rate totals)
exceeds the canonical count; the delta is the rollup double-count), and
``criterion_evidence_count_integrity`` (independently counts CriterionEvidence
rows per criterion with a direct COUNT(*) and flags any criterion whose
self-reported evidence_count from get_version_coverage_summary diverges from
the actual row count; the delta is the silent miscount), and
``criterion_claim_reconciliation`` (grades each active acceptance criterion on
the shared EvidenceStatus axis via criterion_evidence_status and flags those the
open version must prove but whose evidence does not back coverage — the same
tri-state claim_contradicts_evidence predicate the gap/capability reconciliation
checks use, making acceptance criteria a full peer in the shared contract), and
``capability_proof_gate_gap`` (scans each project's problem_model capabilities and
flags per-project the count of ``working`` capabilities whose ``proof_status`` is
not ``proven`` — these capabilities satisfy prerequisite resolution today but would
be blocked if the proof gate were flipped to enforce, so the flag tells operators
exactly how many capabilities need real evidence before the gate can be enabled), and
``capability_proof_gate_auto_graduation`` (computes fleet-wide proof coverage and
automatically advances the gate mode off→observe→enforce as coverage crosses the
configured thresholds; disabled by default — operators must opt in via
``capability_proof_gate_auto_graduation_enabled``).

Config knobs (runtime_config, operator-settable via PATCH /api/admin/settings):

* ``metrics_integrity_sweep_mode``            — ``off`` / ``observe`` / ``enforce``
* ``metrics_integrity_sweep_interval_minutes``— cadence gate (default 60)
* ``metrics_integrity_sweep_max_checks``      — checks run per tick (default 25)
* ``metrics_integrity_sweep_max_projects``    — projects scanned per check (25)
* ``metrics_integrity_sweep_sample_limit``    — rows sampled per DB check (200)
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum
from typing import Any, Awaitable, Callable, Deque, Dict, List, Optional, cast

from sqlalchemy import func
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

#: Provenance for per-capability proof-earn research obligations enrolled by the
#: capability_proof_gate_gap check in enforce mode. Distinct from the generic
#: drift provenance so proof-research probes are attributable and never conflated
#: with counter-drift findings.
CAPABILITY_PROOF_RESEARCH_PROVENANCE = "capability_proof_research"

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
    # Count of gaps this check actively self-HEALED on the tick that produced it
    # (e.g. capability proof gaps auto-resolved). None for checks that only
    # recompute/flag and never mutate. Surfaced structured — not buried in
    # ``detail`` prose — so the observability snapshot and dashboards can report
    # self-heal throughput machine-readably.
    auto_resolved: int | None = None
    # Breakdown of ``auto_resolved`` by the proof tier that closed each gap. Both
    # tiers are structural (``path_proven``) and neither clears the enforce gate,
    # but the split matters operationally: a fleet auto-resolving caps entirely via
    # the weaker ``backing_path`` fallback looks healthy by ``auto_resolved`` count
    # yet has earned no ``named_test_present`` coverage at all. Surfaced structured
    # so the observability snapshot can report tier throughput without parsing the
    # ``detail`` prose. Both are ``None`` for checks that never auto-resolve; when
    # set they sum to ``auto_resolved``.
    auto_resolved_backing_path: int | None = None
    auto_resolved_named_test_present: int | None = None

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
            "auto_resolved": self.auto_resolved,
            "auto_resolved_backing_path": self.auto_resolved_backing_path,
            "auto_resolved_named_test_present": self.auto_resolved_named_test_present,
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
    # Cumulative self-heal throughput across all ticks. Each CheckResult already
    # carries per-result ``auto_resolved`` (and its tier split), but nothing
    # aggregated it, so self-heal throughput was invisible to operators and
    # dashboards. Summed per tick and exposed in ``snapshot()`` alongside the
    # per-tick totals so the trend is machine-readable.
    auto_resolved_total: int = 0
    auto_resolved_backing_path_total: int = 0
    auto_resolved_named_test_present_total: int = 0
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
        auto_resolved = sum(r.auto_resolved or 0 for r in results)
        auto_resolved_bp = sum(r.auto_resolved_backing_path or 0 for r in results)
        auto_resolved_nt = sum(
            r.auto_resolved_named_test_present or 0 for r in results
        )
        self.ticks += 1
        self.checks_run += len(results)
        self.flags_total += len(flagged)
        self.errors_total += len(errored)
        self.enrolled_total += enrolled
        self.auto_resolved_total += auto_resolved
        self.auto_resolved_backing_path_total += auto_resolved_bp
        self.auto_resolved_named_test_present_total += auto_resolved_nt
        tick = {
            "at": now.isoformat(),
            "mode": mode,
            "results": len(results),
            "flagged": len(flagged),
            "errors": len(errored),
            "enrolled": enrolled,
            "auto_resolved": auto_resolved,
            "auto_resolved_backing_path": auto_resolved_bp,
            "auto_resolved_named_test_present": auto_resolved_nt,
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
            "auto_resolved_total": self.auto_resolved_total,
            "auto_resolved_backing_path_total": self.auto_resolved_backing_path_total,
            "auto_resolved_named_test_present_total": (
                self.auto_resolved_named_test_present_total
            ),
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


#: Stable finding_key prefix for per-capability proof-earn research obligations.
#: Distinct from :meth:`CheckResult.finding_key` (``metrics-integrity:<check>:<pid>``)
#: so a research probe per unproven capability never collides with the single
#: per-check drift finding.
CAPABILITY_PROOF_RESEARCH_KEY_PREFIX = "metrics-integrity:capability-proof-research"


def _capability_proof_research_finding_key(project_id: str, cap_name: str) -> str:
    """Stable per-(project, capability) key so re-running the sweep on every tick
    dedupes onto the same ledger row instead of spawning duplicate research."""
    return f"{CAPABILITY_PROOF_RESEARCH_KEY_PREFIX}:{project_id}:{cap_name}"


async def _enroll_capability_proof_research(
    session, *, project_id, cap_name: str, candidate_paths: list[str] | None = None
) -> bool:
    """Enroll ONE deduped proof-earn research obligation for a working capability
    that has no file-like backing_paths and no proof (the ``needs_backing_paths``
    bucket). Returns True when a NEW row was created.

    Idempotent via the stable per-capability finding_key: ``enroll_backlog_item``
    dedupes on ``(project_id, finding_key)``, so repeated ticks never create
    duplicate obligations. The enrolled item drives ``check_evergreen_fill`` ->
    ``spawn_evergreen_probe`` to later spawn a low-priority ``type:"research"``
    task that determines how machine-verified proof can be earned.

    When automated discovery surfaced ``candidate_paths`` (source/test files at
    HEAD whose names match the capability but which are NOT yet test-covered
    enough to auto-record proof), they are folded into the obligation ``detail``
    as concrete SUGGESTIONS so the downstream research task starts from a real
    candidate list instead of nothing.
    """
    from app.evergreen_backlog import enroll_backlog_item
    from app.models import EvergreenBacklogItem

    finding_key = _capability_proof_research_finding_key(str(project_id), cap_name)
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

    title = f"Earn machine-verified proof for capability: {cap_name}"
    detail = (
        f"Working capability '{cap_name}' is declared 'working' but carries no "
        "proven evidence and no file-like backing_paths, so the always-on "
        "backing-path self-heal cannot auto-resolve it. Determine how "
        "machine-verified proof (a named test exercising the capability, or "
        "verifiable file-like backing_paths that resolve at HEAD) can be earned "
        "for this specific capability, and propose concrete backing_paths when "
        "they can be identified. Recording that proof moves the capability out of "
        "the capability_proof_gate gap and lets dependents rely on it."
    )
    if candidate_paths:
        preview = ", ".join(candidate_paths[:5])
        detail += (
            " Automated discovery surfaced candidate backing path(s) whose names "
            "match this capability and which resolve at HEAD (verify they truly "
            f"back the capability and add test coverage): {preview}."
        )
    await enroll_backlog_item(
        session,
        project_id=project_id,
        finding_key=finding_key,
        title=title,
        detail=detail,
        provenance=CAPABILITY_PROOF_RESEARCH_PROVENANCE,
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
            try:
                await session.commit()
            except Exception as exc:  # noqa: BLE001 — a failed commit must not abort the tick
                # The enrolled EvergreenBacklogItem rows are rolled back on a failed
                # commit; log it (observability was about to report enrolled=N) and
                # zero the count so the tick summary does not overstate its own numbers.
                logger.warning(
                    "[METRICS_INTEGRITY] enrollment commit failed; %d enrolled "
                    "finding(s) rolled back: %s",
                    enrolled,
                    exc,
                )
                with contextlib.suppress(Exception):
                    await session.rollback()
                enrolled = 0

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
            .order_by(Artifact.created_at.desc())
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
    )
    from app.acceptance_gate import CODE_ACCEPTANCE_VERDICT_KEY, acceptance_is_proven
    from app.landed_signal import has_landed_signal
    from app.models import CriterionEvidence, Project, Task, TaskStatus
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
        # Batch-fetch evidence for all active criteria in one query instead of
        # issuing N individual queries (one per criterion).
        active_criterion_ids = [c.id for c in active_criteria]
        all_evidence = (
            await ctx.session.execute(
                select(CriterionEvidence)
                .where(CriterionEvidence.criterion_id.in_(active_criterion_ids))
                .order_by(CriterionEvidence.criterion_id, CriterionEvidence.recorded_at)
            )
        ).scalars().all()
        for row in all_evidence:
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
        STATUS_RESOLVED,
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
        # No-CI gate obligation: a live fall-open to an UNVERIFIED merge owes a
        # debt row. last_unverified_reason is a PERMANENT stamp — it is never
        # cleared by a subsequent verified run (see ci_resolution.record_no_ci_gate_runtime).
        # Use recent_outcomes to determine liveness: if the most recent gate outcome
        # was verified, the gate is now functioning and the obligation is not active.
        if isinstance(merge_gate_runtime, dict) and merge_gate_runtime.get(
            "last_unverified_reason"
        ):
            is_live = True
            recent_outcomes = merge_gate_runtime.get("recent_outcomes")
            if isinstance(recent_outcomes, list) and recent_outcomes:
                last = recent_outcomes[-1]
                if isinstance(last, dict) and last.get("verified"):
                    is_live = False
            if is_live:
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
            # The no-CI gate obligation is triggered by
            # ``merge_gate_runtime.last_unverified_reason``, a PERMANENT historical
            # stamp that a verified run NEVER clears (see
            # ci_resolution.record_no_ci_gate_runtime). The stamp therefore does NOT
            # prove the obligation is currently live — the authoritative live signal
            # is the debt row itself, which the recorder RE-OPENS (arecord_completion_debt)
            # on any genuine recurrence. A RESOLVED row is thus a legitimate operator
            # disposition, not an under-count; only an ABSENT row (recorder silently
            # failed to open one at all) is real drift. Without this carve-out,
            # resolving the no_ci_gate debt makes the sweep flag the project on every
            # tick forever (evergreen re-enroll in enforce mode).
            if status == STATUS_RESOLVED and key.startswith("no_ci_gate:"):
                continue
            # Absent, resolved (for obligations recomputed live from ground truth
            # every tick, e.g. capability_trial), or any other non-open/non-dismissed
            # state while the obligation is currently expected active == an under-count.
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
            # Order by created_at, NOT Task.id: task ids are uuid4 (non-monotonic),
            # so id.desc() yields a quasi-static RANDOM subset instead of the most
            # recent tasks — new rollup regressions would land outside the sample
            # with ~uniform probability and go unaudited. Matches the recency
            # ordering used by the other DB checks in this module.
            .order_by(Task.created_at.desc())
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

    def _load_json_file(path: str) -> Any:
        with open(path) as fh:
            return _json.load(fh)

    # 1. Load caps from the tracked JSON.
    try:
        all_limits = await asyncio.to_thread(_load_json_file, _NEAR_CAP_LIMITS_PATH)
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
            def _count_lines(p: str) -> int:
                with open(p, encoding="utf-8", errors="replace") as fh:
                    return len(fh.readlines())
            count = await asyncio.to_thread(_count_lines, path)
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
            history = await asyncio.to_thread(parse_bump_history, rel, "backend")
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
    Rows missing an ``id`` entirely are reported as a DISTINCT population (id-less
    rows) rather than folded into the duplicate count — a missing id is exactly the
    malformed-write population this sweep targets and mislabeling it as a duplicate
    misdirects triage.

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
        total_true_dups = 0
        total_missing = 0
        dup_collections: List[str] = []
        missing_collections: List[str] = []
        for coll_name in _ID_KEYED_COLLECTIONS:
            items = pm.get(coll_name)
            if not isinstance(items, list) or not items:
                continue
            ids = [item.get("id") for item in items if isinstance(item, dict)]
            # Separate the two malformed populations: id-less rows (item has no
            # "id") vs TRUE duplicate ids (a non-None id repeated). Counting raw
            # length against distinct non-None ids conflates them, so a single
            # missing-id row would be mislabeled as a "duplicate id".
            present_ids = [i for i in ids if i is not None]
            missing = len(ids) - len(present_ids)
            distinct_len = len(set(present_ids))
            true_dups = len(present_ids) - distinct_len
            total_raw += len(ids)
            total_distinct += distinct_len
            total_true_dups += true_dups
            total_missing += missing
            if true_dups > 0:
                dup_collections.append(coll_name)
            if missing > 0:
                missing_collections.append(coll_name)

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

        # delta == total_true_dups + total_missing == total_raw - total_distinct,
        # so the self_reported/ground_truth/delta relationship is preserved while
        # the detail attributes each anomaly to its true category.
        if total_true_dups > 0 or total_missing > 0:
            note_parts: List[str] = []
            if total_true_dups > 0:
                note_parts.append(
                    f"{total_true_dups} duplicate id(s) across "
                    f"{len(dup_collections)} collection(s): "
                    + ", ".join(dup_collections)
                )
            if total_missing > 0:
                note_parts.append(
                    f"{total_missing} id-less row(s) across "
                    f"{len(missing_collections)} collection(s): "
                    + ", ".join(missing_collections)
                )
            results.append(
                CheckResult(
                    check="problem_model_duplicate_id_sweep",
                    status=CheckStatus.FLAGGED,
                    self_reported=total_raw,
                    ground_truth=total_distinct,
                    delta=total_true_dups + total_missing,
                    severity="warn",
                    subject=pid,
                    detail="; ".join(note_parts),
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


@register_check(
    "direction_cumulative_merge_invariant",
    "Combine research_history + history_aggregate lifetime totals and flag "
    "structural violations (verified > merged, proven > verified or declared).",
)
async def check_direction_cumulative_merge_invariant(
    ctx: SweepContext,
) -> List[CheckResult]:
    """Ground truth: the combined lifetime merge totals (active research_history +
    evicted history_aggregate) must satisfy proven ≤ verified ≤ merged and
    proven ≤ declared. A violation indicates a counter was written inconsistently
    across eviction, recompute, or stranding patches. Flag-only: never mutates any
    direction data. Bounded to ``max_projects`` projects.
    """
    from app.direction_outcomes import proven_merged_count, verified_merged_count
    from app.models import Project

    projects = (
        await ctx.session.execute(
            select(Project.id, Project.directions).limit(ctx.max_projects)
        )
    ).all()
    if not projects:
        return [
            CheckResult(
                check="direction_cumulative_merge_invariant",
                status=CheckStatus.SKIPPED,
                subject="global",
                detail="no projects to audit",
            )
        ]

    results: List[CheckResult] = []
    any_checked = False
    checked_pids: set = set()
    for project_id, directions in projects:
        pid = str(project_id)
        for d in directions or []:
            h = d.get("research_history") or []
            agg = d.get("history_aggregate") or {}
            if not h and not agg:
                continue
            any_checked = True
            checked_pids.add(pid)
            h_merged = sum(int(e.get("prs_merged") or 0) for e in h)
            h_verified = sum(verified_merged_count(e) for e in h)
            h_proven = sum(proven_merged_count(e) for e in h)
            h_declared = sum(int(e.get("acceptance_declared_merged") or 0) for e in h)
            a_merged = int(agg.get("total_prs_merged") or 0)
            # Legacy fallback in aggregate mirrors _fold_evicted_into_aggregate.
            a_verified = int(agg.get("total_verified_prs_merged") or a_merged)
            a_proven = int(agg.get("total_acceptance_proven_merged") or 0)
            a_declared = int(agg.get("total_acceptance_declared_merged") or 0)
            lt_merged = h_merged + a_merged
            lt_verified = h_verified + a_verified
            lt_proven = h_proven + a_proven
            lt_declared = h_declared + a_declared
            viols: List[str] = []
            if lt_verified > lt_merged:
                viols.append(f"verified ({lt_verified}) > merged ({lt_merged})")
            if lt_proven > lt_verified:
                viols.append(f"proven ({lt_proven}) > verified ({lt_verified})")
            if lt_declared > 0 and lt_proven > lt_declared:
                viols.append(f"proven ({lt_proven}) > declared ({lt_declared})")
            if viols:
                results.append(
                    CheckResult(
                        check="direction_cumulative_merge_invariant",
                        status=CheckStatus.FLAGGED,
                        self_reported=lt_merged,
                        ground_truth=lt_verified,
                        delta=len(viols),
                        severity="warn",
                        subject=pid,
                        detail=(
                            f"direction {d.get('id', 'unknown')}: "
                            + "; ".join(viols[:2])
                        ),
                    )
                )

    if not any_checked:
        return [
            CheckResult(
                check="direction_cumulative_merge_invariant",
                status=CheckStatus.SKIPPED,
                subject="global",
                detail="no directions with research_history or history_aggregate found",
            )
        ]

    flagged_pids = {r.subject for r in results if r.flagged}
    for pid in checked_pids - flagged_pids:
        results.append(
            CheckResult(
                check="direction_cumulative_merge_invariant",
                status=CheckStatus.OK,
                delta=0,
                subject=pid,
                detail="all direction lifetime merge-counter invariants consistent",
            )
        )
    return results


_FA_ELIGIBLE_TYPES: frozenset = frozenset({"create", "refactor"})
_FA_MIN_SAMPLE: int = 5
_FA_LOW_COVERAGE_THRESHOLD: float = 0.5


@register_check(
    "first_attempt_rate_denominator_coverage",
    "Sample recent terminal create/refactor tasks and flag projects where <50% "
    "carry _first_attempt_succeeded, making the FA-rate denominator unreliable.",
)
async def check_first_attempt_rate_denominator_coverage(
    ctx: SweepContext,
) -> List[CheckResult]:
    """Ground truth: every terminal create/refactor task that went through the
    standard PR-review pipeline should carry ``_first_attempt_succeeded`` in its
    context. When coverage drops below ``_FA_LOW_COVERAGE_THRESHOLD``, the FA rate
    is computed over an unrepresentative subset of eligible tasks and silently
    under-samples. Bounded to ``max_projects`` projects; within each project, at
    most ``sample_limit`` terminal tasks are considered; skips projects with fewer
    than ``_FA_MIN_SAMPLE`` eligible tasks.
    """
    from app.models import Project, Task, TaskStatus

    cutoff = ctx.now.replace(tzinfo=None) - timedelta(days=ctx.window_days)
    projects = (
        await ctx.session.execute(select(Project.id).limit(ctx.max_projects))
    ).all()

    if not projects:
        return [
            CheckResult(
                check="first_attempt_rate_denominator_coverage",
                status=CheckStatus.SKIPPED,
                subject="global",
                detail="no projects to audit",
            )
        ]

    results: List[CheckResult] = []
    for (project_id,) in projects:
        pid = str(project_id)
        tasks = (
            await ctx.session.execute(
                select(Task)
                .where(
                    Task.project_id == project_id,
                    Task.status.in_((TaskStatus.COMPLETED, TaskStatus.FAILED)),
                    Task.created_at >= cutoff,
                )
                .order_by(Task.created_at.desc())
                .limit(ctx.sample_limit)
            )
        ).scalars().all()

        total = 0
        measured = 0
        for t in tasks:
            ttype = (t.first_payload or {}).get("type", "")
            if ttype not in _FA_ELIGIBLE_TYPES:
                continue
            total += 1
            if "_first_attempt_succeeded" in (t.context or {}):
                measured += 1
        if total < _FA_MIN_SAMPLE:
            results.append(
                CheckResult(
                    check="first_attempt_rate_denominator_coverage",
                    status=CheckStatus.SKIPPED,
                    subject=pid,
                    detail=f"insufficient sample ({total} < {_FA_MIN_SAMPLE})",
                )
            )
            continue
        coverage = round(measured / total, 3)
        if coverage < _FA_LOW_COVERAGE_THRESHOLD:
            results.append(
                CheckResult(
                    check="first_attempt_rate_denominator_coverage",
                    status=CheckStatus.FLAGGED,
                    self_reported=measured,
                    ground_truth=total,
                    delta=total - measured,
                    severity="warn",
                    subject=pid,
                    detail=(
                        f"{measured}/{total} ({int(coverage * 100)}%) terminal "
                        "create/refactor tasks carry _first_attempt_succeeded; "
                        "FA-rate denominator covers <50% of eligible tasks"
                    ),
                )
            )
        else:
            results.append(
                CheckResult(
                    check="first_attempt_rate_denominator_coverage",
                    status=CheckStatus.OK,
                    self_reported=measured,
                    ground_truth=total,
                    delta=0,
                    subject=pid,
                    detail=(
                        f"{measured}/{total} eligible tasks carry "
                        f"_first_attempt_succeeded ({int(coverage * 100)}% coverage)"
                    ),
                )
            )
    return results


@register_check(
    "acceptance_window_sub_scores_invariant",
    "Scan recent ReleaseVersion qualification verdicts and flag any that violate "
    "the acceptance_window population invariants (verified <= write_total <= "
    "total_all_merges; fraction == verified/write_total or None when write_total=0).",
)
async def check_acceptance_window_sub_scores_invariant(
    ctx: SweepContext,
) -> List[CheckResult]:
    """Ground truth: the ``acceptance_window_*`` fields recorded in
    ``last_qualification["sub_scores"]`` must satisfy the population invariants
    defined by ``acceptance_window_sub_scores_violations`` — the same invariants
    the release-qualifier writes when it restricts the denominator to
    WRITE_TASK_TYPES merges.

    A violation means the numerator/denominator pair was written inconsistently
    (e.g. the old mixed-population bug where the denominator counted ALL landed
    merges while the numerator only counted write-task verified merges). Flag-only:
    never mutates the verdict it audits. Verdicts that predate the
    acceptance_window keys (legacy rows) are skipped, not falsely flagged.

    Bounded to ``max_projects`` open ReleaseVersion rows.
    """
    from app.acceptance_gate import acceptance_window_sub_scores_violations
    from app.models import ReleaseVersion

    versions = (
        await ctx.session.execute(
            select(ReleaseVersion)
            .where(ReleaseVersion.status == "open")
            .limit(ctx.max_projects)
        )
    ).scalars().all()

    if not versions:
        return [
            CheckResult(
                check="acceptance_window_sub_scores_invariant",
                status=CheckStatus.SKIPPED,
                subject="global",
                detail="no open ReleaseVersion rows to audit",
            )
        ]

    results: List[CheckResult] = []
    any_checked = False
    for version in versions:
        pid = str(version.project_id)
        qual = version.last_qualification or {}
        sub_scores = qual.get("sub_scores") or {}
        violations = acceptance_window_sub_scores_violations(sub_scores)
        if violations is None:
            # Should never happen (helper always returns a list), but guard.
            continue
        # Skip when the new population keys are absent — covers both the zero-key
        # case and the pre-fix 3-key schema (total/verified/fraction without the
        # write_total/total_all_merges population keys).
        from app.acceptance_gate import _ACCEPTANCE_WINDOW_POPULATION_KEYS

        has_fields = any(k in sub_scores for k in _ACCEPTANCE_WINDOW_POPULATION_KEYS)
        if not has_fields:
            results.append(
                CheckResult(
                    check="acceptance_window_sub_scores_invariant",
                    status=CheckStatus.SKIPPED,
                    subject=pid,
                    detail=(
                        f"version {version.id}: last_qualification sub_scores "
                        "lacks acceptance_window fields (legacy verdict)"
                    ),
                )
            )
            continue

        any_checked = True
        if violations:
            results.append(
                CheckResult(
                    check="acceptance_window_sub_scores_invariant",
                    status=CheckStatus.FLAGGED,
                    self_reported=int(sub_scores.get("acceptance_window_verified") or 0),
                    ground_truth=int(
                        sub_scores.get("acceptance_window_write_total") or 0
                    ),
                    delta=len(violations),
                    severity="warn",
                    subject=pid,
                    detail=(
                        f"version {version.id}: "
                        + "; ".join(violations[:3])
                    ),
                )
            )
        else:
            results.append(
                CheckResult(
                    check="acceptance_window_sub_scores_invariant",
                    status=CheckStatus.OK,
                    self_reported=int(sub_scores.get("acceptance_window_verified") or 0),
                    ground_truth=int(
                        sub_scores.get("acceptance_window_write_total") or 0
                    ),
                    delta=0,
                    subject=pid,
                    detail=(
                        f"version {version.id}: acceptance_window sub_scores consistent"
                    ),
                )
            )

    if not any_checked and not any(
        r.status != CheckStatus.SKIPPED for r in results
    ):
        return [
            CheckResult(
                check="acceptance_window_sub_scores_invariant",
                status=CheckStatus.SKIPPED,
                subject="global",
                detail="no open versions with acceptance_window fields found",
            )
        ]

    return results


@register_check(
    "undeclared_test_cochange_coverage_split",
    "Report the split between undeclared merges with inferrable test co-change "
    "coverage vs bare-undeclared (no test files or no production files), providing "
    "visibility into whether agents are already shipping tests.",
)
async def check_undeclared_test_cochange_coverage_split(
    ctx: SweepContext,
) -> List[CheckResult]:
    """Observe-only metric: split of undeclared merges by inferred test co-change.

    Ground truth: among recent COMPLETED tasks with a PR artifact (landed merges)
    that carry NO explicit acceptance verdict, count the split between:
      - inferred_test_cochange: CI-verified merge with both test + production files
      - bare_undeclared: all other undeclared (either no test files or no prod files)

    Reports metrics only, never flags. Answers whether agents are shipping tests
    (high inferred fraction) or tests are genuinely absent (all bare-undeclared).
    Bounded to the most recent ``sample_limit`` PR-artifact COMPLETED tasks.
    """
    from app.acceptance_gate import (
        CODE_ACCEPTANCE_VERDICT_KEY,
        _is_test_path,
        _context_merge_verification_status,
    )
    from app.landed_signal import has_landed_signal
    from app.models import Artifact, ArtifactType, Task, TaskStatus

    # Bounded: newest PR-artifact task ids first, capped at the sample limit.
    pr_task_id_rows = (
        await ctx.session.execute(
            select(Artifact.task_id)
            .where(Artifact.artifact_type == ArtifactType.PULL_REQUEST)
            .order_by(Artifact.created_at.desc())
            .limit(ctx.sample_limit)
        )
    ).all()
    pr_task_ids = [row[0] for row in pr_task_id_rows]
    if not pr_task_ids:
        return [
            CheckResult(
                check="undeclared_test_cochange_coverage_split",
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

    # Filter to landed tasks with no explicit acceptance verdict (undeclared).
    undeclared_tasks: List[Task] = []
    for t in tasks:
        if not has_landed_signal(t):
            continue
        recorded = (t.context or {}).get(CODE_ACCEPTANCE_VERDICT_KEY)
        if isinstance(recorded, dict):
            # Explicit verdict recorded — not undeclared.
            continue
        undeclared_tasks.append(t)

    if not undeclared_tasks:
        return [
            CheckResult(
                check="undeclared_test_cochange_coverage_split",
                status=CheckStatus.SKIPPED,
                subject="global",
                detail="no landed undeclared merges in sample window",
            )
        ]

    # Classify each undeclared task: does it have inferred test co-change?
    # Per-project counters for split.
    per_project_split: Dict[str, Dict[str, int]] = {}

    for t in undeclared_tasks:
        pid = str(t.project_id)
        if pid not in per_project_split:
            per_project_split[pid] = {
                "inferred_test_cochange": 0,
                "bare_undeclared": 0,
            }

        # Check merge verification status.
        if _context_merge_verification_status(t.context) != "verified":
            # No-CI gate fall-open: not verified, so no inferred test co-change.
            per_project_split[pid]["bare_undeclared"] += 1
            continue

        # Verified merge: check for test + production files.
        changed = t.context.get("_pr_changed_files") if t.context else None
        if not isinstance(changed, list):
            # No changed files recorded: bare undeclared.
            per_project_split[pid]["bare_undeclared"] += 1
            continue

        str_paths = [p for p in changed if isinstance(p, str) and p.strip()]
        has_test = any(_is_test_path(p) for p in str_paths)
        has_prod = any(not _is_test_path(p) for p in str_paths)

        if has_test and has_prod:
            per_project_split[pid]["inferred_test_cochange"] += 1
        else:
            per_project_split[pid]["bare_undeclared"] += 1

    # Report per project.
    results: List[CheckResult] = []
    for pid in sorted(per_project_split.keys()):
        counts = per_project_split[pid]
        inferred = counts["inferred_test_cochange"]
        bare = counts["bare_undeclared"]
        total = inferred + bare

        # Observe-only: report the split with OK status (never flagged).
        # Use self_reported for inferred count, ground_truth for bare count.
        results.append(
            CheckResult(
                check="undeclared_test_cochange_coverage_split",
                status=CheckStatus.OK,
                self_reported=inferred,
                ground_truth=bare,
                delta=total,
                subject=pid,
                detail=(
                    f"undeclared merges: {inferred} with inferred test co-change "
                    f"(verified + test + prod files), {bare} bare-undeclared "
                    f"({inferred + bare} total)"
                ),
            )
        )

    return results


@register_check(
    "acceptance_window_fraction_recompute",
    "Recompute acceptance_window_fraction over the current open version's write-task "
    "merge window and report it alongside the per-bucket and undeclared-reason split. "
    "This is the operator-visible artifact proving whether inferred_test_cochange lifts "
    "the fraction off zero on real data — or, if it stays zero, the reason split IS the "
    "documented reframe. Reports metrics only; never flags.",
)
async def check_acceptance_window_fraction_recompute(
    ctx: SweepContext,
) -> List[CheckResult]:
    """Observe-only: live recompute of acceptance_window_fraction + breakdown.

    For each of the first ``max_projects`` projects with an open version, calls
    :func:`release_qualifier._compute_acceptance_window_aggregate` — the SAME code
    path the qualification verdict uses — and reports the recomputed fraction and
    the per-bucket / undeclared-reason breakdown. ``self_reported`` carries the
    verified numerator, ``ground_truth`` the write-task denominator, and ``delta``
    the count of undeclared merges that the ``inferred_test_cochange`` bucket did
    NOT rescue (bare undeclared), so operators see the recompute against real data.
    Never flags: a genuinely-zero fraction is reported (not a violation), with the
    reason split explaining exactly why.
    """
    from app.acceptance_gate import (
        ACCEPTANCE_AGGREGATE_BUCKETS,
        _UNDECLARED_REASON_KEYS,
    )
    from app.models import Project
    from app.release_qualifier import _compute_acceptance_window_aggregate
    from app.release_version import current_open_version

    projects = (
        await ctx.session.execute(select(Project.id).limit(ctx.max_projects))
    ).all()
    if not projects:
        return [
            CheckResult(
                check="acceptance_window_fraction_recompute",
                status=CheckStatus.SKIPPED,
                subject="global",
                detail="no projects to audit",
            )
        ]

    results: List[CheckResult] = []
    for (project_id,) in projects:
        pid = str(project_id)
        open_version = await current_open_version(ctx.session, project_id)
        if open_version is None:
            results.append(
                CheckResult(
                    check="acceptance_window_fraction_recompute",
                    status=CheckStatus.SKIPPED,
                    subject=pid,
                    detail="no open version",
                )
            )
            continue

        agg = await _compute_acceptance_window_aggregate(ctx.session, open_version)
        write_total = int(agg.get("acceptance_window_write_total") or 0)
        verified = int(agg.get("acceptance_window_verified") or 0)
        fraction = agg.get("acceptance_window_fraction")
        buckets = agg.get("acceptance_window_buckets") or {}
        reasons = agg.get("acceptance_window_undeclared_reasons") or {}
        undeclared = int(buckets.get("undeclared") or 0)

        frac_str = "None" if fraction is None else f"{fraction:.3f}"
        bucket_str = ", ".join(
            f"{name}={buckets.get(name, 0)}" for name in ACCEPTANCE_AGGREGATE_BUCKETS
        )
        reason_str = ", ".join(
            f"{name}={reasons.get(name, 0)}" for name in _UNDECLARED_REASON_KEYS
        )
        results.append(
            CheckResult(
                check="acceptance_window_fraction_recompute",
                status=CheckStatus.OK,
                self_reported=verified,
                ground_truth=write_total,
                delta=undeclared,
                subject=pid,
                detail=(
                    f"acceptance_window_fraction={frac_str} "
                    f"(verified {verified}/{write_total}); "
                    f"buckets: {bucket_str}; undeclared_reasons: {reason_str}"
                ),
            )
        )

    return results


@register_check(
    "sr_catch_rate_cache_freshness",
    "Check that design_doc['sr_catch_rate_trends']['computed_at'] is within the "
    "staleness window so operators see current catch-rate trends.",
)
async def check_sr_catch_rate_cache_freshness(
    ctx: SweepContext,
) -> List[CheckResult]:
    """Ground truth: the persisted ``sr_catch_rate_trends`` cache must have been
    computed within ``_SR_TREND_STALENESS_DAYS`` days. A stale cache means
    operators read catch-rate trends that do not reflect recent task outcomes.
    The check does NOT recompute the trends (that is expensive); it only validates
    the ``computed_at`` timestamp against the known staleness threshold.

    Bounded to ``max_projects`` projects. Projects without any
    ``sr_catch_rate_trends`` cache are skipped (the cache is optional).
    """
    from app.models import Project
    from app.sr_review_quality_metrics import _SR_TREND_STALENESS_DAYS
    from app.time_utils import _to_naive_utc

    projects = (
        await ctx.session.execute(
            select(Project.id, Project.design_doc).limit(ctx.max_projects)
        )
    ).all()
    if not projects:
        return [
            CheckResult(
                check="sr_catch_rate_cache_freshness",
                status=CheckStatus.SKIPPED,
                subject="global",
                detail="no projects to audit",
            )
        ]

    stale_threshold = timedelta(days=_SR_TREND_STALENESS_DAYS)
    results: List[CheckResult] = []
    for project_id, design_doc in projects:
        pid = str(project_id)
        trends = (design_doc or {}).get("sr_catch_rate_trends") or {}
        if not trends:
            results.append(
                CheckResult(
                    check="sr_catch_rate_cache_freshness",
                    status=CheckStatus.SKIPPED,
                    subject=pid,
                    detail="no sr_catch_rate_trends cache present",
                )
            )
            continue
        computed_at_str = trends.get("computed_at")
        if not computed_at_str:
            results.append(
                CheckResult(
                    check="sr_catch_rate_cache_freshness",
                    status=CheckStatus.SKIPPED,
                    subject=pid,
                    detail="sr_catch_rate_trends cache has no computed_at timestamp",
                )
            )
            continue
        try:
            computed_at = _to_naive_utc(datetime.fromisoformat(str(computed_at_str)))
        except (ValueError, TypeError):
            results.append(
                CheckResult(
                    check="sr_catch_rate_cache_freshness",
                    status=CheckStatus.SKIPPED,
                    subject=pid,
                    detail=f"sr_catch_rate_trends.computed_at unparseable: {str(computed_at_str)[:40]}",
                )
            )
            continue
        age = ctx.now.replace(tzinfo=None) - computed_at
        if age > stale_threshold:
            results.append(
                CheckResult(
                    check="sr_catch_rate_cache_freshness",
                    status=CheckStatus.FLAGGED,
                    self_reported=int(age.total_seconds() // 3600),
                    ground_truth=int(stale_threshold.total_seconds() // 3600),
                    delta=int((age - stale_threshold).total_seconds() // 3600),
                    severity="warn",
                    subject=pid,
                    detail=(
                        f"sr_catch_rate_trends cache is {age.days}d old "
                        f"(>{_SR_TREND_STALENESS_DAYS}d staleness threshold); "
                        "operators see outdated catch-rate trends"
                    ),
                )
            )
        else:
            results.append(
                CheckResult(
                    check="sr_catch_rate_cache_freshness",
                    status=CheckStatus.OK,
                    self_reported=int(age.total_seconds() // 3600),
                    ground_truth=int(stale_threshold.total_seconds() // 3600),
                    delta=0,
                    subject=pid,
                    detail=(
                        f"sr_catch_rate_trends cache is {age.days}d old "
                        f"(within {_SR_TREND_STALENESS_DAYS}d threshold)"
                    ),
                )
            )
    return results


@register_check(
    "evidence_claim_reconciliation",
    "Re-verify structured gap evidence_refs against HEAD and flag active gaps "
    "whose path-backed proven evidence was definitely removed (contradicted), or "
    "whose evidence is absent while the gap claims closure. An indeterminate "
    "(None) path check NEVER flags — only a definite removal counts as stale.",
)
async def check_evidence_claim_reconciliation(
    ctx: SweepContext,
) -> List[CheckResult]:
    """Ground truth: a gap that carries a ``proven`` structured evidence ref
    pointing at a file/test path (``named_test`` / ``backing_path``, or any ref
    whose ``ref`` is a repo file path) only remains proven while that path still
    exists at HEAD. When the backing file was removed the proof is void — the
    evidence is *contradicted* even though the stored ``proven`` flag still reads
    True. This standing check re-verifies each path-backed proven ref via the
    same tri-state helper the capability backing-path verifier uses
    (:func:`app.source_ref_validation._path_exists_at_head`) and flags gaps whose
    proof no longer holds.

    CRITICAL HONESTY RULE: the path helper returns True / False / None. Only a
    definite ``False`` (path removed at HEAD) counts as stale/contradicted. A
    ``None`` (no checkout, git error, timeout, rate limit) degrades to
    ``unverified`` and is NEVER flagged — an indeterminate check must not be
    laundered into "removed".

    Flag-only and side-effect-free in every mode: it reads ``problem_model``
    JSONB directly and only constructs :class:`CheckResult` values — it NEVER
    mutates the durable gap. In ``observe`` mode the sweep records to the bounded
    observability ring only; enforce-mode enrollment flows through the standard
    :func:`_enroll_flag_finding` path. Bounded to ``ctx.max_projects`` projects.
    """
    from app import source_ref_validation as _srv
    from app.evidence_primitive import (
        PATH_BACKED_KINDS,
        claim_contradicts_evidence,
        classify_evidence,
    )
    from app.models import Project

    projects = (
        await ctx.session.execute(
            select(Project.id, Project.problem_model).limit(ctx.max_projects)
        )
    ).all()
    if not projects:
        return [
            CheckResult(
                check="evidence_claim_reconciliation",
                status=CheckStatus.SKIPPED,
                subject="global",
                detail="no projects to audit",
            )
        ]

    def _is_path_backed_ref(kind: Any, ref_str: str) -> bool:
        """Whether ``ref_str`` names a file/test path re-checkable against HEAD."""
        if kind in PATH_BACKED_KINDS:
            return True
        # A ref of any other kind is only re-verified when it literally looks
        # like a repo file path (logical ids — criterion keys, artifact ids —
        # are not resolvable by a path check and are left untouched).
        return _srv._looks_like_file_path(ref_str)

    results: List[CheckResult] = []
    any_checked = False
    for project_id, raw_pm in projects:
        pid = str(project_id)
        pm = raw_pm if isinstance(raw_pm, dict) else {}
        gaps = pm.get("gaps") if isinstance(pm, dict) else None
        if not isinstance(gaps, list) or not gaps:
            continue

        gaps_with_refs = 0
        flagged_details: List[str] = []
        total_proven_refs = 0
        total_stale_refs = 0
        for gap in gaps:
            if not isinstance(gap, dict):
                continue
            refs = gap.get("evidence_refs")
            if not isinstance(refs, list) or not refs:
                continue
            gaps_with_refs += 1

            # Re-verify each PROVEN, path-backed ref against HEAD. A proven ref
            # whose file is definitely removed (False) makes the gap's evidence
            # contradicted; None (indeterminate) is left untouched, never stale.
            proven_refs = 0
            stale_refs: List[str] = []
            for ref in refs:
                if not isinstance(ref, dict) or ref.get("proven") is not True:
                    continue
                ref_str = ref.get("ref")
                if not isinstance(ref_str, str) or not ref_str:
                    continue
                if not _is_path_backed_ref(ref.get("kind"), ref_str):
                    continue
                proven_refs += 1
                exists = await _srv._path_exists_at_head(ref_str)
                if exists is False:
                    stale_refs.append(ref_str)
                # True (present) or None (indeterminate) → not stale.

            # Stored classification, then override to contradicted ONLY on a
            # definite removal. classify_evidence never returns contradicted on
            # its own — that verdict requires the ground-truth re-check above.
            evidence_status = classify_evidence(refs)
            if stale_refs:
                evidence_status = "contradicted"
            total_proven_refs += proven_refs
            total_stale_refs += len(stale_refs)

            gap_id = gap.get("id", "unknown")
            gap_status = gap.get("status")
            # Flag when a proven ref was definitely removed, OR when the gap
            # claims closure while its evidence fails to back it (unproven /
            # contradicted). An in-progress ("active"/"blocked") gap with
            # merely-unproven evidence is expected and never flagged.
            if stale_refs:
                flagged_details.append(
                    f"gap {gap_id}: {len(stale_refs)} proven evidence ref(s) "
                    f"removed at HEAD (e.g. {stale_refs[0]}) — proof contradicted"
                )
            elif claim_contradicts_evidence(gap_status, evidence_status):
                flagged_details.append(
                    f"gap {gap_id}: claims {gap_status} but evidence is "
                    f"{evidence_status}"
                )

        if gaps_with_refs == 0:
            # No gap in this project carries structured evidence refs — nothing
            # to reconcile. Emit a per-project SKIPPED for observability parity.
            results.append(
                CheckResult(
                    check="evidence_claim_reconciliation",
                    status=CheckStatus.SKIPPED,
                    subject=pid,
                    detail="no gaps carry structured evidence_refs",
                )
            )
            continue

        any_checked = True
        if flagged_details:
            preview = "; ".join(flagged_details[:3])
            if len(flagged_details) > 3:
                preview += f" (+{len(flagged_details) - 3} more)"
            results.append(
                CheckResult(
                    check="evidence_claim_reconciliation",
                    status=CheckStatus.FLAGGED,
                    self_reported=total_proven_refs,
                    ground_truth=total_proven_refs - total_stale_refs,
                    delta=len(flagged_details),
                    severity="warn",
                    subject=pid,
                    detail=preview,
                )
            )
        else:
            results.append(
                CheckResult(
                    check="evidence_claim_reconciliation",
                    status=CheckStatus.OK,
                    self_reported=total_proven_refs,
                    ground_truth=total_proven_refs,
                    delta=0,
                    subject=pid,
                    detail=(
                        f"{gaps_with_refs} gap(s) with evidence_refs reconciled; "
                        "all path-backed proven refs still present at HEAD"
                    ),
                )
            )

    if not any_checked and not results:
        return [
            CheckResult(
                check="evidence_claim_reconciliation",
                status=CheckStatus.SKIPPED,
                subject="global",
                detail="no gaps with structured evidence_refs in any project",
            )
        ]
    return results


@register_check(
    "saturated_direction_with_nonmerge_productivity",
    "Detect directions marked possibly_saturated that carry recent non-merge "
    "productive outcomes (verified merges, acceptance evidence) — flags the "
    "self-reported stale saturation state.",
)
async def check_saturated_direction_with_nonmerge_productivity(
    ctx: SweepContext,
) -> List[CheckResult]:
    """Ground truth: a direction marked possibly_saturated should not simultaneously
    carry evidence of recent non-merge productive outcomes (verification verdicts,
    acceptance evidence). When this contradiction exists, the saturation flag is
    stale — productivity has been recorded but not credited by the classifier.

    Non-merge productivity indicators in research_history:
    - verified_prs_merged > 0 (CI-verified merge verdicts)
    - acceptance_proven_merged > 0 (proof evidence accumulated)
    - acceptance_verified_merged > 0 (verified acceptance evidence)

    These are "non-merge" outcomes in the sense that the productivity does not
    come from newly-spawned child tasks, but from verification / proof cycles
    on existing direction state — exactly the pattern the fix (#13378) credits.

    Flag-only: never mutates direction data. Bounded to max_projects projects.
    """
    from app.models import Project

    projects = (
        await ctx.session.execute(
            select(Project.id, Project.directions).limit(ctx.max_projects)
        )
    ).all()
    if not projects:
        return [
            CheckResult(
                check="saturated_direction_with_nonmerge_productivity",
                status=CheckStatus.SKIPPED,
                subject="global",
                detail="no projects to audit",
            )
        ]

    results: List[CheckResult] = []
    for project_id, directions in projects:
        pid = str(project_id)
        for d in directions or []:
            if not d.get("possibly_saturated"):
                continue

            # Check for non-merge productive outcomes in recent research_history
            history = d.get("research_history") or []
            has_nonmerge_outcome = False
            detail_parts: List[str] = []

            for entry in history:
                verified = int(entry.get("verified_prs_merged") or 0)
                acceptance_proven = int(entry.get("acceptance_proven_merged") or 0)
                acceptance_verified = int(entry.get("acceptance_verified_merged") or 0)

                if verified > 0 or acceptance_proven > 0 or acceptance_verified > 0:
                    has_nonmerge_outcome = True
                    if verified > 0:
                        detail_parts.append(f"verified={verified}")
                    if acceptance_proven > 0:
                        detail_parts.append(f"proven={acceptance_proven}")
                    if acceptance_verified > 0:
                        detail_parts.append(f"verified_evidence={acceptance_verified}")
                    break  # Found at least one; enough to flag

            if has_nonmerge_outcome:
                direction_id = d.get("id", "unknown")
                results.append(
                    CheckResult(
                        check="saturated_direction_with_nonmerge_productivity",
                        status=CheckStatus.FLAGGED,
                        self_reported=1,
                        ground_truth=0,
                        delta=1,
                        severity="warn",
                        subject=pid,
                        detail=(
                            f"direction {direction_id} marked possibly_saturated but has "
                            f"recent non-merge productive outcomes: {', '.join(detail_parts[:2])}"
                        ),
                    )
                )

    if not results:
        return [
            CheckResult(
                check="saturated_direction_with_nonmerge_productivity",
                status=CheckStatus.SKIPPED,
                subject="global",
                detail="no saturated directions with non-merge productivity found",
            )
        ]

    return results


@register_check(
    "capability_claim_reconciliation",
    "Re-verify working/partial capability backing_paths against HEAD and flag "
    "capabilities whose EVERY declared backing path was definitely removed. An "
    "indeterminate (None) path check NEVER flags — only definite removal counts.",
)
async def check_capability_claim_reconciliation(
    ctx: SweepContext,
) -> List[CheckResult]:
    """Ground truth: a capability declared 'working' or 'partial' whose
    ``backing_paths`` ALL return a definite False from the tri-state HEAD check is
    claiming operational status while its backing code no longer exists. This
    standing check re-verifies each backing path via
    :func:`app.source_ref_validation._path_exists_at_head` and flags the
    capability only when EVERY path is definitively removed.

    Also flags capabilities whose ``evidence_refs`` contradict their declared
    status via :func:`app.evidence_primitive.claim_contradicts_evidence`, mirroring
    the gap evidence_claim_reconciliation check.

    CRITICAL HONESTY RULE (mirrors ``evidence_claim_reconciliation`` exactly): the
    path helper returns True / False / None. Only a definite ``False`` for ALL
    declared paths triggers a flag. A single ``True`` (still present) OR a single
    ``None`` (indeterminate: no checkout, git error, timeout, rate-limit) anywhere
    in the path list PREVENTS the flag — an outage must never be laundered into
    'removed'. ``planned`` capabilities are aspirational (code not yet built) and
    are NEVER flagged for absent backing paths.

    Flag-only and side-effect-free in every mode: reads ``problem_model`` JSONB
    directly and only constructs :class:`CheckResult` values — NEVER mutates the
    durable capability. Bounded to ``ctx.max_projects`` projects.
    """
    from app import source_ref_validation as _srv
    from app.evidence_primitive import claim_contradicts_evidence, classify_evidence
    from app.models import Project

    projects = (
        await ctx.session.execute(
            select(Project.id, Project.problem_model).limit(ctx.max_projects)
        )
    ).all()
    if not projects:
        return [
            CheckResult(
                check="capability_claim_reconciliation",
                status=CheckStatus.SKIPPED,
                subject="global",
                detail="no projects to audit",
            )
        ]

    results: List[CheckResult] = []
    any_checked = False
    for project_id, raw_pm in projects:
        pid = str(project_id)
        pm = raw_pm if isinstance(raw_pm, dict) else {}
        capabilities = pm.get("capabilities") if isinstance(pm, dict) else None
        if not isinstance(capabilities, list) or not capabilities:
            continue

        # Eligible: working/partial capabilities with at least one file-path-shaped
        # backing_path. planned capabilities are aspirational — never flagged.
        eligible: List[dict] = []
        # Free-claim eligible: working/partial caps with NO backing_paths whose
        # evidence_refs alone already contradict the declared status.
        free_claim_eligible: List[dict] = []
        for cap in capabilities:
            if not isinstance(cap, dict):
                continue
            if cap.get("status") not in ("working", "partial"):
                continue
            paths = cap.get("backing_paths")
            if not isinstance(paths, list) or not paths:
                # No backing_paths — candidate for the free-claim path.
                free_claim_eligible.append(cap)
                continue
            # Keep only paths that look like repo file paths; skip logical ids.
            file_paths = [
                p for p in paths
                if isinstance(p, str) and p and _srv._looks_like_file_path(p)
            ]
            if not file_paths:
                continue
            eligible.append({"cap": cap, "file_paths": file_paths})

        if not eligible and not free_claim_eligible:
            results.append(
                CheckResult(
                    check="capability_claim_reconciliation",
                    status=CheckStatus.SKIPPED,
                    subject=pid,
                    detail="no working/partial capabilities with backing_paths to audit",
                )
            )
            continue

        any_checked = True
        audited = len(eligible) + len(free_claim_eligible)
        flagged_caps: List[str] = []

        for item in eligible:
            cap = item["cap"]
            file_paths = item["file_paths"]

            # Re-verify each backing path. A single True or None prevents a flag.
            all_removed = True
            for path in file_paths:
                exists = await _srv._path_exists_at_head(path)
                if exists is True or exists is None:
                    # Present or indeterminate — do NOT flag. Stop early.
                    all_removed = False
                    break
                # exists is False — definitely removed; keep checking remaining paths.

            cap_name = cap.get("name") or cap.get("id") or "unknown"
            if all_removed:
                flagged_caps.append(cap_name)
                continue

            # Second check: evidence_refs contradiction (mirrors gap reconciliation).
            refs = cap.get("evidence_refs")
            if isinstance(refs, list):
                evidence_status = classify_evidence(refs)
                # Re-verify proven path-backed refs — a definite removal overrides
                # to "contradicted" exactly as evidence_claim_reconciliation does.
                for ref in refs:
                    if not isinstance(ref, dict) or ref.get("proven") is not True:
                        continue
                    ref_str = ref.get("ref")
                    if not isinstance(ref_str, str) or not ref_str:
                        continue
                    if not _srv._looks_like_file_path(ref_str):
                        continue
                    path_exists = await _srv._path_exists_at_head(ref_str)
                    if path_exists is False:
                        evidence_status = "contradicted"
                        break
                if claim_contradicts_evidence(cap.get("status"), evidence_status):
                    flagged_caps.append(cap_name)

        # Free-claim path: caps declaring working/partial with no backing_paths.
        # Flag only when evidence_refs alone already contradict the status
        # (e.g. working + no evidence_refs → classify_evidence returns "unproven"
        # and claim_contradicts_evidence("working", "unproven") is True).
        for cap in free_claim_eligible:
            refs = cap.get("evidence_refs")
            refs_list = refs if isinstance(refs, list) else []
            evidence_status = classify_evidence(refs_list)
            if claim_contradicts_evidence(cap.get("status"), evidence_status):
                cap_name = cap.get("name") or cap.get("id") or "unknown"
                flagged_caps.append(cap_name)

        if flagged_caps:
            preview = ", ".join(flagged_caps[:3])
            if len(flagged_caps) > 3:
                preview += f" (+{len(flagged_caps) - 3} more)"
            results.append(
                CheckResult(
                    check="capability_claim_reconciliation",
                    status=CheckStatus.FLAGGED,
                    self_reported=audited,
                    ground_truth=audited - len(flagged_caps),
                    delta=len(flagged_caps),
                    severity="warn",
                    subject=pid,
                    detail=(
                        f"{len(flagged_caps)} working/partial capability(ies) whose "
                        f"every declared backing path was removed at HEAD: {preview}"
                    ),
                )
            )
        else:
            results.append(
                CheckResult(
                    check="capability_claim_reconciliation",
                    status=CheckStatus.OK,
                    self_reported=audited,
                    ground_truth=audited,
                    delta=0,
                    subject=pid,
                    detail=(
                        f"{audited} working/partial capability(ies) with backing_paths "
                        "reconciled; all paths still present or indeterminate at HEAD"
                    ),
                )
            )

    if not any_checked and not results:
        return [
            CheckResult(
                check="capability_claim_reconciliation",
                status=CheckStatus.SKIPPED,
                subject="global",
                detail="no working/partial capabilities with backing_paths in any project",
            )
        ]
    return results


@register_check(
    "merged_pr_count_consistency",
    "Recompute merged-PR counts with the canonical rollup-aware definition and "
    "flag projects where the naive count (llm_cost_per_merged_pr / merge_rate) "
    "exceeds the canonical count; delta is the rollup double-count.",
)
async def check_merged_pr_count_consistency(ctx: SweepContext) -> List[CheckResult]:
    """Ground truth: the CANONICAL merged-PR count excludes rollup-parent task ids
    so that a rollup landing that credits the parent and all its children is not
    double-counted. This matches ``efficiency_snapshot._cost_and_merges``.

    The NAIVE count (used by ``llm_cost_attribution._compute_llm_cost_per_merged_pr``
    and the ``merge_rate`` totals in ``compute_llm_cost_breakdown``) increments for
    every ``is_merged_completion(t)`` including rollup parents, inflating the
    denominator when rollup tasks are present in the window.

    When ``naive > canonical`` the project is flagged with:
    - ``self_reported`` = naive count (what the LLM-cost / merge_rate axes see)
    - ``ground_truth`` = canonical count (rollup-parent-excluded)
    - ``delta`` = rollup double-count (number of extra merges from rollup parents)

    If the loaded task volume hits the sample cap the ground truth cannot be
    trusted as complete — returns ``INCONCLUSIVE`` instead of a partial delta.

    Bounded to the most-recent ``sample_limit`` COMPLETED tasks; groups per project.
    Flag-only in every mode — never writes to the tasks it audits.
    """
    from app.efficiency_snapshot import _rollup_parent_ids
    from app.landed_signal import is_merged_completion
    from app.models import Task, TaskStatus

    tasks = (
        await ctx.session.execute(
            select(Task)
            .where(Task.status == TaskStatus.COMPLETED)
            # Order by created_at, NOT Task.id: task ids are uuid4 (non-monotonic),
            # so id.desc() yields a quasi-random subset instead of the most recent
            # tasks — new rollup regressions would land outside the sample with
            # ~uniform probability and go unaudited. Matches the recency ordering
            # used by the other DB checks in this module.
            .order_by(Task.created_at.desc())
            .limit(ctx.sample_limit + 1)
        )
    ).scalars().all()

    if len(tasks) > ctx.sample_limit:
        return [
            CheckResult(
                check="merged_pr_count_consistency",
                status=CheckStatus.INCONCLUSIVE,
                subject="global",
                detail=(
                    f"terminal-task volume exceeds sample_limit={ctx.sample_limit}; "
                    "cannot recompute within bounds"
                ),
            )
        ]

    if not tasks:
        return [
            CheckResult(
                check="merged_pr_count_consistency",
                status=CheckStatus.SKIPPED,
                subject="global",
                detail="no completed tasks in sample window",
            )
        ]

    # Group by project.
    by_project: Dict[str, List[Task]] = {}
    for t in tasks:
        by_project.setdefault(str(t.project_id), []).append(t)

    results: List[CheckResult] = []
    any_merged = False

    for pid, proj_tasks in sorted(by_project.items()):
        proj_parent_ids = _rollup_parent_ids(proj_tasks)
        naive = sum(1 for t in proj_tasks if is_merged_completion(t))
        canonical = sum(
            1 for t in proj_tasks
            if is_merged_completion(t) and t.id not in proj_parent_ids
        )

        if naive == 0:
            results.append(
                CheckResult(
                    check="merged_pr_count_consistency",
                    status=CheckStatus.SKIPPED,
                    subject=pid,
                    detail="no merged completions in sample window",
                )
            )
            continue

        any_merged = True

        if naive > canonical:
            delta = naive - canonical
            results.append(
                CheckResult(
                    check="merged_pr_count_consistency",
                    status=CheckStatus.FLAGGED,
                    self_reported=naive,
                    ground_truth=canonical,
                    delta=delta,
                    severity="warn",
                    subject=pid,
                    detail=(
                        f"llm_cost_per_merged_pr and merge_rate totals over-count "
                        f"merged PRs by {delta}: {naive} naive vs {canonical} "
                        "canonical (rollup-parent-excluded). The delta equals the "
                        "number of rollup-parent tasks counted as extra merges."
                    ),
                )
            )
        else:
            results.append(
                CheckResult(
                    check="merged_pr_count_consistency",
                    status=CheckStatus.OK,
                    self_reported=naive,
                    ground_truth=canonical,
                    delta=0,
                    subject=pid,
                    detail=(
                        f"naive={naive} == canonical={canonical}; "
                        "no rollup double-count detected in sample window"
                    ),
                )
            )

    if not any_merged and not results:
        return [
            CheckResult(
                check="merged_pr_count_consistency",
                status=CheckStatus.SKIPPED,
                subject="global",
                detail="no merged completions in sample window",
            )
        ]

    return results


@register_check(
    "criterion_evidence_count_integrity",
    "Independently count CriterionEvidence rows per criterion with a direct "
    "COUNT(*) and flag any criterion whose self-reported evidence_count from "
    "get_version_coverage_summary diverges from the actual row count.",
)
async def check_criterion_evidence_count_integrity(
    ctx: SweepContext,
) -> List[CheckResult]:
    """Ground truth: a direct ``COUNT(*)`` of ``CriterionEvidence`` rows per criterion.

    The self-reported ``evidence_count`` in :func:`get_version_coverage_summary`
    comes from an outer join + Python-side grouping (``len(evidence)``).
    This check independently counts rows with a direct ``CriterionEvidence`` query
    and flags any divergence — a divergence means the outer join or grouping logic
    silently dropped, duplicated, or miscounted evidence rows.

    Bounded to ``ctx.max_projects`` projects; returns SKIPPED for projects with
    no open version or no criteria. Flag-only: never mutates the audited
    evidence rows in any mode.
    """
    from app.acceptance_criteria import (
        get_criteria_for_version,
        get_version_coverage_summary,
    )
    from app.models import CriterionEvidence, Project
    from app.release_version import current_open_version

    projects = (
        await ctx.session.execute(select(Project.id).limit(ctx.max_projects))
    ).all()
    if not projects:
        return [
            CheckResult(
                check="criterion_evidence_count_integrity",
                status=CheckStatus.SKIPPED,
                subject="global",
                detail="no projects to audit",
            )
        ]

    results: List[CheckResult] = []
    for (project_id,) in projects:
        pid = str(project_id)
        open_version = await current_open_version(ctx.session, project_id)
        if open_version is None:
            results.append(
                CheckResult(
                    check="criterion_evidence_count_integrity",
                    status=CheckStatus.SKIPPED,
                    subject=pid,
                    detail="no open version",
                )
            )
            continue

        criteria = await get_criteria_for_version(ctx.session, open_version.id)
        if not criteria:
            results.append(
                CheckResult(
                    check="criterion_evidence_count_integrity",
                    status=CheckStatus.SKIPPED,
                    subject=pid,
                    detail="no criteria on open version",
                )
            )
            continue

        # Self-reported evidence counts from the summary (outer join + Python grouping).
        summary = await get_version_coverage_summary(ctx.session, open_version.id)
        summary_by_key: dict[str, int] = {}
        for c_summary in summary.get("criteria", []):
            summary_by_key[c_summary["criterion_key"]] = c_summary["evidence_count"]

        # Independently count CriterionEvidence rows per criterion (ground truth).
        # Use a single GROUP BY query instead of N individual COUNT(*) queries.
        flagged_criteria: list[str] = []
        ok_count = 0
        criterion_ids = [c.id for c in criteria]
        count_rows = (
            await ctx.session.execute(
                select(CriterionEvidence.criterion_id, func.count().label("cnt"))
                .where(CriterionEvidence.criterion_id.in_(criterion_ids))
                .group_by(CriterionEvidence.criterion_id)
            )
        ).all()
        count_by_cid: dict = {row[0]: row[1] for row in count_rows}
        for criterion in criteria:
            self_reported = summary_by_key.get(criterion.criterion_key, 0)
            ground_truth = count_by_cid.get(criterion.id, 0)

            if self_reported != ground_truth:
                flagged_criteria.append(criterion.criterion_key)
                results.append(
                    CheckResult(
                        check="criterion_evidence_count_integrity",
                        status=CheckStatus.FLAGGED,
                        self_reported=self_reported,
                        ground_truth=ground_truth,
                        delta=abs(ground_truth - self_reported),
                        severity="warn",
                        subject=pid,
                        detail=(
                            f"criterion {criterion.criterion_key}: self-reported "
                            f"evidence_count={self_reported} but actual "
                            f"CriterionEvidence row count={ground_truth} "
                            f"(delta={abs(ground_truth - self_reported)})"
                        ),
                    )
                )
            else:
                ok_count += 1

        if not flagged_criteria and ok_count > 0:
            results.append(
                CheckResult(
                    check="criterion_evidence_count_integrity",
                    status=CheckStatus.OK,
                    self_reported=ok_count,
                    ground_truth=ok_count,
                    delta=0,
                    subject=pid,
                    detail=(
                        f"all {ok_count} criteria have matching evidence counts "
                        "(summary matches independent COUNT(*))"
                    ),
                )
            )

    if not results:
        return [
            CheckResult(
                check="criterion_evidence_count_integrity",
                status=CheckStatus.SKIPPED,
                subject="global",
                detail="no criteria with evidence to audit across all projects",
            )
        ]
    return results


@register_check(
    "criterion_claim_reconciliation",
    "Grade each active acceptance criterion on the shared EvidenceStatus axis "
    "(criterion_evidence_status) and flag those the open version claims it must "
    "prove but whose recorded evidence does not back it (uncovered/partial) — "
    "the strategist-visible '0/N covered, machine 0-for-N' signal. Flag-only.",
)
async def check_criterion_claim_reconciliation(
    ctx: SweepContext,
) -> List[CheckResult]:
    """Ground truth: an ACTIVE acceptance criterion on the open version is a
    standing claim the version must PROVE. Grading it via
    :func:`app.acceptance_criteria.criterion_evidence_status` (the shared axis
    used by gaps/capabilities) and
    :func:`app.evidence_primitive.claim_contradicts_evidence` with the fixed
    done-type ``proven`` claim, a criterion whose recorded evidence does not back
    coverage (``unproven`` / ``partial`` / ``contradicted``) is flagged. This
    makes acceptance criteria a full peer in the shared "evidence met" contract
    (Component 4) — the same tri-state predicate the gap/capability
    reconciliation checks use, rather than the count-only checks.

    DB-only and cheap: it issues no HEAD git calls (unlike the path-backed
    gap/capability reconciliation checks) — a ``covered/attested/backfilled/
    partial/uncovered`` classification of already-loaded evidence rows. Flag-only
    and side-effect-free in every mode: it NEVER mutates a criterion or its
    evidence. Bounded to ``ctx.max_projects`` projects; SKIPPED for projects with
    no open version or no active criteria.
    """
    from app.acceptance_criteria import (
        criterion_evidence_status,
        get_criteria_for_version,
        get_evidence_for_criteria_batch,
    )
    from app.evidence_primitive import claim_contradicts_evidence
    from app.models import Project
    from app.release_version import current_open_version

    projects = (
        await ctx.session.execute(select(Project.id).limit(ctx.max_projects))
    ).all()
    if not projects:
        return [
            CheckResult(
                check="criterion_claim_reconciliation",
                status=CheckStatus.SKIPPED,
                subject="global",
                detail="no projects to audit",
            )
        ]

    results: List[CheckResult] = []
    any_checked = False
    for (project_id,) in projects:
        pid = str(project_id)
        open_version = await current_open_version(ctx.session, project_id)
        if open_version is None:
            results.append(
                CheckResult(
                    check="criterion_claim_reconciliation",
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
                    check="criterion_claim_reconciliation",
                    status=CheckStatus.SKIPPED,
                    subject=pid,
                    detail="no active criteria on open version",
                )
            )
            continue

        any_checked = True
        evidence_by_cid = await get_evidence_for_criteria_batch(
            ctx.session, [c.id for c in active_criteria]
        )
        flagged: List[str] = []
        for criterion in active_criteria:
            rows = evidence_by_cid.get(criterion.id, [])
            evidence_status = criterion_evidence_status(rows)
            # An active criterion is a standing "must be proven" claim; grade it
            # with the fixed done-type claim so an uncovered/partial/contradicted
            # criterion is flagged on the same axis as gaps/capabilities.
            if claim_contradicts_evidence("proven", evidence_status):
                flagged.append(f"{criterion.criterion_key}={evidence_status}")

        audited = len(active_criteria)
        if flagged:
            preview = ", ".join(flagged[:3])
            if len(flagged) > 3:
                preview += f" (+{len(flagged) - 3} more)"
            results.append(
                CheckResult(
                    check="criterion_claim_reconciliation",
                    status=CheckStatus.FLAGGED,
                    self_reported=audited,
                    ground_truth=audited - len(flagged),
                    delta=len(flagged),
                    severity="warn",
                    subject=pid,
                    detail=(
                        f"{len(flagged)}/{audited} active criterion(s) claim the "
                        f"open version must prove them but evidence does not back "
                        f"coverage: {preview}"
                    ),
                )
            )
        else:
            results.append(
                CheckResult(
                    check="criterion_claim_reconciliation",
                    status=CheckStatus.OK,
                    self_reported=audited,
                    ground_truth=audited,
                    delta=0,
                    subject=pid,
                    detail=(
                        f"all {audited} active criterion(s) reconciled; every "
                        "criterion's evidence backs its coverage claim"
                    ),
                )
            )

    if not any_checked and not results:
        return [
            CheckResult(
                check="criterion_claim_reconciliation",
                status=CheckStatus.SKIPPED,
                subject="global",
                detail="no active criteria on any open version to reconcile",
            )
        ]
    return results


@register_check(
    "capability_proof_gate_gap",
    "Scan each project's working capabilities and flag per-project the count whose "
    "proof_status is not 'proven'. Auto-resolves proof gaps when backing_paths are "
    "present and verifiable; for caps with no backing_paths, auto-discovers and "
    "proposes candidate backing paths by name (auto-resolving when a candidate set "
    "is test-covered, else folding the proposals into the enrolled research "
    "obligation); surfaces the remaining caps for strategist action.",
)
async def check_capability_proof_gate_gap(ctx: SweepContext) -> List[CheckResult]:
    """Ground truth: a capability declared ``working`` that carries no proven
    evidence is a claim the repo asserts without machine-backed verification.

    This check auto-resolves gaps where possible:
    - If an unproven working capability declares ``backing_paths`` and ALL resolve
      at HEAD via :func:`_path_exists_at_head`, proof is recorded and the
      capability moves from the gap to ``ground_truth``. Two recorded tiers, both
      structural-only: when a convention-named test file for EVERY backing path is
      present at HEAD and imports it (:func:`_named_tests_covering_paths`, a STATIC
      check — the test is never executed), a ``named_test_present`` proof is
      recorded via :func:`record_capability_named_test_proof`; otherwise a
      ``backing_path`` proof (:func:`record_capability_backing_path_proof`) is the
      fallback. :func:`classify_evidence_tiered` grades BOTH ``path_proven`` — NOT
      ``proven`` — so neither raises auto-graduation coverage nor clears the
      enforce gate (auto-graduation reads proof status through
      :func:`working_capability_proof_status`, which grades these ``path_proven``).
      A capability reaches ``proven`` only via a real green merge-test acceptance
      verdict (:func:`app.capability_proof.record_capability_proof`).
    - Caps with no file-like backing_paths trigger automated backing-path
      DISCOVERY (:func:`_discover_candidate_backing_paths`): search tokens derived
      from the capability name locate candidate files at HEAD. Discovered
      candidates are held to the SAME confidence bar as declared backing_paths —
      only a fully test-covered candidate set (:func:`_named_tests_covering_paths`)
      auto-records a ``named_test_present`` proof and moves the cap to
      ``ground_truth`` (still ``path_proven`` tier — see above).
      Otherwise the candidates are surfaced as concrete SUGGESTIONS folded into
      the enrolled research obligation (enforce mode) and the cap stays in the
      gap — discovery never mutates the strategist-curated backing_paths and
      never records a proof from unverified candidates.
    - Caps with no file-like backing_paths and no usable discovery are surfaced
      with ``needs_backing_paths`` in the detail for strategist/research discovery.
    - Caps whose backing_paths fail verification remain in ``delta``.

    After auto-resolving proofs for a project the session is committed; a failed
    commit for one project is logged and rolled back without aborting the check.

    * ``self_reported`` = total ``working`` capabilities in the project.
    * ``ground_truth``  = caps with a true machine-verified ``proven`` status only;
      ``path_proven`` self-heal tiers (structural backing-path or static
      named_test_present checks) are NOT counted here because they do not clear
      the enforce gate and do not represent an executed green merge-test verdict.
    * ``delta``         = unresolved gap: needs_backing_paths + backing_paths that
      failed HEAD verification + caps that already carry only a path_proven proof.

    Bounded to ``ctx.max_projects`` projects.
    """
    import uuid as _uuid

    from app.capability_proof import (
        _discover_candidate_backing_paths,
        _maybe_enroll_proof_trial,
        _named_tests_covering_paths,
        _read_file_at_head,
        record_capability_backing_path_proof,
        record_capability_named_test_proof,
    )
    from app.capability_registry import (
        exempt_capability_names,
        normalize_capability_name,
        working_capability_proof_status,
    )
    from app.models import Project
    from app.source_ref_validation import _looks_like_file_path, _path_exists_at_head

    # Load full ORM objects so record_capability_backing_path_proof can flag_modified.
    projects = (
        await ctx.session.execute(
            select(Project).limit(ctx.max_projects)
        )
    ).scalars().all()
    if not projects:
        return [
            CheckResult(
                check="capability_proof_gate_gap",
                status=CheckStatus.SKIPPED,
                subject="global",
                detail="no projects to audit",
            )
        ]

    results: List[CheckResult] = []
    any_working = False
    for project in projects:
        pid = str(project.id)
        pm = project.problem_model if isinstance(project.problem_model, dict) else {}
        caps_list = pm.get("capabilities") if pm else None
        if not isinstance(caps_list, list) or not caps_list:
            results.append(
                CheckResult(
                    check="capability_proof_gate_gap",
                    status=CheckStatus.SKIPPED,
                    subject=pid,
                    detail="no capabilities in problem_model",
                )
            )
            continue

        proof_by_name = working_capability_proof_status(pm)
        exempt_set = exempt_capability_names(pm)
        if not proof_by_name:
            results.append(
                CheckResult(
                    check="capability_proof_gate_gap",
                    status=CheckStatus.SKIPPED,
                    subject=pid,
                    detail="no working capabilities to audit",
                )
            )
            continue

        any_working = True
        total_working = len(proof_by_name)
        exempt_count = len(exempt_set)
        # Exempt caps already classify as "proven" — exclude them from the
        # unproven set and the delta so they don't inflate the gap count.
        unproven_names = [
            name for name, status in proof_by_name.items() if status != "proven"
        ]
        proven_count = total_working - len(unproven_names)

        if not unproven_names:
            _ok_detail = (
                f"all {total_working} working capability(ies) carry proven evidence"
            )
            if exempt_count:
                _ok_detail += (
                    f" ({exempt_count} exempt: "
                    + ", ".join(sorted(exempt_set)[:5])
                )
                if exempt_count > 5:
                    _ok_detail += f" +{exempt_count - 5} more"
                _ok_detail += ")"
            results.append(
                CheckResult(
                    check="capability_proof_gate_gap",
                    status=CheckStatus.OK,
                    self_reported=total_working,
                    ground_truth=proven_count,
                    delta=0,
                    subject=pid,
                    detail=_ok_detail,
                )
            )
            continue

        # Build lookup: normalized-name → (raw_name, backing_paths) from raw caps.
        cap_backing: dict[str, tuple[str, list]] = {}
        for cap in caps_list:
            if not isinstance(cap, dict):
                continue
            raw_name = cap.get("name")
            if not isinstance(raw_name, str):
                continue
            norm = normalize_capability_name(raw_name)
            if not norm:
                continue
            bp = cap.get("backing_paths")
            cap_backing[norm] = (raw_name, bp if isinstance(bp, list) else [])

        # Attempt auto-resolution for each unproven capability. The self-heal now
        # only ever records the structural ``backing_path`` tier (finding #2 — no
        # named_test minting from static coverage), so all auto-resolutions are
        # backing_path-tier.
        auto_resolved: list[str] = []
        named_test_present_resolved: list[str] = []
        trials_enrolled: list[str] = []
        needs_backing_paths: list[str] = []
        # Distinct unresolved buckets — kept separate so the FLAGGED detail is
        # truthful about WHY each cap is still a gap (a dedup no-op that already
        # carries a path_proven proof is NOT the same as a declared backing path
        # that fails HEAD verification):
        #   verification_failed — declared file-like backing_paths that do not
        #     resolve at HEAD (stale/moved paths) or a proof-recording exception;
        #     genuinely unresolvable by self-heal, so these also enroll research.
        #   already_recorded — a proof was already present (record was a dedup
        #     no-op); the cap carries path_proven evidence and needs no research
        #     (only a real green merge verdict reaches ``proven``).
        verification_failed: list[str] = []
        already_recorded: list[str] = []

        for cap_name in unproven_names:
            raw_name, backing_paths = cap_backing.get(cap_name, (cap_name, []))
            file_paths = [
                p for p in backing_paths
                if isinstance(p, str) and _looks_like_file_path(p)
            ]
            if not file_paths:
                needs_backing_paths.append(cap_name)
                continue

            # Verify every file-like backing path at HEAD.
            # Any None (indeterminate) or False (absent) prevents proof recording.
            all_present = True
            for p in file_paths:
                try:
                    exists = await _path_exists_at_head(p)
                except Exception:  # noqa: BLE001 — verification failure is non-fatal
                    exists = None
                if exists is not True:
                    all_present = False
                    break

            if all_present:
                # When a convention-named test file for EVERY backing path is
                # present at HEAD and imports it, record a ``named_test_present``
                # proof. This discovery is STATIC (file exists + import regex; the
                # test is never executed), so classify_evidence_tiered grades it
                # ``path_proven`` — the SAME tier as the ``backing_path`` fallback,
                # NOT ``proven``: it does NOT raise auto-graduation coverage nor
                # clear the enforce gate. Only a real green merge-test verdict
                # (record_capability_proof) reaches the ``proven`` named_test tier.
                try:
                    test_refs = await _named_tests_covering_paths(
                        file_paths,
                        verify_path=_path_exists_at_head,
                        read_head=_read_file_at_head,
                    )
                except Exception:  # noqa: BLE001 — test discovery failure is non-fatal
                    logger.warning(
                        "[METRICS_INTEGRITY] capability_proof_gate_gap: "
                        "named-test discovery failed for cap=%s pid=%s",
                        cap_name,
                        pid,
                        exc_info=True,
                    )
                    test_refs = []

                try:
                    if test_refs:
                        # Slice 3 (design 84021e2d): under the execution-trials
                        # flag, ENROLL a bounded capability_proof_trial that RUNS
                        # the covering tests (a green run mints the real ``proven``
                        # tier) instead of minting the static named_test_present
                        # proof. Flag-off / degraded falls through unchanged.
                        _trial = await _maybe_enroll_proof_trial(
                            ctx.session,
                            project,
                            raw_name or cap_name,
                            cap_name,
                            test_refs,
                        )
                        if _trial in ("enrolled", "fresh"):
                            trials_enrolled.append(cap_name)
                            continue
                        recorded = record_capability_named_test_proof(
                            project, raw_name or cap_name, test_refs
                        )
                        if recorded:
                            auto_resolved.append(cap_name)
                            named_test_present_resolved.append(cap_name)
                        else:
                            # Dedup: named_test_present proof already present.
                            already_recorded.append(cap_name)
                    else:
                        recorded = record_capability_backing_path_proof(
                            project, raw_name or cap_name
                        )
                        if recorded:
                            auto_resolved.append(cap_name)
                        else:
                            # Dedup: proof already present (path_proven tier, not
                            # fully proven).
                            already_recorded.append(cap_name)
                except Exception:  # noqa: BLE001 — proof recording is non-fatal
                    logger.warning(
                        "[METRICS_INTEGRITY] capability_proof_gate_gap: "
                        "proof recording failed for cap=%s pid=%s",
                        cap_name,
                        pid,
                        exc_info=True,
                    )
                    verification_failed.append(cap_name)
            else:
                verification_failed.append(cap_name)

        # DISCOVERY pass: for caps with no declared file-like backing_paths, try
        # to discover candidate backing paths at HEAD by name. Apply the SAME
        # confidence bar as the declared-backing_paths self-heal above: a fully
        # test-covered candidate set records a ``named_test`` proof (auto-resolve,
        # moving the cap to ground_truth); otherwise the candidates become
        # concrete suggestions folded into the enrolled research obligation and
        # the cap stays in the gap. This NEVER mutates the strategist-curated
        # backing_paths and NEVER records a proof from unverified candidates.
        # Runs before the commit below so newly-minted proofs are persisted.
        discovery_suggestions: dict[str, list[str]] = {}
        if needs_backing_paths:
            remaining_needs: list[str] = []
            for cap_name in needs_backing_paths:
                raw_name = cap_backing.get(cap_name, (cap_name, []))[0]
                try:
                    candidates = await _discover_candidate_backing_paths(cap_name)
                except Exception:  # noqa: BLE001 — discovery must not abort the check
                    logger.warning(
                        "[METRICS_INTEGRITY] capability_proof_gate_gap: backing-path "
                        "discovery failed for cap=%s pid=%s",
                        cap_name,
                        pid,
                        exc_info=True,
                    )
                    candidates = []
                if not candidates:
                    remaining_needs.append(cap_name)
                    continue

                # Same named_test_present confidence bar as declared backing_paths.
                try:
                    test_refs = await _named_tests_covering_paths(
                        candidates,
                        verify_path=_path_exists_at_head,
                        read_head=_read_file_at_head,
                    )
                except Exception:  # noqa: BLE001 — test discovery failure is non-fatal
                    logger.warning(
                        "[METRICS_INTEGRITY] capability_proof_gate_gap: named-test "
                        "discovery failed for discovered cap=%s pid=%s",
                        cap_name,
                        pid,
                        exc_info=True,
                    )
                    test_refs = []

                if test_refs:
                    # Slice 3 (design 84021e2d): enroll an execution trial for the
                    # discovered-covered candidate set under the flag; a green run
                    # earns the real ``proven`` tier. Flag-off/degraded falls
                    # through to the unchanged static named_test_present recording.
                    _trial = await _maybe_enroll_proof_trial(
                        ctx.session,
                        project,
                        raw_name or cap_name,
                        cap_name,
                        test_refs,
                    )
                    if _trial in ("enrolled", "fresh"):
                        trials_enrolled.append(cap_name)
                        continue
                    try:
                        recorded = record_capability_named_test_proof(
                            project, raw_name or cap_name, test_refs
                        )
                    except Exception:  # noqa: BLE001 — proof recording is non-fatal
                        logger.warning(
                            "[METRICS_INTEGRITY] capability_proof_gate_gap: proof "
                            "recording failed for discovered cap=%s pid=%s",
                            cap_name,
                            pid,
                            exc_info=True,
                        )
                        recorded = False
                    if recorded:
                        auto_resolved.append(cap_name)
                        named_test_present_resolved.append(cap_name)
                        continue
                    # Dedup/failure: fall through and keep the cap as a gap, but
                    # still surface the candidates as research suggestions.
                else:
                    # Candidates present at HEAD (guaranteed by
                    # _discover_candidate_backing_paths) but no covering test
                    # file: apply the SAME backing_path fallback as the
                    # declared-backing_paths self-heal above. The confidence bar
                    # is identical — the discovered paths already passed
                    # _path_exists_at_head — so record a ``backing_path`` proof
                    # (graded ``path_proven``, NOT ``proven``). A successful
                    # record auto-resolves the cap; a dedup no-op means a
                    # path_proven proof already exists, so keep it in the gap
                    # with the candidates folded into the research obligation.
                    try:
                        recorded = record_capability_backing_path_proof(
                            project, raw_name or cap_name
                        )
                    except Exception:  # noqa: BLE001 — proof recording is non-fatal
                        logger.warning(
                            "[METRICS_INTEGRITY] capability_proof_gate_gap: "
                            "backing-path proof recording failed for discovered "
                            "cap=%s pid=%s",
                            cap_name,
                            pid,
                            exc_info=True,
                        )
                        recorded = False
                    if recorded:
                        auto_resolved.append(cap_name)
                        continue
                    # Dedup/failure: fall through and keep the cap as a gap, but
                    # still surface the candidates as research suggestions.

                # Candidates found but not test-covered (or record was a no-op):
                # keep the cap in the gap and remember the suggestions so the
                # enrolled research obligation starts from a real candidate list.
                discovery_suggestions[cap_name] = candidates
                remaining_needs.append(cap_name)
            needs_backing_paths = remaining_needs

        # Commit auto-resolved proofs for this project.
        if auto_resolved:
            try:
                await ctx.session.commit()
            except Exception as exc:  # noqa: BLE001 — commit failure must not abort check
                logger.warning(
                    "[METRICS_INTEGRITY] capability_proof_gate_gap: commit failed "
                    "for project %s after auto-resolving %d cap(s): %s",
                    pid,
                    len(auto_resolved),
                    exc,
                )
                with contextlib.suppress(Exception):
                    await ctx.session.rollback()
                # The commit failed, so no proof was actually persisted — treat
                # these as verification_failed (unresolved + needing research),
                # NOT already_recorded (which implies a durable path_proven proof).
                verification_failed.extend(auto_resolved)
                auto_resolved.clear()

        # ENFORCE mode only: for caps that cannot be auto-proven (no file-like
        # backing_paths, no proof), enroll ONE deduped per-capability research
        # obligation so the evergreen fill loop later spawns a proof-earn probe.
        # Never crashes the check: a per-enroll failure is logged + rolled back
        # and the sweep continues; enrolled rows are committed in their own
        # commit (the auto-resolve proofs, if any, already committed above).
        # Both the no-backing-paths caps AND caps whose declared backing_paths
        # fail HEAD verification are genuine gaps self-heal cannot close on its
        # own — enroll research for both so a stale/moved backing path is
        # investigated rather than flagged forever. already_recorded caps are
        # excluded: they already carry a path_proven proof and only a real green
        # merge verdict can advance them to ``proven``.
        enrolled_research: list[str] = []
        research_needed = needs_backing_paths + verification_failed
        if ctx.mode == "enforce" and research_needed:
            # Reconstruct the UUID from the captured pid string so we never touch
            # a (possibly commit-expired) ORM attribute after the auto-resolve commit.
            project_uuid = _uuid.UUID(pid)
            for cap_name in research_needed:
                try:
                    if await _enroll_capability_proof_research(
                        ctx.session,
                        project_id=project_uuid,
                        cap_name=cap_name,
                        candidate_paths=discovery_suggestions.get(cap_name),
                    ):
                        enrolled_research.append(cap_name)
                except Exception:  # noqa: BLE001 — enrollment must not abort the check
                    logger.warning(
                        "[METRICS_INTEGRITY] capability_proof_gate_gap: "
                        "proof-research enroll failed for cap=%s pid=%s",
                        cap_name,
                        pid,
                        exc_info=True,
                    )
                    with contextlib.suppress(Exception):
                        await ctx.session.rollback()
            if enrolled_research:
                try:
                    await ctx.session.commit()
                except Exception as exc:  # noqa: BLE001 — commit failure must not abort check
                    logger.warning(
                        "[METRICS_INTEGRITY] capability_proof_gate_gap: enroll "
                        "commit failed for project %s after enrolling %d research "
                        "obligation(s): %s",
                        pid,
                        len(enrolled_research),
                        exc,
                    )
                    with contextlib.suppress(Exception):
                        await ctx.session.rollback()

        # path_proven self-heals (backing_path / static named_test_present) are
        # NOT machine-verified proof — they do not clear the enforce gate and
        # must not inflate ground_truth. Only caps with true 'proven' status count.
        new_proven = proven_count
        # Tier split of the auto-resolutions this sweep: named_test_present caps
        # were recorded from a statically-covered path set; the remainder fell
        # back to the weaker structural backing_path tier. Both are path_proven,
        # but the split is surfaced structured so operators can see whether the
        # self-heal is minting any named_test_present coverage or only backing_path.
        # (This is reporting only — it never feeds ground_truth above.)
        auto_resolved_nt = len(named_test_present_resolved)
        auto_resolved_bp = len(auto_resolved) - auto_resolved_nt
        # Caps with an enrolled execution trial are NOT yet proven — the trial
        # must run green first — so they still count as an unresolved gap this
        # sweep, but are reported distinctly (a real proof is en route) rather
        # than as a static resolution or a verification failure.
        unresolved = (
            needs_backing_paths
            + verification_failed
            + already_recorded
            + trials_enrolled
        )

        if unresolved:
            detail_parts: list[str] = []
            if trials_enrolled:
                preview_te = ", ".join(sorted(trials_enrolled)[:3])
                if len(trials_enrolled) > 3:
                    preview_te += f" (+{len(trials_enrolled) - 3} more)"
                detail_parts.append(
                    f"{len(trials_enrolled)} execution trial(s) enrolled "
                    f"(pending green run → proven): {preview_te}"
                )
            if auto_resolved:
                preview_ar = ", ".join(sorted(auto_resolved)[:3])
                if len(auto_resolved) > 3:
                    preview_ar += f" (+{len(auto_resolved) - 3} more)"
                _tier_note = (
                    f" ({len(named_test_present_resolved)} via named_test_present, "
                    f"{len(auto_resolved) - len(named_test_present_resolved)} via backing_paths)"
                    if named_test_present_resolved
                    else " via backing_paths"
                )
                detail_parts.append(
                    f"{len(auto_resolved)} auto-resolved{_tier_note}: {preview_ar}"
                )
            if needs_backing_paths:
                preview_nb = ", ".join(sorted(needs_backing_paths)[:3])
                if len(needs_backing_paths) > 3:
                    preview_nb += f" (+{len(needs_backing_paths) - 3} more)"
                _nb_part = (
                    f"{len(needs_backing_paths)} need backing_paths "
                    f"(needs_backing_paths): {preview_nb}"
                )
                if discovery_suggestions:
                    _nb_part += (
                        f" ({len(discovery_suggestions)} with discovered candidate "
                        "path suggestion(s) folded into research obligations)"
                    )
                detail_parts.append(_nb_part)
            if verification_failed:
                preview_vf = ", ".join(sorted(verification_failed)[:3])
                if len(verification_failed) > 3:
                    preview_vf += f" (+{len(verification_failed) - 3} more)"
                detail_parts.append(
                    f"{len(verification_failed)} backing_paths failed verification: "
                    f"{preview_vf}"
                )
            if already_recorded:
                preview_ir = ", ".join(sorted(already_recorded)[:3])
                if len(already_recorded) > 3:
                    preview_ir += f" (+{len(already_recorded) - 3} more)"
                detail_parts.append(
                    f"{len(already_recorded)} already carry a path_proven proof "
                    f"(awaiting a real merge verdict to reach proven): {preview_ir}"
                )
            _detail = (
                f"{len(unresolved)} working capability(ies) remain unproven "
                "after self-heal: " + "; ".join(detail_parts)
            )
            if exempt_count:
                _detail += (
                    f"; {exempt_count} exempt (not counted in delta): "
                    + ", ".join(sorted(exempt_set)[:5])
                )
                if exempt_count > 5:
                    _detail += f" (+{exempt_count - 5} more)"
            results.append(
                CheckResult(
                    check="capability_proof_gate_gap",
                    status=CheckStatus.FLAGGED,
                    self_reported=total_working,
                    ground_truth=new_proven,
                    delta=len(unresolved),
                    severity="warn",
                    subject=pid,
                    detail=_detail,
                    auto_resolved=len(auto_resolved),
                    auto_resolved_backing_path=auto_resolved_bp,
                    auto_resolved_named_test_present=auto_resolved_nt,
                )
            )
        else:
            _ok_detail = (
                f"all {total_working} working capability(ies) carry proven evidence"
            )
            if auto_resolved:
                if named_test_present_resolved:
                    _ok_detail += (
                        f" ({len(auto_resolved)} auto-resolved: "
                        f"{len(named_test_present_resolved)} via named_test_present, "
                        f"{len(auto_resolved) - len(named_test_present_resolved)} "
                        "via backing_paths)"
                    )
                else:
                    _ok_detail += (
                        f" ({len(auto_resolved)} auto-resolved via backing_paths)"
                    )
            if exempt_count:
                _ok_detail += (
                    f" ({exempt_count} exempt: "
                    + ", ".join(sorted(exempt_set)[:5])
                )
                if exempt_count > 5:
                    _ok_detail += f" +{exempt_count - 5} more"
                _ok_detail += ")"
            results.append(
                CheckResult(
                    check="capability_proof_gate_gap",
                    status=CheckStatus.OK,
                    self_reported=total_working,
                    ground_truth=new_proven,
                    delta=0,
                    subject=pid,
                    detail=_ok_detail,
                    auto_resolved=len(auto_resolved),
                    auto_resolved_backing_path=auto_resolved_bp,
                    auto_resolved_named_test_present=auto_resolved_nt,
                )
            )

    if not any_working and not any(r.flagged for r in results):
        return [
            CheckResult(
                check="capability_proof_gate_gap",
                status=CheckStatus.SKIPPED,
                subject="global",
                detail="no projects with working capabilities found",
            )
        ]
    return results


async def compute_fleet_proof_coverage(
    session: Any, max_projects: int, now: Optional[datetime] = None
) -> Dict[str, Any]:
    """Compute the fleet-wide capability-proof coverage ratio.

    This is the exact quantity the auto-graduation thresholds
    (``capability_proof_gate_auto_graduation_observe_threshold`` /
    ``..._enforce_threshold``) are compared against, extracted so it can be
    surfaced read-only to the operator (e.g. the settings endpoint) as well as
    driving :func:`check_capability_proof_gate_auto_graduation`.

    Coverage = total proven working caps / total non-exempt working caps.
    Exempt capabilities are excluded from BOTH numerator and denominator, and a
    project with working caps that are all exempt does not contribute.

    Bounded by ``max_projects`` (mirrors the sweep's ``ctx.max_projects``).

    Returns a dict with:

    * ``num_projects`` — projects examined (bounded by ``max_projects``)
    * ``projects_contributing`` — projects with >=1 non-exempt working cap
    * ``contributing_project_ids`` — ids of those contributing projects (used by
      the auto-graduation check to notify each contributing project's operator)
    * ``total_non_exempt_working`` — coverage denominator
    * ``total_proven`` — coverage numerator
    * ``coverage_ratio`` — ``total_proven / total_non_exempt_working`` as a
      float, or ``None`` when the denominator is zero (ratio undefined)
    """
    from app.capability_registry import (
        exempt_capability_names,
        working_capability_proof_status,
    )
    from app.models import Project

    # Snapshot ``now`` ONCE for the whole fleet scan and thread it explicitly into
    # every ``working_capability_proof_status`` call below. PROOF_FRESHNESS_TTL
    # demotion is applied against this single instant, so a proof older than the TTL
    # is NOT counted toward ``total_proven`` — stale proofs cannot silently inflate
    # graduation coverage. Passing an explicit ``now`` (rather than relying on the
    # per-call ``_utcnow()`` default) also keeps the freshness cutoff deterministic
    # across all projects in one scan instead of drifting mid-loop.
    if now is None:
        now = _utcnow()

    # ``capability_proof_records`` (the platform-only ledger) is loaded alongside
    # the problem_model so an operator-recorded exemption is honored here: without
    # it, ``exempt_capability_names`` defaults ``proof_records`` to None (an EMPTY
    # ledger) and a genuinely unprovable-but-exempted working capability counts
    # against the denominator forever, so fleet coverage can never reach the enforce
    # threshold and the gate stalls. Threading the ledger in grades a platform-
    # exempted capability ``proven`` AND drops it from the denominator, matching the
    # every-other gate seam (:func:`exempt_capability_names`). A bare agent-writable
    # ``proof_exempt`` flag still does NOT exclude — only the platform ledger does.
    projects = (
        await session.execute(
            select(
                Project.id,
                Project.problem_model,
                Project.capability_proof_records,
            ).limit(max_projects)
        )
    ).all()

    total_non_exempt_working = 0
    total_proven = 0
    projects_contributing = 0
    contributing_project_ids: list = []

    for project_id, raw_pm, raw_records in projects:
        pm = raw_pm if isinstance(raw_pm, dict) else {}
        proof_records = raw_records if isinstance(raw_records, dict) else {}
        proof_by_name = working_capability_proof_status(
            pm, now=now, proof_records=proof_records
        )
        if not proof_by_name:
            continue
        exempt_set = exempt_capability_names(pm, proof_records)
        # Exclude exempt capabilities from both numerator and denominator.
        non_exempt = {
            name: status
            for name, status in proof_by_name.items()
            if name not in exempt_set
        }
        if not non_exempt:
            # Project has working caps but all are exempt — exclude from aggregate.
            continue
        projects_contributing += 1
        contributing_project_ids.append(project_id)
        total_non_exempt_working += len(non_exempt)
        total_proven += sum(1 for s in non_exempt.values() if s == "proven")

    coverage_ratio = (
        total_proven / total_non_exempt_working
        if total_non_exempt_working > 0
        else None
    )

    return {
        "num_projects": len(projects),
        "projects_contributing": projects_contributing,
        "contributing_project_ids": contributing_project_ids,
        "total_non_exempt_working": total_non_exempt_working,
        "total_proven": total_proven,
        "coverage_ratio": coverage_ratio,
    }


@register_check(
    "capability_proof_gate_auto_graduation",
    "Compute fleet-wide capability proof coverage and automatically advance the "
    "capability proof gate mode (off→observe→enforce) when coverage crosses the "
    "configured thresholds. Exempt capabilities are excluded from the calculation. "
    "Only advances, never regresses. Disabled by default — opt-in via "
    "capability_proof_gate_auto_graduation_enabled.",
)
async def check_capability_proof_gate_auto_graduation(
    ctx: SweepContext,
) -> List[CheckResult]:
    """Ground truth: compute proof coverage across all projects and, when the
    auto-graduation feature is enabled, advance the capability proof gate mode
    (off→observe→enforce) as coverage crosses the configured thresholds.

    * Coverage = total proven working caps / total non-exempt working caps
    * Exempt capabilities are excluded from both numerator and denominator
    * Projects with zero non-exempt working caps are excluded from the aggregate
    * Auto-graduation is disabled by default — operators must opt in
    * Only advances (off→observe→enforce), never regresses
    * Advances persist to the database so they survive restarts

    Returns a CheckResult with coverage ratio, threshold, and whether advancement
    occurred. Idempotent — running twice with the same coverage advances at most once.
    """
    from app.models import DeploymentState
    from app.shared_state import runtime_config as _runtime_config

    # Read auto-graduation config from runtime_config.
    auto_enabled = bool(
        _runtime_config.get("capability_proof_gate_auto_graduation_enabled", False)
    )
    if not auto_enabled:
        return [
            CheckResult(
                check="capability_proof_gate_auto_graduation",
                status=CheckStatus.OK,
                subject="global",
                detail="auto-graduation disabled (capability_proof_gate_auto_graduation_enabled=false)",
            )
        ]

    observe_threshold = float(
        _runtime_config.get(
            "capability_proof_gate_auto_graduation_observe_threshold", 0.5
        )
    )
    enforce_threshold = float(
        _runtime_config.get(
            "capability_proof_gate_auto_graduation_enforce_threshold", 0.95
        )
    )
    current_mode = str(
        _runtime_config.get("capability_proof_gate_mode", "off") or "off"
    ).lower()

    # Already at enforce — nothing to do.
    if current_mode == "enforce":
        return [
            CheckResult(
                check="capability_proof_gate_auto_graduation",
                status=CheckStatus.OK,
                subject="global",
                detail="gate already at enforce (maximum); no further advancement possible",
            )
        ]

    # Compute fleet-wide proof coverage (shared with the settings endpoint).
    coverage = await compute_fleet_proof_coverage(ctx.session, ctx.max_projects)
    if coverage["num_projects"] == 0:
        return [
            CheckResult(
                check="capability_proof_gate_auto_graduation",
                status=CheckStatus.SKIPPED,
                subject="global",
                detail="no projects to compute coverage from",
            )
        ]

    total_non_exempt_working = coverage["total_non_exempt_working"]
    total_proven = coverage["total_proven"]
    projects_contributing = coverage["projects_contributing"]
    contributing_project_ids: list = coverage["contributing_project_ids"]

    if total_non_exempt_working == 0:
        return [
            CheckResult(
                check="capability_proof_gate_auto_graduation",
                status=CheckStatus.SKIPPED,
                subject="global",
                detail="no non-exempt working capabilities across fleet; nothing to graduate on",
            )
        ]

    coverage_ratio = coverage["coverage_ratio"]

    # Determine if advancement is warranted.
    advance_to: str | None = None
    if current_mode == "off" and coverage_ratio >= observe_threshold:
        advance_to = "observe"
    elif current_mode == "observe" and coverage_ratio >= enforce_threshold:
        advance_to = "enforce"

    if advance_to is None:
        # Coverage below the threshold for the next step — no advancement.
        threshold = observe_threshold if current_mode == "off" else enforce_threshold
        return [
            CheckResult(
                check="capability_proof_gate_auto_graduation",
                status=CheckStatus.OK,
                self_reported=total_non_exempt_working,
                ground_truth=total_proven,
                delta=total_non_exempt_working - total_proven,
                subject="global",
                detail=(
                    f"coverage={coverage_ratio:.4f} (threshold={threshold}, "
                    f"mode={current_mode}, projects={projects_contributing}); "
                    f"below threshold — no advancement"
                ),
            )
        ]

    # Advance the gate mode — update in-memory config.
    _runtime_config["capability_proof_gate_mode"] = advance_to
    logger.info(
        "[METRICS-INTEGRITY] capability proof gate auto-graduated: "
        "%s → %s (coverage=%.4f, observe_threshold=%.2f, enforce_threshold=%.2f, "
        "projects=%d, total_non_exempt=%d, total_proven=%d)",
        current_mode,
        advance_to,
        coverage_ratio,
        observe_threshold,
        enforce_threshold,
        projects_contributing,
        total_non_exempt_working,
        total_proven,
    )

    # Persist the new gate mode to the database so it survives restarts.
    try:
        state = await ctx.session.get(DeploymentState, 1)
        if state is not None:
            config_json = getattr(state, "runtime_config_json", None)
            if isinstance(config_json, dict):
                config_json["capability_proof_gate_mode"] = advance_to
            else:
                config_json = {"capability_proof_gate_mode": advance_to}
            state.runtime_config_json = config_json
            ctx.session.add(state)
            await ctx.session.commit()
    except Exception:
        logger.warning(
            "[METRICS-INTEGRITY] failed to persist capability proof gate "
            "auto-graduation to DB; in-memory value is %s",
            advance_to,
            exc_info=True,
        )

    # Notify the operator of this autonomous gate-mode change. The advance
    # (especially → enforce, which begins BLOCKING task spawning and release
    # promotion pipeline-wide) was previously visible only in an INFO log.
    # Route one operator-visible message per contributing project through the
    # unified message-audience system. Best-effort — a post failure must never
    # abort the check or roll back the already-persisted gate advancement.
    try:
        from app.notification_dispatch import (
            post_capability_proof_gate_graduated_message,
        )

        advance_threshold = (
            observe_threshold if current_mode == "off" else enforce_threshold
        )
        for pid in contributing_project_ids:
            await post_capability_proof_gate_graduated_message(
                ctx.session,
                pid,
                from_mode=current_mode,
                to_mode=advance_to,
                coverage=coverage_ratio,
                threshold=advance_threshold,
                projects_contributing=projects_contributing,
                total_non_exempt=total_non_exempt_working,
                total_proven=total_proven,
            )
        if contributing_project_ids:
            await ctx.session.commit()
    except Exception:
        logger.warning(
            "[METRICS-INTEGRITY] failed to post operator notification for "
            "capability proof gate auto-graduation (%s → %s); gate advancement "
            "itself is unaffected",
            current_mode,
            advance_to,
            exc_info=True,
        )

    # Resolve the dormant-knob debt for capability_proof_gate_mode when advancing
    # to enforce (the knob's "off" / "observe" value meant an open obligation).
    if advance_to == "enforce":
        try:
            from app.completion_debt import aresolve_completion_debt

            await aresolve_completion_debt(
                ctx.session,
                "dormant_knob:capability_proof_gate_mode",
                reason="graduated:auto_graduation",
            )
        except Exception:
            logger.warning(
                "[METRICS-INTEGRITY] failed to resolve dormant-knob debt for "
                "capability_proof_gate_mode after auto-graduation to enforce",
                exc_info=True,
            )

    return [
        CheckResult(
            check="capability_proof_gate_auto_graduation",
            status=CheckStatus.FLAGGED,
            self_reported=total_non_exempt_working,
            ground_truth=total_proven,
            delta=total_non_exempt_working - total_proven,
            severity="info",
            subject="global",
            detail=(
                f"auto-graduated gate from {current_mode} → {advance_to} "
                f"(coverage={coverage_ratio:.4f}, threshold="
                f"{observe_threshold if current_mode == 'off' else enforce_threshold}, "
                f"projects={projects_contributing})"
            ),
        )
    ]
