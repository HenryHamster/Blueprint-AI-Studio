"""Path-based layer classifier, backfill helper, and observation alerts.

Extracted from ``pipeline_regression_dispatcher`` as a single cohesive
responsibility:

  * ``_classify_layers_from_paths`` — map changed file paths to canonical
    infrastructure-layer tags.
  * ``_backfill_layers_targeted_once`` — populate ``layers_targeted`` on
    fix-log entries that lack it (idempotent recovery-scan helper).
  * ``compute_unclassified_fix_coverage`` — aggregate path-classifier
    coverage across recent fix-log entries.
  * ``_evaluate_path_classifier_coverage`` — open a project-scoped Alert
    when classifier coverage is stale.
  * ``_evaluate_awaiting_backfill_alert`` — open a project-scoped Alert
    when the cross-layer ``awaiting_backfill`` backlog stops draining.
  * ``_escalate_stale_observation_alerts`` — second-stage escalator that
    opens a remediation case for observation-only alerts open past SLA.

The dispatcher re-exports each public name so existing
``from app.pipeline_regression_dispatcher import X`` callers keep working.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from app.dispatcher_formatters import _parse_dispatcher_dt
from app.models import (
    Alert,
    AlertSeverity,
    RegressionCaseStatus,
    RegressionRemediationCase,
    Task,
    TaskStatus,
)
from app.payload_schemas import validate_internal_payload
from app.pipeline_health import _LONE_CHECKER_ALERT_TITLE
from app.regression_dispatcher_metrics import _LAYER_DEFENSE_LEDGER_LOOKBACK_DAYS
from app.shared_state import (
    get_project_integration_branch as _get_project_integration_branch,
)
from app.time_utils import _to_naive_utc, _utcnow

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Path-based layer classification (for fix-log layers_targeted backfill)
# ---------------------------------------------------------------------------

# Bound for how many cases to backfill per dispatcher tick. The recovery scan
# runs every 60s and each call hits the GitHub API once per case; capping
# prevents a multi-hundred-case backlog from saturating the API budget.
_LAYER_BACKFILL_PER_TICK = 5

# Cap for the per-entry ``unmatched_paths`` list written by
# ``_backfill_layers_targeted_once`` when classification yields ``[]``. Bounds
# the JSON column on noisy PRs so a single 200-file refactor cannot bloat the
# row.
_UNMATCHED_PATHS_PER_ENTRY_CAP = 20

# Path-classifier coverage signal thresholds. The signal trips when the share
# of recently-merged fix-log entries that classified to ``[]`` exceeds the
# rate over a minimum sample. Both are module-level so tests can monkeypatch.
_PATH_CLASSIFIER_COVERAGE_MIN_SAMPLE = 5
_PATH_CLASSIFIER_COVERAGE_RATE_THRESHOLD = 0.20
# Top-N most-frequent unmatched paths to surface in the alert message.
_PATH_CLASSIFIER_COVERAGE_TOP_PATHS = 10
# Stable Alert title used for dedup so a recurring stale-classifier condition
# does not pile up duplicate alerts each cycle.
_PATH_CLASSIFIER_COVERAGE_ALERT_TITLE = "Path-layer classifier coverage stale"

# Awaiting-backfill backlog alert thresholds. Cross-layer compliance buckets
# fix-log entries with spawn_was_cross_layer=True and merged_at set but no
# layers_targeted into ``awaiting_backfill``. The 24h grace inside
# ``compute_cross_layer_compliance`` already separates transient (recent
# merges, backfill not yet retried) from systemic backlog. The thresholds
# below trip the operator alert when EITHER signal indicates the backlog has
# stopped draining: 5 entries past the 24h grace, OR an oldest entry older
# than 3 days. Both point at the same likely causes — GitHub auth failures in
# ``_backfill_layers_targeted_once`` (which logs and continues silently) or
# path-classifier coverage gaps that yield empty layer assignments.
_AWAITING_BACKFILL_COUNT_THRESHOLD = 5
_AWAITING_BACKFILL_AGE_THRESHOLD = timedelta(days=3)
# Window mirrors the GET /regression-cases/cross-layer-compliance default.
_AWAITING_BACKFILL_WINDOW = timedelta(days=30)
_AWAITING_BACKFILL_ALERT_TITLE = "Cross-layer awaiting_backfill backlog stuck"

# Second-stage escalation for observation-only alerts that stay open past SLA.
# When an alert with one of the monitored titles has been open continuously for
# longer than the threshold, the dispatcher opens a RegressionRemediationCase and
# spawns one investigate task so the autonomous loop can remediate the root cause.
# Reuses case dedup (unique regression_class per project) so a single escalation
# is opened per stuck alert regardless of how many dispatcher cycles fire.
_OBSERVATION_ALERT_SLA_HOURS_DEFAULT = 72
# Per-title overrides — key is the alert title, value is SLA hours. Falls back
# to _OBSERVATION_ALERT_SLA_HOURS_DEFAULT when a title is not listed here.
_OBSERVATION_ALERT_SLA_HOURS: dict[str, int] = {}
# Titles of observation-only alerts that are eligible for second-stage escalation.
# admin_digest alert titles are imported lazily inside the function to avoid cycles.
_OBSERVATION_ONLY_ALERT_TITLES = [
    _PATH_CLASSIFIER_COVERAGE_ALERT_TITLE,
    _AWAITING_BACKFILL_ALERT_TITLE,
]


def _classify_layers_from_paths(paths: list[str]) -> list[str]:
    """Map a list of changed file paths to canonical infrastructure-layer tags.

    Returns a sorted, deduplicated subset of ``ALL_LAYERS`` from
    ``regression_layer_classifier``. Paths that don't match any rule are
    silently dropped — empty result is meaningful ("none of the canonical
    layers were touched") and is preserved as ``[]`` for the caller.

    Rules align with the pattern matcher in ``regression_layer_classifier``
    so message-based and path-based classifications use the same vocabulary:
      * ``.github/workflows/**`` → CI_WORKFLOW
      * ``infra/packer/**``, ``setup.sh``, ``scripts/build*`` → AGENT_VM
      * ``agent/src/handlers/**``, ``agent/src/index.ts`` → AGENT_VERB
      * ``backend/app/review_pipeline.py``, ``backend/app/auto_review.py``,
        ``agent/src/handlers/review-fix.*``, ``agent/src/prompts/self-review.ts``,
        ``.claude/skills/review*``, ``.claude/skills/security-review*`` → REVIEWER_PASS
      * ``backend/testing/module_size_limits.json``, lint configs
        (``eslint.config.js``, ``ruff.toml``, ``pyproject.toml``),
        ``backend/app/source_ref_validation.py``, ``check`` script,
        ``backend/app/fix_loop_telemetry.py`` → BASELINE_GATE
    """
    from app.regression_layer_classifier import (
        LAYER_AGENT_VERB,
        LAYER_AGENT_VM,
        LAYER_BASELINE_GATE,
        LAYER_CI_WORKFLOW,
        LAYER_REVIEWER_PASS,
        LAYER_VIEWPORT,
    )

    tags: set[str] = set()
    for raw in paths or []:
        if not raw or not isinstance(raw, str):
            continue
        # Strip a leading "./" prefix only — must NOT use lstrip("./") because
        # that would also chop the leading "." off paths like ".github/...".
        p = raw.strip()
        if p.startswith("./"):
            p = p[2:]

        # CI_WORKFLOW
        if p.startswith(".github/workflows/"):
            tags.add(LAYER_CI_WORKFLOW)

        # AGENT_VM — VM image, system-level provisioning
        if (
            p.startswith("infra/packer/")
            or p.startswith("infra/terraform/")
            or p == "setup.sh"
            or p.startswith("scripts/build")
            or p == "Dockerfile"
            or p.endswith("/Dockerfile")
        ):
            tags.add(LAYER_AGENT_VM)

        # REVIEWER_PASS — checked BEFORE AGENT_VERB so review-fix handlers
        # in agent/src/handlers/ classify as REVIEWER_PASS, not AGENT_VERB.
        is_reviewer = (
            p == "backend/app/review_pipeline.py"
            or p == "backend/app/auto_review.py"
            or p.startswith("backend/app/sr_")
            or p == "agent/src/prompts/self-review.ts"
            or p.startswith("agent/src/handlers/review-fix")
            or ".claude/skills/review" in p
            or ".claude/skills/security-review" in p
        )
        if is_reviewer:
            tags.add(LAYER_REVIEWER_PASS)

        # AGENT_VERB — handler invocation / verb dispatch / scope check
        if not is_reviewer and (
            p.startswith("agent/src/handlers/")
            or p == "agent/src/index.ts"
            or p == "agent/src/prompt.ts"
            or p.startswith("agent/src/prompts/")
        ):
            tags.add(LAYER_AGENT_VERB)

        # BASELINE_GATE — lint/scope/size/baseline configs and gate code
        if (
            p == "backend/testing/module_size_limits.json"
            or p == "eslint.config.js"
            or p.endswith("/eslint.config.js")
            or p == "admin-ui/eslint.config.js"
            or p == "ruff.toml"
            or p == "pyproject.toml"
            or p == "backend/app/source_ref_validation.py"
            or p == "backend/app/fix_loop_telemetry.py"
            or p == "check"
        ):
            tags.add(LAYER_BASELINE_GATE)

        # VIEWPORT — viewport test infrastructure and check script
        if (
            p == "check-viewport.sh"
            or p == "admin-ui/src/test/viewport.ts"
            or (p.startswith("admin-ui/src/") and p.endswith(".viewport.test.tsx"))
        ):
            tags.add(LAYER_VIEWPORT)

    return sorted(tags)


async def _backfill_layers_targeted_once(
    session: AsyncSession,
    project_id: uuid.UUID,
) -> int:
    """Populate ``layers_targeted`` on fix-log entries that lack it.

    Idempotent backfill helper invoked from ``run_recovery_once``. Iterates
    cases for ``project_id`` whose ``fingerprint_to_fix_log`` contains entries
    with ``fix_pr_number`` set but no ``layers_targeted`` populated. For each
    such entry, fetches the PR's changed files via the existing GitHub helper
    and writes ``layers_targeted`` (a list, possibly empty) back onto the entry.

    Returns the number of entries mutated. Bounded to
    ``_LAYER_BACKFILL_PER_TICK`` cases per call so backlog cannot starve the
    rest of the dispatcher tick. GitHub auth / API failures are logged and
    skipped — never raised — so a transient outage never breaks recovery.
    """
    from sqlalchemy.orm.attributes import flag_modified

    from app.github import get_pr_files, get_repo_installation_token
    from app.models import Project

    project = await session.get(Project, project_id)
    if project is None or not project.github_owner or not project.github_repo:
        return 0

    rows = (
        (
            await session.execute(
                select(RegressionRemediationCase).where(
                    RegressionRemediationCase.project_id == project_id,
                )
            )
        )
        .scalars()
        .all()
    )

    # Find cases with at least one entry missing layers_targeted.
    pending: list[RegressionRemediationCase] = []
    for case in rows:
        log = case.fingerprint_to_fix_log or []
        for entry in log:
            if not isinstance(entry, dict):
                continue
            if entry.get("fix_pr_number") and "layers_targeted" not in entry:
                pending.append(case)
                break
        if len(pending) >= _LAYER_BACKFILL_PER_TICK:
            break

    if not pending:
        return 0

    token: str | None = None
    try:
        token = await get_repo_installation_token(
            project.github_owner, project.github_repo
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "[REGRESSION_DISPATCHER] layers backfill skipped — token fetch failed for "
            "project=%s: %s",
            project_id,
            exc,
        )
        return 0
    if not token:
        return 0

    mutated = 0
    for case in pending:
        log = list(case.fingerprint_to_fix_log or [])
        log_changed = False
        for entry in log:
            if not isinstance(entry, dict):
                continue
            pr = entry.get("fix_pr_number")
            if not pr or "layers_targeted" in entry:
                continue
            try:
                files = await get_pr_files(
                    token,
                    project.github_owner,
                    project.github_repo,
                    int(pr),
                    project_id=str(project_id),
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "[REGRESSION_DISPATCHER] layers backfill skipped PR #%s for "
                    "project=%s case=%s: %s",
                    pr,
                    project_id,
                    case.id,
                    exc,
                )
                continue
            paths = [str(f.get("filename") or "") for f in files if isinstance(f, dict)]
            classified = _classify_layers_from_paths(paths)
            entry["layers_targeted"] = classified
            # When the path classifier returned [] but at least one path was
            # supplied, persist the unmatched paths so the coverage-staleness
            # signal can name them. Cap the list so a 200-file refactor cannot
            # bloat the JSON column. When classification is non-empty, do NOT
            # write unmatched_paths — by definition every path that didn't
            # match a rule is irrelevant to the matched layers.
            non_empty_paths = [p for p in paths if p]
            if not classified and non_empty_paths:
                entry["unmatched_paths"] = non_empty_paths[
                    :_UNMATCHED_PATHS_PER_ENTRY_CAP
                ]
            log_changed = True
            mutated += 1
        if log_changed:
            case.fingerprint_to_fix_log = log
            flag_modified(case, "fingerprint_to_fix_log")
            session.add(case)

    if mutated:
        await session.commit()
    return mutated


def compute_unclassified_fix_coverage(
    cases: list[RegressionRemediationCase],
    *,
    lookback_days: int = _LAYER_DEFENSE_LEDGER_LOOKBACK_DAYS,
) -> dict[str, Any]:
    """Aggregate path-classifier coverage across recent fix-log entries.

    Walks ``RegressionRemediationCase.fingerprint_to_fix_log`` entries within
    the same ``merged_at`` lookback window used by ``compute_layer_defense_density``
    and ``_build_layer_defense_ledger``. Counts entries where classification
    was attempted (``layers_targeted`` present OR ``unmatched_paths`` present)
    toward ``total_qualifying_entries``; among those, counts entries where
    ``layers_targeted`` is the empty list (or absent) toward
    ``entries_with_empty_layers``.

    Pre-classification entries (neither key present) are NOT counted in
    either numerator or denominator — the coverage signal only fires once
    backfill has actually run.

    Returns a dict with:
      * ``total_qualifying_entries`` (int)
      * ``entries_with_empty_layers`` (int)
      * ``unclassified_fix_rate`` (float; 0.0 when total < min sample)
      * ``top_unmatched_paths`` (list of (path, count) tuples, most-frequent
        first, capped at ``_PATH_CLASSIFIER_COVERAGE_TOP_PATHS``)
    """
    now = _utcnow()
    cutoff = now - timedelta(days=lookback_days)

    total_qualifying = 0
    empty_layers = 0
    path_freq: dict[str, int] = {}

    for case in cases or []:
        log = case.fingerprint_to_fix_log or []
        for entry in log:
            if not isinstance(entry, dict):
                continue
            merged_dt = _parse_dispatcher_dt(entry.get("merged_at"))
            if merged_dt is None or merged_dt < cutoff:
                continue
            has_layers = "layers_targeted" in entry
            has_unmatched = "unmatched_paths" in entry
            if not (has_layers or has_unmatched):
                continue
            total_qualifying += 1
            layers = entry.get("layers_targeted")
            is_empty_layers = (has_layers and not layers) or (
                not has_layers and has_unmatched
            )
            if is_empty_layers:
                empty_layers += 1
                for raw_path in entry.get("unmatched_paths") or []:
                    if not raw_path or not isinstance(raw_path, str):
                        continue
                    path_freq[raw_path] = path_freq.get(raw_path, 0) + 1

    rate = 0.0
    if total_qualifying >= _PATH_CLASSIFIER_COVERAGE_MIN_SAMPLE:
        rate = empty_layers / total_qualifying

    top_paths = sorted(path_freq.items(), key=lambda kv: (-kv[1], kv[0]))[
        :_PATH_CLASSIFIER_COVERAGE_TOP_PATHS
    ]

    return {
        "total_qualifying_entries": total_qualifying,
        "entries_with_empty_layers": empty_layers,
        "unclassified_fix_rate": rate,
        "top_unmatched_paths": top_paths,
    }


async def _evaluate_path_classifier_coverage(
    session: AsyncSession,
    project_id: uuid.UUID,
) -> None:
    """Open a project-scoped Alert when path-classifier coverage is stale.

    Non-spawning observation step — never spawns investigate tasks or consumes
    spawn budget. Wrapped by callers in try/except so a transient failure here
    cannot break the rest of the recovery scan.

    Trips when both:
      * ``total_qualifying_entries >= _PATH_CLASSIFIER_COVERAGE_MIN_SAMPLE``
      * ``unclassified_fix_rate >= _PATH_CLASSIFIER_COVERAGE_RATE_THRESHOLD``

    Dedup mirrors ``_emit_signal_staleness_alerts`` — skip insertion when an
    open Alert with the same project_id, no task_id, WARNING severity, and
    matching title already exists.
    """
    cases = (
        (
            await session.execute(
                select(RegressionRemediationCase).where(
                    RegressionRemediationCase.project_id == project_id,
                )
            )
        )
        .scalars()
        .all()
    )

    coverage = compute_unclassified_fix_coverage(list(cases))

    total = coverage["total_qualifying_entries"]
    empty = coverage["entries_with_empty_layers"]
    rate = coverage["unclassified_fix_rate"]
    top_paths: list[tuple[str, int]] = coverage["top_unmatched_paths"]

    if total < _PATH_CLASSIFIER_COVERAGE_MIN_SAMPLE:
        return
    if rate < _PATH_CLASSIFIER_COVERAGE_RATE_THRESHOLD:
        return

    title = _PATH_CLASSIFIER_COVERAGE_ALERT_TITLE
    existing = (
        (
            await session.execute(
                select(Alert).where(
                    Alert.project_id == project_id,
                    Alert.task_id.is_(None),
                    Alert.severity == AlertSeverity.WARNING,
                    Alert.dismissed_at.is_(None),
                    Alert.title == title,
                )
            )
        )
        .scalars()
        .first()
    )
    if existing is not None:
        return

    path_lines = (
        "\n".join(f"  - {path} ({count}×)" for path, count in top_paths)
        or "  (no unmatched paths recorded yet)"
    )
    message = (
        f"Path-based layer classifier returned [] for {empty}/{total} "
        f"({rate:.0%}) of recently-merged fix-log entries — the canonical "
        f"layer rules in _classify_layers_from_paths are stale relative to "
        f"where the autonomous loop is shipping fixes. Most-frequent "
        f"unmatched paths:\n{path_lines}\n"
        f"Update _classify_layers_from_paths or extend the canonical layer "
        f"list to cover these paths."
    )
    session.add(
        Alert(
            project_id=project_id,
            severity=AlertSeverity.WARNING,
            title=title,
            message=message[:1000],
        )
    )
    await session.commit()


async def _evaluate_awaiting_backfill_alert(
    session: AsyncSession,
    project_id: uuid.UUID,
) -> None:
    """Open a project-scoped Alert when cross-layer awaiting_backfill backlog is stuck.

    Non-spawning observation step — never spawns investigate tasks or consumes
    spawn budget. Wrapped by callers in try/except so a transient failure here
    cannot break the rest of the recovery scan.

    Trips when EITHER:
      * ``awaiting_backfill_over_threshold_count >= _AWAITING_BACKFILL_COUNT_THRESHOLD``
      * ``awaiting_backfill_oldest_merged_at`` is older than ``_AWAITING_BACKFILL_AGE_THRESHOLD``

    Dedup mirrors ``_evaluate_path_classifier_coverage`` — skip insertion when
    an open Alert with the same project_id, no task_id, WARNING severity, and
    matching title already exists.
    """
    # Pure metric helper extracted into digest_metrics; imported lazily because
    # digest_metrics imports from this module at function scope, so a module-level
    # import here could create a load-time cycle.
    from app.digest_metrics import compute_cross_layer_compliance

    cutoff = _utcnow() - _AWAITING_BACKFILL_WINDOW
    cases = (
        (
            await session.execute(
                select(RegressionRemediationCase).where(
                    RegressionRemediationCase.project_id == project_id,
                )
            )
        )
        .scalars()
        .all()
    )

    metrics = compute_cross_layer_compliance(list(cases), cutoff)

    awaiting_count = int(metrics.get("awaiting_backfill_count") or 0)
    over_threshold = int(metrics.get("awaiting_backfill_over_threshold_count") or 0)
    oldest_iso = metrics.get("awaiting_backfill_oldest_merged_at")
    oldest_dt: datetime | None = None
    if oldest_iso:
        try:
            oldest_dt = _to_naive_utc(
                datetime.fromisoformat(str(oldest_iso).replace("Z", "+00:00"))
            )
        except (ValueError, TypeError):
            oldest_dt = None

    age_trip = (
        oldest_dt is not None
        and (_utcnow() - oldest_dt) >= _AWAITING_BACKFILL_AGE_THRESHOLD
    )
    count_trip = over_threshold >= _AWAITING_BACKFILL_COUNT_THRESHOLD
    if not (age_trip or count_trip):
        return

    title = _AWAITING_BACKFILL_ALERT_TITLE
    existing = (
        (
            await session.execute(
                select(Alert).where(
                    Alert.project_id == project_id,
                    Alert.task_id.is_(None),
                    Alert.severity == AlertSeverity.WARNING,
                    Alert.dismissed_at.is_(None),
                    Alert.title == title,
                )
            )
        )
        .scalars()
        .first()
    )
    if existing is not None:
        return

    oldest_str = oldest_iso if oldest_iso else "(none)"
    message = (
        f"Cross-layer awaiting_backfill backlog is not draining: "
        f"{awaiting_count} entries total, {over_threshold} older than 24h, "
        f"oldest merged_at={oldest_str}. Likely causes: (1) GitHub auth "
        f"failures in _backfill_layers_targeted_once (which logs and "
        f"continues silently — check logs for token/installation errors), "
        f"(2) path-classifier coverage gaps in _classify_layers_from_paths "
        f"that yield empty layer assignments and never advance to a "
        f"layers_targeted value. Backlog growth corrupts the "
        f"cross-layer-compliance numerator/denominator surfaced at "
        f"GET /api/regression-cases/cross-layer-compliance."
    )
    session.add(
        Alert(
            project_id=project_id,
            severity=AlertSeverity.WARNING,
            title=title,
            message=message[:1000],
        )
    )
    await session.commit()


async def _escalate_stale_observation_alerts(
    session: AsyncSession,
    project_id: uuid.UUID,
) -> int:
    """Second-stage escalation for observation-only alerts open past SLA.

    For each WARNING alert whose title appears in ``_OBSERVATION_ONLY_ALERT_TITLES``
    (or is imported from admin_digest) and which has been open continuously for
    longer than ``_OBSERVATION_ALERT_SLA_HOURS_DEFAULT`` (or its per-title override),
    the dispatcher opens a RegressionRemediationCase and spawns one investigate task.
    Uses case dedup via a stable ``regression_class`` derived from the alert title so
    exactly one escalation is opened per stuck alert. Records ``source_alert_id`` in
    ``signal_snapshot_at_spawn`` so downstream consumers can dismiss the alert when the
    case closes. Returns the number of new investigate tasks spawned.
    """
    from app.models import Project as _EscProject  # noqa: PLC0415

    _esc_project = await session.get(_EscProject, project_id)
    monitored_titles = set(_OBSERVATION_ONLY_ALERT_TITLES) | {_LONE_CHECKER_ALERT_TITLE}
    now = _utcnow()

    open_alerts = (
        (
            await session.execute(
                select(Alert).where(
                    Alert.project_id == project_id,
                    Alert.task_id.is_(None),
                    Alert.severity == AlertSeverity.WARNING,
                    Alert.dismissed_at.is_(None),
                    Alert.title.in_(list(monitored_titles)),
                )
            )
        )
        .scalars()
        .all()
    )

    spawned = 0
    for alert in open_alerts:
        sla_hours = _OBSERVATION_ALERT_SLA_HOURS.get(
            alert.title, _OBSERVATION_ALERT_SLA_HOURS_DEFAULT
        )
        elapsed = now - alert.created_at
        if elapsed < timedelta(hours=sla_hours):
            continue

        regression_class = f"observation_alert_sla:{alert.title}"[:512]
        existing_case = (
            (
                await session.execute(
                    select(RegressionRemediationCase).where(
                        RegressionRemediationCase.project_id == project_id,
                        RegressionRemediationCase.regression_class == regression_class,
                        RegressionRemediationCase.status.in_(
                            [
                                RegressionCaseStatus.ACTIVE,
                                RegressionCaseStatus.REMEDIATING,
                                RegressionCaseStatus.MERGED,
                                RegressionCaseStatus.NEEDS_MANUAL_INTERVENTION,
                            ]
                        ),
                    )
                )
            )
            .scalars()
            .first()
        )
        if existing_case is not None:
            continue

        elapsed_hours = elapsed.total_seconds() / 3600
        prompt = (
            f"Observation-only alert '{alert.title}' has been open for "
            f"{elapsed_hours:.1f}h (SLA: {sla_hours}h) without operator remediation.\n\n"
            f"Alert message:\n{alert.message}\n\n"
            f"Diagnose the root cause and ship a fix so this alert clears. "
            f"Do NOT simply dismiss the alert — resolve the underlying condition."
        )
        payload = validate_internal_payload(
            {
                "type": "investigate",
                "prompt": prompt,
                "base_branch": _get_project_integration_branch(_esc_project),
                "effort": "low",
            },
            "pipeline_regression_dispatcher:observation_alert_sla_escalation",
        )
        context: dict[str, Any] = {
            "spawned_by": "pipeline_regression_dispatcher_observation_sla",
            "regression_class": regression_class,
            "signal_name": "observation_alert_sla",
            "source_alert_id": str(alert.id),
            "alert_title": alert.title,
            "elapsed_hours": round(elapsed_hours, 1),
        }
        new_task = Task(
            project_id=project_id,
            title=f"Investigate stuck alert: {alert.title}"[:200],
            description=(
                f"Observation-only alert open {elapsed_hours:.1f}h past "
                f"{sla_hours}h SLA — autonomous escalation."
            )[:500],
            payloads=[payload],
            context=context,
            vm_instance_id=f"agent-vm-{uuid.uuid4().hex}",
            status=TaskStatus.QUEUED,
            created_by="ORCHESTRATOR",
        )
        session.add(new_task)
        await session.flush()

        signal_snapshot: dict[str, Any] = {
            "signal_name": "observation_alert_sla",
            "source_alert_id": str(alert.id),
            "alert_title": alert.title,
            "elapsed_hours": round(elapsed_hours, 1),
            "sla_hours": sla_hours,
            "captured_at": now.isoformat(),
        }
        session.add(
            RegressionRemediationCase(
                project_id=project_id,
                regression_class=regression_class,
                status=RegressionCaseStatus.ACTIVE,
                signal_snapshot_at_spawn=signal_snapshot,
                last_remediation_task_id=new_task.id,
                last_seen_at=now,
                hit_count=1,
                signal_first_crossed_at=alert.created_at,
                signal_first_seen_at=alert.created_at,
                investigate_spawned_at=now,
            )
        )
        session.add(
            Alert(
                project_id=project_id,
                severity=AlertSeverity.INFO,
                title=f"Escalating stuck observation alert: {alert.title}"[:200],
                message=(
                    f"Alert '{alert.title}' open {elapsed_hours:.1f}h. "
                    f"Spawned investigate task {new_task.id}."
                )[:1000],
            )
        )
        spawned += 1

    if spawned:
        await session.commit()
    return spawned
