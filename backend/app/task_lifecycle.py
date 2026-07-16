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
from app.models import (
    Task,
    TaskHistory,
    Project,
    TaskStatus,
    ArtifactType,
    Alert,
    AlertSeverity,
    TaskWakeRegistration,
    WakeAction,
    WakeDeliveryState,
    TASK_PARKABLE_STATUSES,
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
    strategic_phase_for,
)
from app.json_extract import scan_balanced_json_objects
from app.task_blocked import reset_task_for_fresh_attempt
import app.shared_state as _shared_state
from app.broadcast import broadcast_to_project, _sanitize_log
from app.payload_schemas import read_acceptance_evidence, validate_internal_payload
from app.review_pipeline import (
    REVIEW_FIX_GOAL_PREAMBLE,
    SPAWN_REASON_DEFERRED,
    SPAWN_REASON_KEY,
    _genuine_blocking_issue_exists,
    _spawn_pipeline_task,
    build_ci_failure_fix_prompt,
    build_merge_conflict_fix_prompt,
    detect_fix_all_issue_class,
    fix_all_occurrences_directive,
    reconciled_approve_markers,
    should_auto_approve_diff,
)
from app.failure_classification import (
    classify_failure,
    classify_external_api_subtype,
    EXTERNAL_API,
    EXTERNAL_API_SUBTYPE_PROVIDER_MODEL_CONFIG,
)
from app.branch_resolution import (
    classify_rollup_merge_outcome,
    is_custom_staged_rollup_root,
    merged_pr_counts_as_integration_landing,
)
from app.landed_signal import apply_merged_landed_signal
from app.rollup_management import _check_and_trigger_rollup, _cleanup_branches
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


class PostCommitLocked(Exception):
    """Raised when _process_post_commit can't acquire the task row within the
    retry budget. Distinct from DBAPIError so the caller in agent_status.py
    can treat "row still held by peer" as a retryable pending, not an error.
    """


class PostCommitFinalCommitFailed(Exception):
    """Raised when _process_post_commit's FINAL commit fails after the
    post-terminal chain ran.

    The final commit flushes the best-effort post-terminal side effects
    (spawned follow-up / remediation tasks, rollup nudges, analytics,
    artifacts). If it fails, those side effects are DROPPED — they were never
    persisted. The task's own terminal status is safe (it was committed on the
    early-commit / status-endpoint session before post-commit ran), so this
    is NOT a fatal error, but the dropped side effects MUST be retried.

    Distinct signal so the callers (inline status endpoint, the terminal
    idempotency retry path, _sweep_post_commit_pending) PRESERVE the
    write-ahead ``post_commit_pending`` marker for the sweep to retry instead
    of clearing it on what would otherwise look like a clean return. Crucially
    the inline / retry callers translate it to a clean 200 ("post_commit_retry
    _pending") rather than re-raising a 500 to the agent — the work survives in
    the marker, so there is nothing for the agent to do. See incident
    eefbed05: a swallowed final-commit failure cleared the marker, the sweep
    never retried, and the dependent chain orphaned.
    """


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
    _append_approve_to_review_history,
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
    _merge_with_retry,
    _pick_merge_method,
)


async def _has_active_strategic_task(project_id, session: AsyncSession) -> bool:
    """Return True if any QUEUED/PROVISIONING/IN_PROGRESS/EVALUATING task is a design_doc_update or strategic_review.

    Takes a FOR UPDATE lock on the project row so the check+spawn is transactionally
    atomic. Without it, two concurrent callers (e.g. design_doc completion chaining
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
        .with_for_update(skip_locked=True)
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


async def _resolve_safeguard_challenge_completion(
    challenge: Task, payload: AgentStatusPayload, session: AsyncSession
) -> None:
    """Route a safeguard-challenge verdict back to the parked safeguard task.

    The challenge (spawned by ``task_evaluators.spawn_safeguard_challenge``)
    assessed a safeguard audit's critical/high finding.  Here we parse the
    outcome ("confirmed" | "rejected" | "downgraded"), write it onto the
    safeguard task's context as ``SAFEGUARD_CHALLENGE_VERDICT_KEY``, restore
    the safeguard to COMPLETED, and re-invoke ``_maybe_release_safeguard_gate``
    so the gate decision reflects the challenger's verdict.

    Resolves BOTH a COMPLETED and a FAILED challenger (a failed challenge
    fails closed: outcome="confirmed") so the safeguard never strands EVALUATING.
    """
    from app.task_evaluators import (
        SAFEGUARD_CHALLENGE_SOURCE_KEY,
        SAFEGUARD_CHALLENGE_VERDICT_KEY,
        parse_safeguard_challenge_outcome,
    )
    from app.task_completion import _maybe_release_safeguard_gate

    source_id = (challenge.context or {}).get(SAFEGUARD_CHALLENGE_SOURCE_KEY)
    try:
        source_uuid = uuid.UUID(str(source_id))
    except (ValueError, TypeError):
        await session.commit()
        return

    safeguard = (
        (
            await session.execute(
                select(Task).where(Task.id == source_uuid).with_for_update()
            )
        )
        .scalars()
        .first()
    )
    if safeguard is None:
        await session.commit()
        logger.warning(
            "[SAFEGUARD] Safeguard task %s for challenge %s is gone; verdict dropped",
            source_id,
            challenge.id,
        )
        return

    ctx = dict(safeguard.context or {})
    lane = "safeguard_challenge"
    pending_application = _sidecar_application_pending(
        ctx, lane=lane, sidecar_id=challenge.id
    )
    if not pending_application and (
        SAFEGUARD_CHALLENGE_VERDICT_KEY in ctx
        or "awaiting_safeguard_challenge" not in ctx
    ):
        await session.commit()
        logger.info(
            "[SAFEGUARD] Challenge %s for safeguard %s already resolved; ignoring replayed terminal status",
            challenge.id,
            safeguard.id,
        )
        return

    if not pending_application:
        completed = payload.status == TaskStatus.COMPLETED
        challenge_logs = payload.logs or challenge.latest_status_message or ""
        verdict = parse_safeguard_challenge_outcome(challenge_logs, completed=completed)
        challenge.latest_status_message = (
            f"Safeguard challenge: {verdict['outcome']} — {verdict['reasoning']}"
        )[:500]
        session.add(challenge)
        ctx[SAFEGUARD_CHALLENGE_VERDICT_KEY] = {
            "outcome": verdict["outcome"],
            "reasoning": verdict["reasoning"],
            "challenge_task_id": str(challenge.id),
        }
        ctx.pop("awaiting_safeguard_challenge", None)
        _set_sidecar_application_pending(
            ctx,
            lane=lane,
            sidecar_id=challenge.id,
            verdict_key=SAFEGUARD_CHALLENGE_VERDICT_KEY,
        )
    safeguard.context = ctx
    flag_modified(safeguard, "context")
    # Restore terminal status so _maybe_release_safeguard_gate sees a resolved task.
    safeguard.status = TaskStatus.COMPLETED
    session.add(safeguard)
    await session.commit()

    # Re-read findings from the stored plan_output and re-invoke the gate function.
    _plan_output = safeguard.plan_output
    if isinstance(_plan_output, dict) and isinstance(
        _plan_output.get("findings"), list
    ):
        findings = _plan_output["findings"]
    elif isinstance(_plan_output, list):
        findings = _plan_output
    else:
        findings = None

    await _maybe_release_safeguard_gate(safeguard, findings, session)
    await session.refresh(safeguard)
    _clear_sidecar_application_pending(safeguard, lane=lane, sidecar_id=challenge.id)
    session.add(safeguard)
    await session.commit()

    logger.info(
        "[SAFEGUARD] Challenge %s resolved safeguard %s with outcome=%s",
        challenge.id,
        safeguard.id,
        ((safeguard.context or {}).get(SAFEGUARD_CHALLENGE_VERDICT_KEY) or {}).get(
            "outcome", "applied"
        ),
    )


async def _resolve_review_second_opinion(
    challenger: Task, payload: AgentStatusPayload, session: AsyncSession
) -> None:
    """Resolve an independent panel-review challenge and reconcile it.

    The challenger (spawned by
    ``task_evaluators.spawn_review_second_opinion``) ran an independent
    multi-lens panel review of the same PR. Here we parse its verdict,
    reconcile it against the parked primary verdict, persist a durable
    reconciliation record on the review task's context, and re-dispatch the
    review task through the normal finalization path with the reconciled
    verdict. Fail-closed: if the challenge FAILED or its output is
    unparseable, do NOT merge or spawn.
    """
    from app.task_evaluators import (
        BLIND_REVIEW_SOURCE_KEY,
        REVIEW_SECOND_OPINION_SOURCE_KEY,
        REVIEW_SECOND_OPINION_AWAITING_KEY,
        REVIEW_SECOND_OPINION_PARKED_KEY,
    )
    from app.task_completion import (
        _parse_panel_review_output,
        reconcile_review_second_opinion,
        REVIEW_SECOND_OPINION_RECONCILIATION_KEY,
    )
    from app.broadcast import broadcast_to_project

    source_id = (challenger.context or {}).get(REVIEW_SECOND_OPINION_SOURCE_KEY)
    try:
        source_uuid = uuid.UUID(str(source_id))
    except (ValueError, TypeError):
        await session.commit()
        return

    review = (
        (
            await session.execute(
                select(Task).where(Task.id == source_uuid).with_for_update()
            )
        )
        .scalars()
        .first()
    )
    if review is None:
        await session.commit()
        logger.warning(
            "[REVIEW SECOND-OPINION] Review task %s for challenger %s is gone; verdict dropped",
            source_id,
            challenger.id,
        )
        return

    _review_ctx = review.context or {}
    lane = "review_second_opinion"
    pending_application = _sidecar_application_pending(
        dict(_review_ctx), lane=lane, sidecar_id=challenger.id
    )
    if not pending_application and (
        REVIEW_SECOND_OPINION_RECONCILIATION_KEY in _review_ctx
        or REVIEW_SECOND_OPINION_AWAITING_KEY not in _review_ctx
    ):
        await session.commit()
        logger.info(
            "[REVIEW SECOND-OPINION] Challenge %s for review %s already resolved — ignoring replayed terminal status",
            challenger.id,
            review.id,
        )
        return

    parked = (review.context or {}).get(REVIEW_SECOND_OPINION_PARKED_KEY) or {}
    primary_verdict = parked.get("verdict") or "REQUEST_CHANGES"
    primary_issues = parked.get("issues") or []

    if pending_application:
        record = dict(
            (review.context or {}).get(REVIEW_SECOND_OPINION_RECONCILIATION_KEY) or {}
        )
        panel = {"pending_replay": True} if record else None
    else:
        completed = payload.status == TaskStatus.COMPLETED
        panel = (
            _parse_panel_review_output(
                payload.logs or challenger.latest_status_message or ""
            )
            if completed
            else None
        )

    if panel is None:
        # Model-config-rejection: the challenger's model is operator-configured but
        # inaccessible (e.g. "model does not exist or you do not have access").
        # Silently failing closed would set requires_approval=True on every trunk
        # APPROVE — converting the gate into an invisible universal merge block.
        # Instead: degrade visibly, preserve the primary verdict, signal operators.
        if (
            not pending_application
            and payload.status == TaskStatus.FAILED
            and classify_failure(challenger, logs=payload.logs) == EXTERNAL_API
            and classify_external_api_subtype(challenger, logs=payload.logs)
            == EXTERNAL_API_SUBTYPE_PROVIDER_MODEL_CONFIG
        ):
            logger.warning(
                "[REVIEW SECOND-OPINION] Challenge %s for review %s: panel model "
                "unavailable (provider model-config rejection) — degrading gracefully, "
                "primary verdict %r preserved",
                challenger.id,
                review.id,
                primary_verdict,
            )
            try:
                from app.alert_dispatch import (
                    _maybe_create_adversarial_gate_fail_open_alert,
                )

                await _maybe_create_adversarial_gate_fail_open_alert(
                    review, challenger, session
                )
            except Exception as _ae:
                logger.warning(
                    "[REVIEW SECOND-OPINION] fail-open alert dispatch failed: %s", _ae
                )
            _deg_record: dict = {
                "state": "degraded_model_unavailable",
                "reconciled_verdict": primary_verdict,
                "surviving_issues": primary_issues,
                "challenger_task_id": str(challenger.id),
                "review_task_id": str(review.id),
            }
            _deg_ctx = dict(review.context or {})
            _deg_ctx.pop(REVIEW_SECOND_OPINION_AWAITING_KEY, None)
            _deg_ctx[REVIEW_SECOND_OPINION_RECONCILIATION_KEY] = _deg_record
            _set_sidecar_application_pending(
                _deg_ctx,
                lane=lane,
                sidecar_id=challenger.id,
                verdict_key=REVIEW_SECOND_OPINION_RECONCILIATION_KEY,
            )
            review.context = _deg_ctx
            flag_modified(review, "context")
            review.status = TaskStatus.COMPLETED
            session.add(review)
            challenger.latest_status_message = (
                "Second-opinion panel model unavailable — degraded to primary verdict"
            )[:500]
            session.add(challenger)
            await session.commit()
            _deg_reconstructed = AgentStatusPayload(
                task_id=review.id,
                status=TaskStatus.COMPLETED,
                verdict_json=json.dumps(
                    {
                        "verdict": _deg_record["reconciled_verdict"],
                        "issues": _deg_record.get("surviving_issues", []),
                        "feedback": parked.get("feedback", ""),
                        "investigated_files": parked.get("investigated_files", []),
                    }
                ),
            )
            await session.commit()
            await _process_post_commit(review.id, _deg_reconstructed)
            await session.refresh(review)
            _deg_ctx2 = dict(review.context or {})
            _deg_ctx2.pop(REVIEW_SECOND_OPINION_PARKED_KEY, None)
            review.context = _deg_ctx2
            _clear_sidecar_application_pending(
                review, lane=lane, sidecar_id=challenger.id
            )
            session.add(review)
            await session.commit()
            return

        original_task_id = parked.get("original_task_id") or ""
        ctx = dict(review.context or {})
        ctx.pop(REVIEW_SECOND_OPINION_AWAITING_KEY, None)
        ctx[REVIEW_SECOND_OPINION_RECONCILIATION_KEY] = {
            "state": "unchallenged",
            "escalated": True,
            "reason": (
                "second-opinion challenge failed or produced an unparseable "
                "verdict — escalated to manual review"
            ),
            "primary_verdict": primary_verdict,
            "challenger_task_id": str(challenger.id),
            "review_task_id": str(review.id),
        }
        ctx.pop(REVIEW_SECOND_OPINION_PARKED_KEY, None)
        review.context = ctx
        flag_modified(review, "context")
        review.status = TaskStatus.COMPLETED
        review.latest_status_message = (
            "Second-opinion challenge unresolved — manual review required; "
            "no merge or fix spawned"
        )[:500]
        session.add(review)
        challenger.latest_status_message = (
            "Second-opinion panel review failed/unparseable — review escalated"
        )[:500]
        session.add(challenger)
        _gates_on = _runtime_config.get("require_approval_gates", True)
        if original_task_id:
            try:
                orig = await session.get(Task, uuid.UUID(original_task_id))
                if orig:
                    if _gates_on:
                        orig.requires_approval = True
                    orig.latest_status_message = (
                        "Second-opinion review challenge unresolved — manual "
                        "review required"
                    )[:500]
                    session.add(orig)
            except (ValueError, TypeError):
                pass
        await session.commit()
        logger.warning(
            "[REVIEW SECOND-OPINION] Challenge %s for review %s failed/unparseable — fail-closed escalation, no action taken",
            challenger.id,
            review.id,
        )
        try:
            await broadcast_to_project(
                str(review.project_id),
                "task_updated",
                {"task_id": str(review.id), "status": review.status},
            )
        except Exception:
            logger.debug(
                "[REVIEW SECOND-OPINION] broadcast after escalation failed",
                exc_info=True,
            )
        return

    if not pending_application:
        record = reconcile_review_second_opinion(primary_verdict, primary_issues, panel)
        record["challenger_task_id"] = str(challenger.id)
        record["review_task_id"] = str(review.id)
        # Distinguish blind multi-reviewer from single second-opinion challenger
        # so the reconciliation record is self-describing for telemetry.
        record["is_blind"] = bool(
            (challenger.context or {}).get(BLIND_REVIEW_SOURCE_KEY)
        )

        ctx = dict(review.context or {})
        ctx[REVIEW_SECOND_OPINION_RECONCILIATION_KEY] = record
        ctx.pop(REVIEW_SECOND_OPINION_AWAITING_KEY, None)
        _set_sidecar_application_pending(
            ctx,
            lane=lane,
            sidecar_id=challenger.id,
            verdict_key=REVIEW_SECOND_OPINION_RECONCILIATION_KEY,
        )
        review.context = ctx
        flag_modified(review, "context")
        review.status = TaskStatus.COMPLETED
        session.add(review)
        challenger.latest_status_message = (
            f"Second-opinion panel: {record['state']} → {record['reconciled_verdict']}"
        )[:500]
        session.add(challenger)
        await session.commit()

    reconstructed = AgentStatusPayload(
        task_id=review.id,
        status=TaskStatus.COMPLETED,
        verdict_json=json.dumps(
            {
                # ``record`` may be a stored record loaded on the pending-application
                # REPLAY path; a fail-closed escalation can persist one without a
                # ``reconciled_verdict`` key. Fall back to the (in-scope) primary
                # verdict — the fail-closed value that preserves current behavior on
                # every reachable path (reconcile_review_second_opinion always sets
                # the key, so the default is only used by a keyless replay record).
                "verdict": record.get("reconciled_verdict", primary_verdict),
                "issues": record.get("surviving_issues", []),
                "feedback": parked.get("feedback", ""),
                "investigated_files": parked.get("investigated_files", []),
            }
        ),
    )
    _blind = " blind" if record.get("is_blind") else ""
    logger.info(
        "[REVIEW SECOND-OPINION]%s Challenge %s for review %s reconciled as %s (%s → %s); re-dispatching review",
        _blind,
        challenger.id,
        review.id,
        record["state"],
        primary_verdict,
        record.get("reconciled_verdict", primary_verdict),
    )
    # Release the review row's FOR UPDATE lock (acquired above) BEFORE
    # _process_post_commit: it opens a SEPARATE session and re-acquires FOR UPDATE
    # SKIP LOCKED on this same review row. On the pending_application=True REPLAY
    # path the commit inside `if not pending_application:` above is skipped, so
    # without this the outer lock is still held → the inner acquire returns None
    # every retry → PostCommitLocked, the marker cleanup below is never reached,
    # and the reconciled verdict is never applied (deadlock-by-construction on
    # PostgreSQL). Mirrors the invariant honored at routers/agent_status.py and by
    # the non-replay branch's commit above; a second commit on the already-committed
    # non-replay path is a harmless no-op.
    await session.commit()
    await _process_post_commit(review.id, reconstructed)
    await session.refresh(review)
    ctx = dict(review.context or {})
    ctx.pop(REVIEW_SECOND_OPINION_PARKED_KEY, None)
    review.context = ctx
    _clear_sidecar_application_pending(review, lane=lane, sidecar_id=challenger.id)
    session.add(review)
    await session.commit()


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

    All four resolver functions already handle a FAILED payload fail-closed, so
    no additional logic is needed here — this is purely a routing shim.
    """
    from app.task_evaluators import (
        QUALITY_GRADER_SOURCE_KEY,
        SAFEGUARD_CHALLENGE_SOURCE_KEY,
        FINDING_CHALLENGE_SOURCE_KEY,
        REVIEW_SECOND_OPINION_SOURCE_KEY,
    )

    synthetic = AgentStatusPayload(
        task_id=sidecar.id,
        status=sidecar.status,
        logs=sidecar.latest_status_message or "",
    )
    ctx = sidecar.context or {}
    if ctx.get(SAFEGUARD_CHALLENGE_SOURCE_KEY):
        await _resolve_safeguard_challenge_completion(sidecar, synthetic, session)
    elif ctx.get(QUALITY_GRADER_SOURCE_KEY):
        await _resolve_quality_grader_completion(sidecar, synthetic, session)
    elif ctx.get(FINDING_CHALLENGE_SOURCE_KEY):
        await _resolve_finding_challenge_completion(sidecar, synthetic, session)
    elif ctx.get(REVIEW_SECOND_OPINION_SOURCE_KEY):
        await _resolve_review_second_opinion(sidecar, synthetic, session)
    else:
        logger.warning(
            "[SIDECAR-RECOVERY] Sidecar %s has no recognised source key; cannot route",
            sidecar.id,
        )


async def _process_post_commit(
    task_id: uuid.UUID,
    payload: AgentStatusPayload,
    _session_factory=None,
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
        close_pull_request,
    )
    from app.task_completion import (
        _handle_plan_completion,
        _handle_design_doc_completion,
        _handle_panel_review_completion,
        _handle_security_audit_completion,
        _handle_perf_completion,
        _handle_compute_experiment_completion,
        _handle_self_audit_completion,
        _handle_memo_completion,
        _handle_repo_bootstrap_completion,
        _handle_capability_proof_trial_completion,
        _try_recover_memo_from_parse_failure,
        _record_remediation_if_terminal,
    )
    from app.strategic_completion import (
        _handle_strategic_completion,
        _record_strategic_task_failure,
        strategic_completion_message_count,
    )
    from app.project_message_creation import reserve_project_message_sequences
    from app.research_completion import _handle_research_completion
    from app.knowledge_completion import (
        _handle_design_completion,
        _handle_web_research_completion,
    )

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
            raise PostCommitLocked(f"task {task_id} row held by peer after 3 retries")

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

        # Safeguard-challenge side-car: an independent challenger spawned by
        # ``_maybe_release_safeguard_gate`` carries SAFEGUARD_CHALLENGE_SOURCE_KEY
        # pointing at the safeguard task whose gate decision it challenges.
        # Handled BEFORE the quality-grader check so a challenge task is never
        # mis-routed as a quality-grader completion, and before the
        # COMPLETED-gated pipeline so even a FAILED challenger resolves
        # fail-closed rather than stranding the safeguard EVALUATING forever.
        from app.task_evaluators import SAFEGUARD_CHALLENGE_SOURCE_KEY

        if (task.context or {}).get(SAFEGUARD_CHALLENGE_SOURCE_KEY):
            await _resolve_safeguard_challenge_completion(task, payload, session)
            return {"status": "accepted"}

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

        # Review second-opinion challenger side-car: a panel_review task spawned
        # by the second-opinion gate carries REVIEW_SECOND_OPINION_SOURCE_KEY
        # pointing at the parked ordinary review task. Its completion resolves
        # the challenge and re-dispatches the review with the reconciled verdict
        # — it does NOT flow through the panel_review completion handler, and
        # (critically) it never re-enters the review-finalization branch, so the
        # challenger can never recursively re-trigger the gate. Handled before
        # the COMPLETED-gated pipeline so a FAILED challenger fails closed
        # (escalates) rather than stranding the review EVALUATING.
        from app.task_evaluators import REVIEW_SECOND_OPINION_SOURCE_KEY

        if (task.context or {}).get(REVIEW_SECOND_OPINION_SOURCE_KEY):
            await _resolve_review_second_opinion(task, payload, session)
            return {"status": "accepted"}

        # Immediately unblock dependents when task terminally fails (retries exhausted).
        # Uses the same session (not a separate one) to avoid self-deadlock: a second
        # session scanning the task table would block on the FOR UPDATE lock held by
        # this session, causing a hang in Postgres (asyncio is single-threaded).
        if payload.status == TaskStatus.FAILED:
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

            # Record strategic task failures for the circuit breaker and cycle history
            if task.first_payload.get("type") in (
                "design_doc_update",
                "strategic_review",
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

            # C5: release the cross-cutting safeguard merge gate when the
            # safeguard task itself FAILS. The gate is released only on the
            # security_audit COMPLETED branch (_handle_security_audit_completion),
            # so a FAILED/reaped safeguard would otherwise leave orig.requires_
            # approval=True forever — a permanent strand (services.py then 2h-
            # timeout-FAILs the healthy parent, discarding merged siblings).
            # Pass findings=None: a failed safeguard produced no blocker, so the
            # gate releases (post-hoc review) rather than hanging indefinitely.
            # _maybe_release_safeguard_gate no-ops for non-safeguard tasks.
            if task.first_payload.get("type") == "security_audit":
                try:
                    from app.task_completion import _maybe_release_safeguard_gate

                    await _maybe_release_safeguard_gate(task, None, session)
                except Exception as _sg_exc:
                    logger.warning(
                        "[SAFEGUARD] Failed to release gate on FAILED safeguard %s: %r",
                        task.id,
                        _sg_exc,
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
        if session.dirty or session.new:
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
                # QP6: the gate is the single, unit-tested `should_auto_approve_diff`
                # helper (review_pipeline.py) — NOT a weaker inline copy. The helper
                # applies the strict checks the old inline logic skipped: source-type
                # eligibility, test-file exclusion from the line/file budgets, and the
                # scope-overflow warning. The main-branch path keeps two *additional*
                # live constraints on top of the helper: it only runs for non-staging
                # PRs and only when a resolvable direction has favorable acceptance
                # history (a missing/unknown direction is NOT auto-approved here, even
                # though the helper would pass `direction_data=None` by default).
                auto_approve_small = False
                if (
                    not is_intermediate
                    and scope == "small"
                    and _runtime_config.get("auto_approve_small_diff_enabled", True)
                ):
                    try:
                        # Resolve the direction so the helper's acceptance-rate gate
                        # runs against real history. The main-branch path additionally
                        # *requires* a resolvable direction (see comment above).
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
                        # Main-branch path requires a *positive* acceptance signal:
                        # an unknown direction or a direction with no merge/reject
                        # history (acceptance == None) is NOT auto-approved here. This
                        # is intentionally stricter than the helper's default (which
                        # passes unknown acceptance) because main-branch merges skip
                        # review entirely.
                        from app.direction_metrics import _compute_acceptance_rate

                        acceptance = (
                            _compute_acceptance_rate(_aa_dir)
                            if _aa_dir is not None
                            else None
                        )
                        if acceptance is None or acceptance <= 0.5:
                            logger.info(
                                f"[PIPELINE] PR #{pr_number} not auto-approved — "
                                f"direction acceptance={acceptance} (require >0.5 with "
                                "history); defaulting to review"
                            )
                        else:
                            skip, reason = should_auto_approve_diff(task, _aa_dir)
                            if skip:
                                auto_approve_small = True
                                logger.info(
                                    f"[PIPELINE] PR #{pr_number} auto-approved for "
                                    f"merge ({reason}, acceptance={acceptance:.2f}) — "
                                    "skipping review"
                                )
                            else:
                                logger.info(
                                    f"[PIPELINE] PR #{pr_number} not auto-approved "
                                    f"({reason}) — defaulting to review"
                                )
                    except Exception as _aa_exc:
                        logger.info(
                            f"[PIPELINE] Auto-approve eligibility check failed for PR #{pr_number}: {_aa_exc!r} — defaulting to review"
                        )

                if (
                    is_intermediate and scope in ("small", "medium")
                ) or auto_approve_small or not is_intermediate:
                    # Security scan then auto-merge, skip model review.
                    #
                    # Two cases route here:
                    #   1) Small/medium scope on an intermediate staging branch
                    #      (rollup child). The rollup PR to main gets the full
                    #      review — staging-branch merges don't need it.
                    #   2) Non-rollup single task landing directly into automain
                    #      (any scope, not intermediate). CI + the promotion gate
                    #      already backstop these, so the expensive model review
                    #      is redundant and the cut matches the v24 cost plan
                    #      (P2: Reduce duplicate agent work). Rollup PRs where
                    #      changes aggregate keep full review.
                    try:
                        gh_token, owner, repo = await get_github_token_for_task(
                            task.id, session
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

                                        task.status = TaskStatus.EVALUATING
                                        task.latest_status_message = (
                                            "Merge held — auto-finalized provisional "
                                            "Alembic migration "
                                            f"{_finalization['old_revision']}->"
                                            f"{_finalization['finalized_revision']} "
                                            f"against {base_branch}; awaiting fresh CI "
                                            "on the rewritten PR head"
                                        )[:500]
                                        task.context = {
                                            **(task.context or {}),
                                            "_migration_finalization": _finalization,
                                            "review_loop": {
                                                **_reset_review_loop_pr_head_cache(
                                                    (task.context or {}).get(
                                                        "review_loop"
                                                    )
                                                ),
                                                "ci_waiting": True,
                                                "review_approved": True,
                                                "ci_passed": False,
                                                "pr_number": pr_number,
                                                "branch": branch,
                                                "base_branch": base_branch,
                                            },
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
                                    complete_source_on_fix_spawn=False,
                                )
                                session.add(task)
                                await session.commit()
                                return {"status": "accepted"}

                        merge_result = await _merge_with_retry(  # premerge-guard-exempt: migration PRs are parked above in the dual-head guard branch (returns "accepted"); this merge only runs for staging-only branches that never carry a migration onto the integration trunk
                            merge_pull_request,
                            gh_token,
                            owner,
                            repo,
                            pr_number,
                            project_id=str(task.project_id),
                            merge_method=_pick_merge_method(task),
                        )
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
                                apply_merged_landed_signal(
                                    task,
                                    _project,
                                    base_branch=base_branch,
                                    hotfix_lane=bool(
                                        (task.first_payload or {}).get("hotfix")
                                        or (task.context or {}).get("hotfix")
                                    ),
                                    non_merge_reason="merged_pr_non_integration_branch",
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
                            #   blocked / unstable → CI gating; queue GitHub auto-merge
                            #     and let it fire when checks clear.
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
                                from app.github import enable_auto_merge

                                pr_detail = await get_pull_request(
                                    gh_token,
                                    owner,
                                    repo,
                                    pr_number,
                                    project_id=str(task.project_id),
                                )
                                ms = (pr_detail.get("mergeable_state") or "").lower()
                                if ms in ("blocked", "unstable", "clean"):
                                    from app.rollup_migration_validation import (
                                        pr_touches_migrations,
                                    )

                                    # Migration-carrying PRs must NOT be delegated to
                                    # GitHub native auto-merge: the target branch can
                                    # advance while checks are still pending, so the
                                    # eventual merge could land against a different
                                    # migration chain than the one Blueprint validated.
                                    # Skip delegation and park in CI-wait so
                                    # handle_ci_completion re-enters and re-runs the
                                    # pre-merge Alembic guard against the live ci_base
                                    # before merging.
                                    if await pr_touches_migrations(
                                        gh_token, owner, repo, pr_number
                                    ):
                                        recovery_type = None
                                        logger.info(
                                            f"[PIPELINE] PR #{pr_number} (mergeable_state={ms}) "
                                            f"touches Alembic migrations — skipping GitHub auto-merge; "
                                            f"parking in CI-wait for re-validation against {base_branch}"
                                        )
                                    else:
                                        am_result = await enable_auto_merge(  # premerge-guard-exempt: only the non-migration branch reaches here; migration-touching PRs are diverted to CI-wait re-validation in the if-branch above, so auto-merge never fires for a migration PR
                                            gh_token,
                                            owner,
                                            repo,
                                            pr_number,
                                            project_id=str(task.project_id),
                                        )
                                        if am_result.get("enabled"):
                                            logger.info(
                                                f"[PIPELINE] Enabled GitHub auto-merge on PR #{pr_number} (mergeable_state={ms})"
                                            )
                                        else:
                                            logger.info(
                                                f"[PIPELINE] Auto-merge enablement declined for PR #{pr_number}: {am_result.get('message')}"
                                            )
                                        if ms == "clean":
                                            # PR is clean/ready — transient API failure, not a real
                                            # conflict. Skip recovery task spawn; let auto-merge
                                            # fire when GitHub is ready.
                                            recovery_type = None
                                elif ms == "dirty":
                                    recovery_type = "fix"
                            except Exception as _am_exc:
                                logger.warning(
                                    f"[PIPELINE] Mergeable-state probe failed for PR #{pr_number}: {_am_exc}"
                                )
                            # Spawn the recovery task. For DIRTY conflicts on cluster
                            # sub-PRs we use `fix` (rebase + resolve); otherwise
                            # `review` (the existing default).
                            # When recovery_type is None (clean state) no spawn fires —
                            # auto-merge was re-enabled above and the task parks in CI-wait.
                            #
                            # Pass fix_reason="merge_conflict" so the fix-completion
                            # handler can route straight to security-scan + auto-merge
                            # WITHOUT a re-review. Rationale (per the small/medium
                            # intermediate path): the original task was scope-gated
                            # to skip review entirely; once the conflict is resolved
                            # there's no review-worthy logic change to re-assess —
                            # rebase output is mechanical. This keeps the cost model
                            # symmetric with the no-conflict happy path.
                            if recovery_type is None:
                                logger.info(
                                    f"[PIPELINE] Auto-merge failed for PR #{pr_number} (mergeable_state=clean) — re-enabled auto-merge, parking in CI-wait"
                                )
                                task.status = TaskStatus.EVALUATING
                                task.latest_status_message = f"Auto-merge failed for PR #{pr_number} (clean state) — parked for CI-wait retry"
                                # Small/medium intermediate (and auto_approve_small) explicitly
                                # skip review — no review task will ever set review_approved.
                                # mergeable_state="clean" also implies CI is already green.
                                # Pre-satisfy both gate flags so handle_ci_completion can merge
                                # on the next signal instead of stranding the task on
                                # _review_approved.
                                task.context = {
                                    **(task.context or {}),
                                    "review_loop": {
                                        "ci_waiting": True,
                                        "review_approved": True,
                                        "ci_passed": True,
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
                        task.latest_status_message = f"Review not spawned for PR #{pr_number} — iteration cap reached"
                        task.requires_approval = True
                        session.add(task)
                        await session.commit()
                        logger.warning(
                            f"[REVIEW PIPELINE] WARNING: Review spawn returned None for PR #{pr_number}"
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
                investigated_files: list[str] = []

                # Parse verdict: prefer structured verdict_json field, fall back to log parsing
                verdict_parsed = False
                verdict_unparseable = False
                review_was_parallelized = False
                reviewed_head_sha: str = ""

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

                # ── Default-on second-opinion gate ──────────────────────────
                # Before a high-stakes ordinary review verdict commits, require
                # an independent panel_review challenge and reconcile it against
                # this primary verdict. High-stakes = an APPROVE on a PR
                # targeting a trunk branch (integration branch or promotion
                # target) — the only verdict that commits an irreversible
                # auto-merge. REQUEST_CHANGES and non-trunk reviews proceed
                # synchronously (recorded ``unchallenged``): a wrong rejection
                # self-corrects through the fix loop, and research-branch PRs
                # are re-reviewed at the rollup boundary. Default-on; operator
                # rollback via the ``review_second_opinion_gate_enabled``
                # runtime flag restores fully synchronous behavior.
                #
                # Re-entry: once the challenge has reconciled, the review task
                # carries the reconciliation record. That record (a) overrides
                # ``verdict``/``issues`` with the reconciled, surviving set and
                # (b) suppresses re-spawning the gate (single-shot), so the rest
                # of this branch runs exactly once more with the reconciled
                # verdict and every downstream gate unchanged.
                from app.task_completion import (
                    REVIEW_SECOND_OPINION_RECONCILIATION_KEY,
                )
                from app.task_evaluators import (
                    review_is_high_stakes,
                    spawn_blind_multi_reviewer,
                    spawn_review_second_opinion,
                )
                from app.review_heuristics import (
                    _diff_lines_changed,
                    _is_test_file,
                )

                _recon_record = (task.context or {}).get(
                    REVIEW_SECOND_OPINION_RECONCILIATION_KEY
                )
                if isinstance(_recon_record, dict) and _recon_record.get(
                    "review_task_id"
                ) != str(task.id):
                    # The record belongs to a DIFFERENT review task (or predates
                    # ownership stamping): it leaked in via a context copy, e.g.
                    # the review→fix→review propagation. Adopting it would
                    # clobber this review's fresh verdict with the earlier
                    # iteration's verdict + verbatim issues — the identical
                    # issue set then trips the stuck-cycle detector and falsely
                    # parks the chain (PR #9243). Scrub the whole inherited
                    # second-opinion family and run the gate fresh.
                    logger.warning(
                        "[REVIEW PIPELINE] PR #%s: ignoring second-opinion "
                        "record inherited from review %s on review %s",
                        pr_number,
                        _recon_record.get("review_task_id"),
                        task.id,
                    )
                    from app.task_completion import (
                        REVIEW_SECOND_OPINION_TASK_LOCAL_KEYS,
                    )

                    _scrubbed_ctx = dict(task.context or {})
                    for _so_key in REVIEW_SECOND_OPINION_TASK_LOCAL_KEYS:
                        _scrubbed_ctx.pop(_so_key, None)
                    task.context = _scrubbed_ctx
                    session.add(task)
                    _recon_record = None
                if _recon_record is not None:
                    # Re-entrant pass after reconciliation: adopt the reconciled
                    # verdict + surviving issue set, then fall through to the
                    # normal action branches without re-evaluating the gate.
                    _reconciled = _recon_record.get("reconciled_verdict")
                    if _reconciled in ("APPROVE", "REQUEST_CHANGES"):
                        verdict = _reconciled
                    _surviving = _recon_record.get("surviving_issues")
                    if isinstance(_surviving, list):
                        issues = _surviving
                elif not verdict_unparseable and _runtime_config.get(
                    "review_second_opinion_gate_enabled", True
                ):
                    # Load source task diff stats for the size-exemption check.
                    # The review task itself does not carry _pr_diff_summary /
                    # _pr_changed_files (both are excluded from context
                    # propagation in review_pipeline._PROPAGATE_UNDERSCORE_KEYS),
                    # so we read them from the original create/fix/refactor task.
                    # One extra DB read per APPROVE-to-trunk verdict; skipped
                    # when original_task_id is unavailable (fail-open: challenge).
                    _so_diff_lines: int | None = None
                    _so_file_count: int | None = None
                    _is_observable_behavior: bool = False
                    if original_task_id:
                        try:
                            _so_source = await session.get(
                                Task, uuid.UUID(original_task_id)
                            )
                            if _so_source is not None:
                                _so_ctx = _so_source.context or {}
                                _so_diff_lines = _diff_lines_changed(
                                    _so_ctx.get("_pr_diff_summary"),
                                    exclude_test_files=True,
                                )
                                _so_files = _so_ctx.get("_pr_changed_files") or []
                                _so_file_count = sum(
                                    1
                                    for f in _so_files
                                    if not _is_test_file(f)
                                )
                                # Check whether the source task declared
                                # observable_behavior acceptance evidence.
                                # These tasks always get a second opinion
                                # regardless of diff size — their proof is
                                # entirely self-reported with no
                                # machine-checkable assertion.
                                try:
                                    _evidence = read_acceptance_evidence(
                                        _so_source.first_payload
                                    )
                                    if (
                                        _evidence is not None
                                        and _evidence.kind == "observable_behavior"
                                    ):
                                        _is_observable_behavior = True
                                except Exception:
                                    pass
                        except Exception:
                            logger.debug(
                                "[REVIEW PIPELINE] PR #%s: could not load source task "
                                "%s for diff-size check — proceeding with challenge",
                                pr_number,
                                original_task_id,
                            )
                    if review_is_high_stakes(
                        verdict,
                        issues,
                        investigated_files,
                        task,
                        base_branch=base_branch,
                        trunk_branches=(
                            _shared_state.get_project_integration_branch(
                                _review_project
                            ),
                            _shared_state.get_project_promotion_target(_review_project),
                        ),
                        diff_non_test_lines=_so_diff_lines,
                        diff_non_test_file_count=_so_file_count,
                        is_observable_behavior=_is_observable_behavior,
                    ):
                        _sample_rate = float(
                            _runtime_config.get(
                                "review_second_opinion_sample_rate", 1.0
                            )
                        )
                        _task_slot = task.id.int % 1_000_000 / 1_000_000.0
                        if _task_slot >= _sample_rate:
                            logger.info(
                                "[REVIEW PIPELINE] PR #%s: challenger sampled out "
                                "(slot=%.6f >= rate=%.4f) — proceeding synchronously",
                                pr_number,
                                _task_slot,
                                _sample_rate,
                            )
                            task.context = {
                                **(task.context or {}),
                                REVIEW_SECOND_OPINION_RECONCILIATION_KEY: {
                                    "state": "sampled_out",
                                    "sample_rate": _sample_rate,
                                    "primary_verdict": verdict,
                                    "reconciled_verdict": verdict,
                                    "surviving_issues": issues,
                                    "review_task_id": str(task.id),
                                },
                            }
                            session.add(task)
                        else:
                            _use_blind = bool(
                                _runtime_config.get(
                                    "blind_multi_reviewer_gate_enabled", False
                                )
                            )
                            if _use_blind:
                                _challenger = await spawn_blind_multi_reviewer(
                                    task,
                                    session,
                                    verdict=verdict,
                                    issues=issues,
                                    feedback=feedback,
                                    pr_number=pr_number,
                                    branch=branch,
                                    base_branch=base_branch,
                                    original_task_id=original_task_id,
                                    original_prompt=original_prompt,
                                    investigated_files=investigated_files,
                                )
                            else:
                                _challenger = await spawn_review_second_opinion(
                                    task,
                                    session,
                                    verdict=verdict,
                                    issues=issues,
                                    feedback=feedback,
                                    pr_number=pr_number,
                                    branch=branch,
                                    base_branch=base_branch,
                                    original_task_id=original_task_id,
                                    original_prompt=original_prompt,
                                    investigated_files=investigated_files,
                                )
                            if _challenger is not None:
                                logger.info(
                                    "[REVIEW PIPELINE] PR #%s: parked review %s "
                                    "awaiting second-opinion challenge %s "
                                    "(primary verdict=%s) — not acting yet",
                                    pr_number,
                                    task.id,
                                    _challenger.id,
                                    verdict,
                                )
                                return {"status": "accepted"}
                            # spawn refused: a challenge is already in flight. Stay
                            # parked rather than act on the unreconciled verdict.
                            if (task.context or {}).get(
                                "awaiting_review_second_opinion"
                            ):
                                return {"status": "accepted"}
                    else:
                        # Gate on but verdict not high-stakes — proceed
                        # synchronously and record the unchallenged outcome.
                        task.context = {
                            **(task.context or {}),
                            REVIEW_SECOND_OPINION_RECONCILIATION_KEY: {
                                "state": "unchallenged",
                                "reason": "verdict not high-stakes",
                                "primary_verdict": verdict,
                                "reconciled_verdict": verdict,
                                "surviving_issues": issues,
                                "review_task_id": str(task.id),
                            },
                        }
                        session.add(task)
                elif not verdict_unparseable:
                    # Gate disabled — preserve today's synchronous behavior and
                    # record the unchallenged outcome.
                    task.context = {
                        **(task.context or {}),
                        REVIEW_SECOND_OPINION_RECONCILIATION_KEY: {
                            "state": "unchallenged",
                            "reason": "second-opinion gate disabled",
                            "primary_verdict": verdict,
                            "reconciled_verdict": verdict,
                            "surviving_issues": issues,
                            "review_task_id": str(task.id),
                        },
                    }
                    session.add(task)

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
                                    _orig_loop["review_approved"] = True
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
                                    task.status = TaskStatus.COMPLETED
                                    task.latest_status_message = f"Review approved for rollup PR #{pr_number} — handed off to CI gate"
                                    session.add(task)

                                    if _orig_loop.get("ci_passed"):
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
                            try:
                                _approve_files = [
                                    f.get("filename", "")
                                    for f in (
                                        await get_pr_files(
                                            gh_token, owner, repo, pr_number
                                        )
                                    )
                                    if isinstance(f, dict) and f.get("filename")
                                ]
                            except Exception as _af_e:
                                logger.warning(
                                    f"[REVIEW PIPELINE] APPROVE entry: failed to fetch PR files for #{pr_number}: {_af_e}"
                                )
                                _approve_files = []
                            _approved_history = _append_approve_to_review_history(
                                review_loop, _approve_files
                            )
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
                                    "head_sha": head_sha,
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

                                        task.status = TaskStatus.EVALUATING
                                        task.latest_status_message = (
                                            "Merge held — auto-finalized provisional "
                                            "Alembic migration "
                                            f"{_finalization['old_revision']}->"
                                            f"{_finalization['finalized_revision']} "
                                            f"against {base_branch}; awaiting fresh CI "
                                            "on the rewritten PR head"
                                        )[:500]
                                        task.context = {
                                            **(task.context or {}),
                                            "_migration_finalization": _finalization,
                                            "review_loop": {
                                                **_reset_review_loop_pr_head_cache(
                                                    review_loop
                                                ),
                                                "ci_waiting": True,
                                                "review_approved": True,
                                                "ci_passed": False,
                                                "pr_number": pr_number,
                                                "branch": branch,
                                                "base_branch": base_branch,
                                                "original_task_id": original_task_id,
                                            },
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
                                    complete_source_on_fix_spawn=True,
                                )
                                session.add(task)
                                await session.commit()
                                action_taken = True
                                return {"status": "accepted"}

                        merge_result = await _merge_with_retry(  # premerge-guard-exempt: migration PRs are parked above in the dual-head guard branch (returns "accepted"); this merge only runs for staging-only branches that never carry a migration onto the integration trunk
                            merge_pull_request,
                            gh_token,
                            owner,
                            repo,
                            pr_number,
                            project_id=str(task.project_id),
                            merge_method=_pick_merge_method(task),
                        )

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
                                            apply_merged_landed_signal(
                                                orig,
                                                _orig_project,
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

                            try:
                                _approve_files = [
                                    f.get("filename", "")
                                    for f in (
                                        await get_pr_files(
                                            gh_token, owner, repo, pr_number
                                        )
                                    )
                                    if isinstance(f, dict) and f.get("filename")
                                ]
                            except Exception as _af_e:
                                logger.warning(
                                    f"[REVIEW PIPELINE] APPROVE entry: failed to fetch PR files for #{pr_number}: {_af_e}"
                                )
                                _approve_files = []
                            merge_wait_loop = dict(review_loop)
                            merge_wait_loop["review_history"] = (
                                _append_approve_to_review_history(
                                    review_loop, _approve_files
                                )
                            )
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
                        # Best-effort: gh_token may not be in scope if the failure
                        # happened before the auth call.  An empty file list here
                        # still records that the iteration was approved.
                        try:
                            _approve_files = [
                                f.get("filename", "")
                                for f in (
                                    await get_pr_files(gh_token, owner, repo, pr_number)
                                )
                                if isinstance(f, dict) and f.get("filename")
                            ]
                        except Exception:
                            _approve_files = []
                        _approved_history = _append_approve_to_review_history(
                            review_loop, _approve_files
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
                                "head_sha": head_sha,
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
                    # After verdict normalization above this is the only non-APPROVE branch;
                    # any non-standard verdict (e.g., legacy COMMENT) has already been
                    # rewritten to REQUEST_CHANGES and lands here.

                    # Unparseable verdict short-circuit: the fix agent would receive no
                    # structured issues and a placeholder feedback string, consuming a VM
                    # cycle without useful guidance. The original task is already flagged
                    # requires_approval above, so human intervention is expected.
                    if verdict_unparseable:
                        logger.warning(
                            f"[REVIEW PIPELINE] PR #{pr_number}: verdict unparseable — skipping fix spawn, original task flagged requires_approval"
                        )
                        task.status = TaskStatus.COMPLETED
                        task.latest_status_message = f"Review verdict unparseable for PR #{pr_number} — fix skipped, manual review required"
                        session.add(task)
                        await session.commit()
                        return {"status": "accepted"}

                    # NOTE: a verification-only REQUEST_CHANGES on
                    # authoritatively-green CI is handled BEFORE the verdict
                    # branch (search "verification-only REQUEST_CHANGES → treat
                    # as APPROVE" above): it flips the verdict to APPROVE so the
                    # PR merges through the normal approve path. By the time we
                    # reach this REQUEST_CHANGES branch, the verdict is a genuine
                    # change request (concrete defect, or CI not authoritatively
                    # green), so we proceed to spawn a fix as usual.

                    # Empty-PR short-circuit: if the PR has no file changes (because a
                    # prior fix cycle rebased the branch flat so all its commits are in
                    # the base), spawning another fix is futile — there's nothing to
                    # fix. Close the PR and classify the rollup parent's landing
                    # so intermediate staged targets keep waiting downstream.
                    try:
                        (
                            _empty_gh,
                            _empty_owner,
                            _empty_repo,
                        ) = await get_github_token_for_task(task.id, session)
                        _pr_files = await get_pr_files(
                            _empty_gh,
                            _empty_owner,
                            _empty_repo,
                            pr_number,
                            project_id=str(task.project_id),
                        )
                    except Exception as _empty_err:
                        logger.warning(
                            f"[REVIEW PIPELINE] Empty-PR check failed for #{pr_number}: {_empty_err} — proceeding with fix spawn"
                        )
                        _pr_files = None
                    if _pr_files == []:
                        logger.info(
                            f"[REVIEW PIPELINE] PR #{pr_number} has no file changes — closing as empty rollup"
                        )
                        try:
                            await close_pull_request(
                                _empty_gh,
                                _empty_owner,
                                _empty_repo,
                                pr_number,
                                project_id=str(task.project_id),
                            )
                        except Exception as _close_err:
                            logger.warning(
                                f"[REVIEW PIPELINE] Failed to close empty PR #{pr_number}: {_close_err}"
                            )
                        if original_task_id:
                            _orig_broadcast_status = TaskStatus.EVALUATING
                            # Captured for the post-commit direction-outcome
                            # refresh: the empty-PR cleanup lands ORIG as a
                            # merge-equivalent integration landing, but ORIG (not
                            # this review ``task``) carries the direction lineage,
                            # so the terminal recompute below misses it.
                            _orig_to_refresh: Task | None = None
                            try:
                                # Phase A1: skip_locked + bounded retry. If the
                                # parent row is held, defer the empty-PR cleanup;
                                # the next poll cycle will retry. Better than
                                # 500'ing the review handler.
                                _orig = await _lock_original_task(
                                    session,
                                    original_task_id,
                                    source="review_empty_pr_cleanup",
                                )
                                if _orig:
                                    _orig_loop = (_orig.context or {}).get(
                                        "review_loop", {}
                                    ) or {}
                                    _orig_loop.pop("ci_waiting", None)
                                    _orig_project = await session.get(
                                        Project, _orig.project_id
                                    )
                                    (
                                        _record_pr_merged,
                                        _terminal_rollup_merge,
                                        _landed_branch,
                                    ) = await classify_rollup_merge_outcome(
                                        _orig,
                                        _orig_project,
                                        session,
                                        pr_number=pr_number,
                                        merged_base_branch=base_branch,
                                        source="task_lifecycle.review_empty_pr_cleanup",
                                    )
                                    _orig.context = {
                                        **(_orig.context or {}),
                                        "review_loop": _orig_loop,
                                        "pr_closed_empty": True,
                                    }
                                    if _record_pr_merged:
                                        apply_merged_landed_signal(
                                            _orig,
                                            _orig_project,
                                            base_branch=_landed_branch,
                                            hotfix_lane=bool(
                                                (_orig.first_payload or {}).get(
                                                    "hotfix"
                                                )
                                                or (_orig.context or {}).get("hotfix")
                                            ),
                                            non_merge_reason=(
                                                "merged_pr_non_integration_branch"
                                            ),
                                        )
                                    flag_modified(_orig, "context")
                                    _orig.status = (
                                        TaskStatus.COMPLETED
                                        if _terminal_rollup_merge
                                        else TaskStatus.EVALUATING
                                    )
                                    if _terminal_rollup_merge:
                                        _orig.latest_status_message = (
                                            f"✓ Rollup PR #{pr_number} closed — "
                                            f"branch equivalent to {base_branch}, "
                                            "no new changes to merge"
                                        )
                                    else:
                                        _orig.latest_status_message = (
                                            f"✓ Rollup PR #{pr_number} closed — "
                                            f"branch equivalent to {_landed_branch}, "
                                            "no new changes to merge; awaiting "
                                            "downstream landing"
                                        )
                                    _orig_broadcast_status = _orig.status
                                    session.add(_orig)
                                    if _record_pr_merged:
                                        _orig_to_refresh = _orig
                                else:
                                    logger.warning(
                                        f"[REVIEW PIPELINE] Empty-PR cleanup deferred — parent {original_task_id[:8]} contended"
                                    )
                            except Exception as _orig_err:
                                logger.warning(
                                    f"[REVIEW PIPELINE] Failed to update rollup parent after empty-PR close: {_orig_err}"
                                )
                        task.status = TaskStatus.COMPLETED
                        task.latest_status_message = f"Review complete — PR #{pr_number} closed as empty (obsolete rollup)"
                        session.add(task)
                        await session.commit()
                        # Heal ORIG's direction merge counts now that its
                        # merge-equivalent landing is committed (mirrors the
                        # review_merge_success path). No-op unless ORIG carries a
                        # direction/research lineage; best-effort.
                        if original_task_id and _orig_to_refresh is not None:
                            from app.direction_outcomes import (
                                refresh_direction_outcomes_after_merge,
                            )

                            await refresh_direction_outcomes_after_merge(
                                _orig_to_refresh, session
                            )
                        if _orig_to_refresh is not None:
                            try:
                                from app.reaper_sweeps import (
                                    event_driven_unblock_dependents,
                                )

                                await event_driven_unblock_dependents(
                                    _orig_to_refresh, session
                                )
                            except Exception:
                                logger.exception(
                                    "[PIPELINE] event_driven_unblock_dependents "
                                    "failed for task %s",
                                    _orig_to_refresh.id,
                                )
                        if original_task_id:
                            try:  # noqa: SIM105
                                await broadcast_to_project(
                                    str(task.project_id),
                                    "task_updated",
                                    {
                                        "task_id": original_task_id,
                                        "status": _orig_broadcast_status,
                                    },
                                )
                            except Exception:  # noqa: S110
                                pass
                        return {"status": "accepted"}

                    ci_errors_text = ""
                    ci_check: dict | None = None
                    head_sha = ""
                    try:
                        gh_token, owner, repo = await get_github_token_for_task(
                            task.id, session
                        )
                        pr_data = await get_pull_request(
                            gh_token, owner, repo, pr_number
                        )
                        head_sha = pr_data.get("head", {}).get("sha", "")
                        if head_sha:
                            ci_check = await get_check_runs_cached(
                                gh_token, owner, repo, head_sha
                            )
                            if ci_check.get("conclusion") == "failure" and ci_check.get(
                                "failed_logs"
                            ):
                                ci_errors_text = f"\n\nCI build errors (also need fixing):\n{ci_check['failed_logs']}"
                    except Exception as e:
                        logger.error(
                            f"[REVIEW PIPELINE] Could not fetch CI errors for fix context: {e}",
                            exc_info=True,
                        )

                    # Stale-head guard: the review ran against an older PR head than
                    # the current one. Discard the verdict and respawn a fresh review
                    # so the fix isn't guided by findings on code that's already changed.
                    # Backward compat: old verdicts without reviewed_head_sha are treated
                    # as current-head (no action).
                    if reviewed_head_sha and head_sha and reviewed_head_sha != head_sha:
                        _stale_discard_count = int(
                            (task.context or {}).get(
                                "stale_head_consecutive_discards", 0
                            )
                        )
                        if _stale_discard_count < 2:
                            # Discard: mark this review as stale, respawn fresh review.
                            # _review_stale_head: underscore-prefix so it is NOT propagated
                            # to the fresh review task (only exempts THIS discarded review
                            # from the iteration cap). stale_head_consecutive_discards has
                            # no underscore so it IS propagated, letting the fresh review
                            # inherit the running count and eventually accept on the 2nd hit.
                            #
                            # Replay guard: _review_stale_head being set means a prior run
                            # already committed the counter increment and spawned the fresh
                            # review.  Skip both actions to avoid double-increment and
                            # double-spawn.
                            if not (task.context or {}).get("_review_stale_head"):
                                task.context = {
                                    **(task.context or {}),
                                    "stale_head_consecutive_discards": _stale_discard_count
                                    + 1,
                                    "_review_stale_head": True,
                                }
                                flag_modified(task, "context")
                                task.status = TaskStatus.COMPLETED
                                task.latest_status_message = (
                                    f"Stale-head review discarded for PR #{pr_number} "
                                    f"(reviewed {reviewed_head_sha[:8]}, "
                                    f"head is now {head_sha[:8]}) — respawning fresh review"
                                )
                                session.add(task)
                                logger.warning(
                                    f"[REVIEW PIPELINE] PR #{pr_number}: stale-head verdict "
                                    f"discarded (reviewed={reviewed_head_sha[:8]}, "
                                    f"live={head_sha[:8]}) — respawning review "
                                    f"(discard #{_stale_discard_count + 1})"
                                )
                                fresh_review, _ = await _spawn_pipeline_task(
                                    task,
                                    "review",
                                    session,
                                    pr_number,
                                    branch,
                                    base_branch,
                                    original_task_id=original_task_id,
                                )
                                if fresh_review:
                                    logger.info(
                                        f"[REVIEW PIPELINE] Spawned fresh review "
                                        f"{fresh_review.id} for PR #{pr_number} "
                                        f"after stale-head discard"
                                    )
                            await session.commit()
                            return {"status": "accepted"}
                        else:
                            # 2nd consecutive stale-head: accept verdict, annotate it.
                            _stale_note = (
                                f"_(Reviewer judged head {reviewed_head_sha[:8]}; "
                                f"PR head is now {head_sha[:8]}.)_\n\n"
                            )
                            feedback = _stale_note + feedback
                            logger.warning(
                                f"[REVIEW PIPELINE] PR #{pr_number}: 2nd consecutive "
                                f"stale-head discard — accepting verdict with annotation "
                                f"(reviewed={reviewed_head_sha[:8]}, live={head_sha[:8]})"
                            )

                    # CI vs reviewer build-gate disagreement detector. When the
                    # reviewer flags build/lint complaints but CI shows the
                    # corresponding job GREEN, the issue is almost always a
                    # broken reviewer-VM env — pre-warn the fix agent so it
                    # disputes via /tmp/fix_responses rather than thrashing on
                    # source code that isn't broken (PR #3884 saga, 2026-05-06).
                    disagreement_hint = _detect_ci_review_disagreement(
                        issues,
                        feedback,
                        ci_check,
                        pr_number,
                    )
                    if issues:

                        def _fmt_issue(i):
                            desc = i.get("description", "")
                            file_ = i.get("file")
                            line_ = i.get("line")
                            if file_ and line_ is not None:
                                return f"- [{file_}:{line_}] {desc}"
                            if file_:
                                return f"- [{file_}] {desc}"
                            return f"- {desc}"

                        critical = [
                            i for i in issues if i.get("severity") == "critical"
                        ]
                        major = [i for i in issues if i.get("severity") == "major"]
                        minor = [i for i in issues if i.get("severity") == "minor"]
                        structured = ""
                        if critical:
                            structured += (
                                "\n\nCRITICAL issues (must fix):\n"
                                + "\n".join(_fmt_issue(i) for i in critical)
                            )
                        if major:
                            structured += "\n\nMajor issues:\n" + "\n".join(
                                _fmt_issue(i) for i in major
                            )
                        if minor:
                            structured += "\n\nMinor issues:\n" + "\n".join(
                                _fmt_issue(i) for i in minor
                            )
                        fix_prompt_text = f"{disagreement_hint}{REVIEW_FIX_GOAL_PREAMBLE}A code review flagged this PR. Issues to address:{structured}{ci_errors_text}"
                    else:
                        fix_prompt_text = f"{disagreement_hint}{REVIEW_FIX_GOAL_PREAMBLE}A code review flagged this PR. Reviewer feedback:\n\n{feedback}{ci_errors_text}"
                    # FIX-ALL-OCCURRENCES: when the flagged issues look like
                    # instances of ONE file-spanning class (shared description
                    # stem, or an explicit "all instances"/"other call sites"
                    # cue), prepend a directive to grep + fix EVERY occurrence
                    # in this pass. Prevents the whack-a-mole loop where the fix
                    # agent migrates only the cited line and the next review
                    # finds the next sibling site. The directive explicitly
                    # reconciles with the narrow-focus guardrails injected later
                    # in _spawn_pipeline_task: migrating sibling sites of the
                    # flagged class is completing the fix, not refactoring.
                    if detect_fix_all_issue_class(issues):
                        logger.info(
                            f"[REVIEW PIPELINE] PR #{pr_number}: flagged issues "
                            f"form a file-spanning class — injecting "
                            f"fix-all-occurrences directive"
                        )
                        fix_prompt_text = (
                            f"{fix_all_occurrences_directive()}{fix_prompt_text}"
                        )
                    # First-pass minor-only verdict: when every blocking finding
                    # from the initial review is minor-severity, the spawned fix
                    # only addresses nits. Mark the fix so its completion handler
                    # skips the follow-up re-review (CI gates the merge instead),
                    # saving one review iteration + one review-phase LLM call.
                    review_iteration = review_loop.get("iteration", 1)
                    first_attempt_minor_only = (
                        review_iteration == 1
                        and bool(issues)
                        and all(
                            (i.get("severity", "minor") or "minor").lower() == "minor"
                            for i in issues
                            if i.get("description")
                        )
                    )
                    fix_task, _ = await _spawn_pipeline_task(
                        task,
                        "fix",
                        session,
                        pr_number,
                        branch,
                        base_branch,
                        fix_prompt=fix_prompt_text,
                        original_prompt=original_prompt,
                        original_task_id=original_task_id,
                        current_feedback=feedback,
                        current_issues=issues,
                        investigated_files=investigated_files,
                        skip_rereview_minor_only=first_attempt_minor_only,
                    )
                    if fix_task:
                        logger.info(
                            f"[REVIEW PIPELINE] Spawned fix for PR #{pr_number} → task {fix_task.id}"
                        )
                        # Reset the parking parent's CI-wait clock: each spawned fix
                        # is a fresh cycle of work, so the 30-min backstop in
                        # services.py should count from here instead of the initial
                        # rollup-creation timestamp.  Without this, a rollup that
                        # legitimately cycles through 3-4 fix iterations gets killed
                        # mid-pipeline.
                        if original_task_id:
                            try:
                                # Phase A1: skip_locked + bounded retry. If the
                                # parent row is held, skip the ci_wait reset —
                                # it's an optimization (resets the 30-min
                                # backstop clock), not correctness; the parent
                                # will still terminate, just on the original
                                # baseline if reset is missed.
                                _orig = await _lock_original_task(
                                    session,
                                    original_task_id,
                                    source="review_ci_wait_reset",
                                )
                                if _orig:
                                    _orig_loop = dict(
                                        (_orig.context or {}).get("review_loop", {})
                                        or {}
                                    )
                                    if _orig_loop.get("ci_waiting"):
                                        from app import pause_clock

                                        _orig_loop["ci_wait_started"] = (
                                            _utcnow().isoformat()
                                        )
                                        _orig_loop[
                                            "ci_wait_pause_baseline"
                                        ] = await pause_clock.current_baseline(session)
                                        _orig.context = {
                                            **(_orig.context or {}),
                                            "review_loop": _orig_loop,
                                        }
                                        flag_modified(_orig, "context")
                                        session.add(_orig)
                                else:
                                    logger.info(
                                        f"[REVIEW PIPELINE] ci_wait reset skipped for parent {original_task_id[:8]} — row contended"
                                    )
                            except Exception as _reset_err:
                                logger.warning(
                                    f"[REVIEW PIPELINE] Failed to reset ci_wait_started on parent {original_task_id[:8]}: {_reset_err}"
                                )
                    else:
                        task.latest_status_message = f"Fix not spawned for PR #{pr_number} — iteration cap reached"
                        task.requires_approval = True
                        session.add(task)
                        cap_alert_title = (
                            f"PR #{pr_number} stalled — iteration cap reached"
                        )
                        existing_cap_alert = (
                            (
                                await session.execute(
                                    select(Alert).where(
                                        Alert.project_id == task.project_id,
                                        Alert.severity == AlertSeverity.WARNING,
                                        Alert.dismissed_at.is_(None),
                                        Alert.title == cap_alert_title,
                                    )
                                )
                            )
                            .scalars()
                            .first()
                        )
                        if not existing_cap_alert:
                            alert = Alert(
                                project_id=task.project_id,
                                severity=AlertSeverity.WARNING,
                                title=cap_alert_title,
                                message=f"Review requested changes on PR #{pr_number} but the fix iteration cap was reached. Manual intervention required.",
                                task_id=task.id,
                            )
                            session.add(alert)
                            await session.commit()
                            await broadcast_to_project(
                                str(task.project_id),
                                "alert_created",
                                {
                                    "alert_id": str(alert.id),
                                    "severity": "WARNING",
                                    "title": alert.title,
                                },
                            )
                        else:
                            await session.commit()
                        logger.warning(
                            f"[REVIEW PIPELINE] WARNING: Fix spawn returned None for PR #{pr_number}"
                        )
                    # On every review rejection, store which issue categories caused it
                    # on the original task.  Iteration 1 → _first_attempt_rejection_categories
                    # (existing field); all iterations → _all_iterations_rejection_categories
                    # (accumulated superset used by all-iterations catch-rate metric).
                    if issues and original_task_id:
                        try:
                            from app.review_learning import (
                                cache_first_attempt_classification,
                                _keyword_from_flat,
                            )

                            rh_items = [
                                {
                                    "description": i.get("description", ""),
                                    "severity": i.get("severity", "minor"),
                                    "iteration": review_iteration,
                                }
                                for i in issues
                                if i.get("description")
                            ]
                            if rh_items:
                                orig_task = await session.get(
                                    Task, uuid.UUID(original_task_id)
                                )
                                if orig_task:
                                    task_ctx = dict(orig_task.context or {})
                                    if review_iteration == 1:
                                        buckets = (
                                            await cache_first_attempt_classification(
                                                task_ctx, rh_items
                                            )
                                        )
                                    else:
                                        buckets = _keyword_from_flat(rh_items)
                                    rejection_cats = {
                                        cat: len(items)
                                        for cat, items in buckets.items()
                                        if items
                                    }
                                    if rejection_cats:
                                        if review_iteration == 1:
                                            task_ctx[
                                                "_first_attempt_rejection_categories"
                                            ] = rejection_cats
                                            task_ctx["_first_attempt_minor_only"] = (
                                                first_attempt_minor_only
                                            )
                                        existing_all = dict(
                                            task_ctx.get(
                                                "_all_iterations_rejection_categories"
                                            )
                                            or {}
                                        )
                                        for cat, cnt in rejection_cats.items():
                                            existing_all[cat] = (
                                                existing_all.get(cat, 0) + cnt
                                            )
                                        task_ctx[
                                            "_all_iterations_rejection_categories"
                                        ] = existing_all
                                    orig_task.context = task_ctx
                                    flag_modified(orig_task, "context")
                                    session.add(orig_task)
                                    await session.commit()
                        except Exception as _cat_e:
                            logger.warning(
                                f"[REVIEW PIPELINE] Failed to record rejection categories: {_cat_e}"
                            )
                    action_taken = True

                if not action_taken:
                    task.latest_status_message = f"Review completed but no action taken (verdict={verdict}) — check orchestrator logs"
                    session.add(task)
                    await session.commit()
                    logger.warning(
                        f"[REVIEW PIPELINE] WARNING: Review {task.id} for PR #{pr_number} completed with no downstream action (verdict={verdict})"
                    )

            # Fix task completed → security scan → spawn new review
            elif task_type == "fix":
                pr_number = review_loop.get("pr_number") or task_payload.get(
                    "pr_number"
                )
                branch = review_loop.get("branch") or task_payload.get("branch")
                _fix_project = await session.get(Project, task.project_id)
                base_branch = (
                    review_loop.get("base_branch")
                    or task_payload.get("base_branch")
                    or _shared_state.get_project_integration_branch(_fix_project)
                )
                original_task_id = review_loop.get(
                    "original_task_id", ""
                ) or task_payload.get("original_task_id", "")

                # Backend stamp: ensure fix_iteration is set on the self-review
                # outcome so the catch-rate dashboard and reviewer priming can
                # identify which fix iteration produced the SR. The agent stamps
                # it via code-change.ts readSelfReviewOutcome; this is the
                # fallback for agents that ran before that stamp was added.
                _fix_sro = (task.context or {}).get("_self_review_outcome")
                if isinstance(_fix_sro, dict) and "fix_iteration" not in _fix_sro:
                    _pipeline_iter = int(review_loop.get("iteration", 2))
                    # Pipeline sequence: review(1)→fix(2)→review(3)→fix(4)→...
                    # fix_iteration = pipeline_iter // 2
                    _fix_iter_n = max(1, _pipeline_iter // 2)
                    _stamped_ctx = dict(task.context)
                    _stamped_ctx["_self_review_outcome"] = {
                        **_fix_sro,
                        "fix_iteration": _fix_iter_n,
                    }
                    task.context = _stamped_ctx
                    flag_modified(task, "context")
                    session.add(task)

                if not pr_number or not branch:
                    logger.info(
                        f"[REVIEW PIPELINE] Fix task {task.id} missing pr_number ({pr_number}) or branch ({branch}) — cannot continue pipeline"
                    )
                    task.latest_status_message = (
                        "Fix pipeline error: missing pr_number or branch"
                    )
                    session.add(task)
                    await session.commit()
                    return {"status": "accepted"}

                # Early exit: skip only when PR is genuinely merged/done.
                # A non-merged 'closed' state may be a transient self-close
                # from the orchestrator's mergeability-reset loop
                # (close+reopen to force GitHub to recompute mergeable=None).
                # Gating on merged==True only prevents silently dropping the
                # re-review handoff during that brief closed window.
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
                            f"[REVIEW PIPELINE] PR #{pr_number} already merged — skipping fix re-review for task {task.id}"
                        )
                        task.latest_status_message = (
                            f"PR #{pr_number} already merged — re-review skipped"
                        )
                        session.add(task)
                        await session.commit()
                        return {"status": "accepted"}
                    if pr_data_check.get("state") == "closed":
                        # PR is closed but not merged — could be a transient
                        # mergeability-reset. Proceed with re-review so the
                        # handoff is preserved; the review/merge gate will
                        # handle a genuinely abandoned PR gracefully.
                        logger.info(
                            f"[REVIEW PIPELINE] PR #{pr_number} is closed (not merged) for task {task.id} — "
                            f"may be a transient mergeability-reset; proceeding with re-review"
                        )
                except Exception as e:
                    logger.info(
                        f"[REVIEW PIPELINE] Could not check PR #{pr_number} state: {e} — proceeding"
                    )

                # Security scan fix output before re-review
                scan_blocked = False
                try:
                    gh_token, owner, repo = await get_github_token_for_task(
                        task.id, session
                    )
                    scan_result = await scan_pr_for_injection(
                        gh_token, owner, repo, pr_number
                    )
                    logger.info(f"[SECURITY] {scan_result['summary']}")
                    if scan_result["blocked"]:
                        scan_blocked = True
                        task.requires_approval = True
                        task.latest_status_message = scan_result["summary"]
                        session.add(task)
                        await session.commit()
                    if scan_result["findings"]:
                        await create_pr_comment(
                            gh_token, owner, repo, pr_number, scan_result["report"]
                        )
                except Exception as e:
                    logger.error(
                        f"[SECURITY] Scan failed for fix PR #{pr_number}, flagging for approval: {e}",
                        exc_info=True,
                    )
                    task.requires_approval = True
                    task.latest_status_message = _safe_exception_message(
                        e,
                        "Security scan failed — review will proceed but merge requires manual approval",
                    )
                    session.add(task)
                    await session.commit()

                fix_reason = review_loop.get("fix_reason", "code_review")
                # First-pass minor-only fix: the initial review flagged only
                # minor nits, so this fix carries skip_rereview_minor_only (set
                # at spawn time in _spawn_pipeline_task). It skips the re-review
                # the same way ci_failure/merge_conflict fixes do.
                skip_rereview_minor_only = bool(
                    review_loop.get("skip_rereview_minor_only")
                )
                # Skip re-review for fixes whose origin doesn't add review-worthy
                # logic on top of an already-decided merge gate:
                #   - ci_failure  → original review already approved code; CI is
                #                   the only gate. Wait for CI then merge.
                #   - merge_conflict → cluster sub-PR rebase. The original task
                #                   was scope-gated to skip review (small/medium
                #                   intermediate); rebase output is mechanical;
                #                   re-reviewing would be inconsistent with the
                #                   no-conflict happy path. Wait for CI then merge.
                #   - migration_guard → mechanical Alembic renumber/rechain after
                #                   the pre-merge guard blocked an approved PR.
                #                   Wait for CI then merge.
                #   - minor-only first-pass verdict → the fix only addressed
                #                   minor nits; a re-review would near-certainly
                #                   approve, so spend CI as the only gate.
                skip_rereview = (
                    fix_reason in ("ci_failure", "merge_conflict", "migration_guard")
                    or skip_rereview_minor_only
                )
                if not scan_blocked and skip_rereview:
                    from app import pause_clock

                    ci_loop = review_loop.copy()
                    # Per-episode respawn budget: a fix completion starts a fresh
                    # review-wait episode.  Reset the backstop's per-episode count
                    # so a respawn consumed in an earlier episode (e.g. a superseded
                    # review) does not permanently exhaust the budget.
                    ci_loop.pop("review_wait_respawn_count", None)
                    ci_loop["ci_waiting"] = True
                    ci_loop["review_approved"] = True
                    ci_loop["ci_wait_started"] = _utcnow().isoformat()
                    ci_loop[
                        "ci_wait_pause_baseline"
                    ] = await pause_clock.current_baseline(session)
                    ci_loop["ci_poll_interval"] = 10
                    task.status = TaskStatus.EVALUATING
                    task.context = {**(task.context or {}), "review_loop": ci_loop}
                    if fix_reason == "ci_failure":
                        label = "CI-only fix"
                    elif fix_reason == "merge_conflict":
                        label = "merge-conflict fix"
                    elif fix_reason == "migration_guard":
                        label = "migration-guard fix"
                    else:
                        label = "minor-nit fix"
                    skip_detail = (
                        fix_reason
                        if fix_reason
                        in ("ci_failure", "merge_conflict", "migration_guard")
                        else "minor_only"
                    )
                    task.latest_status_message = f"{label} applied for PR #{pr_number} — waiting for CI (review skipped: {skip_detail})"
                    session.add(task)
                    await session.commit()
                    logger.info(
                        f"[REVIEW PIPELINE] {label} for PR #{pr_number} (skip reason={skip_detail}) — skipping re-review, waiting for CI"
                    )
                elif not scan_blocked:
                    review_task, _ = await _spawn_pipeline_task(
                        task,
                        "review",
                        session,
                        pr_number,
                        branch,
                        base_branch,
                        original_task_id=original_task_id,
                    )
                    if review_task:
                        logger.info(
                            f"[REVIEW PIPELINE] Spawned review after fix for PR #{pr_number} → task {review_task.id}"
                        )
                    else:
                        # _spawn_pipeline_task may have rolled back + committed
                        # (DEFERRED lock-contention path). Re-fetch the task for
                        # a fresh un-expired instance.
                        fresh_task = await session.get(Task, task.id)
                        if fresh_task is None:
                            await session.commit()
                            return {"status": "accepted"}
                        task = fresh_task

                        # Read the precise spawn reason written by
                        # _spawn_pipeline_task into the source task's context.
                        _sr = (task.context or {}).get(SPAWN_REASON_KEY, "unknown")
                        _is_transient = _sr == SPAWN_REASON_DEFERRED

                        if _is_transient:
                            # Transient deferral (e.g. in-flight peer holds the
                            # spawn lock): create a specific alert and retry once.
                            logger.warning(
                                "[REVIEW PIPELINE] Review spawn after fix deferred "
                                "for PR #%s (reason=%s) — retrying once",
                                pr_number,
                                _sr,
                            )
                            from app.alert_dispatch import _maybe_create_failure_alert

                            await _maybe_create_failure_alert(
                                task,
                                session,
                                f"Review spawn after fix for PR #{pr_number} "
                                f"deferred ({_sr}) — retrying",
                            )
                            await asyncio.sleep(5)
                            # Re-fetch the task in case the spawn call rolled back
                            # the session and expired the ORM instance.
                            _reloaded = await session.get(Task, task.id)
                            if _reloaded is not None:
                                task = _reloaded
                            review_task, _ = await _spawn_pipeline_task(
                                task,
                                "review",
                                session,
                                pr_number,
                                branch,
                                base_branch,
                                original_task_id=original_task_id,
                            )
                            if review_task:
                                logger.info(
                                    "[REVIEW PIPELINE] Spawned review after fix "
                                    "(retry) for PR #%s → task %s",
                                    pr_number,
                                    review_task.id,
                                )

                        if not review_task:
                            # Terminal failure: cap, stuck cycle, already merged,
                            # or retry also failed. Escalate with accurate reason.
                            task.latest_status_message = (
                                f"Fix applied for PR #{pr_number} but review spawn "
                                f"{'deferred (concurrent spawn)' if _is_transient else 'failed (' + _sr + ')'}"
                            )
                            if not _is_transient:
                                task.requires_approval = True
                            session.add(task)

                            # Always create a WARNING alert with the specific spawn
                            # reason so operators see the failure immediately, not
                            # just a generic "iteration cap" message.
                            _alert_reason_label = (
                                "deferred" if _is_transient else _sr
                            )
                            alert_title = (
                                f"PR #{pr_number} stalled — review spawn {_alert_reason_label}"
                            )
                            existing_alert = (
                                (
                                    await session.execute(
                                        select(Alert).where(
                                            Alert.project_id == task.project_id,
                                            Alert.severity == AlertSeverity.WARNING,
                                            Alert.dismissed_at.is_(None),
                                            Alert.title == alert_title,
                                        )
                                    )
                                )
                                .scalars()
                                .first()
                            )
                            if not existing_alert:
                                _alert_msg = (
                                    f"Fix was applied to PR #{pr_number} but the "
                                    f"review spawn failed (reason: {_sr}). "
                                    + (
                                        "The spawn was deferred due to concurrent "
                                        "activity; the next CI cycle will retry."
                                        if _is_transient
                                        else "Manual intervention may be required."
                                    )
                                )
                                alert = Alert(
                                    project_id=task.project_id,
                                    severity=AlertSeverity.WARNING,
                                    title=alert_title,
                                    message=_alert_msg,
                                    task_id=task.id,
                                )
                                session.add(alert)
                                await session.commit()
                                await broadcast_to_project(
                                    str(task.project_id),
                                    "alert_created",
                                    {
                                        "alert_id": str(alert.id),
                                        "severity": "WARNING",
                                        "title": alert.title,
                                    },
                                )
                            else:
                                await session.commit()

                            logger.warning(
                                "[REVIEW PIPELINE] Review spawn after fix returned None "
                                "for PR #%s (reason: %s)%s",
                                pr_number,
                                _sr,
                                " — transient, will retry" if _is_transient else "",
                            )

                        # For transient deferrals, mark the CI task so the next
                        # backstop cycle detects this as a recent unresolved
                        # spawn and accelerates retry.
                        if _is_transient:
                            from app.time_utils import _utcnow as _ut
                            _now = _ut().isoformat()
                            # Propagate the deferred marker to the original CI
                            # task (not just the fix task) so handle_ci_completion
                            # can read it when entering review-wait.
                            if original_task_id:
                                try:
                                    _ci_task = await session.get(
                                        Task, uuid.UUID(original_task_id)
                                    )
                                    if _ci_task:
                                        _ci_ctx = dict(_ci_task.context or {})
                                        _ci_rl = dict(
                                            _ci_ctx.get("review_loop") or {}
                                        )
                                        _ci_rl["review_spawn_deferred_at"] = _now
                                        _ci_ctx["review_loop"] = _ci_rl
                                        _ci_task.context = _ci_ctx
                                        flag_modified(_ci_task, "context")
                                        session.add(_ci_task)
                                except Exception:
                                    logger.exception(
                                        "[REVIEW PIPELINE] Failed to propagate "
                                        "deferred-spawn marker to CI task %s",
                                        original_task_id,
                                    )
                            # Also set on the fix task's own review_loop as a
                            # fallback (used when the CI task lookup fails).
                            rl = dict(task.context.get("review_loop") or {})
                            rl["review_spawn_deferred_at"] = _now
                            task.context = {
                                **(task.context or {}),
                                "review_loop": rl,
                            }
                            flag_modified(task, "context")
                            session.add(task)
                            await session.commit()

            # Completion handlers — wrapped in try/except to prevent VM leak.
            # If a handler throws after the early commit, BackgroundTasks (including
            # delete_agent_vm) won't run on a 500 response. We catch, log, and ensure
            # VM cleanup via asyncio.create_task as a fallback.
            try:
                # Registry-driven completion dispatch. The task-type registry
                # DECLARES which logical completion handler each type routes to
                # (TaskCapability.completion_handler); this map binds each
                # logical name to its concrete handler function. Adding a type
                # with a completion handler is therefore a single registry edit
                # plus a binding here — no new if/elif branch. This replaces the
                # former hand-maintained if/elif cascade; behaviour is identical
                # (each task type still routes to exactly the same handler with
                # the same log/project arguments).
                _completion_handlers = {
                    "plan": _handle_plan_completion,
                    "design_doc": _handle_design_doc_completion,
                    "strategic": _handle_strategic_completion,
                    "research": _handle_research_completion,
                    "panel_review": _handle_panel_review_completion,
                    "security_audit": _handle_security_audit_completion,
                    "perf": _handle_perf_completion,
                    "compute_experiment": _handle_compute_experiment_completion,
                    "self_audit": _handle_self_audit_completion,
                    "memo": _handle_memo_completion,
                    "web_research": _handle_web_research_completion,
                    "design": _handle_design_completion,
                    "repo_bootstrap": _handle_repo_bootstrap_completion,
                    "capability_proof_trial": _handle_capability_proof_trial_completion,
                }
                _capability = (
                    _peek_capability
                    if task_type == _peek_task_type
                    else get_task_capability(task_type)
                )
                _handler_name = _capability.completion_handler if _capability else None
                if _handler_name:
                    _handler = _completion_handlers[_handler_name]
                    _handler_logs = payload.logs or task.latest_status_message or ""
                    # Round 3 C3 + C4: the strategic completion handlers
                    # (design_doc_update, strategic_review) require the
                    # canonically pre-locked Project. ``locked_project`` was
                    # acquired in Project → Task order at the top of
                    # _process_post_commit via ``lock_project_then_task`` (see
                    # docs/concurrency.md); passing it lets the handler skip its
                    # own re-acquisition. None propagates the legacy
                    # bounded-retry path inside the handler (defensive only).
                    if _handler_name == "strategic":
                        await _handler(
                            task,
                            _handler_logs,
                            session,
                            project=locked_project,
                            message_sequences=preallocated_strategic_message_sequences,
                        )
                    elif _capability.completion_needs_locked_project:
                        await _handler(
                            task, _handler_logs, session, project=locked_project
                        )
                    else:
                        await _handler(task, _handler_logs, session)
            except Exception as handler_exc:
                # NB: this log line must use the plain ``task_id`` argument,
                # NOT ``task.id`` — when the handler poisoned the session AND
                # expired ``task`` (an internal rollback inside the handler),
                # the attribute read issues a refresh against the aborted
                # transaction and re-raises PendingRollbackError OUT of this
                # except block, skipping all recovery below (prod burst
                # 2026-06-07 00:56).
                logger.error(
                    f"[STATUS] Completion handler failed for {task_type} task {task_id}: {handler_exc!r}",
                    exc_info=True,
                )
                # A handler that raised on a DB error (asyncpg QueryCanceledError
                # from statement_timeout, LockNotAvailableError, a closed
                # connection, …) leaves the session in an aborted transaction.
                # Every recovery write below — and the downstream
                # rollup/aggregation/final-commit sections — then fail on the
                # poisoned session ("current transaction is aborted" / "raised as
                # a result of Query-invoked autoflush"), so the requeue/FAIL never
                # persists and a task the agent reported terminal is silently
                # stranded with its work undispatched (research findings, plan
                # children, …). Production 2026-05-29: a research task hit a 120s
                # statement_timeout in _handle_research_completion, the requeue
                # commit then died on autoflush, and the task stuck COMPLETED with
                # 0 children and its findings undispatched. Roll the poisoned
                # transaction back so recovery runs on a clean transaction.
                # rollback() expires every ORM instance, so re-fetch the task by
                # its (stable, argument-supplied) id before reading or mutating
                # it — attribute access on the now-expired instance would issue a
                # synchronous lazy-load and raise MissingGreenlet.
                _recovered_task = await _rollback_and_refetch_task()
                if _recovered_task is None:
                    # Row vanished (archived/deleted) during post-commit — nothing
                    # left to recover.
                    logger.warning(
                        "[HANDLER] Task %s gone after handler failure; skipping recovery",
                        task_id,
                    )
                    return {"status": "accepted"}
                task = _recovered_task
                # Detect transient errors (GitHub API, DB, network) and requeue instead of FAILED
                err_str = str(handler_exc).lower()
                is_transient = any(
                    kw in err_str
                    for kw in [
                        "api.github.com",
                        "502",
                        "503",
                        "connection",
                        "timeout",
                        "rate limit",
                        "ssl",
                        "name resolution",
                        "connection refused",
                        "greenlet",  # post-rollback expired-attr lazy-load; retryable
                    ]
                )
                # Research task interrupted mid-spawn: findings are already committed
                # to task.plan_output and _research_spawn_pending is durable in context.
                # Requeuing the agent would discard those findings and start a new
                # research cycle. Instead raise PostCommitFinalCommitFailed — which
                # preserves the post_commit_pending marker so the sweep retries just
                # the spawn from stored plan_output without re-running the agent.
                #
                # This recovery is NOT gated on the error being keyword-transient.
                # The previous version only preserved the marker for errors whose
                # string matched a transient keyword (timeout/connection/…), so a
                # mid-spawn failure that did NOT match (a lock contention surfaced
                # as a bare LockNotAvailableError, a SerializationFailure, an
                # autoflush IntegrityError, …) fell through to the FAILED branch
                # below and STRANDED its committed findings: the task showed
                # terminal FAILED / "no actionable findings" with 0 children while
                # plan_output held real, undispatched work — the irrecoverable
                # ~28%-of-zero-child class (cause B). Resuming the spawn is always
                # safe regardless of the original error: the resume path
                # (_handle_research_completion, _research_spawn_pending branch)
                # never re-runs the agent — it replays the spawn from the durable
                # plan_output — and the duplicate-spawn guard makes a re-run that
                # actually did land children a no-op. So whenever findings are
                # durable and the spawn was in flight, prefer sweep-retry over a
                # terminal FAILED that buries the work.
                _spawn_pending = bool((task.context or {}).get("_research_spawn_pending"))
                _has_durable_findings = bool(
                    isinstance(task.plan_output, list) and task.plan_output
                )
                if (
                    task_type == "research"
                    and _spawn_pending
                    and _has_durable_findings
                ):
                    logger.warning(
                        "[HANDLER] Research task %s: error mid-spawn with %d durable "
                        "finding(s) in plan_output (transient=%s); preserving "
                        "post_commit_pending for sweep to retry spawn from stored "
                        "findings rather than stranding them on a terminal FAILED "
                        "(handler_exc=%r)",
                        task_id,
                        len(task.plan_output),
                        is_transient,
                        handler_exc,
                    )
                    raise PostCommitFinalCommitFailed(
                        f"research mid-spawn error for {task_id}; "
                        f"sweep retries spawn from {len(task.plan_output)} stored "
                        f"finding(s): {handler_exc!r}"
                    ) from handler_exc
                if is_transient and task.attempt_count < _runtime_config["max_retries"]:
                    reset_task_for_fresh_attempt(task)
                    task.status = TaskStatus.QUEUED
                    # The earlier COMPLETED/FAILED assignment fired the SQLAlchemy set
                    # event which added task.id to _terminated_task_ids, revoking the
                    # JWT. Since the completion handler hit a transient error and we're
                    # requeuing, drop the cache entry so the next VM can claim.
                    from app.routers.agent import _terminated_task_ids

                    _terminated_task_ids.pop(str(task.id), None)
                    task.attempt_count += 1
                    task.vm_instance_id = f"agent-vm-{uuid.uuid4().hex}"
                    # Handler ran after a successful VM provision — reset provision budget
                    # so a transient handler failure doesn't consume retries meant for VM issues.
                    handler_ctx = dict(task.context or {})
                    handler_ctx.pop("provision_failures", None)
                    task.context = handler_ctx
                    task.latest_status_message = _sanitize_log(
                        f"Handler hit transient error, requeuing (attempt {task.attempt_count}): {type(handler_exc).__name__}: {handler_exc!s}"
                    )[:500]
                    logger.warning(
                        f"[HANDLER] Transient error for {task.id}, requeuing: {handler_exc}"
                    )
                else:
                    task.status = TaskStatus.FAILED
                    task.latest_status_message = _safe_exception_message(
                        handler_exc, "Completed but handler failed"
                    )
                    from app.failure_classification import persist_failure_category
                    from app.routers.agent_status import record_task_result

                    _fc_handler = classify_failure(
                        task, at_time=task.last_heartbeat or task.started_at
                    )
                    persist_failure_category(task, _fc_handler)
                    record_task_result(
                        success=False,
                        failure_class=_fc_handler,
                        task_id=str(task.id),
                        task_type=(task.first_payload or {}).get("type", ""),
                    )
                session.add(task)
                # Commit QUEUED immediately — the subsequent helpers each call
                # _rollback_and_refetch_task() on their own failures, which rolls
                # back the whole transaction and discards any uncommitted status
                # mutation, stranding the task as COMPLETED with work undispatched
                # (prod incident 2026-05-29). Commit now so a later rollback cannot
                # undo the requeue; then return early — all remaining helpers are
                # terminal-only (COMPLETED/FAILED/CANCELLED) and are no-ops for a
                # requeued task.
                if task.status == TaskStatus.QUEUED:
                    try:
                        await session.commit()
                    except Exception as _requeue_commit_exc:
                        logger.exception(
                            "[HANDLER] Failed to commit QUEUED state for task %s: %r — "
                            "preserving post_commit_pending for sweep retry",
                            task_id,
                            _requeue_commit_exc,
                        )
                        with contextlib.suppress(Exception):
                            await session.rollback()
                        raise PostCommitFinalCommitFailed(
                            f"requeue commit failed for {task_id}: {_requeue_commit_exc!r}"
                        ) from _requeue_commit_exc
                    return {"status": "received"}

        # 6. Parent task rollup — delegated to shared function
        if task.parent_task_id and payload.status in [
            TaskStatus.COMPLETED,
            TaskStatus.FAILED,
            TaskStatus.CANCELLED,
        ]:
            try:
                await _check_and_trigger_rollup(task, session)
            except Exception as e:
                # NB: ``task_id`` (plain argument), never ``task.id`` — on a
                # poisoned session with an expired instance the attribute read
                # re-raised PendingRollbackError out of this except block
                # (prod burst 2026-06-07 01:05: lock_timeout inside
                # _spawn_pipeline_task).
                logger.error(
                    f"[ROLLUP] Rollup check failed for task {task_id}: {e!r}",
                    exc_info=True,
                )
                # C1: _check_and_trigger_rollup does FOR UPDATE selects +
                # internal commits that can raise on a DB error, leaving this
                # post-commit ``session`` in PendingRollback. Without this
                # rollback every subsequent post-terminal helper (direction
                # outcomes, remediation/regression, strategic backstop) and the
                # final commit silently fail or 500 the agent with
                # PendingRollbackError. Roll back so the chain continues clean,
                # and re-fetch ``task`` — the sections below keep reading
                # task.context / task.first_payload on it.
                task = await _rollback_and_refetch_task()
                if task is None:
                    return {"status": "accepted"}

        # 6b. Update direction research history outcomes for child tasks of a research cycle
        _task_ctx = task.context or {}
        if (
            _task_ctx.get("direction_id")
            and _task_ctx.get("research_task_id")
            and payload.status
            in [TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.CANCELLED]
        ):
            try:
                from app.direction_outcomes import _update_direction_research_outcomes

                await _update_direction_research_outcomes(
                    task.project_id,
                    _task_ctx["direction_id"],
                    _task_ctx["research_task_id"],
                    session,
                )
            except Exception as _outcome_exc:
                logger.warning(
                    f"[RESEARCH] Failed to update direction research outcomes for task {task_id}: {_outcome_exc!r}"
                )
                # C1: _update_direction_research_outcomes takes the directions
                # FOR NO KEY UPDATE lock and commits; an IntegrityError /
                # serialization failure there leaves this session aborted. Roll
                # back so the terminal-record helpers below + the final commit
                # don't inherit the PendingRollback, and re-fetch ``task`` (the
                # rollback expired it; the helpers below read its attributes).
                task = await _rollback_and_refetch_task()
                if task is None:
                    return {"status": "accepted"}

        # C1: the terminal-record helper does its own session writes (it calls
        # _record_remediation_outcome which commits). It is an independent
        # operation on the shared post-commit session: a raise must not leave the
        # session in PendingRollback for the final commit below. Run it behind a
        # rollback-on-failure guard so the chain is poison-free.
        try:
            await _record_remediation_if_terminal(task, payload.status, session)
        except Exception as _helper_exc:
            logger.error(
                f"[LIFECYCLE] Terminal-record helper _record_remediation_if_terminal "
                f"failed for task {task_id}: {_helper_exc!r}",
                exc_info=True,
            )
            task = await _rollback_and_refetch_task()
            if task is None:
                return {"status": "accepted"}
        try:
            from app.task_completion import (
                _expire_migration_reservation_if_terminal,
            )

            await _expire_migration_reservation_if_terminal(
                task, payload.status, session
            )
        except Exception as _mr_exc:
            logger.error(
                f"[LIFECYCLE] Migration-reservation terminal cleanup failed for "
                f"task {task_id}: {_mr_exc!r}",
                exc_info=True,
            )
            task = await _rollback_and_refetch_task()
            if task is None:
                return {"status": "accepted"}

        # Release-qualifier finalization — fires from EVERY terminal path
        # (COMPLETED / FAILED / CANCELLED / BLOCKED) for BOTH a release_audit
        # task and a qualifier-smoketest task. This deliberately lives in the
        # terminal section (not the COMPLETED-only review-pipeline block above):
        # a qualifier child that fails/cancels/blocks must still advance
        # qualification,
        # or the ReleaseVersion stays {status:"pending"} forever and the
        # version is wedged.
        #
        # Why here and not in agent_status.py: this runs after the status
        # endpoint's early commit, on a fresh post-commit session. The task's
        # terminal status AND any SMOKETEST_RESULT / RELEASE_AUDIT_REPORT
        # Artifact the agent posted are therefore already committed and visible
        # to the finalize SELECT (_latest_artifact_logs). The old
        # agent_status.py hook ran pre-flush with autoflush disabled and could
        # not see its own just-added Artifact.
        #
        # maybe_finalize_qualification opens its OWN dedicated DB session
        # (AsyncSessionLocal) for the entire finalize→promote chain — it does
        # NOT use this post-commit ``session``. That decoupling is deliberate:
        # promote() merges to production main and issues an intermediate
        # commit() to release its row lock before 30-120s of GitHub I/O; on a
        # caller's session that commit would prematurely commit this handler's
        # unrelated dirty work (the completed task's terminal status,
        # TaskHistory, Artifacts, remediation/regression records). Because it
        # owns its session, this call also cannot self-deadlock against the
        # Task-row lock this handler holds (it locks the ReleaseVersion row, a
        # different row). It is idempotent (it FOR UPDATE-locks the version and
        # no-ops unless last_qualification.status == "pending" and the required
        # children are terminal) and a cheap O(1) no-op — no session opened, no
        # I/O — for the overwhelming majority of tasks (those with no
        # release_version_id in context). Keyed on context["release_version_id"]
        # and/or task_type == "release_audit" / context["qualifier_smoketest"].
        if payload.status in (
            TaskStatus.COMPLETED,
            TaskStatus.FAILED,
            TaskStatus.CANCELLED,
            TaskStatus.BLOCKED,
        ):
            _qual_ctx = task.context or {}
            _qual_first_payload = task.first_payload or {}
            _is_qualifier_child = bool(
                _qual_ctx.get("release_version_id")
                or _qual_first_payload.get("type") == "release_audit"
                or _qual_ctx.get("qualifier_smoketest")
            )
            if _is_qualifier_child:
                try:
                    from app import release_qualifier

                    # maybe_finalize_qualification opens its OWN dedicated
                    # session (see the comment above) — never this post-commit
                    # ``session``. ``_session_factory`` is threaded through so
                    # the dedicated session is opened from the SAME factory
                    # this handler runs on: in production that is None →
                    # AsyncSessionLocal (a genuine independent connection,
                    # preserving the transaction-decoupling); under the test
                    # harness it is the test-engine factory, so the finalize
                    # sees the committed seed data and its commit is visible to
                    # the test's assertions instead of opening an unrelated
                    # connection on a different event loop.
                    await release_qualifier.maybe_finalize_qualification(
                        task, session_factory=_session_factory
                    )
                except Exception as _qual_exc:
                    logger.error(
                        f"[RELEASE_QUALIFIER] maybe_finalize_qualification failed "
                        f"for terminal task {task_id} (status={payload.status}): "
                        f"{_qual_exc!r}",
                        exc_info=True,
                    )
                    # C1 (defensive): maybe_finalize_qualification owns its own
                    # dedicated session, so it should not poison this shared
                    # ``session`` — but the pre-call accessors (task.context /
                    # task.first_payload) can trigger a lazy load that aborts the
                    # transaction, and a future refactor might touch the shared
                    # session. Roll back to match the sibling terminal-record /
                    # backstop / final-commit guards so the chain below stays
                    # poison-free.
                    task = await _rollback_and_refetch_task()
                    if task is None:
                        return {"status": "accepted"}

        # Audit W2 (D2/D4): backstop for the synchronous strategic chain.
        # If a strategic task (design_doc_update or strategic_review) reached
        # a terminal status but the completion handlers above failed to update
        # the StrategicCycle row, the cycle would stay "phase*_running"
        # forever and the stalled-cycle reaper would have to clean it up
        # later. Catch it here while the task lifecycle still has context.
        try:
            cycle_id = task.strategic_cycle_id or (task.context or {}).get(
                "strategic_cycle_id"
            )
            if cycle_id and payload.status in (
                TaskStatus.COMPLETED,
                TaskStatus.FAILED,
                TaskStatus.CANCELLED,
            ):
                from app import strategic_cycle

                cycle_row = await strategic_cycle.get(session, cycle_id)
                task_type = task.first_payload.get("type", "")
                failed_phase = strategic_phase_for(task_type) or "phase2"
                # Only backstop when the cycle is still stuck in THIS task's OWN
                # phase — i.e. the completion handler genuinely failed to advance
                # it. A successful phase-1 (design_doc_update) chains phase-2 and
                # leaves the cycle in phase2_running; that is success, not a stall,
                # and must NOT be marked failed_phase1. Marking it failed inflated
                # _strategic_failures toward the circuit breaker and polluted the
                # success metric — the source of the false "strategic review
                # complete … but FAILED" reports. (The genuine catch — a task that
                # went terminal while its OWN phase's running outcome never
                # advanced — is preserved.)
                _stuck_outcomes = {
                    "phase1": {
                        strategic_cycle.OUTCOME_IN_PROGRESS,
                        strategic_cycle.OUTCOME_PHASE1_RUNNING,
                    },
                    "phase2": {strategic_cycle.OUTCOME_PHASE2_RUNNING},
                }.get(failed_phase, frozenset())
                if cycle_row is not None and cycle_row.outcome in _stuck_outcomes:
                    logger.warning(
                        f"[STRATEGIC] Task {task.id} ({task_type}) reached {payload.status} "
                        f"but cycle {cycle_id} still in {cycle_row.outcome} — backstop marking failed_{failed_phase}"
                    )
                    await strategic_cycle.mark_failure(
                        session,
                        cycle_id,
                        phase=failed_phase,
                        reason=f"lifecycle_backstop: task {task.id} terminal in {payload.status} without completion-handler cycle update",
                    )
                    await session.commit()
        except Exception as _bk_exc:
            logger.error(
                f"[STRATEGIC] Lifecycle backstop failed for task {task_id}: {_bk_exc!r}",
                exc_info=True,
            )
            # C1: strategic_cycle.mark_failure + the commit inside the backstop
            # can raise (lock contention / DB error), leaving the session
            # aborted. Roll back so the final commit below does not raise
            # PendingRollbackError and return a 500 to the agent, and re-fetch
            # ``task`` (the final broadcast/backstop below read its attributes).
            task = await _rollback_and_refetch_task()
            if task is None:
                return {"status": "accepted"}

        # Final commit for any deferred changes (status, artifacts, spawned tasks).
        # C1: guard the commit. The task's own terminal status was already
        # committed on the early-commit (status-endpoint) session before this
        # post-commit handler ran, so a failure here is NOT fatal to the task.
        # BUT the deferred side effects it would flush (spawned follow-up /
        # remediation tasks, rollup nudges, artifacts) are now DROPPED — they
        # were never persisted. _process_post_commit runs behind the
        # write-ahead ``post_commit_pending`` marker + sweep-retry machinery;
        # both the inline path (_clear_post_commit_pending, agent_status.py
        # ~2946) and the sweep (services.py ~1954) clear that marker on a CLEAN
        # return, so swallowing this failure and returning {status:"received"}
        # would clear the marker and the sweep would NEVER retry the dropped
        # side effects (incident eefbed05: dependent chain orphaned).
        #
        # So roll back the poisoned transaction (cleanly closing the session)
        # and raise PostCommitFinalCommitFailed: a distinct signal the callers
        # treat as "PRESERVE the marker for the sweep" — while still NOT 500ing
        # the agent (the inline/retry callers translate it to a clean 200).
        if session.dirty or session.new:
            try:
                await session.commit()
            except Exception as _commit_exc:
                logger.error(
                    f"[LIFECYCLE] Final post-commit commit failed for task "
                    f"{task_id}: {_commit_exc!r} — preserving post_commit_pending "
                    f"marker for sweep retry (deferred side effects not persisted)",
                    exc_info=True,
                )
                with contextlib.suppress(Exception):
                    await session.rollback()
                raise PostCommitFinalCommitFailed(
                    f"final post-commit commit failed for task {task_id}: "
                    f"{_commit_exc!r}"
                ) from _commit_exc

        # Broadcast AFTER all processing is complete (artifacts saved, pipeline spawned)
        await broadcast_to_project(
            str(task.project_id),
            "task_updated",
            {"task_id": str(task.id), "status": payload.status},
        )

        # Strategist-indicator backstop: a strategic task (design_doc_update /
        # strategic_review) that terminates FAILED/CANCELLED (VM crash, timeout)
        # never reaches the COMPLETED completion handlers that emit
        # strategic_cycle_completed, so the frontend "Strategist working…"
        # indicator would stick on. Pair the spawn-time strategic_cycle_started
        # here. COMPLETED is deliberately excluded — the completion handlers own
        # that emit, and a COMPLETED design_doc_update that chained to Phase 2
        # must keep the cycle active.
        _peek_strategic_type = task.first_payload.get("type", "")
        if _peek_strategic_type in (
            "design_doc_update",
            "strategic_review",
        ) and payload.status in (TaskStatus.FAILED, TaskStatus.CANCELLED):
            from app.strategic_events import emit_strategic_cycle_completed

            await emit_strategic_cycle_completed(
                task.project_id,
                task.id,
                1 if _peek_strategic_type == "design_doc_update" else 2,
                cycle_id=task.strategic_cycle_id
                or (task.context or {}).get("strategic_cycle_id"),
                phase_state="failed",
                completion={
                    "status": getattr(payload.status, "value", str(payload.status)),
                    "status_message": task.latest_status_message,
                },
            )

        return {
            "status": "received",
            "message": f"Status update for task {payload.task_id} recorded.",
        }


# ---------------------------------------------------------------------------
# Orphan-PR cleanup on cancellation
# ---------------------------------------------------------------------------
#
# When a task is cancelled, any PR it opened is left orphaned: the work is
# no longer wanted, but the PR sits there blocking the rollup gate (parent
# stays EVALUATING waiting on an open PR that nobody will merge). Most
# common cause is duplicate-dedup — a task's twin shipped the same finding,
# the twin merged via rollup, the duplicate was cancelled, but its PR
# stayed open. See c56fa118 / PR #5983 incident on 2026-05-26.
#
# We expose `close_orphan_pr_if_any` as an explicit helper that cancel-site
# callers invoke with their already-open session + the task being cancelled.
# An earlier rev tried to wire this into the Task.status terminal-transition
# event listener as a fire-and-forget; that opened a second session on the
# StaticPool SQLite engine the test fixtures share, racing the test's own
# session and breaking ~20 unrelated requeue tests. Explicit calls at known
# cancel sites are noisier but safer.
async def close_orphan_pr_if_any(
    task: Task, session: AsyncSession, *, reason: str = "task_cancelled"
) -> bool:
    """Best-effort: close the open PR associated with `task` if any.

    Returns True if a PR was closed, False otherwise. Skips merged PRs
    (state=closed,merged=true) and already-closed PRs. Failures (no token,
    GitHub 5xx, PR vanished) are logged but never raised — the caller's
    cancel must always complete.

    Caller should invoke this AFTER setting task.status to CANCELLED but
    BEFORE the session.commit() that persists the cancel — the GitHub PATCH
    is independent of the DB transaction either way, and grouping them
    lets the caller bail cleanly if the GitHub call hangs.
    """
    payload_types = {
        str(payload.get("type") or "")
        for payload in (task.payloads or [])
        if isinstance(payload, dict)
    }
    if payload_types & {"review", "panel_review"}:
        # Review tasks reference the PR they inspect; they do not own it.
        # In particular, review_loop.pr_number is shared with create/fix
        # tasks, so treating that reference as ownership lets cancelling a
        # duplicate/stale reviewer close the implementation PR underneath the
        # surviving review/fix lane.
        logger.info(
            "[CANCEL-PR-CLEANUP] Skipping PR close for non-owning review task "
            f"{task.id} (reason={reason})"
        )
        return False

    ctx = task.context or {}

    # Resolve PR number from any recorded location.  The url-keyed context
    # entries (_pr_url / pr_url) are checked first for backward compat; then
    # the per-review-cycle review_loop.pr_number (create/fix tasks) and the
    # rollup_pr_number (plan/research sub-rollup tasks whose PR targets the
    # parent staging branch rather than main).
    pr_number: int | None = None
    pr_url = ctx.get("_pr_url") or ctx.get("pr_url") or ""
    if pr_url:
        m = re.search(r"/pull/(\d+)", pr_url)
        if m:
            pr_number = int(m.group(1))
    if pr_number is None:
        # Also check review_loop.pr_number — set by the review pipeline for
        # sub-rollup and review-loop PRs that store the number but not a full URL
        # in _pr_url / pr_url (the canonical lookup above misses these, causing
        # the cancel path to leak the PR open and block the parent rollup).
        _rl = ctx.get("review_loop") or {}
        if isinstance(_rl, dict) and _rl.get("pr_number") is not None:
            try:
                pr_number = int(_rl["pr_number"])
            except (ValueError, TypeError):
                pass
    if pr_number is None:
        # Also check rollup_pr_number / rollup_pr_url — set for rollup parents
        # in awaiting-approval and security-blocked states (never seeds review_loop).
        _rollup_url = ctx.get("rollup_pr_url") or ""
        if _rollup_url:
            _m = re.search(r"/pull/(\d+)", _rollup_url)
            if _m:
                pr_number = int(_m.group(1))
    if pr_number is None:
        _rpn = ctx.get("rollup_pr_number")
        if _rpn is not None:
            try:
                pr_number = int(_rpn)
            except (ValueError, TypeError):
                pass
    if pr_number is None:
        return False

    from app.github import close_pull_request, get_pull_request
    from app.routers.agent import get_github_token_for_task

    try:
        gh_token, owner, repo = await get_github_token_for_task(task.id, session)
    except Exception:
        logger.exception(
            f"[CANCEL-PR-CLEANUP] get_github_token_for_task failed for "
            f"task {task.id} (reason={reason})"
        )
        return False
    try:
        pr = await get_pull_request(
            gh_token, owner, repo, pr_number, project_id=str(task.project_id)
        )
    except Exception:
        logger.warning(
            f"[CANCEL-PR-CLEANUP] get_pull_request failed for task "
            f"{task.id} PR #{pr_number} (reason={reason}) — likely already "
            f"closed or GitHub transient; skipping"
        )
        return False
    if pr.get("merged"):
        return False
    if pr.get("state") != "open":
        return False
    try:
        ok = await close_pull_request(
            gh_token, owner, repo, pr_number, project_id=str(task.project_id)
        )
    except Exception:
        logger.exception(
            f"[CANCEL-PR-CLEANUP] close_pull_request raised for task "
            f"{task.id} PR #{pr_number} (reason={reason})"
        )
        return False
    if ok:
        logger.info(
            f"[CANCEL-PR-CLEANUP] Closed orphan PR #{pr_number} for "
            f"cancelled task {task.id} (reason={reason})"
        )
    return ok


# ---------------------------------------------------------------------------
# Task park-and-wake primitive (alembic 0199 / TaskWakeRegistration).
#
# A task that is waiting on an external, non-agent signal parks via
# :func:`park_task` — it releases its VM/JWT/fleet slot immediately and writes a
# durable :class:`TaskWakeRegistration`. The external source later fires via
# :func:`fire_task_wakes`, and the leader-owned wake-delivery worker
# (``reaper_sweeps.sweep_deliver_task_wakes``) claims each FIRED registration
# ``FOR UPDATE SKIP LOCKED`` and calls :func:`deliver_wake`, which advances the
# PARKED task **exactly once**. Registrations that never fire are aged out by
# :func:`expire_wake` at their deadline rather than stranding the task.
#
# These are the centralized, row-lock-aware helpers; the sweep is the only
# scheduled caller of deliver/expire, but tests drive them directly.
# ---------------------------------------------------------------------------

# Statuses a live task may transition into PARKED from. Terminal states and
# BLOCKED are excluded — a parked task is *running* work that chose to wait.
_PARKABLE_STATUSES = TASK_PARKABLE_STATUSES

# Default age after which a PARKED task with no live (PENDING/FIRED) wake
# registration is treated as orphaned and failed rather than stranded forever.
_PARK_STRAND_SECONDS = 6 * 60 * 60


async def _maybe_fire_terminal_compute_wake(
    session,
    registration: TaskWakeRegistration,
    *,
    now,
) -> None:
    """Close the terminal-before-park lost-wakeup race for compute-job wakes.

    Register-then-recheck: after the PENDING ``compute_job_terminal`` registration
    exists, acquire the SAME compute-job row lock the monitor takes on its
    terminal-fire path (``compute_jobs.process_compute_jobs_once`` →
    ``_lock_compute_job``) and check whether the job is ALREADY terminal. If it is,
    fire the just-created registration immediately (PENDING → FIRED) so the leader
    sweep finalizes it identically to a normally-fired wake.

    Why this is race-free in BOTH orderings:

    * terminal-before-park — the monitor finalized + fired under the row lock while
      no registration existed (it fired nothing and will never revisit the terminal
      job). This recheck runs AFTER the lock is free, sees the terminal job, and
      fires the registration the monitor missed.
    * park-before-terminal — the recheck sees a non-terminal job and leaves the
      registration PENDING; the monitor later finalizes and fires it. If the two
      race, the row lock serializes them: whichever writer holds the lock completes
      before the other observes the job, so exactly one of them fires the row.

    Idempotent: only a PENDING registration is fired (PENDING → FIRED); an
    already-FIRED/DELIVERED registration is left untouched, so a delayed or retried
    park can never double-fire.
    """
    from app.compute_jobs import (
        COMPUTE_JOB_TERMINAL_WAKE_KIND,
        _COMPUTE_JOB_TERMINAL_STATUSES,
        _lock_compute_job,
    )

    if registration.wake_source_kind != COMPUTE_JOB_TERMINAL_WAKE_KIND:
        return
    if registration.delivery_state != WakeDeliveryState.PENDING:
        return
    if not registration.wake_source_ref:
        return
    try:
        job_id = uuid.UUID(str(registration.wake_source_ref))
    except (ValueError, TypeError):
        return

    # SELECT ... FOR UPDATE on the compute job row — the shared serialization
    # point with the monitor's terminal-fire path. autoflush persists the new
    # PENDING registration before this read, keeping it consistent in-session.
    job = await _lock_compute_job(session, job_id)
    if job is None:
        return
    if job.status in _COMPUTE_JOB_TERMINAL_STATUSES:
        registration.delivery_state = WakeDeliveryState.FIRED
        registration.fired_at = now
        session.add(registration)
        logger.info(
            "[PARK] compute job %s already terminal at park time; fired "
            "registration %s (terminal-before-park catch-up)",
            job_id,
            registration.id,
        )


async def park_task(
    session,
    task: Task,
    *,
    wake_source_kind: str,
    wake_source_ref: str | None = None,
    source_metadata: dict | None = None,
    wake_action=WakeAction.RESUME,
    deadline_at=None,
    reason: str | None = None,
    now=None,
) -> TaskWakeRegistration:
    """Transition ``task`` into PARKED and persist a durable wake registration.

    Mirrors :func:`app.task_blocked.enter_blocked_state`: the caller owns the
    task row lock (``SELECT ... FOR UPDATE``), the ``session.commit()``, and any
    post-commit VM teardown (``_ensure_vm_deleted``). This helper does the
    in-critical-section work that must be atomic with the status flip: releasing
    the account/fleet slot exactly once, stamping operator-visible park metadata,
    and adding the ``TaskWakeRegistration`` row. Returns the registration.

    The fleet slot is released here (not deferred) so a parked task stops pinning
    capacity the moment it parks — that is the whole point of the primitive.

    Idempotent on retry: if the task is already PARKED and a live (PENDING or
    FIRED) wake registration matching (task_id, kind, ref) already exists, that
    registration is returned immediately without creating a duplicate.  This
    covers the normal failure mode where the agent's first park POST response was
    lost and it retries — the second call must not create a second registration.
    """
    from app.time_utils import _utcnow

    now = now or _utcnow()

    # Guard: only parkable statuses may transition into PARKED.
    # If already PARKED, check for an existing live registration before
    # creating another — idempotent retry support.
    if task.status == TaskStatus.PARKED:
        existing_reg = (
            (
                await session.execute(
                    select(TaskWakeRegistration).where(
                        TaskWakeRegistration.task_id == task.id,
                        TaskWakeRegistration.wake_source_kind
                        == str(wake_source_kind)[:64],
                        TaskWakeRegistration.wake_source_ref
                        == (str(wake_source_ref)[:255] if wake_source_ref else None),
                        TaskWakeRegistration.delivery_state.in_(
                            [WakeDeliveryState.PENDING, WakeDeliveryState.FIRED]
                        ),
                    )
                )
            )
            .scalars()
            .first()
        )
        if existing_reg is not None:
            # Delayed/retried park: re-run the terminal recheck so a job that went
            # terminal AFTER the original park (but whose monitor fire found no
            # registration, or whose registration is still PENDING) is caught here
            # too. Idempotent — only fires a PENDING registration.
            await _maybe_fire_terminal_compute_wake(session, existing_reg, now=now)
            return existing_reg
        # No live registration found (it was lost); fall through to create one.
    elif task.status not in _PARKABLE_STATUSES:
        raise ValueError(
            f"task {task.id} cannot be parked from status {task.status!r} "
            f"(parkable: {', '.join(s.value for s in _PARKABLE_STATUSES)})"
        )

    # Release the account/fleet slot exactly once (idempotent via the
    # ``_slot_released`` marker, mirroring the BLOCKED path in agent_status).
    try:
        from app.max_plan_pool import (
            get_task_llm_account_id,
            release_max_plan_account,
        )

        acct_id = get_task_llm_account_id(task)
        if acct_id and not (task.context or {}).get("_slot_released"):
            await release_max_plan_account(
                acct_id,
                project_id=str(task.project_id) if task.project_id else None,
            )
            task.context = {**(task.context or {}), "_slot_released": True}
    except Exception:
        logger.exception("[PARK] slot release failed for task %s", task.id)

    task.status = TaskStatus.PARKED
    task.parked_at = now
    task.park_reason = (reason or f"waiting on {wake_source_kind}")[:2000]
    task.vm_stopped_at = now

    # Retire any LEASED pool member before clearing vm_instance_id. Once the
    # linkage is gone, the terminal-completion path (_release_pool_member_if_terminal)
    # can't find the member and it stays LEASED forever, silently exhausting pool
    # capacity. Best-effort: a pool failure must never block the park itself.
    _park_pool_vm_id = task.vm_instance_id
    if _park_pool_vm_id:
        try:
            from app.models import PoolVmState
            from app.warm_pool import (
                is_warm_pool_enabled,
                mark_pool_vm_retiring,
            )

            if is_warm_pool_enabled():
                await mark_pool_vm_retiring(
                    session, _park_pool_vm_id, source_states=(PoolVmState.LEASED,)
                )
        except Exception:
            logger.exception(
                "[PARK] pool-member retire failed for task %s vm=%s",
                task.id,
                _park_pool_vm_id,
            )

    task.vm_instance_id = None

    reg = TaskWakeRegistration(
        task_id=task.id,
        project_id=task.project_id,
        wake_source_kind=str(wake_source_kind)[:64],
        wake_source_ref=(str(wake_source_ref)[:255] if wake_source_ref else None),
        source_metadata=dict(source_metadata or {}),
        wake_action=WakeAction(wake_action),
        delivery_state=WakeDeliveryState.PENDING,
        requested_at=now,
        deadline_at=deadline_at,
    )
    session.add(task)
    session.add(reg)

    # Register-then-recheck: now that the PENDING registration exists, close the
    # terminal-before-park lost-wakeup race for the compute-job wake source. No-op
    # for every other wake kind.
    await _maybe_fire_terminal_compute_wake(session, reg, now=now)
    return reg


async def fire_task_wakes(
    session,
    *,
    wake_source_kind: str,
    wake_source_ref: str | None,
    fired_at=None,
    metadata_update: dict | None = None,
) -> int:
    """Mark every PENDING registration for ``(kind, ref)`` as FIRED. Idempotent.

    Called by the external consumer when its source fires (e.g. a ComputeJob
    reaching a terminal state). Rows are locked ``FOR UPDATE SKIP LOCKED`` so a
    concurrent firing cannot double-fire the same registration. The leader-owned
    worker delivers the FIRED rows on its next pass. Returns the count newly
    fired. The caller commits.
    """
    from app.time_utils import _utcnow

    fired_at = fired_at or _utcnow()
    stmt = (
        select(TaskWakeRegistration)
        .where(
            TaskWakeRegistration.wake_source_kind == str(wake_source_kind)[:64],
            TaskWakeRegistration.wake_source_ref == wake_source_ref,
            TaskWakeRegistration.delivery_state == WakeDeliveryState.PENDING,
        )
        .with_for_update(skip_locked=True)
    )
    rows = (await session.execute(stmt)).scalars().all()
    n = 0
    for reg in rows:
        reg.delivery_state = WakeDeliveryState.FIRED
        reg.fired_at = fired_at
        if metadata_update:
            reg.source_metadata = {**(reg.source_metadata or {}), **metadata_update}
        session.add(reg)
        n += 1
    return n


async def deliver_wake(
    session, registration: TaskWakeRegistration, *, now=None
) -> bool:
    """Advance the PARKED task behind a FIRED registration **exactly once**.

    Exactly-once is enforced by the FIRED→DELIVERED flip happening atomically
    with the task advance, under the registration row lock the worker already
    holds (``FOR UPDATE SKIP LOCKED``). Any second call — a retry, or a racing
    worker — sees ``delivery_state != FIRED`` (or the task no longer PARKED) and
    returns ``False`` without touching the task. Returns ``True`` iff this call
    advanced the task.

    The caller (the sweep, or a test) owns the ``session.commit()`` for EVERY
    branch — RESUME, generic FINALIZE, and the compute-job FINALIZE branch. None
    of them commit internally, so the FIRED→DELIVERED flip and the task advance
    commit together atomically under the caller's single commit. The compute
    branch finalizes through ``_handle_compute_experiment_completion`` with
    ``owns_commit=False`` (pure mutation, no internal commit); its post-commit
    side effects (operator broadcast + durable knowledge persistence) are run by
    the sweep AFTER it commits, via
    ``task_completion.run_compute_wake_post_commit``.
    """
    from app.time_utils import _utcnow

    now = now or _utcnow()
    registration.attempt_count = (registration.attempt_count or 0) + 1

    # Idempotency guard: only a FIRED registration is deliverable.
    if registration.delivery_state != WakeDeliveryState.FIRED:
        session.add(registration)
        return False

    task = (
        (
            await session.execute(
                select(Task).where(Task.id == registration.task_id).with_for_update()
            )
        )
        .scalars()
        .first()
    )
    if task is None or task.status != TaskStatus.PARKED:
        # The task moved on (already woken by a sibling registration, cancelled,
        # etc.). Record a truthful no-op outcome and do NOT advance anything.
        registration.delivery_state = WakeDeliveryState.FAILED
        registration.result_state = "task_not_parked"
        registration.result_detail = (
            f"task {registration.task_id} was "
            f"{getattr(task, 'status', None)} not PARKED at delivery"
        )[:2000]
        registration.delivered_at = now
        registration.claimed_at = registration.claimed_at or now
        session.add(registration)
        return False

    action = WakeAction(registration.wake_action)

    # Compute-job-terminal wake source: finalize the parked compute_experiment
    # task through its REAL completion path so it lands with the honest terminal
    # outcome, final cost, and result reference rather than a bare status flip.
    # The FIRED→DELIVERED flip is recorded BEFORE the handler runs so it commits
    # atomically with the task finalization the handler performs — preserving
    # exactly-once delivery (a re-fired or re-claimed registration is no longer
    # FIRED and returns False above). Critically the handler runs with
    # ``owns_commit=False`` (pure mutation, no internal commit), so the caller's
    # single commit covers BOTH the FIRED→DELIVERED flip and the task advance and
    # a mid-batch commit can never release sibling delivery locks early.
    from app.compute_jobs import COMPUTE_JOB_TERMINAL_WAKE_KIND

    if (
        action == WakeAction.FINALIZE
        and registration.wake_source_kind == COMPUTE_JOB_TERMINAL_WAKE_KIND
    ):
        from app.compute_jobs import build_compute_result_logs_async
        from app.models import ComputeJob
        from app.task_completion import (
            _COMPUTE_WAKE_POST_COMMIT_META_KEY,
            _handle_compute_experiment_completion,
        )

        job = None
        if registration.wake_source_ref:
            with contextlib.suppress(ValueError, TypeError):
                job = await session.get(
                    ComputeJob, uuid.UUID(registration.wake_source_ref)
                )
        registration.delivery_state = WakeDeliveryState.DELIVERED
        registration.delivered_at = now
        registration.claimed_at = registration.claimed_at or now
        session.add(registration)
        # Clear the park markers before the completion handler mutates the task so
        # the finalized task carries no stale parked_at/park_reason (the completion
        # path only manages status, not the park bookkeeping).
        task.parked_at = None
        task.park_reason = None
        session.add(task)
        if job is None:
            # The ComputeJob row is missing (dangling ref, deleted row, or
            # unparseable wake_source_ref). Stamp a distinct, truthful failure
            # classification so operators and auto-remediation can distinguish
            # this from a legitimate metric-gate miss.
            registration.result_state = "job_record_missing"
            task.status = TaskStatus.FAILED
            task.latest_status_message = (
                "Compute wake finalized: job record missing at delivery"
            )[:500]
            ctx = dict(task.context or {})
            ctx["failure_category"] = "compute_job_record_missing"
            ctx["failure_class"] = "compute_job_record_missing"
            task.context = ctx
            flag_modified(task, "context")
            with contextlib.suppress(Exception):
                from app.jwt_revocation import emit_task_terminated_notify

                emit_task_terminated_notify(session, str(task.id))
            return True
        registration.result_state = "finalized"
        logs = await build_compute_result_logs_async(job)
        # _handle_compute_experiment_completion gates the artifact and sets the
        # task to its honest terminal status (COMPLETED on pass, FAILED on a missed
        # gate) and stamps the job result reference. With owns_commit=False it does
        # NOT commit, broadcast, or persist knowledge — the caller (sweep) commits,
        # then runs run_compute_wake_post_commit for the broadcast + knowledge.
        await _handle_compute_experiment_completion(
            task, logs, session, owns_commit=False
        )
        # Seed the recoverability marker atomically with the FIRED→DELIVERED commit
        # so a crash ANYWHERE after this point still leaves a marker for the retry
        # pass in sweep_deliver_task_wakes to pick up and re-attempt the post-commit
        # side effects (operator broadcast + durable KnowledgeDocument).
        _meta = dict(registration.source_metadata or {})
        _meta[_COMPUTE_WAKE_POST_COMMIT_META_KEY] = {}
        registration.source_metadata = _meta
        flag_modified(registration, "source_metadata")
        session.add(registration)
        return True

    if action == WakeAction.FINALIZE:
        terminal = TaskStatus.COMPLETED
        raw = (registration.source_metadata or {}).get("finalize_status")
        if raw:
            with contextlib.suppress(Exception):
                candidate = TaskStatus(raw)
                if candidate in (
                    TaskStatus.COMPLETED,
                    TaskStatus.FAILED,
                    TaskStatus.CANCELLED,
                ):
                    terminal = candidate
        task.status = terminal
        task.parked_at = None
        task.park_reason = None
        task.latest_status_message = (
            f"Woken by {registration.wake_source_kind}: finalized {terminal.value}"
        )[:500]
        with contextlib.suppress(Exception):
            from app.jwt_revocation import emit_task_terminated_notify

            emit_task_terminated_notify(session, str(task.id))
        registration.result_state = "finalized"
    else:  # RESUME — return the task to the dispatch queue on a fresh VM.
        reset_task_for_fresh_attempt(task)
        task.status = TaskStatus.QUEUED
        task.latest_status_message = (
            f"Woken by {registration.wake_source_kind}: re-queued for dispatch"
        )[:500]
        registration.result_state = "resumed"

    registration.delivery_state = WakeDeliveryState.DELIVERED
    registration.delivered_at = now
    registration.claimed_at = registration.claimed_at or now
    session.add(task)
    session.add(registration)
    return True


async def expire_wake(session, registration: TaskWakeRegistration, *, now=None) -> bool:
    """Age out a PENDING registration whose deadline passed before it fired.

    The PARKED task is failed with a truthful ``park_wake_timeout`` classification
    rather than left to strand. Idempotent: a non-PENDING registration is a no-op.
    Returns ``True`` iff this call expired the registration.
    """
    from app.time_utils import _utcnow

    now = now or _utcnow()
    if registration.delivery_state != WakeDeliveryState.PENDING:
        return False

    task = (
        (
            await session.execute(
                select(Task).where(Task.id == registration.task_id).with_for_update()
            )
        )
        .scalars()
        .first()
    )
    registration.delivery_state = WakeDeliveryState.EXPIRED
    registration.result_state = "expired"
    registration.delivered_at = now
    if task is not None and task.status == TaskStatus.PARKED:
        from app.compute_jobs import COMPUTE_JOB_TERMINAL_WAKE_KIND

        failure_detail = None
        diagnostics = None
        if registration.wake_source_kind == COMPUTE_JOB_TERMINAL_WAKE_KIND:
            from app.models import ComputeJob

            job = None
            if registration.wake_source_ref:
                with contextlib.suppress(ValueError, TypeError):
                    job = await session.get(
                        ComputeJob, uuid.UUID(registration.wake_source_ref)
                    )
            deadline_at = registration.deadline_at
            job_completed_at = job.completed_at if job is not None else None
            job_started_at = job.started_at if job is not None else None
            job_created_at = job.created_at if job is not None else None
            job_status = getattr(job.status, "value", job.status) if job else None
            job_terminal_by_deadline = bool(
                job is not None
                and job_completed_at is not None
                and deadline_at is not None
                and job_completed_at <= deadline_at
            )
            # Provisioning vs execution split: how much of the deadline was spent
            # waiting for the job to start (provisioning) vs actually running.
            # Clamped to 0 to guard against clock-skew or backdated test fixtures.
            provisioning_seconds: float | None = None
            execution_seconds: float | None = None
            if job_created_at is not None and deadline_at is not None:
                if job_started_at is not None:
                    provisioning_seconds = max(
                        0.0,
                        round((job_started_at - job_created_at).total_seconds(), 1),
                    )
                    execution_seconds = max(
                        0.0,
                        round((deadline_at - job_started_at).total_seconds(), 1),
                    )
                else:
                    # Job never reached running state — all budget went to provisioning.
                    provisioning_seconds = max(
                        0.0,
                        round((deadline_at - job_created_at).total_seconds(), 1),
                    )
            diagnostics = {
                "wake_source_kind": registration.wake_source_kind,
                "wake_source_ref": registration.wake_source_ref,
                "requested_at": registration.requested_at.isoformat(),
                "claimed_at": (
                    registration.claimed_at.isoformat()
                    if registration.claimed_at
                    else None
                ),
                "fired_at": (
                    registration.fired_at.isoformat() if registration.fired_at else None
                ),
                "deadline_at": deadline_at.isoformat() if deadline_at else None,
                "job_status": job_status,
                "job_created_at": job_created_at.isoformat()
                if job_created_at
                else None,
                "job_started_at": job_started_at.isoformat()
                if job_started_at
                else None,
                "job_completed_at": (
                    job_completed_at.isoformat() if job_completed_at else None
                ),
                "job_terminal_by_deadline": job_terminal_by_deadline,
                "provisioning_seconds": provisioning_seconds,
                "execution_seconds": execution_seconds,
            }
            if job_terminal_by_deadline:
                failure_detail = "compute_job_terminal_wake_lost_after_job_completed"
                task.latest_status_message = (
                    "Park wake on compute_job_terminal expired after the job "
                    "completed, but the wake never fired before the deadline "
                    f"(job_completed_at={diagnostics['job_completed_at']}, "
                    f"deadline_at={diagnostics['deadline_at']}, "
                    f"requested_at={diagnostics['requested_at']}, "
                    f"fired_at={diagnostics['fired_at']})"
                )[:500]
            else:
                failure_detail = "compute_job_terminal_job_not_terminal_by_deadline"
                task.latest_status_message = (
                    "Park wake on compute_job_terminal expired before the job "
                    "reached terminal state "
                    f"(job_status={diagnostics['job_status']}, "
                    f"job_completed_at={diagnostics['job_completed_at']}, "
                    f"deadline_at={diagnostics['deadline_at']}, "
                    f"requested_at={diagnostics['requested_at']}, "
                    f"fired_at={diagnostics['fired_at']})"
                )[:500]
        else:
            task.latest_status_message = (
                f"Park wake on {registration.wake_source_kind} timed out at deadline"
            )[:500]
        task.status = TaskStatus.FAILED
        task.parked_at = None
        # Clear park_reason too so an expired task carries no stale "Parked …"
        # marker on operator surfaces — the task is now FAILED, not parked.
        task.park_reason = None
        ctx = dict(task.context or {})
        ctx["failure_category"] = "park_wake_timeout"
        ctx["failure_class"] = "park_wake_timeout"
        if failure_detail is not None:
            ctx["failure_detail"] = failure_detail
            ctx["park_wake_timeout_diagnostics"] = diagnostics
        task.context = ctx
        registration.result_detail = task.latest_status_message
        with contextlib.suppress(Exception):
            from app.jwt_revocation import emit_task_terminated_notify

            emit_task_terminated_notify(session, str(task.id))
        session.add(task)
    session.add(registration)
    return True
