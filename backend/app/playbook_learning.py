"""Auto-generate playbook entries from repeated failure patterns.

When the same failure signature appears 3+ times in 7 days for a project,
a playbook entry is automatically created to surface the pattern to future
agents.  Entries are marked 'auto-generated' to distinguish them from
agent-written entries.

Threshold and window are configurable via runtime_config:
  auto_playbook_threshold  (default 3)
  auto_playbook_window_days (default 7)

`cache_first_attempt_classification` is intended to be invoked at
first-review-rejection time by the lifecycle pipeline (sibling cluster wires it
in) so subsequent reads from admin_digest can skip the keyword scan.
"""

import json
import logging
import uuid
from datetime import datetime
from datetime import timedelta
from typing import Any, Optional

from sqlalchemy import func
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import load_only
from sqlalchemy.orm.attributes import flag_modified
from sqlmodel import select

from app import sr_analytics as _sr_analytics
from app.models import (
    Project,
    Task,
    TaskStatus,
    _utcnow,
)
from app.sr_blind_spots import _REVIEWER_ORIGINS  # noqa: F401  # re-exported for backward compatibility

# Playbook entry lifecycle (pending-source finalize, suppression/resurrection,
# retirement, follow/confirm bookkeeping, stale-ref flagging) lives in
# ``app.playbook_lifecycle``.  Re-exported here so callers and tests that
# patch via ``app.playbook_learning.<name>`` keep working unchanged.
from app.playbook_lifecycle import (  # noqa: F401
    _AUTO_PLAYBOOK_RESURRECTION_COOLDOWN_DAYS,
    _FILE_PATH_RE,
    _IDLE_RETIRE_DAYS,
    _RETIRE_MAX_SUCCESS_RATE,
    _RETIRE_MIN_FOLLOWED,
    _STALE_AGE_DAYS,
    _STALE_CONFIRM_THRESHOLD,
    _extract_file_paths,
    _get_prior_archive_entry,
    _is_auto_playbook_suppressed,
    check_and_flag_stale_refs,
    count_idle_auto_retired_within,
    count_low_success_retired_within,
    record_playbook_applied,
    record_playbook_confirmed,
    retire_ineffective_patterns,
)

# Canonical storage helpers live in ``app.playbook_storage``.  Re-exported here
# so existing callers and tests that patch via ``app.playbook_learning.<name>``
# keep working unchanged.
from app.playbook_storage import (  # noqa: F401
    _PLAYBOOK_CAPACITY_BYTES,
    _playbook_size_bytes,
    finalize_pending_playbook_entries,
    maybe_archive_for_capacity,
    retire_idle_auto_entries,
)

# In-app Alert row title prefixes for SR catch-rate alerts. The router that
# powers AlertsBanner returns Alert rows verbatim; we use these prefixes both
# to format new rows and to find/dismiss prior matching rows on clear-transition.
_SR_REGRESSION_ALERT_TITLE_PREFIX = "Self-review catch-rate regression"
_SR_FLOOR_ALERT_TITLE_PREFIX = "Self-review catch-rate below floor"
_SR_QUADRANT_ALERT_TITLE_PREFIX = "Self-review precision/recall shift"
_PASS_ENGAGEMENT_THINNING_ALERT_TITLE_PREFIX = "PASS reasoning thinning"
# Cap how many regressing categories/severities we name in an Alert title
# before truncating; the full breakdown lives in `Alert.message`.
_SR_ALERT_TITLE_MAX_PARTS = 5
# z=1.28 ≈ 80% CI two-tailed; used by significance-gated SR trend helpers below.
_TREND_Z_MIN = 1.28


def _format_sr_alert_title(prefix: str, parts: list[str]) -> str:
    """Compose an Alert title from a prefix and a list of regressing identifiers."""
    if not parts:
        return prefix
    if len(parts) > _SR_ALERT_TITLE_MAX_PARTS:
        head = ", ".join(parts[:_SR_ALERT_TITLE_MAX_PARTS])
        joined = f"{head}, +{len(parts) - _SR_ALERT_TITLE_MAX_PARTS} more"
    else:
        joined = ", ".join(parts)
    return f"{prefix}: {joined}"


_EXAMPLE_MAX_PER_CAT = _sr_analytics._EXAMPLE_MAX_PER_CAT
_FALSE_ATTESTATION_RATE_THRESHOLD = _sr_analytics._FALSE_ATTESTATION_RATE_THRESHOLD
_HALLUCINATION_RATE_MIN_FINDINGS = _sr_analytics._HALLUCINATION_RATE_MIN_FINDINGS
_MIN_DIRECTION_SR_SAMPLE = _sr_analytics._MIN_DIRECTION_SR_SAMPLE
_MIN_SR_FINDINGS = _sr_analytics._MIN_SR_FINDINGS
_MIN_SR_SAMPLE = _sr_analytics._MIN_SR_SAMPLE
_SENSITIVE_TOKENS = _sr_analytics._SENSITIVE_TOKENS
_compute_cat_severity_calibration = _sr_analytics._compute_cat_severity_calibration
_compute_cat_fp_stats = _sr_analytics._compute_cat_fp_stats
_compute_miss_latency_stats = _sr_analytics._compute_miss_latency_stats
_stats_for_tasks = _sr_analytics._stats_for_tasks
compute_direction_sr_breakdown_for_endpoint = (
    _sr_analytics.compute_direction_sr_breakdown_for_endpoint
)
compute_edges_trend = _sr_analytics.compute_edges_trend
compute_fresh_reviewer_trend = _sr_analytics.compute_fresh_reviewer_trend
compute_sr_catch_rate_by_direction = _sr_analytics.compute_sr_catch_rate_by_direction
compute_sr_catch_rate_trends = _sr_analytics.compute_sr_catch_rate_trends
compute_sr_false_attestation_trends = _sr_analytics.compute_sr_false_attestation_trends
compute_sr_quality_by_fingerprint = _sr_analytics.compute_sr_quality_by_fingerprint
compute_sr_regression_risk_test_signal_trends = (
    _sr_analytics.compute_sr_regression_risk_test_signal_trends
)
compute_sr_reviewer_disagreements = _sr_analytics.compute_sr_reviewer_disagreements
get_sr_alert_drilldown_tasks = _sr_analytics.get_sr_alert_drilldown_tasks
select_sr_miss_examples_for_injection = (
    _sr_analytics.select_sr_miss_examples_for_injection
)

# Capacity-management constants/functions are in playbook_capacity.py (imported above).
# Entry-lifecycle constants (_RETIRE_MIN_FOLLOWED, _IDLE_RETIRE_DAYS,
# _AUTO_PLAYBOOK_RESURRECTION_COOLDOWN_DAYS, _STALE_AGE_DAYS,
# _STALE_CONFIRM_THRESHOLD, _FILE_PATH_RE) live in playbook_lifecycle.py
# and are re-exported above for backward compatibility.

# Review-extracted playbook entries are refreshed in-place when their
# updated_at exceeds _REVIEW_PLAYBOOK_REFRESH_DAYS. Stale-flagging and the
# broader storage lifecycle live in app.playbook_lifecycle and are re-exported
# here for backward compatibility.
_REVIEW_PLAYBOOK_REFRESH_DAYS = 30

# Execution-evidence SR check categories. These track whether the agent
# actually ran verification commands (pytest, lint, typecheck) and got exit 0.
# Miss rate = fraction of tasks where the agent reported passed: false.
_EXECUTION_EVIDENCE_CATEGORIES = frozenset(
    ["tests_executed_cleanly", "lint_executed_cleanly", "typecheck_executed_cleanly"]
)

# _FILE_PATH_RE is re-exported above from playbook_lifecycle.

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Lifecycle helpers
# ---------------------------------------------------------------------------
# Entry-lifecycle helpers (check_and_flag_stale_refs,
# finalize_pending_playbook_entries, _get_prior_archive_entry,
# _is_auto_playbook_suppressed, record_playbook_applied,
# record_playbook_confirmed, retire_ineffective_patterns,
# retire_idle_auto_entries, count_idle_auto_retired_within,
# count_low_success_retired_within) live in app.playbook_lifecycle and
# are re-exported above.


# ---------------------------------------------------------------------------
# Auto-playbook from failure patterns
# ---------------------------------------------------------------------------
# Failure-signature extraction and the ``_maybe_auto_playbook`` writer were
# extracted to ``app.playbook_auto``.  Re-exported here so callers and tests
# that patch via ``app.playbook_learning._maybe_auto_playbook`` keep working.
from app.playbook_auto import (  # noqa: E402,F401
    _extract_failure_module,
    _extract_failure_signature,
    _hint_for_failure_class,
    _maybe_auto_playbook,
    _signature_to_key,
)
from app.review_learning import (  # noqa: E402,F401
    _DIRECTION_FIRST_ATTEMPT_KEY,
    _FIRST_ATTEMPT_PLAYBOOK_THRESHOLD,
    _FLOOR_MIN_CONSECUTIVE_WINDOWS,
    _MIN_REVIEW_SAMPLE,
    _MODULE_FEEDBACK_DECAY_AGE_DAYS,
    _MODULE_FEEDBACK_DECAY_HALF_LIFE_DAYS,
    _MODULE_FEEDBACK_KEY,
    _MODULE_FEEDBACK_MAX_ENTRIES,
    _PRECISION_RECALL_QUADRANT_THRESHOLD,
    _REVIEW_CATEGORIES,
    _SR_ENGAGEMENT_MIN_PASSES,
    _SR_ENGAGEMENT_THRESHOLD,
    _SR_FLOOR_THRESHOLD,
    _SR_SEVERITY_DELTA_THRESHOLDS,
    _TREND_DELTA_THRESHOLD,
    _VALID_CATEGORIES,
    _accumulate_direction_first_attempt,
    _categorize_review_issues,
    _categorize_review_issues_keyword,
    _decay_module_feedback_entry,
    _keyword_from_flat,
    _maybe_auto_playbook_from_first_attempt,
    _update_module_feedback_profiles,
    cache_first_attempt_classification,
    categorize_review_issues_llm,
    compute_recurring_first_review_descriptions,
    compute_review_effectiveness,
    extract_review_feedback_to_playbook,
    resolve_sr_miss_human_truth,
)


def summarize_decomposition_outcomes(direction: dict) -> Optional[dict]:
    """Summarize decomposition_quality from the last 3 productive research cycles.

    Returns None if there is no decomposition data to summarize.
    """
    history = direction.get("research_history", []) or []
    productive = [h for h in history if h.get("tasks_created", 0) > 0][-3:]
    flat: list[dict] = []
    for cycle in productive:
        dq = cycle.get("decomposition_quality") or []
        if isinstance(dq, list):
            flat.extend(e for e in dq if isinstance(e, dict))
    if not flat:
        return None

    def _nums(key, cast=float):
        return [cast(e[key]) for e in flat if isinstance(e.get(key), (int, float))]

    merge_rates = _nums("subtask_merge_rate")
    files_per = _nums("avg_files_per_subtask")
    max_files_vals = _nums("max_files_in_subtask", int)
    dep_depths = _nums("dependency_depth", int)
    high_xmod = [
        float(e["subtask_merge_rate"])
        for e in flat
        if isinstance(e.get("subtask_merge_rate"), (int, float))
        and isinstance(e.get("cross_module_count"), (int, float))
        and int(e["cross_module_count"]) >= 2
    ]
    low_xmod = [
        float(e["subtask_merge_rate"])
        for e in flat
        if isinstance(e.get("subtask_merge_rate"), (int, float))
        and isinstance(e.get("cross_module_count"), (int, float))
        and int(e["cross_module_count"]) < 2
    ]
    high_corr_low = False
    if high_xmod and low_xmod:
        if (sum(high_xmod) / len(high_xmod)) + 0.1 <= (sum(low_xmod) / len(low_xmod)):
            high_corr_low = True

    out: dict = {
        "prior_decomposition_count": len(flat),
        "high_cross_module_correlates_with_low_merge": high_corr_low,
    }
    if merge_rates:
        out["avg_subtask_merge_rate"] = round(sum(merge_rates) / len(merge_rates), 3)
    if files_per:
        out["avg_files_per_subtask"] = round(sum(files_per) / len(files_per), 2)
    if max_files_vals:
        out["max_files_in_subtask"] = max(max_files_vals)
    if dep_depths:
        out["avg_dependency_depth"] = round(sum(dep_depths) / len(dep_depths), 2)
        out["max_dependency_depth"] = max(dep_depths)
    return out


# ---------------------------------------------------------------------------
# Success-pattern capture
# ---------------------------------------------------------------------------


async def capture_success_pattern(
    task: Task,
    session: AsyncSession,
) -> Optional[str]:
    """Capture a positive pattern from a first-attempt success as a playbook entry.

    Uses a total-completions noise filter: only captures a success pattern after the
    task type has accumulated at least 3 total completions (successful or failed) in the
    project.  This avoids noise from one-off tasks without requiring failures first,
    solving the cold-start problem for new task types like 'migrate' or 'perf'.

    One entry per (task_type, scope) pair — e.g. ``playbook/success-create-small``.
    Each entry tracks success_count, scope_distribution (how many successes per scope
    level), and lightweight metadata: num_files_changed, tests_added, first_attempt_success.

    Returns the short key (e.g. ``"success-create-small"``) if an entry was created or
    updated, ``None`` if the noise filter was not met.
    """
    task_type = task.first_payload.get("type", "")
    if not task_type:
        return None

    scope: str = (task.context or {}).get("scope") or "unspecified"
    related_files: list[str] = list((task.context or {}).get("related_files") or [])
    direction_id: str = (task.context or {}).get("direction_id") or ""
    num_files = len(related_files)

    # Richer implementation-shape signals: file-type distribution and test count.
    file_type_dist: dict[str, int] = {}
    test_file_count = 0
    for f in related_files:
        if (
            "/test_" in f
            or f.startswith("test_")
            or ".test." in f
            or ".spec." in f
            or "/testing/" in f
            or "/tests/" in f
        ):
            test_file_count += 1
        ext = f.rsplit(".", 1)[-1] if "." in f else ""
        if ext and len(ext) <= 6:
            file_type_dist[ext] = file_type_dist.get(ext, 0) + 1

    tests_added = test_file_count > 0

    _proj_row = await session.execute(
        select(Project)
        .where(Project.id == task.project_id)
        .with_for_update(skip_locked=False, key_share=True)
    )
    project = _proj_row.scalar_one_or_none()
    if not project:
        return None

    # Noise filter: require at least 3 total completions (success or failure) for
    # this task type before capturing a success pattern.  Use a COUNT query with
    # JSONB type filter to avoid loading all project tasks into memory.
    prior_count = (
        await session.execute(
            select(func.count())
            .select_from(Task)
            .where(
                Task.project_id == task.project_id,
                Task.status.in_([TaskStatus.COMPLETED, TaskStatus.FAILED]),
                Task.id != task.id,
                Task.payloads[0]["type"].as_string() == task_type,
            )
        )
    ).scalar_one()
    same_type_count = prior_count + 1  # +1 for the current task

    if same_type_count < 3:
        logger.debug(
            "[SUCCESS_PLAYBOOK] project=%s type=%s: only %d completion(s) — noise filter not met",
            task.project_id,
            task_type,
            same_type_count,
        )
        return None

    doc = dict(project.design_doc or {})

    # One entry per (task_type, scope) pair.
    scope_slug = scope.lower().replace(" ", "-")
    key = f"success-{task_type}-{scope_slug}"
    playbook_full_key = f"playbook/{key}"
    files_note = f" Files: {', '.join(related_files[:5])}." if related_files else ""

    existing = doc.get(playbook_full_key)
    if existing and isinstance(existing, dict):
        entry = dict(existing)
        count = entry.get("success_count", 1) + 1
        scope_dist = dict(entry.get("scope_distribution") or {})
        scope_dist[scope] = scope_dist.get(scope, 0) + 1
        raw_dir_succ = entry.get("direction_successes")
        dir_succ = dict(raw_dir_succ) if isinstance(raw_dir_succ, dict) else {}
        if direction_id:
            dir_succ[direction_id] = dir_succ.get(direction_id, 0) + 1
        entry["success_count"] = count
        entry["scope_distribution"] = scope_dist
        entry["direction_successes"] = dir_succ
        entry["num_files_changed"] = num_files
        entry["tests_added"] = tests_added
        entry["test_file_count"] = test_file_count
        entry["file_type_distribution"] = file_type_dist
        entry["updated_at"] = _utcnow().isoformat()
        entry["content"] = (
            f"First-attempt success pattern for {task_type} tasks "
            f"({count} observations, latest scope: {scope}).{files_note} "
            f"Scope distribution: {scope_dist}."
        )
        doc[playbook_full_key] = entry
        action = "updated"
    else:
        content = (
            f"First-attempt success pattern for {task_type} tasks "
            f"(1 observation, scope: {scope}).{files_note} "
            f"Files changed: {num_files}, tests added: {tests_added}."
        )
        new_entry = {
            "content": content,
            "source_task_id": str(task.id),
            "updated_at": _utcnow().isoformat(),
            "confirmed_count": 0,
            "success_count": 1,
            "source": "success-pattern",
            "applicable_types": [task_type],
            "scope": scope,
            "num_files_changed": num_files,
            "tests_added": tests_added,
            "test_file_count": test_file_count,
            "file_type_distribution": file_type_dist,
            "direction_successes": {direction_id: 1} if direction_id else {},
            "first_attempt_success": True,
            "scope_distribution": {scope: 1},
        }
        doc = maybe_archive_for_capacity(doc, len(json.dumps(new_entry).encode()))
        doc[playbook_full_key] = new_entry
        action = "created"

    meta = dict(doc.get("_meta", {"revision": 0}))
    meta["revision"] = meta.get("revision", 0) + 1
    meta["last_updated"] = _utcnow().isoformat()
    meta["updated_by"] = "success-playbook"
    doc["_meta"] = meta

    project.design_doc = doc
    flag_modified(project, "design_doc")
    session.add(project)

    logger.info(
        "[SUCCESS_PLAYBOOK] project=%s %s entry '%s' for task_type=%s scope=%s",
        task.project_id,
        action,
        key,
        task_type,
        scope,
    )
    return key


# Task types where surfacing 'recent successes' patterns at spawn time is useful.
# Other types (review, plan, qa, audit, etc.) treat this signal as noise.
RECENT_SUCCESSES_TASK_TYPES: frozenset[str] = frozenset({"create", "fix", "research"})


def summarize_recent_successes(
    design_doc: dict,
    task_type: str,
    direction_id: Optional[str],
    scope: Optional[str],
    *,
    max_bullets: int = 5,
    char_budget: int = 600,
) -> Optional[str]:
    """Return a markdown 'Recent successes' section drawn from playbook/success-* entries.

    Helper is task-type-agnostic — call sites should gate on
    ``RECENT_SUCCESSES_TASK_TYPES``. Reads only from *design_doc*; performs no
    I/O. Returns None when nothing qualifies so callers can omit the section.

    Ranking: direction-affinity (direction_successes[direction_id] > 0), then
    exact (task_type, scope) match, then ``success_count``, then ``updated_at``
    recency. Entries with success_count < 2 are filtered out.

    Output is hard-capped at *char_budget* by dropping trailing bullets — never
    truncated mid-line. Returns None if even the first bullet doesn't fit.
    """
    if not isinstance(design_doc, dict) or not task_type:
        return None
    prefix = f"playbook/success-{task_type}-"
    candidates: list[tuple[str, dict]] = []
    for k, v in design_doc.items():
        if not isinstance(v, dict) or not k.startswith(prefix):
            continue
        if int(v.get("success_count") or 0) < 2:
            continue
        candidates.append((k, v))
    if not candidates:
        return None

    scope_slug = (scope or "").lower().replace(" ", "-")
    exact_key = f"playbook/success-{task_type}-{scope_slug}" if scope_slug else None

    def _rank(item: tuple[str, dict]) -> tuple:
        k, v = item
        dir_succ = v.get("direction_successes") or {}
        affinity = (
            int(dir_succ.get(direction_id, 0))
            if direction_id and isinstance(dir_succ, dict)
            else 0
        )
        scope_match = 1 if exact_key and k == exact_key else 0
        return (
            affinity,
            scope_match,
            int(v.get("success_count") or 0),
            v.get("updated_at") or "",
        )

    candidates.sort(key=_rank, reverse=True)

    bullets: list[str] = []
    for k, v in candidates[:max_bullets]:
        scope_label = k[len(prefix) :] or "unspecified"
        sc = int(v.get("success_count") or 0)
        parts = [f"{task_type}-{scope_label} ({sc}x)"]
        ftd = v.get("file_type_distribution")
        if isinstance(ftd, dict) and ftd:
            top_exts = sorted(ftd.items(), key=lambda kv: -int(kv[1] or 0))[:3]
            parts.append("files: " + ", ".join(f"{ext}:{n}" for ext, n in top_exts))
        if v.get("tests_added"):
            tfc = int(v.get("test_file_count") or 0)
            parts.append(f"tests added ({tfc})" if tfc else "tests added")
        dir_succ = v.get("direction_successes")
        if direction_id and isinstance(dir_succ, dict):
            local = int(dir_succ.get(direction_id, 0))
            if local > 0:
                parts.append(f"this direction: {local}")
        bullets.append("- " + "; ".join(parts))

    if not bullets:
        return None

    header = "## Recent successes (what has compounded here)"
    out_lines: list[str] = [header]
    out_len = len(header)
    for b in bullets:
        added = len(b) + 1  # +1 for the joining newline
        if out_len + added > char_budget:
            break
        out_lines.append(b)
        out_len += added

    if len(out_lines) == 1:
        return None
    return "\n".join(out_lines)


# ---------------------------------------------------------------------------
# Fix-loop divergence regression alert
# ---------------------------------------------------------------------------


async def check_fix_loop_divergence_regression(
    project_id: uuid.UUID,
    session: AsyncSession,
) -> Optional[dict[str, Any]]:
    """Detect fix-loop divergence rate regression across two consecutive 7-day windows.

    Mirrors _MIN_SR_SAMPLE / _TREND_DELTA_THRESHOLD from compute_sr_catch_rate_trends.
    Idempotent per ISO year-week via project.design_doc["fix_loop_divergence_alerted_window"]
    so a persistent regression alerts at most once per week.
    """
    from app.notification_dispatch import (  # noqa: PLC0415
        post_fix_loop_divergence_regression_message,
    )

    now = _utcnow()
    iso_year, iso_week, _ = now.isocalendar()
    window_key = f"{iso_year}-W{iso_week:02d}"

    _proj_row = await session.execute(
        select(Project)
        .where(Project.id == project_id)
        .with_for_update(skip_locked=False, key_share=True)
    )
    project = _proj_row.scalar_one_or_none()
    if not project:
        return None
    doc = dict(project.design_doc or {})
    if doc.get("fix_loop_divergence_alerted_window") == window_key:
        doc["fix_loop_divergence_signal_last_computed_at"] = now.isoformat()
        project.design_doc = doc
        flag_modified(project, "design_doc")
        session.add(project)
        await session.commit()
        return None

    tasks = (
        (
            await session.execute(
                select(Task)
                .where(
                    Task.project_id == project_id,
                    Task.status.in_([TaskStatus.COMPLETED, TaskStatus.FAILED]),
                    Task.created_at >= now - timedelta(days=14),
                )
                .options(load_only(Task.id, Task.fix_loop_history, Task.created_at))
            )
        )
        .scalars()
        .all()
    )

    def _naive(dt: datetime) -> datetime:
        return dt.replace(tzinfo=None) if dt.tzinfo else dt

    c7 = _naive(now - timedelta(days=7))
    c14 = _naive(now - timedelta(days=14))
    loops = ("lint", "test", "check")

    def _divergence_stats(start: datetime, end: datetime) -> dict[str, dict]:
        s: dict[str, dict] = {loop: {"n": 0, "d": 0} for loop in loops}
        for t in tasks:
            ts = _naive(t.created_at) if t.created_at else None
            if ts is None or not (start <= ts < end):
                continue
            for loop in loops:
                bucket = (t.fix_loop_history or {}).get(loop) or []
                if not bucket:
                    continue
                last_exit: Optional[str] = None
                for ev in reversed(bucket):
                    reason = ev.get("exit_reason") if isinstance(ev, dict) else None
                    if reason and reason != "in_progress":
                        last_exit = reason
                        break
                if last_exit is None:
                    last_exit = (
                        bucket[-1].get("exit_reason")
                        if isinstance(bucket[-1], dict)
                        else None
                    )
                s[loop]["n"] += 1
                if last_exit and last_exit.startswith("divergence_"):
                    s[loop]["d"] += 1
        return s

    curr = _divergence_stats(c7, datetime(9999, 12, 31))
    prev = _divergence_stats(c14, c7)

    regressions: list[dict] = []
    for loop in loops:
        cn, cd = curr[loop]["n"], curr[loop]["d"]
        pn, pd = prev[loop]["n"], prev[loop]["d"]
        if cn < _MIN_SR_SAMPLE or pn < _MIN_SR_SAMPLE:
            continue
        if cd / cn - pd / pn >= _TREND_DELTA_THRESHOLD:
            regressions.append(
                {
                    "loop": loop,
                    "prior_rate": round(pd / pn, 3),
                    "current_rate": round(cd / cn, 3),
                    "prior_count": pn,
                    "current_count": cn,
                }
            )

    if not regressions:
        return None

    await post_fix_loop_divergence_regression_message(session, project_id, regressions)
    doc["fix_loop_divergence_alerted_window"] = window_key
    doc["fix_loop_divergence_regressions"] = regressions
    doc["fix_loop_divergence_detected_at"] = now.isoformat()
    doc["fix_loop_divergence_signal_last_computed_at"] = now.isoformat()
    project.design_doc = doc
    flag_modified(project, "design_doc")
    session.add(project)
    await session.commit()
    logger.info(
        "[FIX_LOOP_DIVERGENCE] project=%s loops=%s",
        project_id,
        [r["loop"] for r in regressions],
    )
    return {"regressions": regressions}
