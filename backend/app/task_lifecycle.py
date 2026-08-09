"""Parent-child task lifecycle orchestration.

Extracted from main.py to reduce its size.  Contains the core task state
machine: _process_post_commit, _check_and_trigger_rollup, _spawn_pipeline_task,
and supporting functions.

Deferred imports of the extracted helpers (``_maybe_create_failure_alert`` from
``app.alert_dispatch``, ``_utcnow`` from ``app.time_utils``) are intentional —
they avoid circular imports (task_lifecycle is imported BY main) and preserve
test-patchability (tests mock via the helper's home module, e.g.
``app.alert_dispatch._maybe_create_failure_alert``).
"""

import asyncio
import contextlib
import json
import logging
import re
import uuid

from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import load_only
from sqlalchemy.orm.attributes import flag_modified
from sqlmodel import select

from app.database import AsyncSessionLocal
from app.context_bounds import bound_review_history
from app.models import (
    TASK_ORIGINATION_REMEDIATION,
    TASK_ORIGINATION_SMOKETEST,
    Task,
    TaskHistory,
    Project,
    TaskStatus,
    ArtifactType,
)
from app.schemas import AgentStatusPayload
from app.shared_state import (
    runtime_config as _runtime_config,
    derive_task_branch as _derive_task_branch,
)
from app.task_capabilities import (
    STRATEGIC_TASK_TYPES,
    WRITE_TASK_TYPES,
    get_task_capability,
)
from app.json_extract import scan_balanced_json_objects
import app.shared_state as _shared_state
from app.broadcast import broadcast_to_project, _sanitize_log
from app.payload_schemas import validate_internal_payload
from app.review_pipeline import (
    SPAWN_REASON_KEY,
    _genuine_blocking_issue_exists,
    _spawn_pipeline_task,
    build_issue_ledger,
    build_ci_failure_fix_prompt,
    build_merge_conflict_fix_prompt,
    reconciled_approve_markers,
    evaluate_auto_approve_small_main,
    ensure_issue_ids,
    should_skip_initial_review,
)
from app.failure_classification import (  # noqa: F401
    # Re-exported: app.task_lifecycle_completion_dispatch and
    # app.task_lifecycle_requeue deferred-import classify_failure from here
    # (not from app.failure_classification directly) so test patches on
    # app.task_lifecycle.classify_failure keep intercepting both call sites.
    classify_failure,
)
from app.branch_resolution import (
    is_custom_staged_rollup_root,
    merged_pr_counts_as_integration_landing,
)
from app.landed_signal import corroborate_and_apply_merged_landed_signal
from app.rollup_management import _check_and_trigger_rollup, _cleanup_branches
from app.task_lifecycle_completion_dispatch import run_completion_dispatch
from app.task_lifecycle_fix_completion import run_fix_completion
from app.task_lifecycle_post_commit_terminal import run_post_commit_terminal
from app.review_request_changes import run_request_changes
from app.rollup_locking import _lock_original_task, _OriginalTaskLockTimeout
from app.notification_dispatch import (
    is_residual_remediation_task,
    post_remediation_result_message,
)

logger = logging.getLogger(__name__)


# Write task types whose COMPLETED handler opens a *fresh* PR and routes it
# straight into the review pipeline (the first branch of the completion if/elif
# chain in _process_post_commit). Derived from the canonical WRITE_TASK_TYPES so
# a new write type is honored automatically. Two deliberate deviations:
#   • "fix" is excluded — fix tasks have their own `elif task_type == "fix"`
#     branch (they iterate an EXISTING PR, not open a new one).
#   • "" is included — a legacy/untyped task defaults to create-like routing.
_PR_OPENING_COMPLETION_TYPES: frozenset[str] = (WRITE_TASK_TYPES - {"fix"}) | {""}


def _safe_exception_message(e: Exception, context: str) -> str:
    return _sanitize_log(f"{context}: {type(e).__name__}")[:500]


def _pipeline_spawn_refusal_label(task: Task) -> str:
    """Human-readable reason why ``_spawn_pipeline_task`` returned ``None``.

    ``_spawn_pipeline_task`` records its refusal reason in
    ``task.context[SPAWN_REASON_KEY]`` (stuck_cycle, cap_exhausted,
    supersession_churn_halt, ...). Status messages must surface THAT reason —
    not a blanket "iteration cap reached" (lineage d3c12745 / PR #16759: 13
    tasks claimed the cap when the truth was stuck_cycle; Blueprint directive:
    surface REASONS, not just states). Unmapped reason codes pass through
    verbatim so a new refusal reason is never mislabeled.
    """
    reason = (task.context or {}).get(SPAWN_REASON_KEY) or "unknown"
    return {
        "stuck_cycle": "stuck_cycle (operator review required)",
        "cap_exhausted": "cap_exhausted (iteration cap reached)",
    }.get(reason, reason)


def _sidecar_application_marker(
    *, lane: str, sidecar_id: object, verdict_key: str
) -> dict[str, str]:
    return {
        "lane": lane,
        "sidecar_task_id": str(sidecar_id),
        "verdict_key": verdict_key,
    }


def _sidecar_application_pending(
    ctx: dict, *, lane: str, sidecar_id: object | None = None
) -> bool:
    marker = ctx.get("sidecar_verdict_application_pending")
    if not isinstance(marker, dict) or marker.get("lane") != lane:
        return False
    return sidecar_id is None or marker.get("sidecar_task_id") == str(sidecar_id)


def _set_sidecar_application_pending(
    ctx: dict, *, lane: str, sidecar_id: object, verdict_key: str
) -> None:
    ctx["sidecar_verdict_application_pending"] = _sidecar_application_marker(
        lane=lane,
        sidecar_id=sidecar_id,
        verdict_key=verdict_key,
    )


def _clear_sidecar_application_pending(
    task: Task, *, lane: str, sidecar_id: object | None = None
) -> None:
    ctx = dict(task.context or {})
    if not _sidecar_application_pending(ctx, lane=lane, sidecar_id=sidecar_id):
        return
    ctx.pop("sidecar_verdict_application_pending", None)
    task.context = ctx
    flag_modified(task, "context")


# Verdict-word aliases used by the structured-verdict fallback parser. Each
# canonical verdict maps to the surface forms an agent might emit. Matched only
# when *anchored* (quoted, JSON-field, or standalone verdict line) so a bare
# prose occurrence of "approve" cannot be read as an approval (see the fallback
# in _process_post_commit).
_VERDICT_TOKEN_ALIASES: dict[str, tuple[str, ...]] = {
    "APPROVE": ("APPROVE", "APPROVED"),
    "REQUEST_CHANGES": ("REQUEST_CHANGES", "REQUEST CHANGES"),
}


def _build_anchored_verdict_pattern(token: str) -> "re.Pattern[str]":
    """Compile the anchored matcher for one canonical verdict token.

    An anchored match is one of:
      - a quoted token:                "APPROVE" / 'APPROVE' / `APPROVE`
      - a JSON-style verdict field:    "verdict": "APPROVE"
      - a standalone verdict line:     APPROVE   (optionally `VERDICT: APPROVE`)

    A *bare* prose occurrence ("do not APPROVE this yet") matches NONE of these
    by design — that is the false-approval class this guards against.
    """
    alts = "|".join(re.escape(t) for t in _VERDICT_TOKEN_ALIASES[token])
    pattern = (
        rf"""(["'`])(?:{alts})\1"""
        rf"""|["']verdict["']\s*:\s*["'`](?:{alts})["'`]"""
        rf"""|^\s*(?:verdict\s*[:=]\s*)?(?:{alts})\s*\.?\s*$"""
    )
    return re.compile(pattern, re.IGNORECASE | re.MULTILINE)


_ANCHORED_APPROVE_RE = _build_anchored_verdict_pattern("APPROVE")
_ANCHORED_REQUEST_CHANGES_RE = _build_anchored_verdict_pattern("REQUEST_CHANGES")


def _last_match_start(pattern: "re.Pattern[str]", text: str) -> int:
    """Return the start index of the *last* match, or -1 when there is none."""
    last = -1
    for m in pattern.finditer(text):
        last = m.start()
    return last


def _resolve_fallback_verdict(verdict_text: str) -> str | None:
    """Resolve a review verdict from free text when structured parsing failed.

    This is the last-resort fallback after the verdict_json field (Method 1) and
    JSON-in-log parsing (Method 2). It requires a STRUCTURED / anchored approval
    signal — NOT a bare prose substring — so prose like "do not APPROVE this yet"
    or "needs changes before I can approve" can never be read as an approval and
    merge unreviewed code.

    Returns:
      - "APPROVE" / "REQUEST_CHANGES" when an anchored verdict is found
        (rightmost anchored token wins, so a later REQUEST_CHANGES overrides an
        earlier APPROVE), or
      - None when no anchored verdict is present (caller defaults to the safe
        REQUEST_CHANGES + manual-review path).
    """
    approve_idx = _last_match_start(_ANCHORED_APPROVE_RE, verdict_text)
    rc_idx = _last_match_start(_ANCHORED_REQUEST_CHANGES_RE, verdict_text)
    if approve_idx == -1 and rc_idx == -1:
        return None
    if approve_idx > rc_idx:
        return "APPROVE"
    return "REQUEST_CHANGES"


async def _resolve_live_review_target(
    task: Task,
    session: AsyncSession,
    pr_number: int,
) -> dict[str, str | int] | None:
    """Return the PR's current immutable review target, when GitHub is reachable.

    Target-aware review deduplication is defence in depth, so a transient
    metadata lookup must not block the normal review lifecycle.  Callers pass
    ``None`` on failure and retain the legacy same-chain guard in that case.
    """
    try:
        from app.github import get_pull_request
        from app.routers.agent import get_github_token_for_task

        token, owner, repo = await get_github_token_for_task(task.id, session)
        pr_data = await get_pull_request(
            token,
            owner,
            repo,
            pr_number,
            project_id=str(task.project_id),
        )
        base_sha = str(pr_data.get("base", {}).get("sha", ""))
        head_sha = str(pr_data.get("head", {}).get("sha", ""))
        if base_sha and head_sha:
            return {
                "project_id": str(task.project_id),
                "pr_number": int(pr_number),
                "base_sha": base_sha,
                "head_sha": head_sha,
            }
    except Exception as exc:  # noqa: BLE001
        logger.info(
            "[REVIEW PIPELINE] Could not resolve immutable review target for "
            "PR #%s (task %s): %s — using legacy same-chain dedup",
            pr_number,
            task.id,
            exc,
        )
    return None


async def _persist_stale_review_evidence(
    task: Task,
    session: AsyncSession,
    *,
    original_task_id: str,
    pr_number: int,
    review_loop: dict,
    review_entry: dict,
    live_target: dict[str, str | int] | None,
) -> None:
    """Append a late/stale verdict after newer proof and revoke publication.

    Completion order is authoritative here: an older-target review that lands
    late cannot be discarded behind a newer APPROVE. Its blockers remain in
    the durable ledger until a subsequent exact-target review resolves them.
    """
    from app.task_locking import acquire_pr_spawn_lock

    await acquire_pr_spawn_lock(session, task.project_id, pr_number)
    entry = {**review_entry, "review_task_id": str(task.id)}
    child_history = [
        row
        for row in (review_loop.get("review_history") or [])
        if isinstance(row, dict) and row.get("review_task_id") != str(task.id)
    ]
    task_history = bound_review_history([*child_history, entry])
    task_ledger = build_issue_ledger(
        task_history,
        **(
            {"seed_ledger": review_loop.get("issue_ledger")}
            if review_loop.get("issue_ledger") is not None
            else {}
        ),
        stamp_complete=True,
    )
    gated_loop = {
        **review_loop,
        "review_approved": False,
        "review_history": task_history,
        "issue_ledger": task_ledger,
        "authoritative_review_required": True,
        **({"review_target": dict(live_target)} if live_target else {}),
    }
    for key in ("review_not_required", "review_policy_reason", "verdict"):
        gated_loop.pop(key, None)
    task.context = {**(task.context or {}), "review_loop": gated_loop}
    flag_modified(task, "context")
    session.add(task)

    if not original_task_id:
        return
    try:
        orig = await _lock_original_task(
            session,
            original_task_id,
            source="late_review_authority_revoke",
            skip_locked=False,
        )
    except _OriginalTaskLockTimeout as exc:
        raise PostCommitFinalCommitFailed(
            f"could not revoke parent authority for late review on PR #{pr_number}"
        ) from exc
    if orig is None:
        task.requires_approval = True
        return

    parent_loop = dict((orig.context or {}).get("review_loop") or {})
    parent_history = [
        row
        for row in (parent_loop.get("review_history") or [])
        if isinstance(row, dict) and row.get("review_task_id") != str(task.id)
    ]
    # Preserve every parent-only completion, including one at the same numeric
    # iteration. Iteration is planning metadata, not a total ordering across
    # concurrently admitted reviews.
    combined = list(parent_history)
    for row in child_history:
        row_id = str(row.get("review_task_id") or "")
        if row_id and any(
            str(existing.get("review_task_id") or "") == row_id
            for existing in combined
        ):
            continue
        if row not in combined:
            combined.append(row)
    combined_history = bound_review_history([*combined, entry])
    parent_ledger = build_issue_ledger(
        combined_history,
        **(
            {"seed_ledger": parent_loop.get("issue_ledger")}
            if parent_loop.get("issue_ledger") is not None
            else {}
        ),
        stamp_complete=True,
    )
    parent_loop.update(
        {
            "review_approved": False,
            "review_history": combined_history,
            "issue_ledger": parent_ledger,
            "authoritative_review_required": True,
            **({"review_target": dict(live_target)} if live_target else {}),
        }
    )
    for key in ("review_not_required", "review_policy_reason", "verdict"):
        parent_loop.pop(key, None)
    orig.context = {**(orig.context or {}), "review_loop": parent_loop}
    flag_modified(orig, "context")
    session.add(orig)


from app.post_commit_errors import (  # noqa: E402, F401, RUF100
    PostCommitFinalCommitFailed,
    PostCommitLocked,
)


from app.failure_stage import (  # noqa: E402, F401, RUF100
    _TRANSIENT_SCAN_ERROR_PATTERNS,
    _derive_failure_stage,
    _is_transient_scan_error,
)


from app.retry_prompt import (  # noqa: E402, F401, RUF100
    _ANALYSIS_ARTIFACT_KEYS,
    _BUILD_GATE_PATTERN,
    _RETRY_SUFFIX_AUTO_PREFIXES,
    _SCOPE_OVERFLOW_THRESHOLDS,
    _build_retry_prompt_suffix,
    _build_scope_overflow_details,
    _copy_analysis_artifacts_to_previous,
    _detect_ci_review_disagreement,
    _extract_error_files,
    _extract_failing_tests,
    merge_retry_prompt_suffix,
)


from app.task_lifecycle_requeue import (  # noqa: E402, F401, RUF100
    _record_parent_failure,
    _requeue_parent_task,
    _resolve_terminal_parent_verdict,
    _terminate_all_non_genuine_cancelled,
)


from app.task_lifecycle_github_helpers import (  # noqa: E402, F401, RUF100
    _comment_with_retry,
    _get_approval_gate_file_lists,
    _pick_merge_method,
)


async def _has_active_strategic_task(project_id, session: AsyncSession) -> bool:
    """Return True if any QUEUED/PROVISIONING/IN_PROGRESS/EVALUATING task is a design_doc_update or strategic_review.

    Takes a FOR NO KEY UPDATE lock on the project row so the check+spawn is transactionally
    atomic while remaining compatible with Task FK KEY SHARE inserts. Without it,
    two concurrent callers (e.g. design_doc completion chaining
    to phase 2 and an autonomous-research tick) can both read "none active" in the
    same window and each insert a duplicate strategic task.

    Phase A1: skip_locked=True. If the project row is held by another caller,
    we return True (fail-safe — defer this spawn to the next cycle); the
    other caller will resolve the spawn in their own transaction. Returning
    False under contention would risk both callers concluding "none active"
    and double-spawning, the exact race this lock is here to prevent.

    Phase L-C4 audit: this aggregate read after the project lock is NOT
    upgraded to SERIALIZABLE because the Project lock acquired here is the
    canonical (Project before Task) gate for any strategic spawn — every
    spawn path goes through the same lock helper (now centralized in
    ``lock_project_for_strategic_transition``), so phantom ``Task`` rows
    appearing between this SELECT and the eventual INSERT are impossible
    by construction: any concurrent inserter would have to be holding the
    same project row, which our SKIP LOCKED here just rejected (we already
    returned True). Symmetry assumption confirmed: as long as Project is
    locked before any strategic-task SELECT/INSERT, repeatable-read is
    sufficient and SERIALIZABLE upgrades aren't required. See
    ``docs/concurrency.md`` for the full canonical-order argument.
    """
    result = await session.execute(
        select(Project.id)
        .where(Project.id == project_id)
        # SQLAlchemy renders key_share=True (without read=True) as
        # ``FOR NO KEY UPDATE`` on PostgreSQL: strategic spawners still
        # serialize with each other, while ordinary Task FK inserts and
        # conversation sends can take KEY SHARE without convoying.
        .with_for_update(skip_locked=True, key_share=True)
    )
    if result.first() is None:
        # Row missing or held — fail safe. The other holder will spawn (or
        # already has); our caller will skip and re-evaluate next cycle.
        return True
    # Phase B4 (H-P6): we only need first_payload.type per row to filter, so
    # drop status_log/plan_output JSONB. Loading payloads keeps first_payload
    # cheap (L-P2 — without payloads loaded, accessing first_payload would
    # trigger a per-row reload).
    active = (
        (
            await session.execute(
                select(Task)
                .options(
                    load_only(
                        Task.id,
                        Task.status,
                        Task.payloads,
                        Task.project_id,
                        raiseload=True,
                    )
                )
                .where(
                    Task.project_id == project_id,
                    Task.status.in_(
                        [
                            TaskStatus.QUEUED,
                            TaskStatus.PROVISIONING,
                            TaskStatus.IN_PROGRESS,
                            TaskStatus.EVALUATING,
                        ]
                    ),
                )
            )
        )
        .scalars()
        .all()
    )
    return any((t.first_payload).get("type") in STRATEGIC_TASK_TYPES for t in active)


async def _increment_research_spawn_count(
    project: Project, session: AsyncSession
) -> int:
    """Increment _research_spawn_count in project.vision and persist. Returns new count."""
    new_count = (project.vision or {}).get("_research_spawn_count", 0) + 1
    project.vision = {**(project.vision or {}), "_research_spawn_count": new_count}
    flag_modified(project, "vision")
    session.add(project)
    return new_count


async def _mark_dependents_warm_claimable(landed_task: Task) -> None:
    """Completion-side warm-reuse trigger for PRs that land on the integration
    branch from inside ``_process_post_commit`` (the small-scope auto-merge and
    review-approved merge paths).

    The orchestrator's CI-completion path (cluster 1) already stamps the sticky
    ``_deps_satisfied`` readiness marker on the dependents a landing unblocks,
    but the in-process merge paths here are a second class of integration
    landing that previously had no completion-side trigger: the just-unblocked
    dependent stayed unmarked until a later capacity-and-window-bounded dispatch
    scan happened to reach it, so ``landed_task``'s warm VM — polling for
    follow-on work inside its teardown-defer grace window — skipped it and tore
    down cold while the orchestrator cold-provisioned a fresh VM for that very
    dependent.

    Fully delegated to the single authoritative implementation in
    ``orchestrator._mark_unblocked_dependents`` (same GitHub-aware dependency
    resolution, its own sessions with no partial projection, and no loosening
    of the readiness signal — only the ``"eligible"`` result is stamped). The
    delegated call opens its own sessions, so it must run AFTER the caller has
    committed and released its FOR UPDATE lock. Best-effort: any failure here
    must never abort post-commit processing. Deferred import avoids a circular
    import (orchestrator is a heavy module that is already loaded by call time).
    """
    try:
        from app.dep_lifecycle import _mark_unblocked_dependents
        from app.rollup_management import _verify_pr_merged

        unblocked = await _mark_unblocked_dependents(landed_task, _verify_pr_merged)
        if unblocked:
            logger.info(
                "[PIPELINE] Task %s landing unblocked %d dependent(s) — marked warm-claimable",
                landed_task.id,
                unblocked,
            )
    except Exception:
        logger.exception(
            "[PIPELINE] Dependent warm-claim marking failed for task %s",
            landed_task.id,
        )


def _resolve_adversarial_challenged_hashes(source: Task, grader: Task) -> list[str]:
    """Return the challenged hashes for an adversarial grader completion.

    Prefer the hashes persisted on the source task at park time, then the
    grader-side copy (for crash/recovery gaps), then the legacy plan_output
    fallback for parked sources that predate the persistence fix.
    """
    from app.finding_deduplication import (
        canonical_finding_body as _canonical_body,
        finding_hash as _fh,
    )

    _stored_hashes = (source.context or {}).get("_adversarial_challenged_hashes")
    if isinstance(_stored_hashes, list) and _stored_hashes:
        return [h for h in _stored_hashes if isinstance(h, str)]

    _grader_hashes = (grader.context or {}).get("_adversarial_challenged_hashes")
    if isinstance(_grader_hashes, list) and _grader_hashes:
        return [h for h in _grader_hashes if isinstance(h, str)]

    stored_findings = source.plan_output or []
    if isinstance(stored_findings, dict):
        stored_findings = stored_findings.get("findings") or []
    return [
        _fh(
            str(f.get("title", "")),
            _canonical_body(f),
        )
        for f in stored_findings
        if isinstance(f, dict)
    ]


async def _resolve_quality_grader_completion(
    grader: Task, payload: AgentStatusPayload, session: AsyncSession
) -> None:
    """Route an independent forward-quality grader's verdict back to its source.

    The grader (spawned by ``task_evaluators.spawn_forward_quality_grader``)
    graded an upstream artifact whose findings would otherwise spawn downstream
    work. Here we parse its pass/fail verdict, write it onto the parked source
    task, and re-dispatch the source to its registry completion handler so the
    spawn-on-findings path runs (PASS) or the source fails without spawning
    (FAIL). This is the artifact analogue of a code-review verdict returning
    through the re-entrant completion path. Resolves BOTH a COMPLETED and a
    FAILED grader (a FAILED grader fails closed) so the source never strands
    EVALUATING.

    Adversarial mode: when the grader has ``adversarial_grader=True`` in its
    context, the grader produced per-finding verdicts (not an artifact-level
    pass/fail). The per-finding dict is written under ``ADVERSARIAL_GATE_VERDICT_KEY``
    and the source is re-dispatched normally (the spawn loop will defer findings
    whose hash maps to False in the verdict dict). Fail-closed: absent hashes default
    to passed=False so a crashed adversarial grader leaves findings unspawned and
    auditable as unresolved rather than silently converting to an implicit pass.
    """
    from app.task_evaluators import (
        ADVERSARIAL_GATE_VERDICT_KEY,
        ADVERSARIAL_PARSE_CHALLENGER_FAILED,
        ADVERSARIAL_PARSE_EMPTY_LOGS,
        ADVERSARIAL_PARSE_INVALID_JSON,
        ADVERSARIAL_PARSE_NO_HASH_MATCHES,
        ADVERSARIAL_PARSE_NO_JSON_ARRAY,
        ADVERSARIAL_PARSE_OK,
        QUALITY_GRADER_SOURCE_KEY,
        QUALITY_GRADER_VERDICT_KEY,
        parse_adversarial_finding_verdicts,
        parse_grader_verdict,
    )
    from app.research_completion import _handle_research_completion
    from app.task_completion import _handle_security_audit_completion
    from app.knowledge_completion import (
        _handle_design_completion,
        _handle_web_research_completion,
    )
    is_adversarial = bool((grader.context or {}).get("adversarial_grader"))
    completed = payload.status == TaskStatus.COMPLETED
    grader_logs = payload.logs or grader.latest_status_message or ""

    source_id = (grader.context or {}).get(QUALITY_GRADER_SOURCE_KEY)
    try:
        source_uuid = uuid.UUID(str(source_id))
    except (ValueError, TypeError):
        await session.commit()
        return

    source = (
        (
            await session.execute(
                select(Task).where(Task.id == source_uuid).with_for_update()
            )
        )
        .scalars()
        .first()
    )
    if source is None:
        await session.commit()
        logger.warning(
            "[GRADER] Source task %s for grader %s is gone; verdict dropped",
            source_id,
            grader.id,
        )
        return

    ctx = dict(source.context or {})
    # ``parent_task_id`` is immutable; snapshot it now, while ``source`` is
    # freshly loaded. The rollup-trigger decision below runs AFTER the resume
    # handler + commits, by which point a best-effort sub-op rollback (or the
    # connection-drop path) may have expired ``source`` — reading
    # ``source.parent_task_id`` there fires a sync expired-load inside the async
    # session and raises MissingGreenlet (prod 2026-06-13, sidecar-recovery).
    _source_parent_task_id = source.parent_task_id
    lane = "adversarial_grader" if is_adversarial else "quality_grader"
    verdict_key = (
        ADVERSARIAL_GATE_VERDICT_KEY if is_adversarial else QUALITY_GRADER_VERDICT_KEY
    )
    pending_application = _sidecar_application_pending(
        ctx, lane=lane, sidecar_id=grader.id
    )
    if not pending_application and (
        verdict_key in ctx
        or (
            (
                "awaiting_adversarial_grader"
                if is_adversarial
                else "awaiting_quality_grader"
            )
            not in ctx
        )
    ):
        await session.commit()
        logger.info(
            "[GRADER] Grader %s for source %s already resolved; ignoring replayed terminal status",
            grader.id,
            source.id,
        )
        return

    if is_adversarial and not pending_application:
        # Prefer the source-side persisted hashes, then the grader copy written
        # at spawn time, then the legacy plan_output reconstruction.
        all_hashes = _resolve_adversarial_challenged_hashes(source, grader)
        finding_verdicts, parse_outcome = parse_adversarial_finding_verdicts(
            grader_logs, all_hashes, completed=completed
        )
        failed_count = sum(1 for v in finding_verdicts.values() if not v["passed"])
        gate_record = {
            "finding_verdicts": finding_verdicts,
            "grader_task_id": str(grader.id),
            "parse_outcome": parse_outcome,
        }
        try:
            from app.compute import get_vm_runtime_fingerprint, get_vm_source_sha
            from app.models import PoolVm

            vm_id = grader.vm_instance_id
            if vm_id:
                gate_record["grader_vm_instance_id"] = vm_id
                source_sha = get_vm_source_sha(vm_id)
                runtime_fp = get_vm_runtime_fingerprint(vm_id)
                pool_vm = (
                    (
                        await session.execute(
                            select(PoolVm).where(PoolVm.vm_instance_id == vm_id)
                        )
                    )
                    .scalars()
                    .first()
                )
                if pool_vm is not None:
                    source_sha = source_sha or pool_vm.agent_source_sha
                    runtime_fp = runtime_fp or pool_vm.agent_runtime_fingerprint
                    if pool_vm.agent_image_name:
                        gate_record["grader_agent_image_name"] = (
                            pool_vm.agent_image_name
                        )
                    if pool_vm.agent_image_build_ts is not None:
                        gate_record["grader_agent_image_build_ts"] = (
                            pool_vm.agent_image_build_ts
                        )
                if source_sha:
                    gate_record["grader_agent_source_sha"] = source_sha
                if runtime_fp:
                    gate_record["grader_agent_runtime_fingerprint"] = runtime_fp
        except Exception:
            logger.debug(
                "[GRADER] Failed to attach adversarial grader runtime provenance",
                exc_info=True,
            )
        # Bounded operator diagnostic so absent verdicts can be root-caused
        # (stale serving image vs malformed/no grader output) without reading
        # raw logs. Written unconditionally so the parent task always carries
        # challenger provenance after re-entry. Mirrors the provenance already
        # attached to gate_record (None when the grader VM is unknown).
        _parse_causes = {
            ADVERSARIAL_PARSE_OK: "parsed explicit verdict(s) from grader output",
            ADVERSARIAL_PARSE_CHALLENGER_FAILED: (
                "challenger task failed before producing verdicts"
            ),
            ADVERSARIAL_PARSE_EMPTY_LOGS: "grader posted no output",
            ADVERSARIAL_PARSE_NO_JSON_ARRAY: (
                "no JSON array with hash/verdict found in grader output"
            ),
            ADVERSARIAL_PARSE_INVALID_JSON: "candidate JSON array failed to decode",
            ADVERSARIAL_PARSE_NO_HASH_MATCHES: (
                "JSON array contained no known finding hashes"
            ),
        }
        _diag_explicit = sum(1 for v in finding_verdicts.values() if v.get("explicit"))
        # Machine-readable WHY the grader did not respond (cause), drawn from the
        # existing failure taxonomy for a FAILED grader or a bounded refusal-vs-
        # format-miss distinction for a completed-but-unparseable one, plus a
        # best-effort stale-agent-image tag. Observability only — does NOT alter
        # any fail-open/fail-closed, retry, or deferral decision below.
        from app.failure_classification import (
            append_grader_diagnostic_trail,
            classify_grader_nonresponse_cause,
        )

        _grader_cause = await classify_grader_nonresponse_cause(
            grader,
            parse_outcome,
            gate_record,
            completed=completed,
            logs=grader_logs,
            session=session,
            project_id=source.project_id,
        )
        _vm_provenance = {
            "grader_vm_instance_id": gate_record.get("grader_vm_instance_id"),
            "grader_agent_source_sha": gate_record.get("grader_agent_source_sha"),
            "grader_agent_image_name": gate_record.get("grader_agent_image_name"),
            "grader_agent_image_build_ts": gate_record.get(
                "grader_agent_image_build_ts"
            ),
            "grader_agent_runtime_fingerprint": gate_record.get(
                "grader_agent_runtime_fingerprint"
            ),
        }
        ctx["adversarial_gate_diagnostic"] = {
            "challenger_task_id": str(grader.id),
            "completion_state": "completed" if completed else "failed",
            "challenged_count": len(all_hashes),
            "parse_state": parse_outcome,
            "parse_cause": _parse_causes.get(parse_outcome, parse_outcome),
            "cause": _grader_cause,
            "parsed_verdict_count": _diag_explicit,
            "unresolved_count": len(finding_verdicts) - _diag_explicit,
            "vm_image_name": gate_record.get("grader_agent_image_name"),
            "vm_image_build_ts": gate_record.get("grader_agent_image_build_ts"),
            "vm_runtime_fingerprint": gate_record.get(
                "grader_agent_runtime_fingerprint"
            ),
            "vm_source_sha": gate_record.get("grader_agent_source_sha"),
        }
        gate_record["cause"] = _grader_cause
        # Append (don't overwrite) a per-attempt trail entry so the bounded
        # wholesale-retry sequence is auditable after the fact.
        append_grader_diagnostic_trail(
            ctx,
            grader_task_id=grader.id,
            parse_outcome=parse_outcome,
            cause=_grader_cause,
            vm_provenance=_vm_provenance,
        )
        ctx[ADVERSARIAL_GATE_VERDICT_KEY] = gate_record
        ctx.pop("awaiting_adversarial_grader", None)
        _set_sidecar_application_pending(
            ctx,
            lane=lane,
            sidecar_id=grader.id,
            verdict_key=ADVERSARIAL_GATE_VERDICT_KEY,
        )
        grader.latest_status_message = (
            f"Adversarial challenge: {failed_count}/{len(finding_verdicts)} "
            f"finding(s) failed"
        )[:500]
    elif not pending_application:
        verdict = parse_grader_verdict(grader_logs, completed=completed)
        grader.latest_status_message = (
            f"Forward-quality grade: {'PASS' if verdict.passed else 'FAIL'} — "
            f"{verdict.reason}"
        )[:500]
        ctx[QUALITY_GRADER_VERDICT_KEY] = {
            "passed": verdict.passed,
            "reason": verdict.reason,
            "grader_task_id": str(grader.id),
            "strategy": "forward-quality",
        }
        # Same observability treatment as the adversarial lane: record a
        # diagnostic + cause + per-attempt trail entry for the (default-OFF)
        # forward-quality lane, which previously recorded nothing here. The
        # forward-quality grader returns a single artifact-level verdict (no
        # per-finding hashes), so synthesize a parse_state from its output:
        # FAILED → taxonomy cause; empty/unparseable → format-miss; else
        # responsive. Pure observability; the verdict application below is
        # unchanged.
        from app.failure_classification import (
            append_grader_diagnostic_trail,
            classify_grader_nonresponse_cause,
            synthesize_forward_quality_parse_state,
        )

        _fq_parse_state = synthesize_forward_quality_parse_state(
            completed, grader_logs, verdict.reason
        )
        _fq_cause = await classify_grader_nonresponse_cause(
            grader,
            _fq_parse_state,
            {},
            completed=completed,
            logs=grader_logs,
            session=session,
            project_id=source.project_id,
        )
        ctx["quality_grader_diagnostic"] = {
            "challenger_task_id": str(grader.id),
            "completion_state": "completed" if completed else "failed",
            "parse_state": _fq_parse_state,
            "cause": _fq_cause,
            "verdict_passed": verdict.passed,
        }
        append_grader_diagnostic_trail(
            ctx,
            grader_task_id=grader.id,
            parse_outcome=_fq_parse_state,
            cause=_fq_cause,
            vm_provenance={},
        )
        ctx.pop("awaiting_quality_grader", None)
        _set_sidecar_application_pending(
            ctx,
            lane=lane,
            sidecar_id=grader.id,
            verdict_key=QUALITY_GRADER_VERDICT_KEY,
        )
    session.add(grader)
    source.context = ctx
    flag_modified(source, "context")
    # Re-establish the terminal status the source originally reported before it
    # parked; the resume handler flips it to FAILED if the grade failed.
    source.status = TaskStatus.COMPLETED
    session.add(source)
    await session.commit()

    # Re-dispatch the source to its registry completion handler so it resolves.
    # Registry-driven (keyed by TaskCapability.completion_handler) so any
    # blast-radius knowledge type can opt into the same escalation/resume path
    # without another hardcoded entry.
    from app.task_completion import _handle_compute_experiment_completion

    _resume_handlers = {
        "research": _handle_research_completion,
        "security_audit": _handle_security_audit_completion,
        "web_research": _handle_web_research_completion,
        "design": _handle_design_completion,
        "compute_experiment": _handle_compute_experiment_completion,
    }
    source_type = (source.first_payload or {}).get("type")
    cap = get_task_capability(source_type)
    handler = (
        _resume_handlers.get(cap.completion_handler)
        if cap and cap.completion_handler
        else None
    )
    if handler is not None:
        # Reconstruct the artifact text from the stored findings so the handler
        # resumes from the same parsed state it had on the first pass. research
        # stores plan_output as a bare list; security_audit wraps it as
        # ``{"findings": [...]}`` — reconstruct either to the JSON findings array
        # the handler's parser expects, rather than dropping to the status line.
        if isinstance(source.plan_output, list):
            resume_logs = json.dumps(source.plan_output)
        elif isinstance(source.plan_output, dict) and isinstance(
            source.plan_output.get("findings"), list
        ):
            resume_logs = json.dumps(source.plan_output["findings"])
        else:
            resume_logs = source.latest_status_message or ""
        await handler(source, resume_logs, session)
        await session.refresh(source)
        _clear_sidecar_application_pending(source, lane=lane, sidecar_id=grader.id)
        session.add(source)
    else:
        logger.warning(
            "[GRADER] No resume handler for source type %r (task %s); verdict "
            "recorded but source not re-dispatched",
            source_type,
            source.id,
        )
        _clear_sidecar_application_pending(source, lane=lane, sidecar_id=grader.id)
        session.add(source)
    await session.commit()

    # Source is now terminal — propagate to its parent rollup and broadcast.
    # Snapshot plain values first: the rollup can poison the session (its
    # rollback also expires ``source``), and reading source.<attr> afterwards
    # would raise PendingRollbackError / MissingGreenlet.
    _source_id_str = str(source.id)
    _source_project_id_str = str(source.project_id)
    _source_status = source.status
    if _source_parent_task_id:
        try:
            await _check_and_trigger_rollup(source, session)
        except Exception:
            logger.error(
                "[GRADER] rollup after grade failed for %s",
                _source_id_str,
                exc_info=True,
            )
            await session.rollback()
    try:
        await broadcast_to_project(
            _source_project_id_str,
            "task_updated",
            {"task_id": _source_id_str, "status": _source_status},
        )
    except Exception:
        logger.debug("[GRADER] broadcast after grade failed", exc_info=True)


async def _resolve_finding_challenge_completion(
    challenge: Task, payload: AgentStatusPayload, session: AsyncSession
) -> None:
    """Route a per-finding challenge side-car's second opinion back to its source.

    The side-car (spawned by ``task_evaluators.spawn_finding_challenge``) gave an
    independent second opinion on a single high-stakes research-finding verdict.
    Here we parse that opinion into a ``FindingChallengeRecord`` (fail-closed:
    unparseable/incomplete → ``agreement="indeterminate"``, which preserves the
    original verdict), write it onto the parked source under
    ``FINDING_CHALLENGE_VERDICT_KEY``, restore the source's terminal status, and
    re-dispatch the source to its registry completion handler so the
    drop/dispatch decision runs with the second opinion in hand. Resolves BOTH a
    COMPLETED and a FAILED side-car so the source never strands EVALUATING.
    """
    from app.task_evaluators import (
        FINDING_CHALLENGE_SOURCE_KEY,
        FINDING_CHALLENGE_VERDICT_KEY,
        parse_finding_challenge_verdict,
    )
    from app.research_completion import _handle_research_completion

    cctx = challenge.context or {}
    boundary = str(cctx.get("challenged_boundary") or "")
    first_verdict = str(cctx.get("challenged_first_verdict") or "")
    challenged_finding_hash = cctx.get("challenged_finding_hash") or None
    source_id = cctx.get(FINDING_CHALLENGE_SOURCE_KEY)

    try:
        source_uuid = uuid.UUID(str(source_id))
    except (ValueError, TypeError):
        await session.commit()
        return

    source = (
        (
            await session.execute(
                select(Task).where(Task.id == source_uuid).with_for_update()
            )
        )
        .scalars()
        .first()
    )
    if source is None:
        await session.commit()
        logger.warning(
            "[CHALLENGE] Source task %s for challenge %s is gone; verdict dropped",
            source_id,
            challenge.id,
        )
        return

    ctx = dict(source.context or {})
    # Snapshot the immutable parent id while ``source`` is fresh — see the
    # identical guard in _resolve_quality_grader_completion: the rollup decision
    # below runs after a commit/possible rollback that can expire ``source``, and
    # a live ``source.parent_task_id`` read there raises MissingGreenlet.
    _source_parent_task_id = source.parent_task_id
    lane = "finding_challenge"
    pending_application = _sidecar_application_pending(
        ctx, lane=lane, sidecar_id=challenge.id
    )
    if not pending_application and (
        FINDING_CHALLENGE_VERDICT_KEY in ctx or "awaiting_finding_challenge" not in ctx
    ):
        await session.commit()
        logger.info(
            "[CHALLENGE] Challenge %s for source %s already resolved; ignoring replayed terminal status",
            challenge.id,
            source.id,
        )
        return

    if not pending_application:
        completed = payload.status == TaskStatus.COMPLETED
        challenge_logs = payload.logs or challenge.latest_status_message or ""
        record = parse_finding_challenge_verdict(
            challenge_logs,
            first_verdict,
            boundary,
            str(challenge.id),
            completed=completed,
            source_task_id=str(source_id) if source_id else None,
            finding_hash=challenged_finding_hash,
        )
        challenge.latest_status_message = (
            f"Finding challenge ({boundary}): {record.agreement} — {record.reason}"
        )[:500]
        session.add(challenge)
        ctx[FINDING_CHALLENGE_VERDICT_KEY] = record.to_context()
        ctx.pop("awaiting_finding_challenge", None)
        _set_sidecar_application_pending(
            ctx,
            lane=lane,
            sidecar_id=challenge.id,
            verdict_key=FINDING_CHALLENGE_VERDICT_KEY,
        )
    source.context = ctx
    flag_modified(source, "context")
    source.status = TaskStatus.COMPLETED
    session.add(source)
    await session.commit()

    _resume_handlers = {
        "research": _handle_research_completion,
    }
    source_type = (source.first_payload or {}).get("type")
    cap = get_task_capability(source_type)
    handler = (
        _resume_handlers.get(cap.completion_handler)
        if cap and cap.completion_handler
        else None
    )
    if handler is not None:
        if isinstance(source.plan_output, list):
            resume_logs = json.dumps(source.plan_output)
        elif isinstance(source.plan_output, dict) and isinstance(
            source.plan_output.get("findings"), list
        ):
            resume_logs = json.dumps(source.plan_output["findings"])
        else:
            resume_logs = source.latest_status_message or ""
        await handler(source, resume_logs, session)
        await session.refresh(source)
        _clear_sidecar_application_pending(source, lane=lane, sidecar_id=challenge.id)
        session.add(source)
    else:
        logger.warning(
            "[CHALLENGE] No resume handler for source type %r (task %s); verdict recorded but source not re-dispatched",
            source_type,
            source.id,
        )
    await session.commit()

    _source_id_str = str(source.id)
    _source_project_id_str = str(source.project_id)
    _source_status = source.status
    if _source_parent_task_id:
        try:
            await _check_and_trigger_rollup(source, session)
        except Exception:
            logger.error(
                "[CHALLENGE] rollup after challenge failed for %s",
                _source_id_str,
                exc_info=True,
            )
            await session.rollback()
    try:
        await broadcast_to_project(
            _source_project_id_str,
            "task_updated",
            {"task_id": _source_id_str, "status": _source_status},
        )
    except Exception:
        logger.debug("[CHALLENGE] broadcast after challenge failed", exc_info=True)


async def trigger_stranded_sidecar_resolution(
    sidecar: Task, session: AsyncSession
) -> None:
    """Invoke the appropriate lane resolver for a terminal side-car that never posted status.

    Called by the stranded-sidecar recovery sweep when it detects a FAILED or
    CANCELLED side-car whose source task is still EVALUATING.  Constructs a
    synthetic ``AgentStatusPayload`` carrying the sidecar's terminal status and
    last message, then dispatches to the resolver for the sidecar's lane
    (identified by which SOURCE_KEY the sidecar carries in its context).

    All three resolver functions already handle a FAILED payload fail-closed, so
    no additional logic is needed here — this is purely a routing shim.
    """
    from app.task_evaluators import (
        QUALITY_GRADER_SOURCE_KEY,
        FINDING_CHALLENGE_SOURCE_KEY,
    )

    synthetic = AgentStatusPayload(
        task_id=sidecar.id,
        status=sidecar.status,
        logs=sidecar.latest_status_message or "",
    )
    ctx = sidecar.context or {}
    if ctx.get(QUALITY_GRADER_SOURCE_KEY):
        await _resolve_quality_grader_completion(sidecar, synthetic, session)
    elif ctx.get(FINDING_CHALLENGE_SOURCE_KEY):
        await _resolve_finding_challenge_completion(sidecar, synthetic, session)
    else:
        logger.warning(
            "[SIDECAR-RECOVERY] Sidecar %s has no recognised source key; cannot route",
            sidecar.id,
        )


async def _resolve_ci_for_unpolled_rollup_parent(
    session: AsyncSession,
    orig_task: Task,
    orig_loop: dict,
    pr_number: int,
    token: str,
    owner: str,
    repo: str,
) -> tuple[bool, str]:
    """Resolve CI inline for a rollup parent the CI monitor will never poll.

    The orchestrator CI monitor restricts its poll set to EVALUATING rows
    (``orchestrator_ci_monitor._ci_waiting_sql_filter``), so a rollup child
    that is already COMPLETED parks its exact-target approvals at the CI gate
    forever: ``ci_passed`` never flips, the rollup recovery respawns another
    review, and the respawn cap eventually escalates the parent to manual
    approval (PR #16735 treadmill, 2026-08-06). Mirror the monitor's
    *definitive* rules here:

      - a completed all-green build → pass;
      - definitive ``no_checks`` on a base where checks will never appear
        (``ci_resolution.proceed_on_no_checks`` — internal staging branch or
        declared no-CI repo — minus the external verify_tests gate, which
        needs the monitor's polling loop to await a verdict) → pass;
      - pending / unresolved / red → keep parked; never merge on the absence
        of a signal.

    Returns ``(ci_ok, resolved_state)``. Never raises.
    """
    from app import ci_resolution
    from app.shared_state import runtime_config

    try:
        resolved = await ci_resolution.resolve_pr_ci_status(
            pr_number,
            str(orig_loop.get("head_sha") or ""),
            token=token,
            owner=owner,
            repo=repo,
            project_id=str(orig_task.project_id)
            if orig_task.project_id
            else None,
        )
        state = str(resolved.get("state") or "")
        if resolved.get("ci_passed"):
            return True, state
        if state == "no_checks":
            project = (
                await session.get(Project, orig_task.project_id)
                if orig_task.project_id is not None
                else None
            )
            base_ref = str(orig_loop.get("base_branch") or "")
            if (
                project is not None
                and ci_resolution.proceed_on_no_checks(project, base_ref)
                and not ci_resolution.should_run_no_ci_test_gate(
                    project,
                    base_ref,
                    gate_enabled=runtime_config.get(
                        "no_ci_test_gate_enabled", True
                    ),
                )
            ):
                return True, state
        return False, state
    except Exception as exc:
        logger.warning(
            f"[REVIEW PIPELINE] Inline CI resolution failed for rollup PR #{pr_number}: {exc}",
            exc_info=True,
        )
        return False, "error"


async def _process_post_commit(
    task_id: uuid.UUID,
    payload: AgentStatusPayload,
    _session_factory=None,
    _expected_childless_recovery_token: str | None = None,
    _expected_post_commit_claim_token: str | None = None,
    _expected_post_commit_status: TaskStatus | None = None,
) -> dict:
    """Post-commit processing: review pipeline, completion handlers, rollup, aggregation.

    Uses its own session to avoid holding the FOR UPDATE lock from the
    status endpoint while making GitHub API calls (which can hang 15-120s).
    _session_factory is injectable for tests (defaults to AsyncSessionLocal).
    """
    from app.time_utils import _utcnow
    from app.broadcast import broadcast_to_project
    from app.routers.agent import get_github_token_for_task
    from app.security import scan_pr_for_injection
    from app.github import (
        create_pr_comment,
        merge_pull_request,
        get_pull_request,
        get_pr_files,
        get_check_runs_cached,
        submit_pr_review,
        update_check_run,
    )
    from app.task_completion import _try_recover_memo_from_parse_failure
    from app.strategic_completion import (
        _record_strategic_task_failure,
        strategic_completion_message_count,
    )
    from app.project_message_creation import reserve_project_message_sequences
    from app.discovery_review import block_failed_discovery_review

    factory = _session_factory or AsyncSessionLocal
    async with factory() as session:
        # WI-6 (deadlock remediation) + Round 3 C3/C4: the post-commit unit
        # can lock BOTH the Project and the Task row — strategic tasks in their
        # completion chain, and every other type via the downstream project-row
        # writers reached from the completion handlers / rollup / spawn
        # (with_vision_locked, with_design_doc_locked, with_directions_locked,
        # project_lock, ...). The canonical order is **Project before Task,
        # always** (docs/concurrency.md). Previously only strategic types used
        # the canonical pair-locker; every other type locked the Task first
        # (task-only FOR UPDATE) and reached the Project lock downstream — so
        # two concurrent non-strategic post-commits in the same project took
        # Task→Project while a strategic / dispatch / reaper path took
        # Project→Task, and the opposite orders deadlocked (Postgres 40P01
        # DeadlockDetected, ~6/hr on bursty concurrent agent-status callbacks).
        # Acquire Project→Task up front for ALL types via the canonical
        # pair-locker. The invariant this establishes is ORDER, not duration: the
        # Project row is always acquired FIRST (before the Task), by every path.
        # (The up-front locks are RELEASED at the Phase-B3 lock-release commit —
        # "release the lock before slow GitHub calls" — so a downstream project
        # lock taken after that commit is a fresh same-connection acquisition, not
        # a held-through re-entrant no-op. A downstream lock taken while the up-front
        # lock is still held IS re-entrant / non-blocking, but the deadlock freedom
        # comes purely from Project-before-Task being universal, not from holding
        # the row across the whole unit.)
        _peek_row = (
            await session.execute(
                select(Task.project_id, Task.payloads).where(Task.id == task_id)
            )
        ).first()
        if _peek_row is None:
            return {"status": "accepted"}
        _peek_project_id = _peek_row[0]
        _peek_payloads = _peek_row[1] if isinstance(_peek_row[1], list) else []
        _peek_task_type = (
            str((_peek_payloads[0] or {}).get("type") or "")
            if _peek_payloads and isinstance(_peek_payloads[0], dict)
            else ""
        )
        _peek_capability = get_task_capability(_peek_task_type)
        _peek_completion_handler = (
            _peek_capability.completion_handler if _peek_capability else None
        )
        preallocated_strategic_message_sequences: list[int] = []
        if _peek_completion_handler == "strategic":
            message_count = strategic_completion_message_count(payload.logs or "")
            if message_count:
                preallocated_strategic_message_sequences = (
                    await reserve_project_message_sequences(
                        session,
                        _peek_project_id,
                        count=message_count,
                        lock_timeout_ms=1_500,
                    )
                )
                # Reservation gaps are harmless. Commit immediately so the
                # counter row is never held while acquiring Project -> Task.
                await session.commit()

        # skip_locked + bounded retry: the row is typically only held briefly
        # (by the agent_status early-commit path that just released it, or by a
        # concurrent post-commit for the same task). A plain FOR UPDATE would
        # queue behind GitHub API calls (15-120s) and hit the Postgres
        # lock_timeout as LockNotAvailableError, which tears down the session.
        task = None
        # ``locked_project`` holds the canonically pre-locked Project (acquired
        # BEFORE the task). The strategic-completion chain handlers
        # (``completion_needs_locked_project``) receive it via ``project=`` so
        # they skip their own (legacy) re-acquisition; non-strategic handlers
        # ignore it and re-lock the same row re-entrantly under the held lock.
        locked_project: Project | None = None
        blocked_lock_scope = "task"
        from app.task_locking import lock_project_then_task

        for _attempt in range(3):
            if _peek_project_id is not None:
                # Canonical Project → Task ordering. The helper acquires
                # Project first; if the task row is held it yields
                # ``(project, None)`` and we retry; if the project is held it
                # yields ``(None, None)`` and we retry as well.
                async with lock_project_then_task(
                    session,
                    _peek_project_id,
                    task_id,
                    skip_locked=True,
                ) as (_p, _t):
                    if _t is not None:
                        # Both locks acquired. Capture references and break.
                        # SQLAlchemy session-level FOR UPDATE locks persist
                        # past the contextmanager exit; the surrounding
                        # ``async with factory() as session`` commit/rollback
                        # at the end of post-commit releases both row locks.
                        task = _t
                        locked_project = _p
                        break
                    blocked_lock_scope = "project" if _p is None else "task"
                    # Half-pair acquired (project but not task) or no pair
                    # (project also held). Release any held lock before
                    # retrying so we don't accumulate session-level locks
                    # across retry iterations and don't hold a project lock
                    # while a peer attempts the same task lock.
                    if _p is not None:
                        await session.rollback()
            else:
                # Defensive fallback only: Task.project_id is NOT NULL, so this
                # fires solely if the peek raced a hard delete. Task-only lock.
                task = (
                    (
                        await session.execute(
                            select(Task)
                            .where(Task.id == task_id)
                            .with_for_update(skip_locked=True)
                        )
                    )
                    .scalars()
                    .first()
                )
                if task is not None:
                    break
            # Confirm the row still exists before retrying — if it's gone the
            # peer holder is irrelevant.
            exists = (
                await session.execute(select(Task.id).where(Task.id == task_id))
            ).first()
            if exists is None:
                return {"status": "accepted"}
            await asyncio.sleep(2)
        if task is None:
            blocked_id = _peek_project_id if blocked_lock_scope == "project" else task_id
            raise PostCommitLocked(
                f"{blocked_lock_scope} {blocked_id} row held by peer after 3 retries "
                f"while processing task {task_id}",
                lock_scope=blocked_lock_scope,
                resource_id=blocked_id,
            )

        # A marker-less recovery claim is only a lease, not authority to revive
        # a task forever. Revalidate it while holding the canonical Project ->
        # Task lifecycle locks, immediately before any completion handler can
        # mutate state or spawn children. This closes the claim-to-dispatch race
        # with cancel, archive, requeue, and a peer spawn.
        if _expected_childless_recovery_token is not None:
            recovery_meta = dict(
                (task.context or {}).get("_childless_post_commit_recovery") or {}
            )
            plan_output = task.plan_output
            claim_is_current = (
                recovery_meta.get("claim_token")
                == _expected_childless_recovery_token
                and task.status == TaskStatus.EVALUATING
                and not task.archived
                and not task.post_commit_pending
                and (task.first_payload or {}).get("type") == "plan"
                and isinstance(plan_output, dict)
                and isinstance(plan_output.get("subtasks"), list)
                and bool(plan_output.get("subtasks"))
            )
            if claim_is_current:
                child_id = (
                    await session.execute(
                        select(Task.id)
                        .where(Task.parent_task_id == task_id)
                        .limit(1)
                    )
                ).scalar_one_or_none()
                claim_is_current = child_id is None
            if not claim_is_current:
                logger.info(
                    "[POST_COMMIT SWEEP] Skipping stale marker-less recovery "
                    "claim for task %s after lifecycle-lock revalidation",
                    str(task_id)[:8],
                )
                return {"status": "accepted", "recovery_skipped": True}

        # Marker-based recovery claims are leases too. Revalidate the exact
        # claim while holding canonical Project -> Task locks immediately
        # before any handler can spawn or mutate. Status/pending alone are not
        # authority: a later completion can recreate the same values (ABA).
        if _expected_post_commit_claim_token is not None:
            pending_meta = dict(
                (task.context or {}).get("_post_commit_pending_meta") or {}
            )
            claim_is_current = bool(
                task.post_commit_pending
                and not task.archived
                and task.status == _expected_post_commit_status
                and pending_meta.get("claim_token")
                == _expected_post_commit_claim_token
            )
            if not claim_is_current:
                logger.info(
                    "[POST_COMMIT SWEEP] Skipping stale pending recovery claim "
                    "for task %s after lifecycle-lock revalidation",
                    str(task_id)[:8],
                )
                return {"status": "accepted", "recovery_skipped": True}

        async def _rollback_and_refetch_task() -> Task | None:
            """Recover the shared session after a failed post-commit section.

            A handler/helper that died on a DB error (statement_timeout,
            LockNotAvailableError, IntegrityError, ...) leaves the transaction
            aborted: every later session use raises PendingRollbackError, so
            one failed section poisons every section after it (prod bursts
            2026-06-07 00:56 / 01:05). Roll back FIRST, then re-fetch ``task``
            by its (plain, argument-supplied) id — rollback() expires every
            loaded ORM instance even with expire_on_commit=False, so reading
            ``task.<attr>`` without the re-fetch would issue a sync lazy
            refresh and raise MissingGreenlet. Returns None when the row is
            gone (archived/deleted mid-flight); callers bail out cleanly.

            ``populate_existing`` discards any of the failed section's
            rolled-back in-memory mutations still riding the identity-mapped
            instance, and ``no_autoflush`` keeps the re-fetch itself from
            flushing them back out (which could re-raise the original error).
            """
            try:
                await session.rollback()
            except Exception:
                logger.exception(
                    "[LIFECYCLE] Rollback after post-commit section failure "
                    "failed for task %s",
                    task_id,
                )
            with session.no_autoflush:
                return (
                    (
                        await session.execute(
                            select(Task)
                            .where(Task.id == task_id)
                            .execution_options(populate_existing=True)
                        )
                    )
                    .scalars()
                    .first()
                )

        # Phase 0 telemetry: persist warm-VM continuity timing fields into
        # task.context["_timing"] so cold-vs-warm setup cost is observable.
        # Mirrors the existing _shown_playbook_keys storage pattern in
        # routers/agent.py — write all non-None fields under a stable key,
        # respect prior _timing entries (e.g. claim_latency_secs from claim).
        _timing_payload: dict = {}
        if payload.setup_secs is not None:
            _timing_payload["setup_secs"] = float(payload.setup_secs)
        if payload.warm_start is not None:
            _timing_payload["warm_start"] = bool(payload.warm_start)
        if payload.prior_role_vm_id is not None:
            _timing_payload["prior_role_vm_id"] = str(payload.prior_role_vm_id)
        if payload.handler_duration_secs is not None:
            # Wave 5 follow-up: wall-time of the role's handler (post-claim,
            # post-setup). Combined with setup_secs this decomposes total
            # role time so cold-vs-warm gains can be attributed correctly.
            _timing_payload["handler_duration_secs"] = float(
                payload.handler_duration_secs
            )
        # Fallback: derive setup_secs from VM timestamps when the agent exits
        # before computing it (timeout, pre-tool crash). vm_provisioned_at is
        # set at orchestrator cold-provision time and reset to "now" on warm
        # claim, so the difference approximates cold-start boot latency without
        # polluting warm-start buckets. Only writes when not already present.
        if "setup_secs" not in _timing_payload:
            _prior_timing = dict((task.context or {}).get("_timing") or {})
            if (
                "setup_secs" not in _prior_timing
                and task.vm_provisioned_at is not None
                and task.vm_started_at is not None
            ):
                _derived = (task.vm_started_at - task.vm_provisioned_at).total_seconds()
                if _derived >= 0:
                    _timing_payload["setup_secs"] = round(_derived, 1)
        if _timing_payload:
            ctx = dict(task.context or {})
            existing = dict(ctx.get("_timing") or {})
            existing.update(_timing_payload)
            ctx["_timing"] = existing
            task.context = ctx
            flag_modified(task, "context")
            session.add(task)

        # Forward-quality grader side-car: an independent grader spawned by the
        # forward-quality escalation carries QUALITY_GRADER_SOURCE_KEY pointing
        # at the source task it grades. Its completion does NOT flow through the
        # normal review-pipeline / completion-handler path. Resolve the verdict
        # and route it back to the parked source, then return. Handled here —
        # before the COMPLETED-gated pipeline — so even a FAILED grader resolves
        # the source (fail-closed) rather than stranding it EVALUATING forever.
        from app.task_evaluators import (
            FINDING_CHALLENGE_SOURCE_KEY,
            QUALITY_GRADER_SOURCE_KEY,
        )

        if (task.context or {}).get(QUALITY_GRADER_SOURCE_KEY):
            await _resolve_quality_grader_completion(task, payload, session)
            return {"status": "accepted"}

        # Per-finding challenge side-car: an independent second opinion spawned by
        # task_evaluators.spawn_finding_challenge carries FINDING_CHALLENGE_SOURCE_KEY
        # pointing at the source research task. Resolve it the same way (before the
        # COMPLETED-gated pipeline) so even a FAILED side-car resolves the source
        # fail-closed rather than stranding it EVALUATING forever.
        if (task.context or {}).get(FINDING_CHALLENGE_SOURCE_KEY):
            await _resolve_finding_challenge_completion(task, payload, session)
            return {"status": "accepted"}

        # Immediately unblock dependents when task terminally fails (retries exhausted).
        # Uses the same session (not a separate one) to avoid self-deadlock: a second
        # session scanning the task table would block on the FOR UPDATE lock held by
        # this session, causing a hang in Postgres (asyncio is single-threaded).
        if task.status == TaskStatus.FAILED:
            try:
                dep_stmt = select(Task).where(
                    Task.project_id == task.project_id,
                    Task.status == TaskStatus.QUEUED,
                    Task.depends_on.contains([str(task.id)]),
                )
                dep_result = await session.execute(dep_stmt)
                waiting_tasks = dep_result.scalars().all()
                count = 0
                for wt in waiting_tasks:
                    wt.depends_on = [
                        d for d in (wt.depends_on or []) if d != str(task.id)
                    ]
                    session.add(wt)
                    count += 1
                if count:
                    # Don't commit here — let the session's final commit handle it.
                    # An intermediate commit would release the FOR UPDATE lock while
                    # failure-handling code below still needs the task.
                    logger.info(
                        f"[PIPELINE] Cleared terminal dependency {task.id} from {count} waiting tasks"
                    )
            except Exception as e:
                logger.error(
                    f"[PIPELINE] Failed to unblock dependents of {task.id}: {e}",
                    exc_info=True,
                )

            # Record strategic task failures for circuit-breaker accounting.
            if (
                task.status == TaskStatus.FAILED
                and task.first_payload.get("type")
                in ("design_doc_update", "strategic_review")
            ):
                try:
                    # Round 3 C3 + C4: the canonical pair-lock acquired at the
                    # top of _process_post_commit (Project before Task, see
                    # docs/concurrency.md) is passed down here as
                    # ``locked_project``. _record_strategic_task_failure skips
                    # its own re-acquisition when ``project=`` is provided.
                    # Falls back to None for legacy callers / non-strategic
                    # types (the helper does an unlocked session.get(Project)
                    # in that case — the failure-record path doesn't strictly
                    # require the project lock).
                    await _record_strategic_task_failure(
                        task, session, project=locked_project
                    )
                except Exception as _strat_exc:
                    logger.error(
                        f"[STRATEGIC] Failed to record strategic failure for {task.id}: {_strat_exc!r}",
                        exc_info=True,
                    )

            # Record research task failures so _check_direction_health can see
            # cycles that never reached _handle_research_completion (VM crash,
            # provision failure, agent timeout, etc.)
            if task.first_payload.get("type") == "research":
                try:
                    from app.research_completion import _record_research_task_failure

                    await _record_research_task_failure(task, session)
                except Exception as _res_exc:
                    logger.error(
                        f"[RESEARCH] Failed to record research failure for {task.id}: {_res_exc!r}",
                        exc_info=True,
                    )

            # A discovery review is the cycle's durable decision boundary. Once
            # its normal task retries are exhausted, fail the cycle closed
            # instead of leaving it indefinitely in ``reviewing``.
            if task.first_payload.get("type") == "discovery_review":
                try:
                    await block_failed_discovery_review(
                        session,
                        task=task,
                        reason=(
                            payload.failure_category
                            or task.latest_status_message
                            or "discovery review task failed"
                        ),
                    )
                except Exception as _discovery_exc:
                    logger.error(
                        "[DISCOVERY] Failed to block failed review %s: %r",
                        task.id,
                        _discovery_exc,
                        exc_info=True,
                    )

            # Post result message for permanently failed auto-remediation tasks.
            # Routes to residual lifecycle for residual-fingerprint tasks,
            # CI auto-fix lifecycle otherwise.
            if task.auto_remediation:
                try:
                    await post_remediation_result_message(
                        session,
                        task,
                        result="failed",
                    )
                    await session.commit()
                except Exception as _ar_exc:
                    logger.warning(
                        "[AUTO-REMEDIATION] Failed to post failure result message: %s",
                        _ar_exc,
                    )

            # FAILED memo recovery: when a memo task fails with any of the recoverable
            # failure categories, the agent may have emitted a valid memo JSON inside an
            # item.completed envelope that the deployed handler couldn't extract.
            # We widen the trigger beyond 'parse_failed' to also cover 'incomplete' and
            # 'claude_code_failed' — the recovery function is defensive (returns False
            # when no valid envelope is found) so widening is safe.
            if task.first_payload.get("type") == "memo":
                raw_fc = (task.context or {}).get("agent_failure_category_raw", "")
                if raw_fc in ("parse_failed", "incomplete", "claude_code_failed"):
                    try:
                        _recovery_logs = (
                            payload.logs or task.latest_status_message or ""
                        )
                        _recovered = await _try_recover_memo_from_parse_failure(
                            task, _recovery_logs, session
                        )
                        if _recovered:
                            logger.info(
                                "[MEMO] Promoted parse_failed task %s to COMPLETED via error-log recovery",
                                task.id,
                            )
                    except Exception as _memo_rec_exc:
                        logger.warning(
                            "[MEMO] Recovery attempt failed for task %s: %r",
                            task.id,
                            _memo_rec_exc,
                        )

        # Anti-poisoning: resolve playbook entries the task wrote while
        # IN_PROGRESS. On COMPLETED, clear pending_source so peers can see
        # them. On FAILED/CANCELLED, drop them entirely so a task that
        # never landed its code can't leave a "X exists" lie in the
        # playbook. Must run before commit so the design_doc mutation
        # is persisted alongside the rest of the post-commit work.
        if payload.status in (
            TaskStatus.COMPLETED,
            TaskStatus.FAILED,
            TaskStatus.CANCELLED,
        ):
            try:
                from app.playbook_learning import finalize_pending_playbook_entries

                await finalize_pending_playbook_entries(
                    task,
                    session,
                    succeeded=(payload.status == TaskStatus.COMPLETED),
                )
            except Exception:
                logger.exception(
                    "[PLAYBOOK] Failed to finalise pending entries for task %s", task.id
                )

        # Audit 2026-04-25 A7/A12: release the FOR UPDATE row lock before the
        # review pipeline runs its slow GitHub calls (PR fetch, check-runs,
        # PR review submission, merge attempts — each can hit GitHub rate
        # limits and stretch into the tens of seconds, occasionally minutes).
        # Heartbeats arriving in that window would otherwise hit the locked
        # row, get None from skip_locked, and return 409. Multiple skipped
        # heartbeats over 5+ minutes would let the reaper false-orphan the
        # task. Commit here flushes all pending state mutations from sections
        # 1–4 above (dependent unblocking, failure recording, investigation  # noqa: RUF003
        # spawns) and releases the lock; the rest of post-commit processing
        # operates on the already-loaded task object without re-locking.
        # Commit even when the unit of work is clean: the Phase-B3 entry path
        # acquired Project->Task row locks before loading this object, and a
        # clean COMPLETED review still retains those transaction locks. Every
        # subsequent review/fix transition takes PR->Task, so conditionally
        # retaining the initial Task lock would invert the order against direct
        # fix admission (PR lock, then all same-PR task rows) and deadlock.
        await session.commit()

        # 5. Review Pipeline — auto-spawn review/fix tasks based on completed task type
        if payload.status == TaskStatus.COMPLETED:
            task_payload = task.first_payload
            task_type = task_payload.get("type", "create")
            review_loop = (task.context or {}).get("review_loop", {})

            # Create task completed with PR → route based on target branch
            if (
                task_type in _PR_OPENING_COMPLETION_TYPES
                and payload.pr_url
                and payload.artifact_type == ArtifactType.PULL_REQUEST
            ):
                pr_number = int(payload.pr_url.rstrip("/").split("/")[-1])
                _project = await session.get(Project, task.project_id)
                # Onboarding-proof: record the first PR opened for this project.
                # This is the caller that records the freshly-opened PR and has
                # both the Project ORM object and the async session in scope (the
                # low-level create_pull_request helper has neither). Best-effort
                # and idempotent; the staged mutation persists via the routing
                # commits below. Never gates the PR-routing path.
                try:
                    from app.onboarding_proof import stamp_first_pr_opened

                    stamp_first_pr_opened(_project, session, when=_utcnow())
                except Exception:  # observability stamp never blocks PR routing
                    logger.warning(
                        "[POST_COMMIT] onboarding first-pr-opened stamp failed",
                        exc_info=True,
                    )
                branch = _derive_task_branch(
                    task.id,
                    task_type,
                    _shared_state.get_project_task_branch_namespace(_project),
                )
                base_branch = task_payload.get(
                    "base_branch"
                ) or _shared_state.get_project_integration_branch(_project)
                original_prompt = task_payload.get("prompt", task.description or "")
                is_intermediate = _project is not None and _project.is_staging_branch(
                    base_branch
                )
                scope = (task.context or {}).get("scope", "unknown")

                # Auto-approve trivially small main-branch diffs: skip review and route
                # through the same security-scan + auto-merge path used for intermediate
                # small/medium scope. CI still gates the merge.
                #
                # QP6: the decision is owned by `review_policy` — the single,
                # documented home for all review-skip policy — NOT a weaker inline
                # copy. `evaluate_auto_approve_small_main` applies the strict checks
                # the old inline logic skipped (source-type eligibility, test-file
                # exclusion from the line/file budgets, scope-overflow warning) plus
                # the two main-branch-specific constraints: it only skips for
                # non-staging PRs and only when a resolvable direction has favorable
                # acceptance history (a missing/unknown direction is NOT auto-approved,
                # even though should_auto_approve_diff passes `direction_data=None`).
                auto_approve_small = False
                if (
                    not is_intermediate
                    and scope == "small"
                    and _runtime_config.get("auto_approve_small_diff_enabled", True)
                ):
                    try:
                        # Resolve the direction so the policy's acceptance-rate gate
                        # runs against real history. The main-branch path additionally
                        # *requires* a resolvable direction (see policy docstring).
                        direction_id = (task.context or {}).get("direction_id")
                        _aa_dir = None
                        if direction_id:
                            _aa_proj = (
                                (
                                    await session.execute(
                                        select(Project).where(
                                            Project.id == task.project_id
                                        )
                                    )
                                )
                                .scalars()
                                .first()
                            )
                            _aa_dir = next(
                                (
                                    d
                                    for d in (
                                        getattr(_aa_proj, "directions", None) or []
                                    )
                                    if d.get("id") == direction_id
                                ),
                                None,
                            )
                        _aa_decision = evaluate_auto_approve_small_main(
                            task, is_intermediate, scope, _aa_dir
                        )
                        if _aa_decision.skip:
                            auto_approve_small = True
                            _aa_acc = (_aa_decision.detail or {}).get("acceptance")
                            logger.info(
                                f"[PIPELINE] PR #{pr_number} auto-approved for "
                                f"merge ({_aa_decision.reason}, acceptance={_aa_acc}) "
                                "— skipping review"
                            )
                        else:
                            logger.info(
                                f"[PIPELINE] PR #{pr_number} not auto-approved "
                                f"({_aa_decision.reason}) — defaulting to review"
                            )
                    except Exception as _aa_exc:
                        logger.info(
                            f"[PIPELINE] Auto-approve eligibility check failed for PR #{pr_number}: {_aa_exc!r} — defaulting to review"
                        )

                _skip_initial, _skip_initial_reason = should_skip_initial_review(
                    is_intermediate, scope
                )
                _existing_review_history = (
                    ((task.context or {}).get("review_loop") or {}).get(
                        "review_history"
                    )
                )
                _history_allows_initial_bypass = _existing_review_history is None or (
                    isinstance(_existing_review_history, list)
                    and not _existing_review_history
                )
                if (_skip_initial or auto_approve_small) and _history_allows_initial_bypass:
                    _review_bypass_fields = {
                        "review_not_required": True,
                        "review_policy_reason": (
                            "auto_approve_small"
                            if auto_approve_small
                            else _skip_initial_reason
                        ),
                    }
                    # Security scan then auto-merge, skip model review.
                    # Policy is encapsulated in should_skip_initial_review
                    # (review_heuristics). auto_approve_small is an independent
                    # main-branch gate that also routes here.
                    try:
                        gh_token, owner, repo = await get_github_token_for_task(
                            task.id, session
                        )
                        _bypass_pr_snapshot = await get_pull_request(
                            gh_token,
                            owner,
                            repo,
                            pr_number,
                            project_id=str(task.project_id),
                        )
                        _bypass_target = {
                            "project_id": str(task.project_id),
                            "pr_number": int(pr_number),
                            "base_sha": str(
                                (_bypass_pr_snapshot.get("base") or {}).get("sha")
                                or ""
                            ),
                            "head_sha": str(
                                (_bypass_pr_snapshot.get("head") or {}).get("sha")
                                or ""
                            ),
                        }
                        if not _bypass_target["base_sha"] or not _bypass_target[
                            "head_sha"
                        ]:
                            raise RuntimeError(
                                "cannot bind no-review policy to immutable PR target"
                            )
                        _review_bypass_fields.update(
                            {
                                "review_target": dict(_bypass_target),
                                "base_sha": _bypass_target["base_sha"],
                                "head_sha": _bypass_target["head_sha"],
                            }
                        )

                        # Always run security scan before any merge
                        try:
                            scan_result = await scan_pr_for_injection(
                                gh_token, owner, repo, pr_number
                            )
                            logger.info(
                                f"[SECURITY] Pipeline scan: {scan_result['summary']}"
                            )
                            if scan_result["blocked"]:
                                task.requires_approval = True
                                task.latest_status_message = (
                                    f"Security scan blocked: {scan_result['summary']}"
                                )
                                session.add(task)
                                if task.auto_remediation:
                                    try:
                                        await post_remediation_result_message(
                                            session,
                                            task,
                                            result="review_needed",
                                            pr_number=pr_number,
                                        )
                                    except Exception as _ar_exc:
                                        logger.warning(
                                            "[AUTO-REMEDIATION] Failed to post review_needed message: %s",
                                            _ar_exc,
                                        )
                                await session.commit()
                                if scan_result["findings"]:
                                    await create_pr_comment(
                                        gh_token,
                                        owner,
                                        repo,
                                        pr_number,
                                        scan_result["report"],
                                    )
                                return {"status": "accepted"}
                            if scan_result["findings"]:
                                await create_pr_comment(
                                    gh_token,
                                    owner,
                                    repo,
                                    pr_number,
                                    scan_result["report"],
                                )
                        except Exception as e:
                            if _is_transient_scan_error(e):
                                logger.warning(
                                    f"[SECURITY] Small-scope scan transient error for PR #{pr_number}, retrying: {e}"
                                )
                                await asyncio.sleep(5)
                                try:
                                    scan_result = await scan_pr_for_injection(
                                        gh_token, owner, repo, pr_number
                                    )
                                    logger.info(
                                        f"[SECURITY] Small-scope scan retry succeeded: {scan_result['summary']}"
                                    )
                                    if scan_result["blocked"]:
                                        task.requires_approval = True
                                        task.latest_status_message = f"Security scan blocked: {scan_result['summary']}"
                                        session.add(task)
                                        if task.auto_remediation:
                                            try:
                                                await post_remediation_result_message(
                                                    session,
                                                    task,
                                                    result="review_needed",
                                                    pr_number=pr_number,
                                                )
                                            except Exception as _ar_exc:
                                                logger.warning(
                                                    "[AUTO-REMEDIATION] Failed to post review_needed message: %s",
                                                    _ar_exc,
                                                )
                                        await session.commit()
                                        if scan_result["findings"]:
                                            await create_pr_comment(
                                                gh_token,
                                                owner,
                                                repo,
                                                pr_number,
                                                scan_result["report"],
                                            )
                                        return {"status": "accepted"}
                                    if scan_result["findings"]:
                                        await create_pr_comment(
                                            gh_token,
                                            owner,
                                            repo,
                                            pr_number,
                                            scan_result["report"],
                                        )
                                except Exception as retry_exc:
                                    logger.error(
                                        f"[SECURITY] Small-scope scan retry also failed for PR #{pr_number}: {retry_exc}",
                                        exc_info=True,
                                    )
                                    task.requires_approval = True
                                    task.context = {
                                        **(task.context or {}),
                                        "scan_transient_failure": True,
                                    }
                                    task.latest_status_message = _safe_exception_message(
                                        retry_exc,
                                        "Security scan unavailable (infrastructure error) — requires manual approval",
                                    )
                                    session.add(task)
                                    if task.auto_remediation:
                                        try:
                                            await post_remediation_result_message(
                                                session,
                                                task,
                                                result="review_needed",
                                                pr_number=pr_number,
                                            )
                                        except Exception as _ar_exc:
                                            logger.warning(
                                                "[AUTO-REMEDIATION] Failed to post review_needed message: %s",
                                                _ar_exc,
                                            )
                                    await session.commit()
                                    return {"status": "accepted"}
                            else:
                                logger.error(
                                    f"[SECURITY] Small-scope scan failed for PR #{pr_number}, blocking: {e}",
                                    exc_info=True,
                                )
                                task.requires_approval = True
                                task.latest_status_message = _safe_exception_message(
                                    e, "Security scan failed — requires manual approval"
                                )
                                session.add(task)
                                if task.auto_remediation:
                                    try:
                                        await post_remediation_result_message(
                                            session,
                                            task,
                                            result="review_needed",
                                            pr_number=pr_number,
                                        )
                                    except Exception as _ar_exc:
                                        logger.warning(
                                            "[AUTO-REMEDIATION] Failed to post review_needed message: %s",
                                            _ar_exc,
                                        )
                                await session.commit()
                                return {"status": "accepted"}

                        # Pre-merge Alembic dual-head guard for the non-staging
                        # auto-approve path (`auto_approve_small` → main/promotion
                        # target). The staging/intermediate case (`is_intermediate`)
                        # merges onto a staging branch that never carries a
                        # migration onto the integration branch, so it is out of
                        # scope and skips the guard. On a block-or-defer verdict do
                        # NOT merge — park EVALUATING with ci_waiting so the guarded
                        # CI-completion sweep retries once the collision resolves.
                        if not is_intermediate:
                            from app.migration_reservations import (
                                get_migration_reservation_for_task,
                            )
                            from app.rollup_management import (
                                attempt_pr_migration_finalization,
                            )
                            from app.rollup_migration_validation import (
                                enforce_premerge_migration_guard,
                            )
                            from app.migration_guard_recovery import (
                                coerce_migration_guard_result,
                                mark_migration_guard_deferred,
                                spawn_migration_guard_fix_task,
                            )

                            _dh_result = await enforce_premerge_migration_guard(
                                gh_token,
                                owner,
                                repo,
                                pr_number,
                                base_branch,
                                promotion_target=(
                                    _shared_state.get_project_promotion_target(_project)
                                    if _project is not None
                                    else None
                                ),
                            )
                            (
                                _dh_verdict,
                                _dh_reason,
                            ) = coerce_migration_guard_result(_dh_result)
                            if _dh_verdict != "ok":
                                logger.warning(
                                    f"[PIPELINE] Auto-approve merge held for PR "
                                    f"#{pr_number} — Alembic combined-chain guard: "
                                    f"{_dh_reason}"
                                )
                                _dh_loop = {
                                    **((task.context or {}).get("review_loop") or {}),
                                    "ci_waiting": True,
                                    "review_approved": True,
                                    "ci_passed": True,
                                    "pr_number": pr_number,
                                    "branch": branch,
                                    "base_branch": base_branch,
                                    **_review_bypass_fields,
                                }
                                if _dh_verdict == "defer":
                                    task.status = TaskStatus.EVALUATING
                                    mark_migration_guard_deferred(
                                        task,
                                        review_loop=_dh_loop,
                                        reason=_dh_reason,
                                        context_snapshot=dict(task.context or {}),
                                    )
                                    session.add(task)
                                    await session.commit()
                                    return {"status": "accepted"}
                                reservation = await get_migration_reservation_for_task(
                                    session, task_id=task.id
                                )
                                if reservation is not None and branch and base_branch:
                                    try:
                                        _finalization = await attempt_pr_migration_finalization(
                                            gh_token,
                                            owner,
                                            repo,
                                            pr_number=pr_number,
                                            pr_branch=branch,
                                            target_branch=base_branch,
                                            promotion_target=(
                                                _shared_state.get_project_promotion_target(
                                                    _project
                                                )
                                                if _project is not None
                                                else None
                                            ),
                                            project_id=str(task.project_id),
                                        )
                                    except Exception as _mf_exc:
                                        logger.warning(
                                            "[PIPELINE] Migration finalization attempt "
                                            "failed for PR #%s: %s",
                                            pr_number,
                                            _mf_exc,
                                            exc_info=True,
                                        )
                                        _finalization = None
                                    if _finalization is not None:
                                        from app.task_completion import (
                                            _reset_review_loop_pr_head_cache,
                                        )
                                        from app.ci_merge_helpers import (
                                            revoke_review_authority_for_active_fix,
                                        )

                                        task.status = TaskStatus.EVALUATING
                                        task.latest_status_message = (
                                            "Merge held — auto-finalized provisional "
                                            "Alembic migration "
                                            f"{_finalization['old_revision']}->"
                                            f"{_finalization['finalized_revision']} "
                                            f"against {base_branch}; awaiting fresh CI "
                                            "on the rewritten PR head"
                                        )[:500]
                                        _finalized_loop = (
                                            _reset_review_loop_pr_head_cache(
                                                (task.context or {}).get("review_loop")
                                            )
                                        )
                                        _finalized_loop = (
                                            revoke_review_authority_for_active_fix(
                                                task, review_loop=_finalized_loop
                                            )
                                        )
                                        _finalized_loop.update(
                                            {
                                                "ci_waiting": True,
                                                "ci_passed": False,
                                                "authoritative_review_required": True,
                                                "pr_number": pr_number,
                                                "branch": branch,
                                                "base_branch": base_branch,
                                            }
                                        )
                                        task.context = {
                                            **(task.context or {}),
                                            "_migration_finalization": _finalization,
                                            "review_loop": _finalized_loop,
                                        }
                                        session.add(task)
                                        await session.commit()
                                        return {"status": "accepted"}
                                await spawn_migration_guard_fix_task(
                                    task,
                                    session,
                                    pr_number=pr_number,
                                    branch=branch,
                                    base_branch=base_branch,
                                    guard_reason=_dh_reason,
                                    review_loop=_dh_loop,
                                    context_snapshot=dict(task.context or {}),
                                    gh_token=gh_token,
                                    owner=owner,
                                    repo=repo,
                                )
                                session.add(task)
                                await session.commit()
                                return {"status": "accepted"}

                        # Recheck after scan/guard I/O. A changed base alters
                        # the effective diff and a changed head invalidates the
                        # evidence; either race requires an exact review.
                        _bypass_live_pr = await get_pull_request(
                            gh_token,
                            owner,
                            repo,
                            pr_number,
                            project_id=str(task.project_id),
                        )
                        _bypass_live_target = {
                            "project_id": str(task.project_id),
                            "pr_number": int(pr_number),
                            "base_sha": str(
                                (_bypass_live_pr.get("base") or {}).get("sha") or ""
                            ),
                            "head_sha": str(
                                (_bypass_live_pr.get("head") or {}).get("sha") or ""
                            ),
                        }
                        _bypass_loop = {
                            **((task.context or {}).get("review_loop") or {}),
                            "ci_passed": True,
                            "ci_waiting": False,
                            "pr_number": pr_number,
                            "branch": branch,
                            "base_branch": base_branch,
                            **_review_bypass_fields,
                        }
                        task.context = {
                            **(task.context or {}),
                            "review_loop": _bypass_loop,
                        }
                        flag_modified(task, "context")
                        from app.review_merge_authority import (
                            attempt_merge_with_authority,
                        )

                        async def _submit_bypass_merge(
                            expected_head_sha: str,
                        ) -> dict:
                            return await merge_pull_request(  # premerge-guard-exempt: migration PRs are parked above in the dual-head guard branch (returns "accepted"); this merge only runs for staging-only branches that never carry a migration onto the integration trunk
                                gh_token,
                                owner,
                                repo,
                                pr_number,
                                project_id=str(task.project_id),
                                merge_method=_pick_merge_method(task),
                                expected_head_sha=expected_head_sha,
                            )

                        _merge_authority, merge_result = (
                            await attempt_merge_with_authority(
                            task,
                            session,
                            merge_call=_submit_bypass_merge,
                            gh_token=gh_token,
                            owner=owner,
                            repo=repo,
                            ci_loop=_bypass_loop,
                            pr_number=pr_number,
                            branch=branch,
                            base_branch=base_branch,
                            original_task_id=str(task.id),
                            checked_head_sha=str(_bypass_live_target["head_sha"]),
                            checked_base_sha=str(_bypass_live_target["base_sha"]),
                            retry_nonterminal=True,
                        )
                        )
                        if merge_result is None:
                            session.add(task)
                            await session.commit()
                            return {"status": "accepted"}
                        if merge_result.get("merged"):
                            _record_pr_merged = is_intermediate
                            if not _record_pr_merged:
                                _record_pr_merged = await (
                                    merged_pr_counts_as_integration_landing(
                                        task,
                                        _project,
                                        session,
                                        pr_number=pr_number,
                                        merged_base_branch=base_branch,
                                        source="task_lifecycle.small_scope_auto_merge",
                                    )
                                )
                            if _record_pr_merged:
                                await corroborate_and_apply_merged_landed_signal(
                                    task,
                                    _project,
                                    session,
                                    base_branch=base_branch,
                                    hotfix_lane=bool(
                                        (task.first_payload or {}).get("hotfix")
                                        or (task.context or {}).get("hotfix")
                                    ),
                                    non_merge_reason="merged_pr_non_integration_branch",
                                    # Pin acceptance corroboration to the merge
                                    # commit the API just returned; the
                                    # branch-tip cache can still serve the
                                    # pre-merge tip for another 60s.
                                    merge_commit_sha=merge_result.get("sha"),
                                )
                            try:
                                from app.task_completion import (
                                    _finalize_migration_reservation_after_merge,
                                )

                                await _finalize_migration_reservation_after_merge(
                                    task,
                                    session,
                                    terminal_merge=not is_intermediate,
                                )
                            except Exception as _mr_exc:
                                logger.warning(
                                    "[MIGRATION] Failed to finalize reservation for "
                                    "merged task %s: %r",
                                    task.id,
                                    _mr_exc,
                                )
                            session.add(task)
                            _t_ctx = task.context or {}
                            # Audit W1: auto_remediation is now a column.
                            if _t_ctx.get("diagnosis_task_id") or (
                                task.auto_remediation and _t_ctx.get("direction_id")
                            ):
                                try:
                                    from app.task_completion import (
                                        _record_remediation_outcome,
                                    )

                                    await _record_remediation_outcome(
                                        task, merged=True, session=session
                                    )
                                except Exception as _rr_exc:
                                    logger.warning(
                                        f"[DIAGNOSIS] Failed to record remediation outcome on merge for task {task.id}: {_rr_exc!r}"
                                    )
                            await session.commit()
                            await _cleanup_branches(task, session)
                            # Completion-side warm-reuse trigger: the PR has
                            # landed on integration and the lock is released, so
                            # proactively mark any dependents this just unblocked
                            # as warm-claimable for this task's teardown-defer VM.
                            if _record_pr_merged:
                                await _mark_dependents_warm_claimable(task)
                                try:
                                    from app.reaper_sweeps import (
                                        event_driven_unblock_dependents,
                                    )

                                    await event_driven_unblock_dependents(task, session)
                                except Exception:
                                    logger.exception(
                                        "[PIPELINE] event_driven_unblock_dependents "
                                        "failed for task %s",
                                        task.id,
                                    )
                            logger.info(
                                f"[PIPELINE] Auto-merged PR #{pr_number} into {base_branch} "
                                f"(scope={scope}, intermediate={is_intermediate})"
                            )
                        else:
                            logger.error(
                                f"[PIPELINE] Auto-merge failed for PR #{pr_number}: {merge_result.get('message')}"
                            )
                            # Probe GitHub for the PR's mergeable_state so we can
                            # route the recovery correctly:
                            #   blocked / unstable / clean → park on CI and let
                            #     the shared exact-target authority boundary merge.
                            #   dirty → real merge conflict (sibling cluster touched
                            #     overlapping files between PR creation and the merge
                            #     attempt). Spawn a `fix` task — fix is the rebase-
                            #     and-resolve primitive; review is the assess-and-
                            #     give-feedback primitive, the wrong tool here.
                            #     Without this, cluster sub-PRs orphan: tasks COMPLETE,
                            #     the dep chain stays QUEUED on PR_MERGED, and the
                            #     research rollup stalls (see PR#4070, PR#4044 incident
                            #     on 2026-05-06).
                            #   default → fall back to review-spawn (preserves
                            #     existing behavior for unknown states).
                            #
                            # Reviews still fire downstream: when the fix task
                            # COMPLETES, the fix-completion handler at line ~2899
                            # spawns a review task before merging. So a DIRTY-routed
                            # cluster sub-PR gets: create → conflict → fix (rebase)
                            # → review → auto-merge. This change does not bypass
                            # review for tasks that need it.
                            recovery_type = "review"
                            try:
                                pr_detail = await get_pull_request(
                                    gh_token,
                                    owner,
                                    repo,
                                    pr_number,
                                    project_id=str(task.project_id),
                                )
                                ms = (pr_detail.get("mergeable_state") or "").lower()
                                if ms in ("blocked", "unstable", "clean"):
                                    # Native GitHub auto-merge is deliberately
                                    # not used: it has no expected-head compare-
                                    # and-swap and can publish a later push.
                                    recovery_type = None
                                    logger.info(
                                        f"[PIPELINE] PR #{pr_number} "
                                        f"(mergeable_state={ms}) — parking for "
                                        "Blueprint's exact-target CI merge"
                                    )
                                elif ms == "dirty":
                                    recovery_type = "fix"
                            except Exception as _am_exc:
                                logger.warning(
                                    f"[PIPELINE] Mergeable-state probe failed for PR #{pr_number}: {_am_exc}"
                                )
                            # Spawn the recovery task. For DIRTY conflicts on cluster
                            # sub-PRs we use `fix` (rebase + resolve); otherwise
                            # `review` (the existing default).
                            # When recovery_type is None, no spawn fires; the
                            # task parks for the shared exact-head CI merge.
                            #
                            # Pass fix_reason="merge_conflict" for lane/accounting
                            # provenance. Any pushed recovery fix invalidates this
                            # initial-review bypass and receives the standard bounded
                            # exact-head delta review before merge.
                            if recovery_type is None:
                                logger.info(
                                    f"[PIPELINE] Auto-merge deferred for PR #{pr_number} "
                                    f"(mergeable_state={ms}) — parking in CI-wait"
                                )
                                task.status = TaskStatus.EVALUATING
                                task.latest_status_message = (
                                    f"PR #{pr_number} parked for Blueprint's "
                                    "exact-target CI merge"
                                )
                                task.context = {
                                    **(task.context or {}),
                                    "review_loop": {
                                        "ci_waiting": True,
                                        "review_approved": True,
                                        "ci_passed": ms == "clean",
                                        "pr_number": pr_number,
                                        "branch": branch,
                                        "base_branch": base_branch,
                                        "original_task_id": str(task.id),
                                        "ci_wait_started": _utcnow().isoformat(),
                                        **_review_bypass_fields,
                                    },
                                }
                                session.add(task)
                                await session.commit()
                            else:
                                spawn_kwargs = {
                                    "original_prompt": original_prompt,
                                    "original_task_id": str(task.id),
                                }
                                if recovery_type == "fix":
                                    spawn_kwargs["fix_reason"] = "merge_conflict"
                                    spawn_kwargs["fix_prompt"] = (
                                        build_merge_conflict_fix_prompt(
                                            pr_number, base_branch
                                        )
                                    )
                                elif recovery_type == "review":
                                    spawn_kwargs["review_target"] = (
                                        await _resolve_live_review_target(
                                            task, session, pr_number
                                        )
                                    )
                                recovery_task, was_created = await _spawn_pipeline_task(
                                    task,
                                    recovery_type,
                                    session,
                                    pr_number,
                                    branch,
                                    base_branch,
                                    **spawn_kwargs,
                                )
                                if was_created:
                                    logger.info(
                                        f"[PIPELINE] Spawned {recovery_type} task {recovery_task.id} for PR #{pr_number} after auto-merge failure"
                                    )
                                elif recovery_task is not None:
                                    logger.info(
                                        f"[PIPELINE] Auto-merge failed for PR #{pr_number} — same-chain {recovery_task.first_payload.get('type')} task {recovery_task.id} ({recovery_task.status}) already handling it"
                                    )
                                else:
                                    logger.warning(
                                        f"[PIPELINE] Auto-merge failed and no {recovery_type} spawned for PR #{pr_number} (cap hit or guard) — parking in CI-wait"
                                    )
                                    task.status = TaskStatus.EVALUATING
                                    task.latest_status_message = f"Auto-merge failed for PR #{pr_number} — parked for CI-wait retry"
                                    task.context = {
                                        **(task.context or {}),
                                        "review_loop": {
                                            "ci_waiting": True,
                                            "pr_number": pr_number,
                                            "branch": branch,
                                            "base_branch": base_branch,
                                            "original_task_id": str(task.id),
                                            "ci_wait_started": _utcnow().isoformat(),
                                        },
                                    }
                                    session.add(task)
                                    await session.commit()
                    except Exception as e:
                        logger.error(
                            f"[PIPELINE] Auto-merge error for PR #{pr_number}: {e}",
                            exc_info=True,
                        )
                        # Fallback: spawn review so the pipeline doesn't die
                        try:
                            (
                                fallback_review,
                                fb_was_created,
                            ) = await _spawn_pipeline_task(
                                task,
                                "review",
                                session,
                                pr_number,
                                branch,
                                base_branch,
                                original_prompt=original_prompt,
                                original_task_id=str(task.id),
                                review_target=await _resolve_live_review_target(
                                    task, session, pr_number
                                ),
                            )
                            if fb_was_created:
                                logger.info(
                                    f"[PIPELINE] Spawned fallback review {fallback_review.id} for PR #{pr_number} after merge error"
                                )
                            elif fallback_review is not None:
                                logger.info(
                                    f"[PIPELINE] Merge errored for PR #{pr_number} — same-chain task {fallback_review.id} already handling it"
                                )
                            else:
                                logger.warning(
                                    f"[PIPELINE] Merge error and no fallback review spawned for PR #{pr_number} — parking in CI-wait"
                                )
                                task.status = TaskStatus.EVALUATING
                                task.latest_status_message = f"Auto-merge error for PR #{pr_number} — parked for CI-wait retry"
                                task.context = {
                                    **(task.context or {}),
                                    "review_loop": {
                                        "ci_waiting": True,
                                        "pr_number": pr_number,
                                        "branch": branch,
                                        "base_branch": base_branch,
                                        "original_task_id": str(task.id),
                                        "ci_wait_started": _utcnow().isoformat(),
                                    },
                                }
                                session.add(task)
                                await session.commit()
                        except Exception as inner_e:
                            logger.error(
                                f"[PIPELINE] Fallback review spawn failed for PR #{pr_number}: {inner_e}",
                                exc_info=True,
                            )
                            task.status = TaskStatus.EVALUATING
                            task.latest_status_message = f"Pipeline recovery: merge and review spawn failed for PR #{pr_number} — CI monitor will retry"
                            task.context = {
                                **(task.context or {}),
                                "review_loop": {
                                    "ci_waiting": True,
                                    "pr_number": pr_number,
                                    "branch": branch,
                                    "base_branch": base_branch,
                                    "original_task_id": str(task.id),
                                    "ci_wait_started": _utcnow().isoformat(),
                                },
                            }
                            session.add(task)
                            await session.commit()
                else:
                    # Full pipeline: security scan → review (for main targets or medium+ scope)
                    try:
                        gh_token, owner, repo = await get_github_token_for_task(
                            task.id, session
                        )
                        scan_result = await scan_pr_for_injection(
                            gh_token, owner, repo, pr_number
                        )
                        logger.info(f"[SECURITY] {scan_result['summary']}")
                        if scan_result["blocked"]:
                            task.requires_approval = True
                            task.latest_status_message = scan_result["summary"]
                            session.add(task)
                            await session.commit()
                        if scan_result["findings"]:
                            await create_pr_comment(
                                gh_token, owner, repo, pr_number, scan_result["report"]
                            )
                    except Exception as e:
                        if _is_transient_scan_error(e):
                            logger.warning(
                                f"[SECURITY] Scan transient error for PR #{pr_number}, retrying: {e}"
                            )
                            await asyncio.sleep(5)
                            try:
                                scan_result = await scan_pr_for_injection(
                                    gh_token, owner, repo, pr_number
                                )
                                logger.info(
                                    f"[SECURITY] Scan retry succeeded: {scan_result['summary']}"
                                )
                                if scan_result["blocked"]:
                                    task.requires_approval = True
                                    task.latest_status_message = scan_result["summary"]
                                    session.add(task)
                                    await session.commit()
                                if scan_result["findings"]:
                                    await create_pr_comment(
                                        gh_token,
                                        owner,
                                        repo,
                                        pr_number,
                                        scan_result["report"],
                                    )
                            except Exception as retry_exc:
                                logger.error(
                                    f"[SECURITY] Scan retry also failed for PR #{pr_number}: {retry_exc}",
                                    exc_info=True,
                                )
                                task.requires_approval = True
                                task.context = {
                                    **(task.context or {}),
                                    "scan_transient_failure": True,
                                }
                                task.latest_status_message = _safe_exception_message(
                                    retry_exc,
                                    "Security scan unavailable (infrastructure error) — review will proceed but merge requires manual approval",
                                )
                                session.add(task)
                                await session.commit()
                        else:
                            logger.error(
                                f"[SECURITY] Scan failed for PR #{pr_number}, flagging for approval: {e}",
                                exc_info=True,
                            )
                            task.requires_approval = True
                            task.latest_status_message = _safe_exception_message(
                                e,
                                "Security scan failed — review will proceed but merge requires manual approval",
                            )
                            session.add(task)
                            await session.commit()

                    # PR file-path flagging: flag infrastructure/security-critical file changes
                    # Don't block here — let review run, block at merge time.
                    # Gated by require_approval_gates — uses project config if available,
                    # falls back to Blueprint defaults for self-repo.
                    if _runtime_config.get("require_approval_gates", True):
                        try:
                            pr_files = await get_pr_files(
                                gh_token, owner, repo, pr_number
                            )
                            # Fetch project to get approval gate config
                            project = await session.get(Project, task.project_id)
                            INFRA_FILES, CRITICAL_FILES = _get_approval_gate_file_lists(
                                project
                            )
                            touched_infra = [
                                f["filename"]
                                for f in pr_files
                                if f["filename"] in INFRA_FILES
                            ]
                            touched_critical = [
                                f for f in touched_infra if f in CRITICAL_FILES
                            ]
                            if touched_infra:
                                task.context = {
                                    **(task.context or {}),
                                    "extended_monitoring": True,
                                    "risk_tier": "large",
                                    "touched_infra": touched_infra,
                                    "touched_critical": touched_critical,
                                }
                                task.latest_status_message = f"Infrastructure change flagged: {', '.join(touched_infra)}"
                                logger.info(
                                    f"[PIPELINE] PR #{pr_number} touches infra files: {touched_infra} (critical: {touched_critical})"
                                )
                                session.add(task)
                                await session.commit()
                                _shared_state.deploy_monitoring["is_extended"] = True
                        except Exception as e:
                            logger.info(
                                f"[PIPELINE] Could not check PR files for infra flagging: {e}"
                            )

                    # Full model review — only reached for large-scope intermediate
                    # (staging branch) tasks that feed into rollup PRs (the v24 cost
                    # plan skips review for non-rollup single tasks landing directly
                    # into automain; CI + the promotion gate backstop those).
                    # requires_approval gates MERGING (checked at merge time in the
                    # review-complete path), not reviewing. A security scan failure
                    # or blocking finding should not orphan the PR by preventing
                    # review entirely.
                    review_task, _ = await _spawn_pipeline_task(
                        task,
                        "review",
                        session,
                        pr_number,
                        branch,
                        base_branch,
                        original_prompt=original_prompt,
                        original_task_id=str(task.id),
                        review_target=await _resolve_live_review_target(
                            task, session, pr_number
                        ),
                    )
                    if review_task:
                        if task.requires_approval:
                            logger.info(
                                f"[REVIEW PIPELINE] Spawned review for flagged PR #{pr_number} → task {review_task.id} (merge will require manual approval)"
                            )
                        else:
                            logger.info(
                                f"[REVIEW PIPELINE] Spawned review for PR #{pr_number} → task {review_task.id}"
                            )
                    else:
                        # Truthful refusal: _spawn_pipeline_task records why it
                        # returned None in SPAWN_REASON_KEY (stuck_cycle,
                        # cap_exhausted, ...) — surface that instead of a
                        # blanket "iteration cap reached".
                        _review_refusal_label = _pipeline_spawn_refusal_label(task)
                        task.latest_status_message = (
                            f"Review not spawned for PR #{pr_number} — "
                            f"{_review_refusal_label}"
                        )
                        task.requires_approval = True
                        session.add(task)
                        await session.commit()
                        logger.warning(
                            f"[REVIEW PIPELINE] WARNING: Review spawn returned None for PR #{pr_number} ({_review_refusal_label})"
                        )

            # Review task completed → parse verdict
            elif task_type == "review":
                pr_number = review_loop.get("pr_number") or task_payload.get(
                    "pr_number"
                )
                branch = review_loop.get("branch") or task_payload.get("branch")
                _review_project = await session.get(Project, task.project_id)
                base_branch = (
                    review_loop.get("base_branch")
                    or task_payload.get("base_branch")
                    or _shared_state.get_project_integration_branch(_review_project)
                )
                original_task_id = review_loop.get(
                    "original_task_id", ""
                ) or task_payload.get("original_task_id", "")
                original_prompt = task_payload.get("original_prompt", "")
                if not pr_number or not branch:
                    logger.info(
                        f"[REVIEW PIPELINE] Review task {task.id} missing pr_number ({pr_number}) or branch ({branch}) — cannot continue pipeline"
                    )
                    task.latest_status_message = (
                        "Review pipeline error: missing pr_number or branch"
                    )
                    session.add(task)
                    await session.commit()
                    return {"status": "accepted"}

                # Early exit: skip only when the PR is genuinely merged/done.
                # C4: this MUST gate on merged==True, NOT on state=='closed'.
                # A non-merged 'closed' state may be a transient self-close
                # from the orchestrator's mergeability-reset loop (close+reopen
                # to force GitHub to recompute mergeable=None — see
                # orchestrator.py close/reopen nudge). Treating that transient
                # closed window as "review skipped" silently DROPS the
                # review_approved handoff to the parent, stranding the rollup
                # in EVALUATING until the 60-min/6h backstop fires. Gating on
                # merged only — and falling through on closed-not-merged so the
                # review verdict still reaches the parent — mirrors the fix
                # handler's already-corrected pattern (PR #5662).
                try:
                    (
                        gh_token_check,
                        owner_check,
                        repo_check,
                    ) = await get_github_token_for_task(task.id, session)
                    pr_data_check = await get_pull_request(
                        gh_token_check, owner_check, repo_check, pr_number
                    )
                    if pr_data_check.get("merged"):
                        logger.info(
                            f"[REVIEW PIPELINE] PR #{pr_number} already merged — skipping review pipeline for task {task.id}"
                        )
                        task.latest_status_message = (
                            f"PR #{pr_number} already merged — review skipped"
                        )
                        session.add(task)
                        await session.commit()
                        return {"status": "accepted"}
                    if pr_data_check.get("state") == "closed":
                        # Closed but not merged — could be a transient
                        # mergeability-reset. Proceed with the review so the
                        # review_approved handoff is preserved; the merge gate
                        # handles a genuinely abandoned PR gracefully.
                        logger.info(
                            f"[REVIEW PIPELINE] PR #{pr_number} is closed (not merged) for task {task.id} — "
                            f"may be a transient mergeability-reset; proceeding with review"
                        )
                except Exception as e:
                    logger.info(
                        f"[REVIEW PIPELINE] Could not check PR #{pr_number} state: {e} — proceeding with review"
                    )

                verdict_text = payload.logs or task.latest_status_message or ""
                verdict = "REQUEST_CHANGES"
                feedback = verdict_text
                issues = []
                resolutions: list[dict] = []
                investigated_files: list[str] = []

                # Parse verdict: prefer structured verdict_json field, fall back to log parsing
                verdict_parsed = False
                verdict_unparseable = False
                review_was_parallelized = False
                reviewed_head_sha: str = ""
                reviewed_base_sha: str = ""
                # Method 1: verdict_json field (from /tmp/verdict.json on the agent)
                if payload.verdict_json:
                    try:
                        vj = json.loads(payload.verdict_json)
                        if vj.get("verdict"):
                            verdict = vj["verdict"]
                            feedback = vj.get("feedback", verdict_text)
                            issues = (
                                vj.get("issues", [])
                                if isinstance(vj.get("issues"), list)
                                else []
                            )
                            resolutions = (
                                vj.get("resolutions", [])
                                if isinstance(vj.get("resolutions"), list)
                                else []
                            )
                            raw_investigated = vj.get("investigated_files")
                            if isinstance(raw_investigated, list):
                                investigated_files = [
                                    str(p)
                                    for p in raw_investigated
                                    if isinstance(p, str) and p
                                ][:25]
                            review_was_parallelized = bool(
                                vj.get("review_was_parallelized", False)
                            )
                            reviewed_head_sha = str(vj.get("reviewed_head_sha") or "")
                            reviewed_base_sha = str(vj.get("reviewed_base_sha") or "")
                            verdict_parsed = True
                            logger.info(
                                f"[REVIEW PIPELINE] Verdict from verdict_json field: {verdict}"
                            )
                    except (json.JSONDecodeError, TypeError):
                        logger.exception(
                            f"[REVIEW PIPELINE] verdict_json field present but invalid for task {task.id}"
                        )

                # Method 2: Parse JSON from log output
                if not verdict_parsed:
                    try:
                        candidate = scan_balanced_json_objects(
                            verdict_text, required_key="verdict", last=True
                        )
                        if candidate is not None:
                            verdict = candidate["verdict"]
                            feedback = candidate.get("feedback", verdict_text)
                            issues = (
                                candidate.get("issues", [])
                                if isinstance(candidate.get("issues"), list)
                                else []
                            )
                            resolutions = (
                                candidate.get("resolutions", [])
                                if isinstance(candidate.get("resolutions"), list)
                                else []
                            )
                            reviewed_head_sha = str(
                                candidate.get("reviewed_head_sha") or ""
                            )
                            reviewed_base_sha = str(
                                candidate.get("reviewed_base_sha") or ""
                            )
                            verdict_parsed = True
                    except Exception as e:
                        logger.error(
                            f"[REVIEW PIPELINE] Verdict parsing error for task {task.id}: {e}",
                            exc_info=True,
                        )

                if not verdict_parsed:
                    # Fallback: require a STRUCTURED / anchored verdict signal — NOT a
                    # bare prose substring (see _resolve_fallback_verdict). Returns the
                    # canonical verdict, or None when no anchored verdict is present.
                    fallback_verdict = _resolve_fallback_verdict(verdict_text)
                    if fallback_verdict is None:
                        verdict = "REQUEST_CHANGES"
                        feedback = "Review agent did not produce a verdict — manual review required"
                        logger.warning(
                            f"[REVIEW PIPELINE] No verdict found for task {task.id} (len={len(verdict_text)}) — defaulting to REQUEST_CHANGES, marking for manual review"
                        )
                        verdict_unparseable = True
                    else:
                        verdict = fallback_verdict

                # Invariant: pipeline accepts only APPROVE or REQUEST_CHANGES.
                # Legacy COMMENT, typos, and other agent regressions normalize to
                # REQUEST_CHANGES so the pipeline always has a well-defined next step.
                if verdict not in ("APPROVE", "REQUEST_CHANGES"):
                    logger.warning(
                        f"[REVIEW PIPELINE] Task {task.id} produced unsupported verdict "
                        f"{verdict!r} — normalizing to REQUEST_CHANGES"
                    )
                    verdict = "REQUEST_CHANGES"

                # Mechanical hedge detector: scan the PR diff for unjustified
                # default-off/observe/fail-open patterns the reviewer may have
                # missed (or whose introduction the reviewer approved). Findings
                # are MAJOR-severity issues that survive the reconciliation logic
                # as genuine blockers. The detector is pure and synchronous;
                # only the GitHub API call is async.
                _hedge_findings: list[dict] = []
                try:
                    (
                        _hd_token,
                        _hd_owner,
                        _hd_repo,
                    ) = await get_github_token_for_task(task.id, session)
                    if pr_number and _hd_token:
                        _hd_files = await get_pr_files(
                            _hd_token,
                            _hd_owner,
                            _hd_repo,
                            pr_number,
                            project_id=str(task.project_id),
                            include_patch=True,
                        )
                        if _hd_files:
                            from app.review_heuristics import detect_hedges_in_diff

                            _hedge_findings = detect_hedges_in_diff(_hd_files)
                except Exception:
                    logger.warning(
                        f"[REVIEW PIPELINE] Hedge detection failed for PR "
                        f"#{pr_number} (task {task.id}) — skipping mechanical "
                        f"hedge check",
                        exc_info=True,
                    )

                if _hedge_findings:
                    # Merge findings into the issues list, avoiding duplicates
                    # by exact description match.
                    _existing_descs = {
                        i.get("description", "")
                        for i in issues
                        if isinstance(i, dict)
                    }
                    for hf in _hedge_findings:
                        if isinstance(hf, dict) and hf.get("description") not in _existing_descs:
                            issues.append(hf)
                            _existing_descs.add(hf.get("description", ""))
                    # If the reviewer approved despite the hedge, override to
                    # REQUEST_CHANGES so the fix loop addresses it.
                    if verdict == "APPROVE":
                        logger.warning(
                            f"[REVIEW PIPELINE] PR #{pr_number}: hedge detector "
                            f"found {len(_hedge_findings)} unjustified hedge(s) "
                            f"in APPROVED diff — overriding verdict to "
                            f"REQUEST_CHANGES"
                        )
                        verdict = "REQUEST_CHANGES"

                # REQUEST_CHANGES unsupported by its own evidence → reconcile to
                # APPROVE. The decision must follow the reviewer's structured
                # ``issues``, not the free-text ``verdict`` token taken on faith —
                # that decoupling let a reviewer stamp REQUEST_CHANGES while its own
                # findings cleared the PR, churning the fix loop to the iteration
                # cap on a green, mergeable PR (PR #9678).
                # ``_genuine_blocking_issue_exists`` encodes the review contract
                # (agent/src/prompts/review-prompt.ts): a genuine blocker is a
                # CRITICAL/MAJOR issue that is not purely verification-class, so an
                # empty / all-MINOR / verification-only set (the old
                # _is_verification_only_rejection case, now subsumed) is no genuine
                # objection. CONSERVATIVE — flips ONLY with no genuine blocker AND
                # AUTHORITATIVELY-green CI (the same check-run poll the merge gate
                # uses, treat_missing_as_success=False, conclusion=="success"); any
                # blocker or non-green CI keeps REQUEST_CHANGES + spawns a fix
                # (fail-safe). Reconciled trunk APPROVEs still pass second-opinion.
                if (
                    verdict == "REQUEST_CHANGES"
                    and not verdict_unparseable
                    and not _genuine_blocking_issue_exists(issues)
                ):
                    _ci_green_for_downgrade = False
                    try:
                        (
                            _vd_gh,
                            _vd_owner,
                            _vd_repo,
                        ) = await get_github_token_for_task(task.id, session)
                        _vd_pr = await get_pull_request(
                            _vd_gh, _vd_owner, _vd_repo, pr_number
                        )
                        _vd_head_sha = _vd_pr.get("head", {}).get("sha", "")
                        if _vd_head_sha:
                            _vd_ci = await get_check_runs_cached(
                                _vd_gh,
                                _vd_owner,
                                _vd_repo,
                                _vd_head_sha,
                                project_id=str(task.project_id),
                                treat_missing_as_success=False,
                            )
                            # Fail-safe: a transient GitHub API error is tagged
                            # _from_api_error and must NOT count as green. Only
                            # an authoritative completed+success check-run set
                            # flips the verdict.
                            _ci_green_for_downgrade = (
                                not _vd_ci.get("_from_api_error")
                                and _vd_ci.get("status") == "completed"
                                and _vd_ci.get("conclusion") == "success"
                            )
                    except Exception as _vd_err:
                        logger.warning(
                            f"[REVIEW PIPELINE] PR #{pr_number}: verification-only "
                            f"verdict-flip CI check failed ({_vd_err!r}) — keeping "
                            f"REQUEST_CHANGES blocking (fail-safe)"
                        )
                        _ci_green_for_downgrade = False

                    if _ci_green_for_downgrade:
                        _markers, _vd_note = reconciled_approve_markers(
                            issues, feedback
                        )
                        logger.warning(
                            "[REVIEW PIPELINE] PR #%s: REQUEST_CHANGES carries no "
                            "genuine blocking issue and CI is authoritatively GREEN "
                            "— recording APPROVE (%s); not spawning a fix iteration.",
                            pr_number,
                            "+".join(sorted(_markers)),
                        )
                        verdict = "APPROVE"
                        task.context = {**(task.context or {}), **_markers}
                        session.add(task)
                        feedback = f"{_vd_note}\n\n{feedback}" if feedback else _vd_note
                        # These entries describe missing local verification, not
                        # code defects.  Once authoritative CI proves the gate,
                        # do not feed them into the binding blocker ledger that
                        # intentionally ingests structured findings independent
                        # of a legacy/malformed verdict token.
                        issues = []

                # Authoritative current-head blocker ledger. The existing
                # review_history used to be advisory: any later APPROVE (or
                # simple omission) made an earlier REQUEST_CHANGES disappear,
                # even when both reviews examined the identical commit. Attach
                # stable identities, then reconcile this verdict against the
                # binding open set. A changed exact head can prove a fix; the
                # same head requires an explicit refuted/adjudicated disposition.
                _review_iteration = int(review_loop.get("iteration") or 0)
                issues = ensure_issue_ids(
                    issues,
                    reviewed_head_sha=reviewed_head_sha,
                    iteration=_review_iteration,
                )
                _current_review_entry = {
                    "iteration": _review_iteration,
                    "verdict": verdict,
                    "feedback": feedback,
                    "issues": issues,
                    "resolutions": resolutions,
                    "reviewed_head_sha": reviewed_head_sha,
                    "reviewed_base_sha": reviewed_base_sha,
                    **(
                        {"delta_base_sha": review_loop.get("delta_base_sha")}
                        if review_loop.get("delta_base_sha")
                        else {}
                    ),
                }
                _completion_review_target = await _resolve_live_review_target(
                    task, session, pr_number
                )
                _assigned_review_target = review_loop.get("review_target")
                try:
                    _assigned_pr_number = int(
                        (_assigned_review_target or {}).get("pr_number") or 0
                    )
                except (AttributeError, TypeError, ValueError):
                    _assigned_pr_number = 0
                _assigned_target_matches = bool(
                    isinstance(_assigned_review_target, dict)
                    and str(_assigned_review_target.get("project_id") or "")
                    == str(task.project_id)
                    and _assigned_pr_number == int(pr_number)
                    and reviewed_head_sha
                    == str(_assigned_review_target.get("head_sha") or "")
                    and reviewed_base_sha
                    == str(_assigned_review_target.get("base_sha") or "")
                )
                if (
                    _assigned_target_matches
                    and _completion_review_target is not None
                    and reviewed_head_sha
                    == str(_completion_review_target.get("head_sha") or "")
                    and reviewed_base_sha
                    == str(_completion_review_target.get("base_sha") or "")
                ):
                    # Only the backend may grant closure provenance.  Agent-
                    # supplied SHAs are compared first, then overwritten from
                    # the server target and bound to this durable review row.
                    _current_review_entry.update(
                        {
                            "reviewed_head_sha": str(
                                _completion_review_target["head_sha"]
                            ),
                            "reviewed_base_sha": str(
                                _completion_review_target["base_sha"]
                            ),
                            "target_verified_by_backend": True,
                            "review_task_id": str(task.id),
                        }
                    )
                _prior_issue_ledger = review_loop.get("issue_ledger")
                _authoritative_ledger = build_issue_ledger(
                    [
                        *(review_loop.get("review_history") or []),
                        _current_review_entry,
                    ],
                    **(
                        {"seed_ledger": _prior_issue_ledger}
                        if _prior_issue_ledger is not None
                        else {}
                    ),
                    stamp_complete=True,
                )
                _open_blockers = list(_authoritative_ledger.get("open") or [])
                if _open_blockers:
                    # The fix-facing issue list is always the complete binding
                    # open set, not just what the latest reviewer happened to
                    # repeat. This closes the same-head A-omitted/B-raised gap:
                    # the backend ledger, not reviewer omission, owns closure.
                    issues = [
                        {
                            "issue_id": blocker.get("issue_id"),
                            "severity": blocker.get("severity", "major"),
                            "description": blocker.get("description", ""),
                            **(
                                {"file": blocker.get("file")}
                                if blocker.get("file")
                                else {}
                            ),
                            **(
                                {"line": blocker.get("line")}
                                if blocker.get("line") is not None
                                else {}
                            ),
                            **(
                                {"symbol": blocker.get("symbol")}
                                if blocker.get("symbol")
                                else {}
                            ),
                        }
                        for blocker in _open_blockers
                    ]
                if verdict == "APPROVE" and _open_blockers:
                    logger.warning(
                        "[REVIEW PIPELINE] PR #%s: APPROVE cannot clear %d "
                        "unresolved blocker(s) on the same/unproven head; "
                        "continuing through the fix + exact-SHA delta-review path",
                        pr_number,
                        len(_open_blockers),
                    )
                    verdict = "REQUEST_CHANGES"
                    _ledger_note = (
                        "Approval withheld: the following prior blocker(s) have "
                        "no authoritative disposition on this exact head. Each "
                        "must be fixed on a new commit, explicitly refuted, or "
                        "adjudicated: "
                        + ", ".join(
                            str(blocker.get("issue_id") or "unknown")
                            for blocker in _open_blockers
                        )
                    )
                    feedback = (
                        f"{_ledger_note}\n\n{feedback}" if feedback else _ledger_note
                    )
                _current_review_entry = {
                    **_current_review_entry,
                    "verdict": verdict,
                    "feedback": feedback,
                    "issues": list(issues),
                }
                task.context = {
                    **(task.context or {}),
                    "issue_ledger": _authoritative_ledger,
                }
                flag_modified(task, "context")
                session.add(task)

                # If verdict was unparseable, flag the original task for manual review
                if verdict_unparseable:
                    source_loop = (task.context or {}).get("review_loop", {})
                    original_task_id = source_loop.get("original_task_id")
                    if original_task_id:
                        try:
                            orig = await session.get(Task, uuid.UUID(original_task_id))
                            if orig:
                                orig.requires_approval = True
                                orig.latest_status_message = "Review agent did not produce a verdict — manual review required"
                                session.add(orig)
                                await session.commit()
                        except Exception as e:
                            logger.warning(
                                f"[REVIEW PIPELINE] Failed to mark requires_approval on {original_task_id}: {e}",
                                exc_info=True,
                            )

                # Post the review to the PR via GitHub Reviews API (falls back to plain comment)
                try:
                    gh_token, owner, repo = await get_github_token_for_task(
                        task.id, session
                    )
                    event = "APPROVE" if verdict == "APPROVE" else "REQUEST_CHANGES"
                    inline_comments = []
                    body_issues = []
                    for issue in issues:
                        if issue.get("file") and issue.get("line"):
                            sev = issue.get("severity", "minor").upper()
                            inline_comments.append(
                                {
                                    "path": issue["file"],
                                    "line": issue["line"],
                                    "body": f"[{sev}] {issue.get('description', '')}",
                                }
                            )
                        else:
                            body_issues.append(issue)
                    review_body = (
                        f"**Blueprint Code Review** ({verdict})\n\n{feedback[:3000]}"
                    )
                    if body_issues:
                        issues_text = "\n".join(
                            f"- [{i.get('severity', '?').upper()}] {i.get('description', '')}"
                            for i in body_issues
                        )
                        review_body += f"\n\n**Issues:**\n{issues_text}"
                    result = await submit_pr_review(
                        gh_token,
                        owner,
                        repo,
                        pr_number,
                        event,
                        review_body,
                        inline_comments,
                    )
                    if result is None:
                        raise ValueError("PR not found")
                except Exception as e:
                    logger.warning(
                        f"[REVIEW PIPELINE] submit_pr_review failed, falling back: {e}"
                    )
                    try:
                        gh_token, owner, repo = await get_github_token_for_task(
                            task.id, session
                        )
                        review_comment = f"**Blueprint Code Review** ({verdict})\n\n{feedback[:3000]}"
                        await create_pr_comment(
                            gh_token, owner, repo, pr_number, review_comment
                        )
                    except Exception as e2:
                        logger.warning(
                            f"[REVIEW PIPELINE] Fallback comment also failed: {e2}",
                            exc_info=True,
                        )

                # Update Check Run with verdict conclusion (best-effort — errors are swallowed)
                check_run_id = (task.context or {}).get("check_run_id")
                if check_run_id:
                    try:
                        (
                            _cr_token,
                            _cr_owner,
                            _cr_repo,
                        ) = await get_github_token_for_task(task.id, session)
                        _severity_map = {
                            "critical": "failure",
                            "major": "warning",
                            "minor": "notice",
                        }
                        _conclusion = (
                            "success" if verdict == "APPROVE" else "action_required"
                        )
                        _annotations = []
                        for _issue in issues:
                            if _issue.get("file") and _issue.get("line"):
                                _lvl = _severity_map.get(
                                    _issue.get("severity", "minor"), "notice"
                                )
                                _ln = int(_issue["line"])
                                _annotations.append(
                                    {
                                        "path": _issue["file"],
                                        "start_line": _ln,
                                        "end_line": _ln,
                                        "annotation_level": _lvl,
                                        "message": _issue.get("description", ""),
                                    }
                                )
                        _issue_counts = {}
                        for _issue in issues:
                            _sev = _issue.get("severity", "minor")
                            _issue_counts[_sev] = _issue_counts.get(_sev, 0) + 1
                        _counts_line = (
                            ", ".join(
                                f"{v} {k}" for k, v in sorted(_issue_counts.items())
                            )
                            if _issue_counts
                            else "no issues"
                        )
                        _cr_summary = (
                            f"**Blueprint Code Review**: {verdict}\n\n{feedback[:3000]}"
                        )
                        if _issue_counts:
                            _cr_summary += f"\n\n**Issue summary**: {_counts_line}"
                        await update_check_run(
                            _cr_token,
                            _cr_owner,
                            _cr_repo,
                            check_run_id,
                            conclusion=_conclusion,
                            title=f"Blueprint Code Review: {verdict}",
                            summary=_cr_summary,
                            annotations=_annotations or None,
                        )
                        logger.info(
                            f"[CHECK RUN] Updated check run {check_run_id} → {_conclusion} for task {task.id}"
                        )
                    except Exception as _cr_exc:
                        logger.warning(
                            f"[CHECK RUN] Failed to update check run {check_run_id} for task {task.id}: {_cr_exc}"
                        )

                # Store Blueprint's verdict on the review task context so the
                # pull_request_review webhook handler can correlate quality signals.
                task.context = {
                    **(task.context or {}),
                    "blueprint_review_verdict": verdict,
                    "review_was_parallelized": review_was_parallelized,
                }
                session.add(task)

                def _record_structured_review_result(
                    target: dict[str, str | int] | None,
                ) -> None:
                    """Persist only replay-safe structured results for exact-target reuse."""
                    if not verdict_parsed or verdict_unparseable or not target:
                        return
                    replay_verdict = {
                        "verdict": verdict,
                        "feedback": feedback,
                        "issues": issues,
                        "resolutions": resolutions,
                        "investigated_files": investigated_files,
                        # Cache only a verdict whose agent-stamped SHAs matched
                        # the live target; preserve both for replay validation.
                        "reviewed_head_sha": target["head_sha"],
                        "reviewed_base_sha": target["base_sha"],
                    }
                    task.context = {
                        **(task.context or {}),
                        "review_result": {
                            "structured": True,
                            "verdict": verdict,
                            "verdict_json": json.dumps(replay_verdict),
                        },
                        "review_loop": {
                            **review_loop,
                            "review_target": target,
                        },
                    }
                    session.add(task)

                action_taken = False
                head_sha = ""
                if verdict == "APPROVE":
                    # Retire the chain's lane now that the review approved — no fix
                    # will be spawned, so the lane must reach a terminal state here
                    # rather than staying PARKED until the warm-pool hard ceiling.
                    try:
                        from app.review_fix_lane_pilot import (
                            retire_chain_lane as _retire_lane_approve,
                        )

                        await _retire_lane_approve(session, task.context or {})
                    except Exception:
                        logger.debug(
                            "[PIPELINE] retire_chain_lane (APPROVE) failed (non-critical)",
                            exc_info=True,
                        )

                    # Validate immutable review provenance before *any* approval
                    # side effect, including the rollup-parent handoff below.
                    # Previously the direct-merge path checked SHAs later, but
                    # the rollup path could set parent.review_approved and merge
                    # before reaching that guard.
                    _live_approve_target = await _resolve_live_review_target(
                        task, session, pr_number
                    )
                    if _live_approve_target is None:
                        task.status = TaskStatus.COMPLETED
                        task.requires_approval = True
                        task.latest_status_message = (
                            f"Approval for PR #{pr_number} withheld: could not prove "
                            "the live immutable review target"
                        )
                        session.add(task)
                        await session.commit()
                        return {"status": "accepted"}
                    if (
                        not reviewed_head_sha
                        or not reviewed_base_sha
                        or reviewed_head_sha != _live_approve_target["head_sha"]
                        or reviewed_base_sha != _live_approve_target["base_sha"]
                    ):
                        task.status = TaskStatus.COMPLETED
                        task.latest_status_message = (
                            f"Stale/unproven approval discarded for PR #{pr_number} "
                            f"(reviewed {reviewed_base_sha[:8]}..{reviewed_head_sha[:8]}, "
                            f"now {str(_live_approve_target['base_sha'])[:8]}.."
                            f"{str(_live_approve_target['head_sha'])[:8]}) — "
                            "respawning exact-target review"
                        )
                        await _persist_stale_review_evidence(
                            task,
                            session,
                            original_task_id=original_task_id,
                            pr_number=pr_number,
                            review_loop=review_loop,
                            review_entry=_current_review_entry,
                            live_target=_live_approve_target,
                        )
                        task.context = {
                            **(task.context or {}),
                            "_review_stale_head": True,
                        }
                        flag_modified(task, "context")
                        session.add(task)
                        await _spawn_pipeline_task(
                            task,
                            "review",
                            session,
                            pr_number,
                            branch,
                            base_branch,
                            original_task_id=original_task_id,
                            review_target=_live_approve_target,
                        )
                        await session.commit()
                        return {"status": "accepted"}

                    # Persist one canonical exact-target approval proof before
                    # branching into rollup handoff, CI wait, guarded merge, or
                    # retry parking. Rebuilding it once avoids duplicated PR-file
                    # fetches while giving every merge boundary the same evidence.
                    _approved_history = bound_review_history(
                        [
                            *(review_loop.get("review_history") or []),
                            _current_review_entry,
                        ]
                    )
                    review_loop = {
                        **review_loop,
                        "review_approved": True,
                        "review_history": _approved_history,
                        "issue_ledger": _authoritative_ledger,
                        "review_target": dict(_live_approve_target),
                        "head_sha": str(_live_approve_target["head_sha"]),
                        "base_sha": str(_live_approve_target["base_sha"]),
                    }
                    review_loop.pop("review_not_required", None)
                    review_loop.pop("review_policy_reason", None)
                    # Keep the exact approval local until the next authority
                    # boundary.  Persisting it here dirtied the Task before the
                    # original-task/project peeks below; production autoflush
                    # then acquired Task before PR/base advisory locks and
                    # deadlocked with admission's PR -> Task order.
                    # Rollup gate handoff: when the original task is parking in CI wait
                    # (rollup-to-main / large rollup path in rollup_management), the parent
                    # owns the merge gate. The review task must NOT merge directly — it
                    # writes review_approved=True on the parent and lets the CI gate fire
                    # the merge once both signals are in. Without this branch, the review
                    # races the parent's CI poll and whichever finishes first wins, which
                    # is exactly the "merged before review" bug.
                    if original_task_id:
                        try:
                            from app.ci_completion import (
                                _attempt_merge_with_retry_and_escalation,
                            )
                            from app.routers.agent import get_github_token_for_task

                            # ── Site 2 lock-during-IO fix ──
                            # Previously we acquired FOR UPDATE on `_orig`
                            # unconditionally, then only the
                            # `if _orig and _orig_loop.get("ci_waiting"):` branch
                            # consumed the lock. The fall-through (review
                            # task's own merge cluster: get_pull_request,
                            # create_pr_comment, get_check_runs, get_pr_files,
                            # merge_pull_request, delete_branch — 60-120s) ran
                            # with `_orig`'s row lock still held by this
                            # session, blocking concurrent CI/heartbeat writes
                            # on the rollup parent. Fix: do a non-locking read
                            # to peek at ci_waiting first, and only acquire
                            # FOR UPDATE when we're committing to the rollup-
                            # handoff branch.
                            _orig_peek = (
                                (
                                    await session.execute(
                                        select(Task.context).where(
                                            Task.id == uuid.UUID(original_task_id)
                                        )
                                    )
                                )
                                .scalars()
                                .first()
                            )
                            _orig_peek_loop = (
                                (_orig_peek or {}).get("review_loop", {})
                                if _orig_peek
                                else {}
                            )
                            if _orig_peek_loop.get("ci_waiting"):
                                # ── SITE A lock-window fix ──
                                # When ci_waiting AND ci_passed are both set the
                                # rollup-handoff merge fires immediately after
                                # the lock. The token mint (get_github_token_for_task
                                # — up to ~120 s under rate-limit) and the pre-merge
                                # Alembic dual-head guard (enforce_premerge_migration_guard
                                # — GitHub PR-files + list_versions_at_ref fetches)
                                # previously ran WHILE the FOR UPDATE on _orig was
                                # held, blocking concurrent CI/heartbeat writes on
                                # the rollup parent row.  Both calls are read-only
                                # against the immutable PR, so hoist them here —
                                # before _lock_original_task — and pass the results
                                # in so _attempt_merge_with_retry_and_escalation
                                # does not repeat the guard under the lock.
                                _pre_rollup_gh: str | None = None
                                _pre_rollup_own: str | None = None
                                _pre_rollup_rep: str | None = None
                                _pre_rollup_mig_guard: object | None = None
                                _pre_rollup_size_cap: (
                                    tuple[str, str | None] | None
                                ) = None
                                if _orig_peek_loop.get("ci_passed"):
                                    _peek_pr_num = _orig_peek_loop.get("pr_number")
                                    _peek_base = _orig_peek_loop.get("base_branch", "")
                                    _peek_branch = _orig_peek_loop.get("branch", "")
                                    try:
                                        (
                                            _pre_rollup_gh,
                                            _pre_rollup_own,
                                            _pre_rollup_rep,
                                        ) = await get_github_token_for_task(
                                            uuid.UUID(original_task_id), session
                                        )
                                        if _peek_pr_num and _peek_base:
                                            from app.rollup_migration_validation import (
                                                enforce_premerge_migration_guard as _emg_pre,
                                            )

                                            _pre_rollup_proj = await session.get(
                                                Project, task.project_id
                                            )
                                            _pre_rollup_mig_guard = await _emg_pre(
                                                _pre_rollup_gh,
                                                _pre_rollup_own,
                                                _pre_rollup_rep,
                                                _peek_pr_num,
                                                _peek_base,
                                                promotion_target=(
                                                    _shared_state.get_project_promotion_target(
                                                        _pre_rollup_proj
                                                    )
                                                    if _pre_rollup_proj is not None
                                                    else None
                                                ),
                                            )
                                        # Hoist size-cap preflight before the lock for
                                        # direct (non-rollup) task PRs, mirroring the
                                        # migration guard pattern above.  Rollup parents
                                        # have rollup_pr_number in context — skip there.
                                        if (
                                            _peek_pr_num
                                            and _peek_branch
                                            and not (_orig_peek or {}).get(
                                                "rollup_pr_number"
                                            )
                                        ):
                                            from app.rollup_size_preflight import (
                                                preflight_rollup_size_caps as _sc_pre,
                                            )

                                            _pre_rollup_size_cap = await _sc_pre(
                                                _pre_rollup_gh,
                                                _pre_rollup_own,
                                                _pre_rollup_rep,
                                                _peek_pr_num,
                                                _peek_branch,
                                                project_id=str(task.project_id)
                                                if task.project_id
                                                else None,
                                            )
                                    except Exception as _pre_exc:
                                        logger.debug(
                                            "[REVIEW PIPELINE] Pre-lock token/guard "
                                            "for rollup parent %s failed: %s "
                                            "(will fall back to under-lock resolution)",
                                            original_task_id[:8],
                                            _pre_exc,
                                        )
                                        _pre_rollup_gh = None
                                        _pre_rollup_mig_guard = None
                                        _pre_rollup_size_cap = None

                                # Phase A1: skip_locked + bounded retry. If the parent
                                # task row is held by a peer, defer the handoff —
                                # the parent's own poll will pick up review_approved
                                # on the next iteration once the lock clears.
                                # Publication/fix admission owns the project/PR
                                # lock before any authority-bearing parent row
                                # mutation. Persist the exact approval under
                                # that order, then release before the merge path
                                # acquires its global base->PR lock pair.
                                from app.task_locking import acquire_pr_spawn_lock

                                await acquire_pr_spawn_lock(
                                    session, task.project_id, pr_number
                                )
                                _orig = await _lock_original_task(
                                    session,
                                    original_task_id,
                                    source="review_rollup_handoff",
                                )
                                # Re-read the loop from the locked row — peer may
                                # have flipped ci_waiting in the gap between the
                                # peek and the lock acquisition.
                                _orig_loop = (
                                    (_orig.context or {}).get("review_loop", {})
                                    if _orig
                                    else {}
                                )
                                if _orig and _orig_loop.get("ci_waiting"):
                                    # Snapshot identity + status while _orig is
                                    # freshly locked. The merge gate below
                                    # (_attempt_merge_with_retry_and_escalation) can
                                    # roll back internally, which expires _orig even
                                    # with expire_on_commit=False; re-reading
                                    # _orig.project_id / .id / .status at broadcast
                                    # time would then sync lazy-load in async context
                                    # → MissingGreenlet / connection-acquire failure
                                    # (prod 2026-06-03 task_lifecycle:1838). The
                                    # parent stays EVALUATING on this CI-gate handoff,
                                    # so the snapshotted status is correct.
                                    _orig_pid_str = str(_orig.project_id)
                                    _orig_id_str = str(_orig.id)
                                    _orig_status_snap = _orig.status
                                    _orig_checked_head_sha = str(
                                        _orig_loop.get("ci_checked_head_sha")
                                        or _orig_loop.get("head_sha")
                                        or ""
                                    )
                                    _orig_checked_base_sha = str(
                                        _orig_loop.get("ci_checked_base_sha") or ""
                                    )
                                    _orig_loop.update(
                                        {
                                            "review_approved": True,
                                            "review_history": _approved_history,
                                            "issue_ledger": _authoritative_ledger,
                                            "review_target": dict(_live_approve_target),
                                            "head_sha": str(
                                                _live_approve_target["head_sha"]
                                            ),
                                            "base_sha": str(
                                                _live_approve_target["base_sha"]
                                            ),
                                        }
                                    )
                                    # An actual exact-target review supersedes
                                    # any earlier initial-review bypass marker.
                                    _orig_loop.pop("review_not_required", None)
                                    _orig_loop.pop("review_policy_reason", None)
                                    _orig_loop.pop(
                                        "authoritative_review_required", None
                                    )
                                    _orig.context = {
                                        **(_orig.context or {}),
                                        "review_loop": _orig_loop,
                                    }
                                    flag_modified(_orig, "context")
                                    # An APPROVE on this chain resolves a stale
                                    # stuck/standoff/repeat-fingerprint hold —
                                    # release it while the row lock is already
                                    # held, or the recovery sweep's 2h approval
                                    # timer FAILs the parent under live work.
                                    from app.review_loop_convergence import (
                                        clear_review_escalation_approval_gate,
                                    )

                                    clear_review_escalation_approval_gate(
                                        _orig,
                                        resolved_by=f"review_approve_pr_{pr_number}",
                                    )
                                    session.add(_orig)
                                    task.context = {
                                        **(task.context or {}),
                                        "review_loop": dict(review_loop),
                                    }
                                    flag_modified(task, "context")
                                    task.status = TaskStatus.COMPLETED
                                    task.latest_status_message = f"Review approved for rollup PR #{pr_number} — handed off to CI gate"
                                    session.add(task)
                                    await session.commit()

                                    _gate_ci_ok = bool(
                                        _orig_loop.get("ci_passed")
                                    )
                                    if (
                                        not _gate_ci_ok
                                        and _orig_status_snap
                                        != TaskStatus.EVALUATING
                                    ):
                                        # The CI monitor polls only EVALUATING
                                        # rows — nothing else ever flips
                                        # ci_passed for this parent, so the
                                        # approval would park at this gate
                                        # forever while the rollup recovery
                                        # respawns reviews up to the cap.
                                        # Resolve the definitive CI states
                                        # inline; pending keeps parking.
                                        _gate_ci_state = ""
                                        try:
                                            if (
                                                _pre_rollup_gh
                                                and _pre_rollup_own
                                                and _pre_rollup_rep
                                            ):
                                                _cig = _pre_rollup_gh
                                                _cio = _pre_rollup_own
                                                _cir = _pre_rollup_rep
                                            else:
                                                (
                                                    _cig,
                                                    _cio,
                                                    _cir,
                                                ) = await get_github_token_for_task(
                                                    _orig.id, session
                                                )
                                        except Exception:
                                            _cig = _cio = _cir = ""
                                        if _cig:
                                            (
                                                _gate_ci_ok,
                                                _gate_ci_state,
                                            ) = await _resolve_ci_for_unpolled_rollup_parent(
                                                session,
                                                _orig,
                                                _orig_loop,
                                                pr_number,
                                                _cig,
                                                _cio,
                                                _cir,
                                            )
                                        if _gate_ci_ok:
                                            _orig_loop["ci_passed"] = True
                                            _orig_loop["ci_passed_source"] = (
                                                f"rollup_handoff_inline:{_gate_ci_state}"
                                            )
                                            # The CI verdict was just resolved
                                            # at the approval's exact target —
                                            # stamp the checked pair from it,
                                            # or the merge authority compares
                                            # the live base against a
                                            # ci_checked_base_sha stamped
                                            # before a sibling merge moved the
                                            # base and holds forever (PR
                                            # #16735: checked 64a39c00 vs live
                                            # a2ec7e09).
                                            _orig_checked_head_sha = str(
                                                _orig_loop.get("head_sha") or ""
                                            )
                                            _orig_checked_base_sha = str(
                                                _orig_loop.get("base_sha") or ""
                                            )
                                            _orig_loop["ci_checked_head_sha"] = (
                                                _orig_checked_head_sha
                                            )
                                            _orig_loop["ci_checked_base_sha"] = (
                                                _orig_checked_base_sha
                                            )
                                            _orig.context = {
                                                **(_orig.context or {}),
                                                "review_loop": _orig_loop,
                                            }
                                            flag_modified(_orig, "context")
                                            session.add(_orig)
                                            logger.info(
                                                f"[REVIEW PIPELINE] Rollup PR #{pr_number}: CI resolved inline "
                                                f"({_gate_ci_state}) for non-EVALUATING parent {_orig_id_str[:8]} — proceeding to gate merge"
                                            )
                                        else:
                                            logger.warning(
                                                f"[REVIEW PIPELINE] Rollup PR #{pr_number}: approval parked on CI "
                                                f"({_gate_ci_state or 'unresolved'}) for {_orig_status_snap} parent "
                                                f"{_orig_id_str[:8]} — the CI monitor does not poll this row"
                                            )

                                    if _gate_ci_ok:
                                        try:
                                            # Use pre-lock token if resolved; fall
                                            # back to under-lock resolution when the
                                            # pre-lock attempt failed.
                                            if (
                                                _pre_rollup_gh
                                                and _pre_rollup_own
                                                and _pre_rollup_rep
                                            ):
                                                _gh = _pre_rollup_gh
                                                _own = _pre_rollup_own
                                                _rep = _pre_rollup_rep
                                            else:
                                                (
                                                    _gh,
                                                    _own,
                                                    _rep,
                                                ) = await get_github_token_for_task(
                                                    _orig.id, session
                                                )
                                            await _attempt_merge_with_retry_and_escalation(
                                                _orig,
                                                _gh,
                                                _own,
                                                _rep,
                                                session,
                                                source="review_approval",
                                                precomputed_migration_guard=_pre_rollup_mig_guard,
                                                precomputed_size_cap=_pre_rollup_size_cap,
                                                checked_head_sha=_orig_checked_head_sha,
                                                checked_base_sha=_orig_checked_base_sha,
                                            )
                                        except Exception as _gate_e:
                                            logger.error(
                                                f"[REVIEW PIPELINE] Gate merge failed for rollup PR #{pr_number}: {_gate_e}",
                                                exc_info=True,
                                            )

                                    await session.commit()
                                    await broadcast_to_project(
                                        _orig_pid_str,
                                        "task_updated",
                                        {
                                            "task_id": _orig_id_str,
                                            "status": _orig_status_snap,
                                        },
                                    )
                                    logger.info(
                                        f"[REVIEW PIPELINE] Rollup PR #{pr_number}: review approved, handed to CI gate (parent {_orig_id_str})"
                                    )
                                    return {"status": "accepted"}
                        except Exception as _rollup_e:
                            logger.warning(
                                f"[REVIEW PIPELINE] Rollup gate handoff failed for PR #{pr_number}: {_rollup_e}",
                                exc_info=True,
                            )

                    head_sha = ""
                    try:
                        gh_token, owner, repo = await get_github_token_for_task(
                            task.id, session
                        )
                        # Validate PR head branch matches before merging
                        pr_data = await get_pull_request(
                            gh_token,
                            owner,
                            repo,
                            pr_number,
                            project_id=str(task.project_id),
                        )
                        pr_head = pr_data.get("head", {}).get("ref", "")
                        if pr_head and pr_head != branch:
                            logger.info(
                                f"[REVIEW PIPELINE] Branch mismatch: reviewed '{branch}' but PR #{pr_number} head is '{pr_head}' — skipping merge"
                            )
                            await create_pr_comment(
                                gh_token,
                                owner,
                                repo,
                                pr_number,
                                f"**Blueprint Review Pipeline**: Merge aborted — reviewed branch `{branch}` does not match PR head `{pr_head}`.",
                            )
                            task.latest_status_message = f"Branch mismatch: reviewed {branch}, PR head is {pr_head}"
                            task.context = {
                                **(task.context or {}),
                                "review_loop": dict(review_loop),
                            }
                            flag_modified(task, "context")
                            session.add(task)
                            await session.commit()
                            raise ValueError(f"Branch mismatch: {branch} vs {pr_head}")

                        # Check CI status before merging the integration trunk.
                        #
                        # Pre-Phase-1 the integration branch IS "main", so this
                        # was historically written as `base_branch == "main"`.
                        # After the automain/LTS Phase 1 cutover rollup PRs
                        # target `automain` (the resolved integration branch),
                        # and a hard-coded "main" check would skip the pre-merge
                        # CI gate for every rollup — the orchestrator would merge
                        # rollups without waiting for CI.
                        #
                        # Gate on the resolved integration branch so the CI
                        # check runs for whichever branch the pipeline currently
                        # integrates onto. "main" is ALSO kept gated regardless:
                        # post-cutover it is the promotion / hotfix-rollup
                        # target, and a merge to main must never bypass CI.
                        head_sha = pr_data.get("head", {}).get("sha", "")
                        live_base_sha = str(pr_data.get("base", {}).get("sha", ""))
                        approve_target_is_stale = (
                            not reviewed_head_sha
                            or not reviewed_base_sha
                            or reviewed_head_sha != head_sha
                            or reviewed_base_sha != live_base_sha
                        )
                        if approve_target_is_stale:
                            # Do not let an APPROVE issued for a different
                            # comparison range enter CI/merge lifecycle. There
                            # is deliberately no discard-count escape hatch:
                            # repeated branch motion can cost another bounded
                            # delta review, but it can never make a stale
                            # approval authoritative.
                            if not (task.context or {}).get("_review_stale_head"):
                                task.context = {
                                    **(task.context or {}),
                                    "_review_stale_head": True,
                                }
                                flag_modified(task, "context")
                                task.status = TaskStatus.COMPLETED
                                task.latest_status_message = (
                                    f"Stale/unproven approval discarded for PR #{pr_number} "
                                    f"(reviewed {reviewed_base_sha[:8]}..{reviewed_head_sha[:8]}, "
                                    f"now {live_base_sha[:8]}..{head_sha[:8]}) — "
                                    "respawning exact-target review"
                                )
                                session.add(task)
                                fresh_review, _ = await _spawn_pipeline_task(
                                    task,
                                    "review",
                                    session,
                                    pr_number,
                                    branch,
                                    base_branch,
                                    original_task_id=original_task_id,
                                    review_target={
                                        "project_id": str(task.project_id),
                                        "pr_number": int(pr_number),
                                        "base_sha": live_base_sha,
                                        "head_sha": head_sha,
                                    }
                                    if live_base_sha and head_sha
                                    else None,
                                )
                                if fresh_review:
                                    logger.info(
                                        "[REVIEW PIPELINE] Discarded stale/unproven "
                                        "approval for PR #%s; spawned review %s",
                                        pr_number,
                                        fresh_review.id,
                                    )
                            await session.commit()
                            return {"status": "accepted"}
                        if (
                            reviewed_head_sha == head_sha
                            and reviewed_base_sha
                            == live_base_sha
                        ):
                            _record_structured_review_result(
                                {
                                    "project_id": str(task.project_id),
                                    "pr_number": int(pr_number),
                                    "base_sha": live_base_sha,
                                    "head_sha": head_sha,
                                }
                                if head_sha and live_base_sha
                                else None
                            )
                        project = (
                            await session.get(Project, task.project_id)
                            if task.project_id
                            else None
                        )
                        _integration_branch = (
                            _shared_state.get_project_integration_branch(project)
                        )
                        _promotion_target = _shared_state.get_project_promotion_target(
                            project
                        )
                        is_ci_gated_merge = base_branch in (
                            _integration_branch,
                            _promotion_target,
                        )
                        ci_status = (
                            await get_check_runs_cached(
                                gh_token,
                                owner,
                                repo,
                                head_sha,
                                project_id=str(task.project_id),
                            )
                            if (head_sha and is_ci_gated_merge)
                            else None
                        )

                        if ci_status and ci_status["status"] != "completed":
                            # CI still running — park task in EVALUATING (no VM consumed)
                            task.status = TaskStatus.EVALUATING
                            task.latest_status_message = (
                                f"Review approved, waiting for CI on PR #{pr_number}..."
                            )
                            task.context = {
                                **(task.context or {}),
                                "review_loop": {
                                    **review_loop,
                                    "ci_waiting": True,
                                    "review_approved": True,
                                    "review_history": _approved_history,
                                    "issue_ledger": _authoritative_ledger,
                                    "review_target": dict(_live_approve_target),
                                    "head_sha": head_sha,
                                    "base_sha": live_base_sha,
                                    "pr_number": pr_number,
                                    "branch": branch,
                                    "base_branch": base_branch,
                                    "original_task_id": original_task_id,
                                    "ci_wait_started": _utcnow().isoformat(),
                                },
                            }
                            session.add(task)
                            await session.commit()
                            logger.info(
                                f"[CI] PR #{pr_number} approved, CI pending — waiting"
                            )
                            action_taken = True
                            return {"status": "accepted"}

                        if ci_status and ci_status["conclusion"] == "failure":
                            # CI failed — spawn fix with CI errors
                            ci_errors = (
                                ci_status["failed_logs"]
                                or "CI checks failed (no details available)"
                            )
                            await create_pr_comment(
                                gh_token,
                                owner,
                                repo,
                                pr_number,
                                f"**Blueprint CI**: Checks failed — spawning fix task.\n\n```\n{ci_errors[:2000]}\n```",
                            )
                            # Conflict-aware canonical prompt (review_pipeline):
                            # the old prompt here only mentioned "build/lint/test
                            # errors" and said "don't change logic", so a PR that
                            # was ALSO conflicting never got rebased — the fix
                            # chased CI while the conflict outlived the global
                            # iteration cap (PR #5587, 2026-05-23).
                            ci_fix_prompt = build_ci_failure_fix_prompt(
                                pr_number=pr_number,
                                base_branch=base_branch,
                                ci_errors=ci_errors,
                                owner=owner,
                                repo=repo,
                            )
                            # Phase 2d: APPROVE branch must NOT propagate warm-VM
                            # continuity onto downstream spawns. The reviewer's VM
                            # is releasing on this APPROVE, so the new fix must
                            # cold-provision rather than wait for a dying poll.
                            # Preserve the authoritative approval/ledger for the
                            # successor, but only after owning the same PR lock
                            # that the spawn boundary uses. This keeps PR -> Task
                            # ordering without dropping proof from the fix chain.
                            from app.task_locking import acquire_pr_spawn_lock

                            await acquire_pr_spawn_lock(
                                session, task.project_id, pr_number
                            )
                            task.context = {
                                **(task.context or {}),
                                "review_loop": dict(review_loop),
                            }
                            flag_modified(task, "context")
                            session.add(task)
                            fix_task, _ = await _spawn_pipeline_task(
                                task,
                                "fix",
                                session,
                                pr_number,
                                branch,
                                base_branch,
                                fix_prompt=ci_fix_prompt,
                                original_task_id=original_task_id,
                                fix_reason="ci_failure",
                                ci_failure_logs=ci_errors,
                                suppress_warm_handoff=True,
                            )
                            if fix_task:
                                logger.error(
                                    f"[CI] PR #{pr_number} CI failed, spawned fix → {fix_task.id}"
                                )
                            action_taken = True
                            return {"status": "accepted"}

                        # CI passed (or no CI checks exist) — proceed to merge
                        if ci_status and ci_status["conclusion"] == "success":
                            logger.info(f"[CI] PR #{pr_number} CI passed")

                        # Critical files gate: block merge if security-critical files were touched
                        if original_task_id and _runtime_config.get(
                            "require_approval_gates", True
                        ):
                            try:
                                orig_task = await session.get(
                                    Task, uuid.UUID(original_task_id)
                                )
                                critical_touched = (
                                    (orig_task.context or {}).get(
                                        "touched_critical", []
                                    )
                                    if orig_task
                                    else []
                                )
                                if critical_touched:
                                    orig_task.requires_approval = True
                                    orig_task.status = TaskStatus.EVALUATING
                                    orig_task.latest_status_message = f"Review approved — awaiting human approval to merge (critical files: {', '.join(critical_touched)})"
                                    session.add(orig_task)
                                    task.context = {
                                        **(task.context or {}),
                                        "review_loop": dict(review_loop),
                                    }
                                    flag_modified(task, "context")
                                    session.add(task)
                                    await session.commit()
                                    await broadcast_to_project(
                                        str(orig_task.project_id),
                                        "task_updated",
                                        {
                                            "task_id": str(orig_task.id),
                                            "status": TaskStatus.EVALUATING,
                                        },
                                    )
                                    await create_pr_comment(
                                        gh_token,
                                        owner,
                                        repo,
                                        pr_number,
                                        f"**Blueprint Review Pipeline**: Review approved, but PR touches security-critical files ({', '.join(critical_touched)}). Merge requires human approval.",
                                    )
                                    logger.error(
                                        f"[PIPELINE] PR #{pr_number} review approved but merge blocked — critical files: {critical_touched}"
                                    )
                                    action_taken = True
                                    return {"status": "accepted"}
                            except Exception as e:
                                logger.error(
                                    f"[PIPELINE] Error checking critical files for merge gate: {e}",
                                    exc_info=True,
                                )

                        # Security / approval gate: if the original create task was
                        # flagged (security scan blocking, scan failure, etc.), park
                        # for manual merge. The review still ran and posted feedback,
                        # but auto-merge is not safe without human sign-off.
                        if original_task_id and _runtime_config.get(
                            "require_approval_gates", True
                        ):
                            try:
                                _approval_orig = await session.get(
                                    Task, uuid.UUID(original_task_id)
                                )
                                if _approval_orig and _approval_orig.requires_approval:
                                    _approval_orig.status = TaskStatus.EVALUATING
                                    reason = (
                                        _approval_orig.latest_status_message
                                        or "requires manual approval"
                                    )
                                    _approval_orig.latest_status_message = f"Review approved — awaiting human approval to merge ({reason})"
                                    session.add(_approval_orig)
                                    task.context = {
                                        **(task.context or {}),
                                        "review_loop": dict(review_loop),
                                    }
                                    flag_modified(task, "context")
                                    session.add(task)
                                    await session.commit()
                                    await broadcast_to_project(
                                        str(_approval_orig.project_id),
                                        "task_updated",
                                        {
                                            "task_id": str(_approval_orig.id),
                                            "status": TaskStatus.EVALUATING,
                                        },
                                    )
                                    await _comment_with_retry(
                                        create_pr_comment,
                                        gh_token,
                                        owner,
                                        repo,
                                        pr_number,
                                        f"**Blueprint Review Pipeline**: Review approved, but merge requires human approval — {reason}",
                                    )
                                    logger.info(
                                        f"[PIPELINE] PR #{pr_number} review approved but merge blocked — requires_approval set on {original_task_id[:8]}"
                                    )
                                    action_taken = True
                                    return {"status": "accepted"}
                            except Exception as e:
                                logger.error(
                                    f"[PIPELINE] Error checking requires_approval for merge gate: {e}",
                                    exc_info=True,
                                )

                        # Pre-merge Alembic dual-head guard (parity with the
                        # CI-completion, rollup, hotfix back-merge and promotion
                        # paths). When CI has ALREADY finished green by the time
                        # the review APPROVE arrives, the merge fires inline here
                        # rather than from the guarded CI-completion handler — so
                        # without this check two task PRs that each add 'the next'
                        # migration with the same revision id would both merge and
                        # leave the integration branch with a dual Alembic head.
                        # Only integration/promotion-targeted merges need it; on a
                        # block-or-defer verdict do NOT merge — park EVALUATING with
                        # the same ci_waiting/review_approved/ci_passed flags the
                        # CI-completion path sets so the guarded sweep retries once
                        # the collision resolves.
                        if is_ci_gated_merge:
                            from app.migration_reservations import (
                                get_migration_reservation_for_task,
                            )
                            from app.rollup_management import (
                                attempt_pr_migration_finalization,
                            )
                            from app.rollup_migration_validation import (
                                enforce_premerge_migration_guard,
                            )
                            from app.migration_guard_recovery import (
                                coerce_migration_guard_result,
                                mark_migration_guard_deferred,
                                spawn_migration_guard_fix_task,
                            )

                            _dh_result = await enforce_premerge_migration_guard(
                                gh_token,
                                owner,
                                repo,
                                pr_number,
                                base_branch,
                                promotion_target=_promotion_target,
                            )
                            (
                                _dh_verdict,
                                _dh_reason,
                            ) = coerce_migration_guard_result(_dh_result)
                            if _dh_verdict != "ok":
                                logger.warning(
                                    f"[CI] Inline merge held for PR #{pr_number} — "
                                    f"Alembic combined-chain guard: {_dh_reason}"
                                )
                                _dh_loop = {
                                    **review_loop,
                                    "ci_waiting": True,
                                    "review_approved": True,
                                    "ci_passed": True,
                                    "head_sha": head_sha,
                                    "pr_number": pr_number,
                                    "branch": branch,
                                    "base_branch": base_branch,
                                    "original_task_id": original_task_id,
                                }
                                if _dh_verdict == "defer":
                                    task.status = TaskStatus.EVALUATING
                                    mark_migration_guard_deferred(
                                        task,
                                        review_loop=_dh_loop,
                                        reason=_dh_reason,
                                        context_snapshot=dict(task.context or {}),
                                    )
                                    session.add(task)
                                    await session.commit()
                                    action_taken = True
                                    return {"status": "accepted"}
                                reservation_task_id = (
                                    uuid.UUID(original_task_id)
                                    if original_task_id
                                    else task.id
                                )
                                reservation = await get_migration_reservation_for_task(
                                    session, task_id=reservation_task_id
                                )
                                if reservation is not None and branch and base_branch:
                                    try:
                                        _finalization = (
                                            await attempt_pr_migration_finalization(
                                                gh_token,
                                                owner,
                                                repo,
                                                pr_number=pr_number,
                                                pr_branch=branch,
                                                target_branch=base_branch,
                                                promotion_target=_promotion_target,
                                                project_id=str(task.project_id),
                                            )
                                        )
                                    except Exception as _mf_exc:
                                        logger.warning(
                                            "[CI] Migration finalization attempt failed "
                                            "for PR #%s: %s",
                                            pr_number,
                                            _mf_exc,
                                            exc_info=True,
                                        )
                                        _finalization = None
                                    if _finalization is not None:
                                        from app.task_completion import (
                                            _reset_review_loop_pr_head_cache,
                                        )
                                        from app.ci_merge_helpers import (
                                            revoke_review_authority_for_active_fix,
                                        )

                                        task.status = TaskStatus.EVALUATING
                                        task.latest_status_message = (
                                            "Merge held — auto-finalized provisional "
                                            "Alembic migration "
                                            f"{_finalization['old_revision']}->"
                                            f"{_finalization['finalized_revision']} "
                                            f"against {base_branch}; awaiting fresh CI "
                                            "on the rewritten PR head"
                                        )[:500]
                                        _finalized_loop = (
                                            _reset_review_loop_pr_head_cache(review_loop)
                                        )
                                        _finalized_loop = (
                                            revoke_review_authority_for_active_fix(
                                                task, review_loop=_finalized_loop
                                            )
                                        )
                                        _finalized_loop.update(
                                            {
                                                "ci_waiting": True,
                                                "ci_passed": False,
                                                "authoritative_review_required": True,
                                                "pr_number": pr_number,
                                                "branch": branch,
                                                "base_branch": base_branch,
                                                "original_task_id": original_task_id,
                                            }
                                        )
                                        task.context = {
                                            **(task.context or {}),
                                            "_migration_finalization": _finalization,
                                            "review_loop": _finalized_loop,
                                        }
                                        session.add(task)
                                        await session.commit()
                                        action_taken = True
                                        return {"status": "accepted"}
                                await spawn_migration_guard_fix_task(
                                    task,
                                    session,
                                    pr_number=pr_number,
                                    branch=branch,
                                    base_branch=base_branch,
                                    original_task_id=original_task_id,
                                    guard_reason=_dh_reason,
                                    review_loop=_dh_loop,
                                    context_snapshot=dict(task.context or {}),
                                    gh_token=gh_token,
                                    owner=owner,
                                    repo=repo,
                                )
                                session.add(task)
                                await session.commit()
                                action_taken = True
                                return {"status": "accepted"}

                        from app.review_merge_authority import (
                            attempt_merge_with_authority,
                        )

                        async def _submit_approved_merge(
                            expected_head_sha: str,
                        ) -> dict:
                            return await merge_pull_request(  # premerge-guard-exempt: migration PRs are parked above in the dual-head guard branch (returns "accepted"); this merge only runs for staging-only branches that never carry a migration onto the integration trunk
                                gh_token,
                                owner,
                                repo,
                                pr_number,
                                project_id=str(task.project_id),
                                merge_method=_pick_merge_method(task),
                                expected_head_sha=expected_head_sha,
                            )

                        _merge_authority, merge_result = (
                            await attempt_merge_with_authority(
                            task,
                            session,
                            merge_call=_submit_approved_merge,
                            gh_token=gh_token,
                            owner=owner,
                            repo=repo,
                            ci_loop=review_loop,
                            pr_number=pr_number,
                            branch=branch,
                            base_branch=base_branch,
                            original_task_id=original_task_id,
                            checked_head_sha=str(_live_approve_target["head_sha"]),
                            checked_base_sha=str(_live_approve_target["base_sha"]),
                            retry_nonterminal=True,
                        )
                        )
                        if merge_result is None:
                            session.add(task)
                            await session.commit()
                            action_taken = True
                            return {"status": "accepted"}

                        review_loop = _merge_authority.review_loop

                        if merge_result.get("merged"):
                            await _comment_with_retry(
                                create_pr_comment,
                                gh_token,
                                owner,
                                repo,
                                pr_number,
                                "**Blueprint Review Pipeline**: PR auto-merged.",
                            )
                            # Mark original create task so dependents can proceed
                            if original_task_id:
                                try:
                                    # C4: the orig (rollup parent) was previously
                                    # fetched via an UNLOCKED session.get, then
                                    # transitioned EVALUATING→COMPLETED and
                                    # committed on this shared session — racing
                                    # ci_completion, which mutates the SAME parent
                                    # guarded by status==EVALUATING. Two writers,
                                    # no row lock → lost-update / false-success.
                                    #
                                    # The merge has ALREADY succeeded on GitHub, so
                                    # the orig bookkeeping below carries ONE-SHOT
                                    # side effects with NO retry path: hotfix
                                    # back-merge (a missed back-merge breaks the
                                    # next promotion), security-fix rescan spawn,
                                    # remediation-outcome record + result message +
                                    # webhook, review-playbook extraction,
                                    # regression-case advancement, post-merge
                                    # smoketest. A skip_locked DEFER here would skip
                                    # the entire `if orig:` block and drop all of
                                    # them permanently. So we BLOCK on FOR UPDATE
                                    # (skip_locked=False) — bounded by a SET LOCAL
                                    # lock_timeout so we never hang — and the
                                    # peer's brief EVALUATING→terminal hold clears,
                                    # after which the one-shot effects run. On a
                                    # genuine (rare) timeout we log loudly + leave a
                                    # recoverable trail (below), never silently drop.
                                    orig = await _lock_original_task(
                                        session,
                                        original_task_id,
                                        source="review_merge_success",
                                        skip_locked=False,
                                    )
                                    if orig:
                                        _orig_project = await session.get(
                                            Project, orig.project_id
                                        )
                                        _orig_promotion_target = (
                                            _shared_state.get_project_promotion_target(
                                                _orig_project
                                            )
                                        )
                                        _orig_is_custom_staged_rollup = (
                                            is_custom_staged_rollup_root(
                                                orig, _orig_project, base_branch
                                            )
                                        )
                                        _terminal_rollup_merge = True
                                        if (
                                            _orig_is_custom_staged_rollup
                                            and _orig_project is not None
                                        ):
                                            _orig_integration_branch = _shared_state.get_project_integration_branch(
                                                _orig_project
                                            )
                                            _landed_on_integration = (
                                                base_branch == _orig_integration_branch
                                            )
                                            _landed_on_promotion = (
                                                base_branch == _orig_promotion_target
                                            )
                                            if (
                                                not _landed_on_integration
                                                and _landed_on_promotion
                                            ):
                                                await merged_pr_counts_as_integration_landing(
                                                    orig,
                                                    _orig_project,
                                                    session,
                                                    pr_number=pr_number,
                                                    merged_base_branch=base_branch,
                                                    source="task_lifecycle.review_merge_success",
                                                )
                                            _record_pr_merged = _landed_on_integration
                                            _terminal_rollup_merge = (
                                                _landed_on_integration
                                                or _landed_on_promotion
                                            )
                                        else:
                                            _record_pr_merged = await merged_pr_counts_as_integration_landing(
                                                orig,
                                                _orig_project,
                                                session,
                                                pr_number=pr_number,
                                                merged_base_branch=base_branch,
                                                source="task_lifecycle.review_merge_success",
                                            )
                                        if _record_pr_merged:
                                            await corroborate_and_apply_merged_landed_signal(
                                                orig,
                                                _orig_project,
                                                session,
                                                base_branch=base_branch,
                                                hotfix_lane=bool(
                                                    (orig.first_payload or {}).get(
                                                        "hotfix"
                                                    )
                                                    or (orig.context or {}).get(
                                                        "hotfix"
                                                    )
                                                ),
                                                non_merge_reason=(
                                                    "merged_pr_non_integration_branch"
                                                ),
                                                # Immutable merge commit from the
                                                # merge API — pins acceptance
                                                # corroboration off the 60s-TTL
                                                # branch-tip cache.
                                                merge_commit_sha=merge_result.get(
                                                    "sha"
                                                ),
                                            )
                                        try:
                                            from app.task_completion import (
                                                _finalize_migration_reservation_after_merge,
                                            )

                                            await _finalize_migration_reservation_after_merge(
                                                orig,
                                                session,
                                                source_task=task,
                                                terminal_merge=_terminal_rollup_merge,
                                            )
                                        except Exception as _mr_exc:
                                            logger.warning(
                                                "[MIGRATION] Failed to finalize "
                                                "reservation for merged task %s: %r",
                                                orig.id,
                                                _mr_exc,
                                            )
                                        # If this was a rollup PR review, mark the
                                        # parent COMPLETED — but only if it is STILL
                                        # EVALUATING after the relock. A peer (e.g.
                                        # ci_completion / reaper) may have already
                                        # terminalized it during the merge window;
                                        # COMPLETED is irreversible, so we must not
                                        # overwrite a peer's terminal transition.
                                        # "Was this a rollup merge at all?" — true
                                        # whenever the original task was parked
                                        # EVALUATING awaiting its rollup PR (the
                                        # pre-mutation state captured here). This
                                        # drives the downstream smoketest and hotfix
                                        # back-merge gates, which must fire for ANY
                                        # rollup landing on the promotion target —
                                        # NOT only custom-staged (release/) ones.
                                        # The custom-staged terminal-vs-deferred
                                        # decision below uses _orig_is_custom_staged_rollup
                                        # directly; conflating the two (PR #7064 vs
                                        # automain merge) silently skipped the hotfix
                                        # back-merge for a plain hotfix->main landing.
                                        is_rollup_merge = (
                                            orig.status == TaskStatus.EVALUATING
                                        )
                                        if orig.status == TaskStatus.EVALUATING:
                                            if (
                                                not _orig_is_custom_staged_rollup
                                                or _terminal_rollup_merge
                                            ):
                                                orig.status = TaskStatus.COMPLETED
                                                orig.latest_status_message = (
                                                    "Rollup PR merged"
                                                )
                                            else:
                                                orig.latest_status_message = (
                                                    f"Rollup PR merged into "
                                                    f"{base_branch}; awaiting "
                                                    "downstream landing"
                                                )
                                        session.add(orig)
                                        _o_ctx = orig.context or {}
                                        # Audit W1: auto_remediation is now a column.
                                        if _o_ctx.get("diagnosis_task_id") or (
                                            orig.auto_remediation
                                            and _o_ctx.get("direction_id")
                                        ):
                                            try:
                                                from app.task_completion import (
                                                    _record_remediation_outcome,
                                                )

                                                await _record_remediation_outcome(
                                                    orig, merged=True, session=session
                                                )
                                            except Exception as _rr_exc:
                                                logger.warning(
                                                    f"[DIAGNOSIS] Failed to record remediation outcome on merge for task {orig.id}: {_rr_exc!r}"
                                                )
                                        # Post result message for auto-remediation tasks
                                        # (routes between CI auto-fix and residual lifecycles).
                                        if orig.auto_remediation:
                                            try:
                                                await post_remediation_result_message(
                                                    session,
                                                    orig,
                                                    result="merged",
                                                    pr_number=pr_number,
                                                )
                                                try:
                                                    from app.notification_dispatch import (
                                                        dispatch_external_notification,
                                                    )

                                                    if is_residual_remediation_task(
                                                        orig
                                                    ):
                                                        _wh_event = (
                                                            "RESIDUAL_FIX_MERGED"
                                                        )
                                                        _wh_payload = {
                                                            "fingerprint": (
                                                                orig.context or {}
                                                            ).get(
                                                                "error_fingerprint", ""
                                                            ),
                                                            "pr_number": pr_number,
                                                        }
                                                    else:
                                                        _ar_branch = (
                                                            orig.context or {}
                                                        ).get(
                                                            "ci_failure_branch",
                                                            base_branch,
                                                        )
                                                        _wh_event = "AUTO_FIX_MERGED"
                                                        _wh_payload = {
                                                            "branch": _ar_branch,
                                                            "pr_number": pr_number,
                                                        }
                                                    asyncio.ensure_future(  # noqa: RUF006
                                                        dispatch_external_notification(  # noqa: RUF006, RUF100
                                                            orig.project_id,
                                                            _wh_event,
                                                            _wh_payload,
                                                        )
                                                    )
                                                except Exception as _wh_exc:
                                                    logger.warning(
                                                        "[AUTO-REMEDIATION] Failed to dispatch merged webhook: %s",
                                                        _wh_exc,
                                                    )
                                            except Exception as _ar_exc:
                                                logger.warning(
                                                    "[AUTO-REMEDIATION] Failed to post result message: %s",
                                                    _ar_exc,
                                                )
                                        # Extract review feedback as persistent playbook entries
                                        # on every merge (captures first-attempt data at any iteration)
                                        try:
                                            from app.review_learning import (
                                                extract_review_feedback_to_playbook,
                                            )

                                            _orig_task_type = orig.first_payload.get(
                                                "type", "create"
                                            )
                                            await extract_review_feedback_to_playbook(
                                                task=task,
                                                review_loop=review_loop,
                                                pr_number=pr_number,
                                                original_task_type=_orig_task_type,
                                                session=session,
                                            )
                                        except Exception as _rp_exc:
                                            logger.warning(
                                                "[REVIEW_PLAYBOOK] Failed to extract review feedback: %s",
                                                _rp_exc,
                                            )
                                        await session.commit()
                                        # Heal the direction's research-history
                                        # merge counts for ORIG's cycle. ORIG (not
                                        # this review ``task``) is the merged child,
                                        # so the terminal-status recompute below —
                                        # which keys off ``task.context`` — never
                                        # covers ORIG's direction. Recompute now
                                        # that ORIG's landed signal is committed so
                                        # prs_merged doesn't stay stale-low and trip
                                        # the churn / lifetime-burn guardrails.
                                        if _record_pr_merged:
                                            from app.direction_outcomes import (
                                                refresh_direction_outcomes_after_merge,
                                            )

                                            await (
                                                refresh_direction_outcomes_after_merge(
                                                    orig, session
                                                )
                                            )
                                        # Clean up branches (staging + feature)
                                        await _cleanup_branches(orig, session)
                                        # Completion-side warm-reuse trigger: the
                                        # original task's PR has landed on
                                        # integration and the lock is released —
                                        # mark its just-unblocked dependents
                                        # warm-claimable (see cluster 1 parity).
                                        if _record_pr_merged:
                                            await _mark_dependents_warm_claimable(orig)
                                            try:
                                                from app.reaper_sweeps import (
                                                    event_driven_unblock_dependents,
                                                )

                                                await event_driven_unblock_dependents(
                                                    orig, session
                                                )
                                            except Exception:
                                                logger.exception(
                                                    "[PIPELINE] event_driven_unblock_dependents "
                                                    "failed for task %s",
                                                    orig.id,
                                                )
                                        # Spawn a smoketest after rollup merges to the
                                        # promotion target. After Phase 1 (automain/LTS),
                                        # normal rollups target `automain`; only promotion
                                        # PRs (automain → promotion_target) still merge to
                                        # the promotion branch. So base_branch ==
                                        # promotion_target here means a PROMOTION just
                                        # landed — this smoketest acts as a post-promotion
                                        # production health-check, not a routine check.
                                        # Supported runtime_config keys for smoketest:
                                        #   health_check_urls: list[str] — live URLs to probe with HTTP GET after local tests
                                        if (
                                            is_rollup_merge
                                            and base_branch == _orig_promotion_target
                                            and _runtime_config.get("smoketest_enabled")
                                        ):
                                            try:
                                                type_model = (
                                                    _runtime_config.get(
                                                        "task_type_models", {}
                                                    ).get("smoketest")
                                                    or None
                                                )
                                                type_effort = (
                                                    _runtime_config.get(
                                                        "task_type_efforts", {}
                                                    ).get("smoketest")
                                                    or None
                                                )
                                                health_check_urls = (
                                                    _runtime_config.get(
                                                        "health_check_urls"
                                                    )
                                                    or []
                                                )
                                                _smoketest_payload = validate_internal_payload(
                                                    {
                                                        "type": "smoketest",
                                                        "base_branch": base_branch,
                                                        **(
                                                            {"model": type_model}
                                                            if type_model
                                                            else {}
                                                        ),
                                                        **(
                                                            {"effort": type_effort}
                                                            if type_effort
                                                            else {}
                                                        ),
                                                        **(
                                                            {
                                                                "health_check_urls": health_check_urls
                                                            }
                                                            if health_check_urls
                                                            else {}
                                                        ),
                                                    },
                                                    "task_lifecycle:smoketest",
                                                )
                                                smoketest_task = Task(
                                                    project_id=task.project_id,
                                                    title=f"Smoketest after rollup merge to {base_branch}",
                                                    description=f"Post-merge health checks for {owner}/{repo}@{base_branch}",
                                                    payloads=[_smoketest_payload],
                                                    vm_instance_id=f"agent-vm-{uuid.uuid4().hex}",
                                                    status=TaskStatus.QUEUED,
                                                    created_by="ORCHESTRATOR",
                                                    origination=TASK_ORIGINATION_SMOKETEST,
                                                )
                                                session.add(smoketest_task)
                                                await session.commit()
                                                await session.refresh(smoketest_task)
                                                history = TaskHistory(
                                                    task_id=smoketest_task.id,
                                                    action="SPAWNED",
                                                    details={
                                                        "spawned_by": "POST_ROLLUP_MERGE",
                                                        "base_branch": base_branch,
                                                    },
                                                )
                                                session.add(history)
                                                await session.commit()
                                                await broadcast_to_project(
                                                    str(task.project_id),
                                                    "task_updated",
                                                    {
                                                        "task_id": str(
                                                            smoketest_task.id
                                                        ),
                                                        "status": TaskStatus.QUEUED,
                                                    },
                                                )
                                                logger.info(
                                                    f"[SMOKETEST] Spawned smoketest task {smoketest_task.id} after rollup merge to {base_branch}"
                                                )
                                            except Exception as e:
                                                logger.warning(
                                                    f"[SMOKETEST] Failed to spawn smoketest after rollup merge: {e}",
                                                    exc_info=True,
                                                )
                                        # Back-merge: when a HOTFIX merges to the
                                        # promotion target, open a promotion_target ->
                                        # integration_branch PR so branches don't diverge.
                                        # A hotfix is a `plan` task with
                                        # base_branch=promotion_target and hotfix:true.
                                        # A missed back-merge makes the next promotion
                                        # conflict. (automain/LTS plan Phase 5)
                                        if (
                                            is_rollup_merge
                                            and base_branch == _orig_promotion_target
                                            and bool(orig.first_payload.get("hotfix"))
                                        ):
                                            try:
                                                from app import hotfix_backmerge

                                                _merged_pr_url = (
                                                    f"https://github.com/{owner}/{repo}/pull/{pr_number}"
                                                    if owner and repo and pr_number
                                                    else ""
                                                )
                                                await (
                                                    hotfix_backmerge.open_backmerge_pr(
                                                        session,
                                                        task.project_id,
                                                        merged_pr_url=_merged_pr_url,
                                                        hotfix_task_id=orig.id,
                                                    )
                                                )
                                                # open_backmerge_pr deliberately
                                                # does NOT commit (its docstring:
                                                # "the caller commits the
                                                # session"). On a back-merge
                                                # conflict / 405 it session.add +
                                                # flush()es an operator-facing
                                                # ERROR Alert and a high-priority
                                                # conflict-resolution Task. After
                                                # flush() the session is
                                                # flushed-but-clean, so the
                                                # function's final
                                                # ``if session.dirty or
                                                # session.new`` commit is SKIPPED
                                                # and the ``async with`` exit
                                                # would ROLL BACK that Alert/Task
                                                # — leaving a back-merge conflict
                                                # completely unsignalled. Commit
                                                # here, mirroring the
                                                # smoketest-spawn block above.
                                                # The merged-PR work is already
                                                # committed (~:3470); this commit
                                                # only flushes the back-merge's
                                                # own Alert/Task — safe.
                                                await session.commit()
                                            except Exception as _bm_exc:
                                                logger.warning(
                                                    f"[HOTFIX_BACKMERGE] Back-merge trigger failed for hotfix task {orig.id}: {_bm_exc!r}",
                                                    exc_info=True,
                                                )
                                        # Spawn security rescan if merged task's parent was a security_audit
                                        if orig.parent_task_id:
                                            try:
                                                audit_parent = await session.get(
                                                    Task, orig.parent_task_id
                                                )
                                                if (
                                                    audit_parent
                                                    and audit_parent.first_payload.get(
                                                        "type"
                                                    )
                                                    == "security_audit"
                                                    and audit_parent.status
                                                    == TaskStatus.COMPLETED
                                                ):
                                                    # Only rescan once per original audit
                                                    existing_rescan = (
                                                        (
                                                            await session.execute(
                                                                select(Task).where(
                                                                    Task.parent_task_id
                                                                    == audit_parent.id,
                                                                    Task.title.ilike(
                                                                        "%rescan%"
                                                                    ),
                                                                )
                                                            )
                                                        )
                                                        .scalars()
                                                        .first()
                                                    )
                                                    if not existing_rescan:
                                                        try:
                                                            pr_files = (
                                                                await get_pr_files(
                                                                    gh_token,
                                                                    owner,
                                                                    repo,
                                                                    pr_number,
                                                                )
                                                            )
                                                            changed_files = ", ".join(
                                                                f["filename"]
                                                                for f in pr_files
                                                            )
                                                        except Exception:
                                                            changed_files = "(could not fetch file list)"
                                                        audit_title = audit_parent.title
                                                        type_model = (
                                                            _runtime_config.get(
                                                                "task_type_models", {}
                                                            ).get("security_audit")
                                                            or None
                                                        )
                                                        type_effort = (
                                                            _runtime_config.get(
                                                                "task_type_efforts", {}
                                                            ).get("security_audit")
                                                            or None
                                                        )
                                                        _rescan_payload = validate_internal_payload(
                                                            {
                                                                "type": "security_audit",
                                                                "prompt": (
                                                                    f"Verify that the security fixes merged in PR #{pr_number} actually addressed "
                                                                    f"the reported vulnerabilities. Focus audit on these files: {changed_files}. "
                                                                    f"Output only PASS or remaining FAIL findings."
                                                                ),
                                                                **(
                                                                    {
                                                                        "model": type_model
                                                                    }
                                                                    if type_model
                                                                    else {}
                                                                ),
                                                                **(
                                                                    {
                                                                        "effort": type_effort
                                                                    }
                                                                    if type_effort
                                                                    else {}
                                                                ),
                                                            },
                                                            "task_lifecycle:security_audit",
                                                        )
                                                        rescan_task = Task(
                                                            project_id=orig.project_id,
                                                            title=f"Security rescan: verify {audit_title} fixes",
                                                            description=f"Verify that security fixes merged in PR #{pr_number} addressed reported vulnerabilities.",
                                                            payloads=[_rescan_payload],
                                                            parent_task_id=audit_parent.id,
                                                            vm_instance_id=f"agent-vm-{uuid.uuid4().hex}",
                                                            status=TaskStatus.QUEUED,
                                                            requires_approval=False,
                                                            created_by="ORCHESTRATOR",
                                                            origination=TASK_ORIGINATION_REMEDIATION,
                                                        )
                                                        session.add(rescan_task)
                                                        await session.commit()
                                                        await session.refresh(
                                                            rescan_task
                                                        )
                                                        history = TaskHistory(
                                                            task_id=rescan_task.id,
                                                            action="SPAWNED",
                                                            details={
                                                                "spawned_by": "POST_SECURITY_FIX_MERGE",
                                                                "pr_number": pr_number,
                                                                "audit_parent_id": str(
                                                                    audit_parent.id
                                                                ),
                                                            },
                                                        )
                                                        session.add(history)
                                                        await session.commit()
                                                        await broadcast_to_project(
                                                            str(orig.project_id),
                                                            "task_updated",
                                                            {
                                                                "task_id": str(
                                                                    rescan_task.id
                                                                ),
                                                                "status": TaskStatus.QUEUED,
                                                            },
                                                        )
                                                        logger.info(
                                                            f"[SECURITY RESCAN] Spawned rescan task {rescan_task.id} after security fix merge PR #{pr_number}"
                                                        )
                                            except Exception as e:
                                                logger.info(
                                                    f"[SECURITY RESCAN] Failed to spawn security rescan: {e}"
                                                )
                                except _OriginalTaskLockTimeout:
                                    # C4 fix B: the BLOCKING FOR UPDATE on the
                                    # rollup parent hit its bounded lock_timeout —
                                    # a peer held the row past the wait. The merge
                                    # ALREADY landed on GitHub, so the one-shot
                                    # post-merge effects above could not run. Do NOT
                                    # silently drop them: leave a durable, visible,
                                    # recoverable trail. The shared session is in a
                                    # rolled-back state after the timeout, so use a
                                    # FRESH independent session.
                                    logger.error(  # noqa: TRY400 — lock_timeout is not a code fault; the traceback adds noise, the message + recovery marker are the signal
                                        "[REVIEW PIPELINE] C4: lock_timeout acquiring "
                                        "rollup parent %s after merging PR #%s — "
                                        "post-merge one-shots (back-merge / rescan / "
                                        "remediation outcome / playbook / smoketest) "
                                        "DEFERRED to recovery, not dropped. Marking "
                                        "parent with _post_merge_effects_deferred + "
                                        "operator alert.",
                                        original_task_id,
                                        pr_number,
                                    )
                                    try:
                                        from app.alert_dispatch import (
                                            _maybe_create_failure_alert,
                                        )

                                        async with AsyncSessionLocal() as _rec:
                                            _orig_rec = await _rec.get(
                                                Task, uuid.UUID(original_task_id)
                                            )
                                            if _orig_rec is not None:
                                                _rec_ctx = dict(_orig_rec.context or {})
                                                _rec_ctx[
                                                    "_post_merge_effects_deferred"
                                                ] = {
                                                    "pr_number": pr_number,
                                                    # task_id is the function's UUID
                                                    # param — use it, not task.id: the
                                                    # outer session is poisoned by the
                                                    # lock_timeout abort, so reading
                                                    # task.id would lazy-load on a dead
                                                    # session (prod 2026-06-03
                                                    # task_lifecycle:2726).
                                                    "review_task_id": str(task_id),
                                                    "deferred_at": _utcnow().isoformat(),
                                                    "reason": "lock_timeout acquiring "
                                                    "parent FOR UPDATE during "
                                                    "merge-success bookkeeping",
                                                }
                                                _orig_rec.context = _rec_ctx
                                                from sqlalchemy.orm.attributes import (
                                                    flag_modified as _fm,
                                                )

                                                _fm(_orig_rec, "context")
                                                _rec.add(_orig_rec)
                                                await _maybe_create_failure_alert(
                                                    _orig_rec,
                                                    _rec,
                                                    f"PR #{pr_number} merged but the "
                                                    f"rollup parent row was locked past "
                                                    f"the bounded wait — post-merge "
                                                    f"effects (hotfix back-merge / "
                                                    f"security rescan / remediation "
                                                    f"outcome) are deferred and need a "
                                                    f"re-drive or manual check.",
                                                )
                                                await _rec.commit()
                                    except Exception as _rec_exc:
                                        logger.error(
                                            "[REVIEW PIPELINE] C4: failed to persist "
                                            "deferred-effects trail for parent %s "
                                            "(PR #%s merged): %r",
                                            original_task_id,
                                            pr_number,
                                            _rec_exc,
                                            exc_info=True,
                                        )
                                    # The bounded blocking lock rolled the SHARED
                                    # session back (an aborted FOR UPDATE leaves the
                                    # tx in PendingRollback), which EXPIRED the
                                    # `task` ORM instance. Downstream post-handler
                                    # sections (rollup check, direction outcomes,
                                    # terminal records, final commit) read
                                    # ``task.<attr>`` on this same session — an
                                    # expired attribute access would sync lazy-load
                                    # → MissingGreenlet. Re-fetch by stable id so the
                                    # rest of _process_post_commit runs clean.
                                    with contextlib.suppress(Exception):
                                        _re_task = (
                                            (
                                                await session.execute(
                                                    select(Task).where(
                                                        Task.id == task_id
                                                    )
                                                )
                                            )
                                            .scalars()
                                            .first()
                                        )
                                        if _re_task is not None:
                                            task = _re_task
                                except Exception as e:
                                    logger.warning(
                                        f"[REVIEW PIPELINE] Failed to set pr_merged on {original_task_id}: {e}",
                                        exc_info=True,
                                    )
                            logger.info(
                                f"[REVIEW PIPELINE] PR #{pr_number} approved and merged"
                            )
                            action_taken = True
                        else:
                            # Merge blocked — park in EVALUATING with ci_waiting so the
                            # ci_completion retry-with-backoff loop handles it instead of
                            # spawning a fix task immediately.  A first-attempt merge
                            # failure is frequently a stale GitHub mergeability flag
                            # (GitHub recomputes asynchronously after pushes); ci_completion
                            # absorbs that window via its internal 5/15/45s backoff inside
                            # a single merge attempt.  If the merge still fails after that
                            # quiet window, it spawns a rebase fix for the moving-parent
                            # case (sibling rollups advancing the base branch between
                            # fetches).  Fully autonomous; no requires_approval.
                            merge_msg = merge_result.get("message", "unknown")
                            await create_pr_comment(
                                gh_token,
                                owner,
                                repo,
                                pr_number,
                                f"**Blueprint Review Pipeline**: Merge blocked — {merge_msg}. Retrying with backoff.",
                            )
                            from app import pause_clock

                            merge_wait_loop = dict(review_loop)
                            merge_wait_loop["review_history"] = _approved_history
                            merge_wait_loop["ci_waiting"] = True
                            merge_wait_loop["review_approved"] = True
                            merge_wait_loop["ci_passed"] = (
                                True  # CI was already verified green above
                            )
                            merge_wait_loop["ci_wait_started"] = _utcnow().isoformat()
                            merge_wait_loop[
                                "ci_wait_pause_baseline"
                            ] = await pause_clock.current_baseline(session)
                            merge_wait_loop["ci_poll_interval"] = 30
                            merge_wait_loop["pr_number"] = pr_number
                            merge_wait_loop["branch"] = branch
                            merge_wait_loop["base_branch"] = base_branch
                            merge_wait_loop["original_task_id"] = original_task_id
                            merge_wait_loop["head_sha"] = head_sha
                            merge_wait_loop["merge_blocked_msg"] = merge_msg[:300]
                            task.status = TaskStatus.EVALUATING
                            task.context = {
                                **(task.context or {}),
                                "review_loop": merge_wait_loop,
                            }
                            task.latest_status_message = f"Review approved, merge blocked on PR #{pr_number} ({merge_msg[:120]}) — retrying with backoff"
                            session.add(task)
                            await session.commit()
                            logger.info(
                                f"[REVIEW PIPELINE] PR #{pr_number} merge blocked, parked for ci_completion retry: {merge_msg[:200]}"
                            )
                            action_taken = True
                    except Exception as e:
                        # Merge/CI-check failed — park in EVALUATING with ci_waiting so
                        # the orchestrator's CI monitoring loop retries the merge instead
                        # of silently orphaning the PR.
                        logger.error(
                            f"[REVIEW PIPELINE] Failed to merge PR #{pr_number}, parking in CI-wait: {e}",
                            exc_info=True,
                        )
                        task.status = TaskStatus.EVALUATING
                        task.latest_status_message = f"Review approved, merge attempt failed ({type(e).__name__}) — CI monitor will retry"
                        task.context = {
                            **(task.context or {}),
                            "review_loop": {
                                **review_loop,
                                "ci_waiting": True,
                                "review_approved": True,
                                "review_history": _approved_history,
                                "issue_ledger": _authoritative_ledger,
                                "review_target": dict(_live_approve_target),
                                "head_sha": head_sha,
                                "base_sha": str(
                                    _live_approve_target["base_sha"]
                                ),
                                "pr_number": pr_number,
                                "branch": branch,
                                "base_branch": base_branch,
                                "original_task_id": original_task_id,
                                "ci_wait_started": _utcnow().isoformat(),
                            },
                        }
                        session.add(task)
                        await session.commit()
                        action_taken = True
                else:
                    # REQUEST_CHANGES — include CI errors if available (don't wait).
                    # Extracted to app.review_request_changes (S5 of the fdd07b88
                    # plan) — see that module's docstring for the free-variable
                    # analysis. A non-None ``_request_response`` means the caller
                    # must return immediately.
                    action_taken, _request_response = await run_request_changes(
                        task,
                        pr_number=pr_number,
                        branch=branch,
                        base_branch=base_branch,
                        original_task_id=original_task_id,
                        original_prompt=original_prompt,
                        feedback=feedback,
                        issues=issues,
                        resolutions=resolutions,
                        investigated_files=investigated_files,
                        verdict_unparseable=verdict_unparseable,
                        reviewed_head_sha=reviewed_head_sha,
                        reviewed_base_sha=reviewed_base_sha,
                        _review_iteration=_review_iteration,
                        _current_review_entry=_current_review_entry,
                        review_loop=review_loop,
                        _record_structured_review_result=_record_structured_review_result,
                        session=session,
                    )
                    if _request_response is not None:
                        return _request_response

                if not action_taken:
                    task.latest_status_message = f"Review completed but no action taken (verdict={verdict}) — check orchestrator logs"
                    session.add(task)
                    await session.commit()
                    logger.warning(
                        f"[REVIEW PIPELINE] WARNING: Review {task.id} for PR #{pr_number} completed with no downstream action (verdict={verdict})"
                    )

            # Fix task completed → security scan → spawn new review.
            # Extracted to app.task_lifecycle_fix_completion (S4 of the
            # fdd07b88 plan) — see that module's docstring for the
            # free-variable analysis. A non-None ``_fix_response`` means
            # the caller must return immediately; ``task`` may have been
            # re-fetched/mutated either way.
            elif task_type == "fix":
                task, _fix_response = await run_fix_completion(
                    task,
                    task_payload=task_payload,
                    review_loop=review_loop,
                    session=session,
                )
                if _fix_response is not None:
                    return _fix_response

            # Stage D (completion dispatch): registry-driven completion-handler
            # lookup/invocation and failure recovery (requeue-on-transient,
            # FAILED-with-classification, or preserving the post-commit-pending
            # marker for sweep retry). Extracted to
            # app.task_lifecycle_completion_dispatch (S3 of the fdd07b88 plan) —
            # see that module's docstring for the free-variable analysis. Unlike
            # Stage T (the terminal tail) this is not the last stage, so a
            # non-None ``_dispatch_response`` means the caller must return
            # immediately; ``task`` may have been re-fetched/mutated either way.
            task, _dispatch_response = await run_completion_dispatch(
                task,
                task_type=task_type,
                payload=payload,
                task_id=task_id,
                session=session,
                locked_project=locked_project,
                preallocated_strategic_message_sequences=preallocated_strategic_message_sequences,
                _peek_capability=_peek_capability,
                _peek_task_type=_peek_task_type,
                _expected_post_commit_claim_token=_expected_post_commit_claim_token,
                _expected_childless_recovery_token=_expected_childless_recovery_token,
                refetch=_rollback_and_refetch_task,
            )
            if _dispatch_response is not None:
                return _dispatch_response

        # Stage T (terminal tail): parent rollup, direction-outcome/remediation
        # bookkeeping, release-qualifier finalization, strategic backstop,
        # final commit, and broadcast. Extracted to
        # app.task_lifecycle_post_commit_terminal (S2 of the fdd07b88 plan) —
        # see that module's docstring for the free-variable analysis. This is
        # the last stage, so its return value is exactly what this function
        # returns.
        return await run_post_commit_terminal(
            task,
            payload=payload,
            task_id=task_id,
            session=session,
            _session_factory=_session_factory,
            refetch=_rollback_and_refetch_task,
        )


from app.task_lifecycle_wakes import (  # noqa: E402, F401, RUF100
    _maybe_fire_terminal_compute_wake,
    _PARKABLE_STATUSES,
    _PARK_STRAND_SECONDS,
    close_orphan_pr_if_any,
    deliver_wake,
    expire_wake,
    fire_task_wakes,
    park_task,
)
