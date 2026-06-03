"""Rollup PR management and branch cleanup.

Extracted from task_lifecycle.py to reduce its size.  Contains the three
rollup-related helpers: _verify_pr_merged, _cleanup_branches, and
_check_and_trigger_rollup.

All deferred imports from ``app.main`` are intentional — they avoid circular
imports (rollup_management is imported BY main indirectly via task_lifecycle)
and preserve test-patchability.
"""

# fmt: off
import logging
import time
from datetime import timedelta

from fastapi import HTTPException
from sqlalchemy import func as _func
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from app.database import AsyncSessionLocal
from app.rollup_locking import (  # noqa: F401 — re-exported for callers/tests
    _ParentMutatedDuringPRCreation,
    _capture_rollup_parent_snapshot,
    _release_parent_lock,
    _relock_parent,
)
from app.models import (
    Task,
    Artifact,
    Project,
    TaskStatus,
    ArtifactType,
)
from app.review_pipeline import _spawn_pipeline_task, _is_pipeline_task_stale
from app.rollup_branch_cleanup import (  # noqa: F401 — re-exported for callers/tests
    _branch_task_id_prefix,
    _cleanup_branches,
    _partition_orphan_prs,
)
from app.rollup_migration_validation import (  # noqa: F401 - re-exported for callers/tests
    _extract_rollup_migration_sources,
    _plan_rollup_migration_reconciliation,
    _touches_alembic_versions,
    _validate_rollup_migration_chain,
    pr_touches_migrations,
)
from app.rollup_pr_verification import (  # noqa: F401 — re-exported for callers/tests
    MAX_REVIEW_RESPAWNS,
    _PR_MERGE_NEGATIVE_TTL_S,
    _pr_merge_negative_cache,
    _record_rollup_failure_signals,
    _review_respawn_count,
    _verify_pr_merged,
)
from app.rollup_state import (  # noqa: F401 — re-exported for legacy callers/tests
    _ROLLUP_TRANSIENT_CTX_KEYS,
    _clear_rollup_transient_state,
)
from app.branch_resolution import (
    InvalidProjectBranchConfiguration,
    _signal_blocking_project_branch_configuration,
    is_custom_staged_rollup_root,
    merged_pr_counts_as_integration_landing,
)
from app.landed_signal import apply_merged_landed_signal, mark_unblocked_non_merge
from app.shared_state import (
    runtime_config as _runtime_config,
    get_project_integration_branch as _get_project_integration_branch,
    get_project_promotion_target as _get_project_promotion_target,
    get_project_pr_title_prefix as _get_project_pr_title_prefix,
    get_project_task_branch_namespace as _get_project_task_branch_namespace,
)
from app.task_locking import with_directions_locked

logger = logging.getLogger(__name__)

# Duplicate-create guard: when a rollup parent is about to POST its rollup PR it
# stakes a ``rollup_pr_creating_at`` claim (epoch seconds) in parent.context
# UNDER the parent lock, BEFORE releasing it around the slow create_pull_request
# call. A concurrent _check_and_trigger_rollup invocation (sibling children
# completing together, reaper + completion event both firing) that observes a
# FRESH claim and no ``rollup_pr_number`` marker defers instead of POSTing a
# second PR — GitHub enforces one-open-PR-per-(head, base) only best-effort, so
# two near-simultaneous POSTs can both return 201 (incident #7350/#7351). A
# claim older than this TTL is treated as stale (peer crashed mid-create) and
# ignored so creation is never wedged. Paired with the idempotent pre-check in
# github_pulls.create_pull_request, which catches any duplicate that still slips
# through GitHub's non-atomic enforcement.
_ROLLUP_PR_CREATE_CLAIM_TTL_S = 300

# Bounded auto-reconcile attempts per rollup PR for a pure mechanical Alembic
# revision collision. Two attempts is enough to absorb a transient
# push/re-validate race; beyond that we fall back to the operator-alert / agent
# fix path rather than loop on a non-mechanical block.
_ALEMBIC_AUTORECONCILE_CAP = 2


async def _attempt_alembic_autoreconcile(
    parent: Task,
    session: AsyncSession,
    gh_token: str,
    owner: str,
    repo: str,
    rollup_pr_number: int,
    rollup_pr_url: str,
    staging_branch: str,
    target_branch: str,
    parent_ctx: dict,
    current_count: int,
) -> bool:
    """Try to auto-renumber a rollup migration that collides with the target head.

    Returns ``True`` when a reconciliation commit was pushed to *staging_branch*
    (the caller defers one sweep so re-validation can pass and the rollup can
    merge with no operator step). Returns ``False`` — leaving the parent
    untouched — for every non-mechanical case, missing literal, or push failure,
    so the caller falls through to the existing operator-alert / agent-fix path.
    Conservative and fail-safe: only the unambiguous single-added-migration
    collision / stale-anchor is auto-remediated.
    """
    from app.migration_chain import rewrite_expected_schema_revision
    from app.github import get_file_text_at_ref, commit_file_changes

    try:
        plan = await _plan_rollup_migration_reconciliation(
            gh_token, owner, repo, rollup_pr_number, target_branch
        )
    except Exception as e:  # noqa: BLE001 — fail-safe: fall back to operator path
        logger.warning(
            f"[ROLLUP] PR #{rollup_pr_number} alembic auto-reconcile planning "
            f"failed ({type(e).__name__}: {e}); falling back to fix path"
        )
        return False
    if not plan:
        return False

    # The migration becomes the new head, so EXPECTED_SCHEMA_REVISION must bump
    # to match. Read main.py at the staging-branch tip and rewrite the literal;
    # if it is absent we must not guess — fall back.
    try:
        main_text = await get_file_text_at_ref(
            "backend/app/main.py",
            token=gh_token,
            owner=owner,
            repo=repo,
            ref=staging_branch,
            project_id=str(parent.project_id),
        )
    except Exception as e:  # noqa: BLE001
        logger.warning(
            f"[ROLLUP] PR #{rollup_pr_number} alembic auto-reconcile could not "
            f"fetch main.py ({type(e).__name__}: {e}); falling back to fix path"
        )
        return False
    if not main_text:
        return False
    new_main_text, replaced = rewrite_expected_schema_revision(
        main_text, plan["expected_schema_revision"]
    )
    if replaced == 0:
        logger.warning(
            f"[ROLLUP] PR #{rollup_pr_number} alembic auto-reconcile: no "
            f"EXPECTED_SCHEMA_REVISION literal in main.py; falling back to fix path"
        )
        return False

    upserts = {
        plan["new_path"]: plan["new_source"],
        "backend/app/main.py": new_main_text,
    }
    # Renumber always changes the NNNN prefix, so old_path != new_path; delete the
    # original file so the rename is clean (one head, no duplicate revision).
    deletes = [plan["old_path"]] if plan["old_path"] != plan["new_path"] else []
    try:
        await commit_file_changes(
            gh_token,
            owner,
            repo,
            staging_branch,
            upserts=upserts,
            deletes=deletes,
            message=(
                f"fix(alembic): auto-renumber rollup migration "
                f"{plan['old_revision']}->{plan['new_revision']} and rechain onto "
                f"{plan['new_down_revision']}"
            ),
            project_id=str(parent.project_id),
        )
    except Exception as e:  # noqa: BLE001 — push failed: fall back to fix path
        logger.warning(
            f"[ROLLUP] PR #{rollup_pr_number} alembic auto-reconcile push failed "
            f"({type(e).__name__}: {e}); falling back to fix path"
        )
        return False

    parent.context = {
        **parent_ctx,
        "rollup_pr_url": rollup_pr_url,
        "rollup_pr_number": rollup_pr_number,
        "_alembic_autoreconcile_pr": rollup_pr_number,
        "_alembic_autoreconcile_count": current_count + 1,
    }
    parent.latest_status_message = (
        f"Rollup PR #{rollup_pr_number} — auto-renumbered Alembic migration "
        f"{plan['old_revision']}->{plan['new_revision']}; re-validating next cycle"
    )[:500]
    session.add(parent)
    await session.commit()
    logger.info(
        f"[ROLLUP] PR #{rollup_pr_number} alembic collision auto-reconciled: "
        f"renumbered {plan['old_revision']}->{plan['new_revision']} on "
        f"{staging_branch}, bumped EXPECTED_SCHEMA_REVISION; deferring re-validation"
    )
    return True


async def _classify_merged_rollup_landing(
    parent: Task,
    project: Project | None,
    session: AsyncSession,
    *,
    pr_number: int | None,
    target_branch: str,
    source: str,
) -> tuple[bool, bool]:
    """Return ``(integration_landing, terminal_merge)``.

    Terminal rollup merges are those that land on either the integration branch
    or the promotion target. Merges into intermediate staging branches must keep
    the parent EVALUATING until a later downstream landing completes the chain.
    """
    integration_branch = _get_project_integration_branch(project)
    promotion_target = _get_project_promotion_target(project)
    landed = target_branch == integration_branch
    promotion_landing = target_branch == promotion_target
    if not landed and promotion_landing:
        await merged_pr_counts_as_integration_landing(
            parent,
            project,
            session,
            pr_number=pr_number,
            merged_base_branch=target_branch,
            source=source,
        )
    return landed, landed or promotion_landing


def _promotion_target_rollup_allowed(parent: Task, target_branch: str) -> bool:
    """Allow the intentionally-gated hotfix lane to target promotion directly."""
    if not target_branch:
        return False
    payload = parent.first_payload or {}
    ctx = parent.context or {}
    return bool(payload.get("hotfix") or ctx.get("hotfix"))


async def _hold_rollup_for_branch_safety(
    parent: Task,
    project: Project | None,
    session: AsyncSession,
    detail: str,
    *,
    issue_code: str,
) -> None:
    """Persist a loud hold when rollup branch resolution would hit promotion."""
    if project is not None:
        _signal_blocking_project_branch_configuration(project, issue_code, detail)
    if _runtime_config.get("require_approval_gates", True):
        parent.requires_approval = True
    parent.latest_status_message = detail[:500]
    session.add(parent)
    await session.commit()
    logger.error("[ROLLUP] %s", detail)


# ── FIX A: non-rejection PR-close classification ──────────────────────────
# A Blueprint rollup/create PR can be closed without merging for two very
# different reasons:
#   • GENUINE REJECTION — a reviewer requested changes, the review-fix cap was
#     exhausted, or the agent's work was deemed unmergeable. These MUST stay
#     FAILED + alert so direction health and the operator see the real signal.
#   • NON-GENUINE CLEANUP / SUPERSESSION — the work already landed via a sibling
#     PR, a parent/sibling rollup de-scoped this PR, or the Blueprint bot closed
#     it as part of housekeeping. The desired outcome is (effectively) achieved
#     or the task is moot, so failing it false-fails a non-failure and can
#     cascade a rollup parent into a partial-failure finalize.
# This helper decides between the two. It is conservative: ANY genuine-rejection
# signal forces the FAILED path, and a non-genuine verdict additionally REQUIRES
# at least one positive cleanup/supersession signal — absence of signal is never
# treated as non-genuine.

_GENUINE_REJECTION_CTX_MARKERS = ("pr_rejected", "review_cap_exhausted")


def _ctx_has_genuine_rejection_marker(ctx: dict | None) -> bool:
    """True when task context carries an explicit genuine-rejection marker.

    ``pr_rejected`` is set by the review pipeline on a real CHANGES_REQUESTED /
    manual-close rejection; ``review_cap_exhausted`` (set as failure_detail by
    ``_handle_cap_exhaustion``) is a review-quality non-convergence. Either one
    forces the FAILED path regardless of any cleanup signal.
    """
    if not ctx:
        return False
    if ctx.get("pr_rejected") is True:
        return True
    return ctx.get("failure_detail") in _GENUINE_REJECTION_CTX_MARKERS


def _ctx_review_was_approved(ctx: dict | None) -> bool:
    """True when the DB shows this PR was approved before the close.

    Reads the parent's ``review_loop.review_approved`` flag and the legacy
    ``verdict == 'APPROVE'`` / ``blueprint_review_verdict == 'APPROVE'`` forms.
    An approved-then-closed-unmerged PR is, by definition, not a rejection — the
    change was signed off and the close means the work landed (or was de-scoped)
    elsewhere.
    """
    if not ctx:
        return False
    review_loop = ctx.get("review_loop") or {}
    if review_loop.get("review_approved") is True:
        return True
    if review_loop.get("verdict") == "APPROVE":
        return True
    return ctx.get("blueprint_review_verdict") == "APPROVE"


def _ctx_has_supersession_signal(task: "Task") -> bool:
    """True when context / status message marks this PR as superseded or
    de-scoped by a sibling/parent rollup (a recognized cleanup signal)."""
    ctx = task.context or {}
    if ctx.get("superseded") or ctx.get("pr_superseded") or ctx.get("descoped"):
        return True
    blob = " ".join(
        str(ctx.get(k) or "")
        for k in ("pr_close_reason", "close_reason", "rollup_descope_reason")
    )
    msg = (task.latest_status_message or "") + " " + blob
    return "superseded" in msg.lower() or "de-scoped" in msg.lower()


def _pr_author_is_blueprint_bot(pr_data: dict | None) -> bool:
    """True when the PR was authored by the Blueprint GitHub App (a ``[bot]``
    account). Blueprint creates all rollup/create PRs through its installation
    token, so a bot-authored PR closed-unmerged with no rejection marker is a
    housekeeping close, not a human rejection. GitHub does not expose the close
    *actor* on the PR object, so bot *authorship* is the available proxy."""
    if not pr_data:
        return False
    user = pr_data.get("user") or {}
    login = (user.get("login") or "").lower()
    return user.get("type") == "Bot" or login.endswith("[bot]")


async def _pr_closed_is_non_rejection(
    task: "Task",
    pr_data: dict,
    pr_number: int,
    session: AsyncSession,
) -> bool:
    """Decide whether a closed-unmerged PR was a NON-genuine close (→ CANCELLED)
    rather than a genuine rejection (→ FAILED).

    Returns True only when ALL of the following hold:
      1. No genuine-rejection DB marker (pr_rejected / review_cap_exhausted).
      2. No GitHub review with state == CHANGES_REQUESTED.
      3. At least one positive cleanup/supersession signal:
           • the PR was approved before the close, or
           • a supersession / de-scope marker, or
           • the PR is Blueprint-bot-authored (housekeeping close).

    GUARDRAIL: any CHANGES_REQUESTED review, or any genuine-rejection marker, or
    the absence of every cleanup signal → returns False so the caller keeps the
    FAILED path. A failed reviews lookup is treated as "no recorded
    CHANGES_REQUESTED" but can NEVER on its own flip the verdict, because a
    positive cleanup signal is still required.
    """
    from app.github import get_pull_request_reviews
    from app.routers.agent import get_github_token_for_task

    ctx = task.context or {}

    # (1) Genuine-rejection markers always win.
    if _ctx_has_genuine_rejection_marker(ctx):
        return False

    # (2) Live CHANGES_REQUESTED check — a real reviewer rejection.
    try:
        gh_token, owner, repo = await get_github_token_for_task(task.id, session)
        reviews = await get_pull_request_reviews(
            gh_token, owner, repo, pr_number, project_id=str(task.project_id)
        )
        if any(
            (r or {}).get("state") == "CHANGES_REQUESTED" for r in (reviews or [])
        ):
            logger.info(
                f"[PR_VERIFY] Task {task.id} PR #{pr_number} closed-unmerged has a "
                f"CHANGES_REQUESTED review — genuine rejection, keeping FAILED"
            )
            return False
    except Exception as _rev_err:
        # Best-effort: a transient reviews lookup failure must never MASK a
        # rejection. We continue, but the positive-signal requirement below
        # still gates the non-genuine verdict.
        logger.warning(
            f"[PR_VERIFY] Task {task.id} PR #{pr_number} reviews lookup failed "
            f"({type(_rev_err).__name__}: {_rev_err}); requiring an explicit "
            f"cleanup signal before treating the close as non-rejection"
        )

    # (3) Require at least one positive cleanup/supersession signal.
    if _ctx_review_was_approved(ctx):
        return True
    if _ctx_has_supersession_signal(task):
        return True
    return _pr_author_is_blueprint_bot(pr_data)


async def _dispatch_rollup_approval_alert(
    parent: Task, session: AsyncSession, reason: str, pr_number: int | None = None
) -> None:
    """Create a failure alert and fire an external APPROVAL_NEEDED notification
    when a rollup parent enters the approval-gated state so operators are
    notified instead of discovering the stall later.
    """
    import asyncio as _asyncio
    from app.alert_dispatch import _maybe_create_failure_alert
    from app.notification_dispatch import dispatch_external_notification

    try:
        await _maybe_create_failure_alert(parent, session, reason)
    except Exception as e:
        logger.warning(f"[NOTIFY] rollup approval alert creation failed: {e}")
    try:
        parent_ctx = parent.context if isinstance(parent.context, dict) else {}
        parent_payload = parent.first_payload or {}
        pr_url = parent_ctx.get("rollup_pr_url") or ""
        if not pr_url and pr_number:
            repo_full = parent_ctx.get("repo_full_name") or parent_ctx.get("repo") or ""
            if repo_full:
                pr_url = f"https://github.com/{repo_full}/pull/{pr_number}"
        raw_critical = (
            parent_ctx.get("critical_files")
            or parent_ctx.get("_pr_changed_files")
            or []
        )
        critical_files = (
            [f for f in raw_critical if isinstance(f, str)][:10]
            if isinstance(raw_critical, list)
            else []
        )
        direction_text = (
            parent_ctx.get("direction_text")
            or parent_payload.get("direction_text")
            or ""
        )
        scope = parent_ctx.get("scope") or parent_payload.get("scope") or ""
        # Fire-and-forget best-effort external notification; losing the task
        # on GC is acceptable and the call is already try/except-wrapped, so
        # RUF006 (store-a-reference) is intentionally suppressed below.
        _asyncio.ensure_future(  # noqa: RUF006
            dispatch_external_notification(
                parent.project_id,
                "APPROVAL_NEEDED",
                {
                    "task_id": str(parent.id),
                    "task_title": parent.title or str(parent.id)[:8],
                    "pr_number": pr_number,
                    "reason": reason,
                    "approval_reason": reason,
                    "pr_url": pr_url,
                    "critical_files": critical_files,
                    "direction_id": parent_ctx.get("direction_id") or "",
                    "direction_text": direction_text[:200],
                    "scope": scope,
                },
            )
        )
    except Exception as e:
        logger.warning(f"[NOTIFY] APPROVAL_NEEDED dispatch setup failed: {e}")


def _build_rollup_pr_body(
    task_count: int,
    staging_branch: str,
    rollup_note: str = "",
    *,
    direction_id: str | None = None,
    direction_title: str | None = None,
    children: list | None = None,
) -> str:
    """Build the Markdown body for a rollup PR.

    Includes direction, task-type breakdown, and aggregate cost when available.
    """
    lines: list[str] = [f"Rollup of {task_count} subtasks from `{staging_branch}`"]

    if direction_title or direction_id:
        dir_label = (direction_title or "")[:80]
        dir_suffix = f" `{direction_id[:8]}`" if direction_id else ""
        lines.append("")
        lines.append(f"**Direction:** {dir_label}{dir_suffix}")

    if children:
        from collections import Counter

        type_counts: Counter = Counter()
        total_cost: float = 0.0
        has_cost = False
        for c in children:
            payloads = getattr(c, "payloads", None) or []
            t = payloads[0].get("type", "create") if payloads else "create"
            type_counts[t] += 1
            prov_at = getattr(c, "vm_provisioned_at", None)
            stop_at = getattr(c, "vm_stopped_at", None)
            if prov_at and stop_at:
                from app.routers.admin_analytics import ON_DEMAND_RATE, SPOT_RATE

                rate = SPOT_RATE if getattr(c, "vm_spot", False) else ON_DEMAND_RATE
                total_cost += (stop_at - prov_at).total_seconds() * rate
                has_cost = True
        if type_counts:
            breakdown = ", ".join(
                f"{cnt} {typ}" for typ, cnt in sorted(type_counts.items())
            )
            lines.append("")
            lines.append(f"**Tasks:** {breakdown}")
        if has_cost:
            lines.append(f"**Estimated cost:** ~${total_cost:.2f}")

    if rollup_note:
        lines.append(rollup_note)

    lines.append("")
    lines.append("---")
    lines.append("*Automated by Blueprint Pipeline*")
    return "\n".join(lines)


async def _check_and_trigger_rollup(
    child_task: Task, session: AsyncSession, progress_cb=None
) -> None:
    """Check if all siblings are done and trigger rollup PR for the parent.

    Called after a child task completes, fails, or is cancelled.
    Also called from admin PATCH when status/context changes.

    ``progress_cb`` (C3): an optional zero-arg heartbeat invoked inside the
    per-child PR-verification loop. The reaper's stale-EVALUATING sweep passes
    ``_mark_reaper_progress`` so that verifying many unmerged children (each a
    serial GitHub round-trip via _verify_pr_merged) keeps the reaper liveness
    heartbeat fresh instead of silently consuming the whole cycle budget on one
    fan-heavy parent. Defaults to a no-op for the request-path callers.
    """
    from app.alert_dispatch import _maybe_create_failure_alert
    from app.time_utils import _utcnow
    from app.broadcast import broadcast_to_project
    from app.routers.agent import get_github_token_for_task
    from app.security import scan_pr_for_injection
    from app.github import (
        create_pr_comment,
        delete_branch,
        create_pull_request,
        merge_pull_request,
        get_pull_request,
        get_pull_request_diff,
        get_pr_files,
        get_branch_sha,
        close_pull_request,
        pr_is_not_behind_for,
        update_branch_with_sha_retry,
    )

    if not child_task.parent_task_id:
        return

    # Lock parent row to serialize concurrent rollup checks from sibling completions.
    # skip_locked: if a sibling child is already running this check, defer — the
    # reaper's EVALUATING stale-parent sweep retries within 15 min as safety net.
    parent = (
        (
            await session.execute(
                select(Task)
                .where(Task.id == child_task.parent_task_id)
                .with_for_update(skip_locked=True)
            )
        )
        .scalars()
        .first()
    )
    if parent is None:
        logger.info(
            f"[ROLLUP] Skipped rollup check for task {child_task.id} — parent lock held, "
            "reaper sweep will retry"
        )
        return
    if parent.status != TaskStatus.EVALUATING:
        return

    # Lock children with skip_locked to avoid deadlocks with concurrent
    # agent status updates that hold FOR UPDATE on individual child rows.
    all_children = (
        (
            await session.execute(
                select(Task)
                .where(Task.parent_task_id == parent.id)
                .with_for_update(skip_locked=True)
            )
        )
        .scalars()
        .all()
    )

    # Verify we locked all children — if some were skipped (held by agent_status
    # or another rollup), defer to avoid acting on incomplete data.
    total_children = await session.scalar(
        select(_func.count()).select_from(Task).where(Task.parent_task_id == parent.id)
    )
    if len(all_children) < total_children:
        logger.info(
            f"[ROLLUP] Deferred rollup for parent {parent.id} — "
            f"locked {len(all_children)}/{total_children} children (others held)"
        )
        return

    # Consider create, refactor, plan, research, and specialized agent tasks for rollup decisions.
    # Review/fix tasks are pipeline helpers that operate on the same PR — excluded.
    ROLLUP_TYPES = {
        "create",
        "refactor",
        "migrate",
        "",
        "plan",
        "research",
        "security_audit",
        "qa",
        "deps",
        "docs",
        "smoketest",
        "perf",
    }
    children = [
        c
        for c in all_children
        if (c.first_payload).get("type", "create") in ROLLUP_TYPES
    ]

    # Guard: if all children are review/fix types (none in ROLLUP_TYPES), mark parent COMPLETED
    if not children and all_children:
        parent.status = TaskStatus.COMPLETED
        parent.context = _clear_rollup_transient_state(parent.context)
        parent.latest_status_message = (
            "All subtasks were pipeline tasks (review/fix) — no rollup needed"
        )
        session.add(parent)
        await session.commit()
        await _cleanup_branches(parent, session)
        await broadcast_to_project(
            str(parent.project_id),
            "task_updated",
            {"task_id": str(parent.id), "status": TaskStatus.COMPLETED},
        )
        logger.info(
            f"[ROLLUP] Parent {parent.id} completed — all children were review/fix pipeline tasks"
        )
        return

    # Also check if any review/fix pipeline tasks are still active — don't rollup
    # while a review/fix cycle is in progress. Stale tasks (no heartbeat for
    # 15+ min) are excluded so a silently-dead pipeline task can't permanently
    # block rollup; the orchestrator's reaper will eventually cancel them.
    pipeline_active = False
    for c in all_children:
        if (c.first_payload).get("type") not in ("review", "fix"):
            continue
        if c.status not in (
            TaskStatus.QUEUED,
            TaskStatus.PROVISIONING,
            TaskStatus.IN_PROGRESS,
            TaskStatus.EVALUATING,
        ):
            continue
        if _is_pipeline_task_stale(c):
            logger.warning(
                f"[ROLLUP] Ignoring stale pipeline task {str(c.id)[:8]} "
                f"(status={c.status}, last_heartbeat={c.last_heartbeat}) — not blocking rollup"
            )
            continue
        pipeline_active = True
        break

    # Tally non-IO statuses first so we can decide whether the GitHub-backed
    # PR-merge verification loop is even necessary. ``all_done`` starts True
    # and gets cleared by FAILED/active children below; the verify loop also
    # clears it for any unmerged COMPLETED child (mirrors original semantics).
    all_done = True
    cancelled_count = 0
    active_count = 0
    completed_children: list[Task] = []
    for c in children:
        if c.status == TaskStatus.CANCELLED:
            cancelled_count += 1
            continue
        if c.status in (
            TaskStatus.QUEUED,
            TaskStatus.PROVISIONING,
            TaskStatus.IN_PROGRESS,
            TaskStatus.EVALUATING,
            # A BLOCKED child is non-terminal work parked on a precondition; the
            # auto-unblock sweep is expected to return it to QUEUED. Counting it
            # as active keeps the rollup waiting rather than rolling up an
            # incomplete subtree or miscounting it as a silent failure.
            TaskStatus.BLOCKED,
        ):
            active_count += 1
            all_done = False
            continue
        if c.status == TaskStatus.FAILED:
            all_done = False
            continue
        if c.status == TaskStatus.COMPLETED:
            completed_children.append(c)

    # Don't rollup if there are still active tasks or active review/fix pipeline
    if active_count > 0 or pipeline_active:
        if pipeline_active and (parent.latest_status_message or "").startswith(
            "Blocked:"
        ):
            parent.latest_status_message = (
                "Fix/review pipeline running — waiting for PR to merge"
            )
            session.add(parent)
            await session.commit()
        return

    # ── Phase B3 lock-window break (Site 1: _verify_pr_merged) ──
    # _verify_pr_merged calls get_pull_request + get_github_token_for_task per
    # COMPLETED child. With 5-10 unmerged children x 30s GH the worst-case
    # held-lock window is 150-300s on the parent + N children. Release the
    # parent lock (and the children FOR UPDATE locks, which share this
    # session's transaction) around the verification loop and re-acquire the
    # parent afterwards so concurrent sibling heartbeats and admin writes
    # don't queue behind us. The verification only READS PR state from
    # GitHub and updates in-memory + DB via _verify_pr_merged's own
    # write_session, not the caller's session.
    parent_id_local = parent.id
    pre_verify_snapshot = await _release_parent_lock(parent, session)

    # Run the GitHub round-trips with no DB locks held. _verify_pr_merged
    # mutates the cached child's in-memory `task.status` (PR closed unmerged
    # → FAILED) and `task.context["pr_merged"]`, and commits the persisted
    # write via its own AsyncSessionLocal. The cached `children` list
    # objects retain those in-memory mutations across the lock-window break
    # (expire_on_commit=False), preserving original semantics: a child
    # whose verify-call returned True keeps the verify branch's
    # all_done contribution at True, even if the call also flipped its
    # status to FAILED. Downstream branches (the
    # `failed_count == len(children)` guard at line ~927 and the
    # rollup-prep loop) re-derive failed/completed_merged from `c.status`
    # on the same cached list and handle the post-verify FAILED case.
    for c in completed_children:
        # C3 heartbeat: each _verify_pr_merged is a serial GitHub round-trip;
        # a parent with many unmerged children would otherwise consume the
        # caller's whole liveness budget on this loop alone. No-op unless the
        # reaper sweep passed _mark_reaper_progress.
        if progress_cb is not None:
            progress_cb()
        if not await _verify_pr_merged(c, session):
            all_done = False

    # ── Re-acquire parent lock for downstream mutations ──
    # We deliberately do NOT re-fetch the children rows nor re-derive
    # all_done from the cached list: original behavior was that a verify
    # call returning True meant "child resolved" regardless of any
    # status side-effect (e.g. PR-closed-unmerged → FAILED), and the
    # downstream `completed_count == 0 and failed_count > 0` guard
    # finalizes the parent. Preserving this contract by carrying
    # `all_done` from the verify loop forward to the existing dispatch.
    parent = await _relock_parent(
        session,
        parent_id_local,
        pre_verify_snapshot,
        parent_in_session=parent,
    )
    if parent is None:
        return
    if parent.status != TaskStatus.EVALUATING:
        return

    # All children terminal but not all succeeded — finalize immediately instead of waiting 6 hours
    if not all_done and active_count == 0 and not pipeline_active:
        # Cap-exhausted children (requires_approval=True, PR auto-closed by
        # _handle_cap_exhaustion) are excluded from the unmerged respawn list:
        # respawning hits the cap again every cycle. Post-fix the cap handler
        # transitions them to FAILED with failure_detail=review_cap_exhausted;
        # this filter defensively covers any legacy COMPLETED+requires_approval
        # children that predate the fix.
        unmerged = [
            c
            for c in children
            if c.status == TaskStatus.COMPLETED
            and not (c.context or {}).get("pr_merged")
            and not c.requires_approval
        ]
        failed = [c for c in children if c.status == TaskStatus.FAILED]
        completed_merged = [
            c
            for c in children
            if c.status == TaskStatus.COMPLETED and (c.context or {}).get("pr_merged")
        ]

        if unmerged:
            # Some completed but PRs not yet merged.
            # Check if any are stale (>10min) with no active review — respawn pipeline.
            #
            # ── C2 lock-window break (review-respawn loop) ──
            # ``_spawn_pipeline_task`` commits internally (releasing the parent
            # FOR UPDATE lock acquired/re-acquired above) and performs GitHub
            # network I/O. The prior implementation called it INSIDE this loop
            # while the parent lock was nominally held, so each spawn silently
            # released the parent lock mid-loop and the next iteration mutated
            # ``parent`` / ``c`` against an unlocked row — exactly the
            # held-lock-across-IO / mutate-after-silent-release anti-pattern the
            # merge SUCCESS path already eliminated. Mirror that path here:
            #   Phase A — under the parent lock, decide per child and apply only
            #             DB-local mutations (force pr_merged, respawn-cap skip),
            #             collecting the children that need a review respawn.
            #   Phase B — release the parent lock around the spawns.
            #   Phase C — spawn lock-free (each spawn owns its advisory lock).
            #   Phase D — re-lock parent with snapshot validation, apply the
            #             cap-exhaustion escalations + status message, commit.
            from app.shared_state import resolve_task_branch

            now = _utcnow()
            stale_threshold = timedelta(minutes=10)
            respawned = 0
            # Phase A: decide under lock; collect respawn candidates.
            respawn_candidates: list[dict] = []
            for c in unmerged:
                age = (now - c.updated_at).total_seconds() if c.updated_at else 0
                if age < stale_threshold.total_seconds():
                    continue  # Too recent — normal pipeline may still be working
                # Look up PR artifact to get pr_number and branch
                pr_art = (
                    (
                        await session.execute(
                            select(Artifact)
                            .where(
                                Artifact.task_id == c.id,
                                Artifact.artifact_type == ArtifactType.PULL_REQUEST,
                            )
                            .limit(1)
                        )
                    )
                    .scalars()
                    .first()
                )
                if not pr_art or not pr_art.pr_url:
                    # No PR artifact — _verify_pr_merged should have handled this.
                    # Force-set pr_merged so next cycle picks it up.
                    mark_unblocked_non_merge(c, reason="rollup_no_pr_artifact")
                    session.add(c)
                    logger.info(
                        f"[ROLLUP] Child {str(c.id)[:8]} has no PR artifact — marking as merged"
                    )
                    continue
                try:
                    pr_number = int(pr_art.pr_url.rstrip("/").split("/")[-1])
                except (ValueError, IndexError):
                    # Malformed pr_url (e.g. /commit/<sha>) — cannot parse a PR number.
                    # Force-set pr_merged so the next rollup cycle progresses instead of
                    # hanging forever. ROOT CAUSE 2/3 fixes upstream make this rare.
                    logger.warning(
                        f"[ROLLUP] malformed pr_url {pr_art.pr_url!r} for child "
                        f"{str(c.id)[:8]} — cannot parse PR number; "
                        f"force-setting pr_merged=True so rollup progresses"
                    )
                    mark_unblocked_non_merge(c, reason="malformed_pr_url")
                    session.add(c)
                    continue

                # Circuit-breaker: once a child's review has been respawned
                # MAX_REVIEW_RESPAWNS times without the PR merging, stop looping
                # and escalate to manual approval (mirrors the cap-exhaustion
                # escalation below). Guards against a review that keeps failing
                # fast — e.g. dispatch pre-emption — respawning every cycle.
                respawn_count = _review_respawn_count(c)
                if respawn_count >= MAX_REVIEW_RESPAWNS:
                    # Discretionary escalation: only approval-gate the parent when
                    # require_approval_gates is enabled. With gates off the operator
                    # opted out of manual gating, so escalating here would only let
                    # services.py's 2h approval-gate timeout FAIL the otherwise-healthy
                    # parent — discarding its already-merged siblings. Instead skip the
                    # wedged child (force pr_merged, the same drop-from-unmerged
                    # mechanism used for malformed pr_url above) so the parent converges
                    # via partial rollup, and alert so the dropped subtask is visible.
                    # (Sibling of the cap-exhaustion fix below at ~:1090; mirrors the
                    # review_pipeline repeat-fingerprint/stuck-cycle gating, whose
                    # un-skipped children fall through to this terminalization.)
                    gates_on = _runtime_config.get("require_approval_gates", True)
                    if gates_on and not parent.requires_approval:
                        parent.requires_approval = True
                        logger.warning(
                            f"[ROLLUP] Child {str(c.id)[:8]} PR #{pr_number} — review "
                            f"respawned {respawn_count}x without merging; escalating: "
                            f"marked parent {parent.id} requires_approval"
                        )
                        alert_msg = (
                            f"Rollup blocked: PR #{pr_number} review respawned "
                            f"{respawn_count}x without merging — manual intervention required"
                        )
                    elif not gates_on:
                        mark_unblocked_non_merge(c, reason="review_respawn_cap")
                        c.context = {
                            **(c.context or {}),
                            "_rollup_skipped_reason": "review_respawn_cap",
                        }
                        session.add(c)
                        logger.warning(
                            f"[ROLLUP] Child {str(c.id)[:8]} PR #{pr_number} — review "
                            f"respawned {respawn_count}x without merging and gates are "
                            f"off; skipping (force pr_merged) so parent {parent.id} "
                            f"converges via partial rollup"
                        )
                        alert_msg = (
                            f"Subtask PR #{pr_number} dropped from rollup: review "
                            f"respawned {respawn_count}x without merging "
                            f"(require_approval_gates off — converging via partial rollup)"
                        )
                    else:
                        alert_msg = None
                    if alert_msg:
                        try:
                            await _maybe_create_failure_alert(parent, session, alert_msg)
                        except Exception as _alert_err:
                            logger.warning(
                                f"[ROLLUP] Failed to create respawn-cap alert "
                                f"for parent {parent.id}: {_alert_err}"
                            )
                    continue

                c_payload = c.first_payload
                # Branch must respect the child's task type — agents use
                # `blueprint/refactor-*` for refactor tasks, etc. Hardcoding
                # `task-` here stalled review respawns on refactor children
                # (incident 2026-05-25, PRs #5779/#5812/#5815/#5819 all hit
                # the review cap because the clone targeted blueprint/task-*
                # while the agent had pushed blueprint/refactor-*).
                # For fix children, the real branch lives in
                # review_loop.branch or first_payload.branch — synthesizing
                # blueprint/task-{fix_id} yields a name no agent ever pushed,
                # so respawned reviews would fail at `git clone --branch`.
                project = await session.get(Project, c.project_id)
                branch = resolve_task_branch(
                    c, _get_project_task_branch_namespace(project)
                )
                try:
                    base_branch = c_payload.get(
                        "base_branch"
                    ) or _get_project_integration_branch(project)
                # Defense-in-depth: the resolver currently fails SAFE and does not
                # raise. This guard exists so a future re-introduction of a raising
                # resolver path holds the rollup instead of aborting the sweep.
                except InvalidProjectBranchConfiguration as exc:
                    await _hold_rollup_for_branch_safety(
                        parent,
                        project,
                        session,
                        (
                            f"Rollup blocked: child {str(c.id)[:8]} resolved to "
                            f"an unsafe integration branch. {exc.detail}"
                        ),
                        issue_code="child_integration_branch_invalid",
                    )
                    return
                original_prompt = c_payload.get("prompt", c.description or "")
                respawn_candidates.append(
                    {
                        "child_id": c.id,
                        "pr_number": pr_number,
                        "branch": branch,
                        "base_branch": base_branch,
                        "original_prompt": original_prompt,
                        "respawn_count": respawn_count,
                    }
                )

            # Phase B: release the parent (+ children) FOR UPDATE lock around the
            # spawns. Capture a snapshot so the re-lock in Phase D detects peer
            # mutation of the rollup state machine during the unlocked window.
            # context_dirty=False: Phase A mutates CHILD contexts (full reassignment
            # via session.add(c) — SQLA tracks those) and at most the parent's
            # ``requires_approval`` column, never ``parent.context`` in place, so no
            # flag_modified on the parent JSONB is needed (avoids the TOAST-write
            # amplification the _release_parent_lock docstring warns about). The
            # requires_approval flip is a column mutation SQLA tracks on its own;
            # the snapshot below captures its post-mutation value, so the Phase D
            # re-lock won't false-defer on our own escalation.
            parent_id_local = parent.id
            unmerged_titles = [c.title[:40] for c in unmerged]
            pre_respawn_snapshot = await _release_parent_lock(parent, session)

            # Phase C: spawn reviews lock-free. Each _spawn_pipeline_task owns its
            # own per-PR advisory lock and commits its own work; no DB lock is held
            # here. Cap-exhaustion escalations that must touch the parent row are
            # deferred to Phase D (the parent is released here). Per-child mutations
            # (respawn-count bump) are persisted on this session and committed by
            # the Phase D relock commit.
            cap_exhausted_prs: list[int] = []
            for cand in respawn_candidates:
                child = await session.get(Task, cand["child_id"])
                if child is None:
                    continue
                review_task, was_spawned = await _spawn_pipeline_task(
                    child,
                    "review",
                    session,
                    cand["pr_number"],
                    cand["branch"],
                    cand["base_branch"],
                    original_prompt=cand["original_prompt"],
                    original_task_id=str(child.id),
                )
                if review_task and review_task.id != child.id:
                    respawned += 1
                    if was_spawned:
                        child.context = {
                            **(child.context or {}),
                            "_review_respawn_count": cand["respawn_count"] + 1,
                        }
                        session.add(child)
                    logger.warning(
                        f"[ROLLUP] Respawned review for stale child {str(child.id)[:8]} "
                        f"PR #{cand['pr_number']} → review {str(review_task.id)[:8]} "
                        f"(respawn {_review_respawn_count(child)}/{MAX_REVIEW_RESPAWNS})"
                    )
                elif review_task is None and child.requires_approval:
                    # Cap exhaustion: _spawn_pipeline_task sets requires_approval=True on
                    # the child. But the child is COMPLETED, so the EVALUATING approval-gate
                    # timeout never fires on it — the parent would otherwise wait the full
                    # evaluating_fail_hours (default 48h) hard timeout. Escalate: set
                    # requires_approval on the parent so the 2-hour approval gate fires,
                    # and raise a failure alert. Gated on require_approval_gates: with gates
                    # off the operator opted out of manual gating, so the parent must converge
                    # via partial rollup rather than be approval-gated and timed out. (This
                    # also guards against the stuck-cycle / repeat-fingerprint paths, which set
                    # requires_approval on the child too, leaking into a parent escalation.)
                    # Deferred to Phase D — the parent row is released here.
                    if _runtime_config.get("require_approval_gates", True):
                        cap_exhausted_prs.append(cand["pr_number"])

            # Phase D: re-lock the parent for the terminal status write +
            # cap-exhaustion escalation. If a peer mutated the rollup state during
            # the unlocked spawn window, defer to the next cycle.
            await session.commit()  # flush Phase C per-child mutations
            parent = await _relock_parent(
                session,
                parent_id_local,
                pre_respawn_snapshot,
                parent_in_session=parent,
            )
            if parent is None:
                return
            if cap_exhausted_prs and not parent.requires_approval:
                parent.requires_approval = True
                for _pr in cap_exhausted_prs:
                    logger.warning(
                        f"[ROLLUP] Child PR #{_pr} hit review-fix cap — "
                        f"escalating: marked parent {parent.id} requires_approval"
                    )
                    try:
                        await _maybe_create_failure_alert(
                            parent,
                            session,
                            f"Rollup blocked: child PR #{_pr} review-fix cap exhausted — manual intervention required",
                        )
                    except Exception as _alert_err:
                        logger.warning(
                            f"[ROLLUP] Failed to create cap-exhaustion alert for parent {parent.id}: {_alert_err}"
                        )

            if respawned:
                parent.latest_status_message = f"Recovering: respawned {respawned} review(s) for unmerged PR(s) — {', '.join(unmerged_titles)}"
            else:
                parent.latest_status_message = f"Blocked: {len(unmerged)} subtask(s) completed but PR not merged — {', '.join(unmerged_titles)}"
            logger.info(
                f"[ROLLUP] Parent {parent.id} — {len(unmerged)} unmerged, {respawned} reviews respawned"
            )
            session.add(parent)
            await session.commit()
            return

        # No unmerged PRs — only failed/cancelled/merged children remain.
        # Before giving up on failed children, attempt retry via _requeue_parent_task.
        # Cap-exhausted children are terminal: their PR was auto-closed and a
        # fresh retry would hit the same merge conflict that triggered the cap.
        # Exclude them from requeue; count them as failed-for-rollup-decision only.
        retriable_failed = [
            c
            for c in failed
            if (c.context or {}).get("failure_detail") != "review_cap_exhausted"
        ]
        if retriable_failed:
            MAX_PARENT_REQUEUES = 3
            parent_ctx = parent.context or {}
            requeue_count = parent_ctx.get("_requeue_count", 0)
            if requeue_count < MAX_PARENT_REQUEUES:
                # Retry: clone and re-queue failed children; parent returns to EVALUATING.
                # Next rollup cycle will produce a full rollup if retries succeed, or fall
                # through to partial rollup once the requeue limit is exhausted.
                from app.task_lifecycle import _requeue_parent_task

                logger.info(
                    f"[ROLLUP] Parent {parent.id} — {len(retriable_failed)} retriable failed, "
                    f"{len(failed) - len(retriable_failed)} cap-exhausted, "
                    f"{len(completed_merged)} succeeded. Requeuing failed children "
                    f"(attempt {requeue_count + 1}/{MAX_PARENT_REQUEUES}) before partial rollup."
                )
                await _requeue_parent_task(parent, session)
                return
            # Requeue limit exhausted — fall through to partial rollup or full failure.
            logger.warning(
                f"[ROLLUP] Parent {parent.id} — requeue limit reached "
                f"({requeue_count}/{MAX_PARENT_REQUEUES}); proceeding with partial rollup or failure."
            )

        if completed_merged:
            # Some succeeded — proceed with partial rollup of what worked
            logger.warning(
                f"[ROLLUP] Parent {parent.id} — {len(failed)} failed, {len(completed_merged)} succeeded. Rolling up successes."
            )
            all_done = True  # Let rollup proceed with merged children
            # Fall through to rollup logic below
        else:
            # Nothing succeeded — fail the parent immediately
            parent.status = TaskStatus.FAILED
            parent.latest_status_message = (
                f"{len(failed)} of {len(children)} subtask(s) failed, none succeeded"
            )
            from app.failure_classification import (
                worst_child_failure_category,
                persist_failure_category,
            )

            _fc = worst_child_failure_category(failed)
            persist_failure_category(parent, _fc)
            _record_rollup_failure_signals(parent, _fc)
            parent.context = _clear_rollup_transient_state(parent.context)
            session.add(parent)
            await _maybe_create_failure_alert(
                parent, session, f"{len(failed)} subtasks failed, none succeeded"
            )
            await session.commit()
            await broadcast_to_project(
                str(parent.project_id),
                "task_updated",
                {"task_id": str(parent.id), "status": TaskStatus.FAILED},
            )
            logger.error(
                f"[ROLLUP] Parent {parent.id} — all {len(failed)} children failed, marking parent FAILED"
            )
            return

    child_statuses = [c.status for c in children]

    # Edge case: all rollup children CANCELLED — nothing to roll up. Route the
    # terminal verdict through the SAME shared helper the requeue/reaper path
    # uses (``_resolve_terminal_parent_verdict``) so the two finalizers cannot
    # land the same child set in two different terminal states. The
    # EVALUATING-timeout recovery sweep
    # (recovery_sweeps._check_evaluating_timeouts) reaches this finalizer with
    # NO age gate and usually wins the race against the reaper's ~15-min requeue
    # rescue — so it MUST agree with the requeue verdict instead of diverging
    # (the cross-sweep drift the shared helper exists to prevent).
    #
    # ``children`` is the rollup set (review/fix pipeline children already
    # filtered out above) — the same set the shared helper classifies. Every
    # child is CANCELLED in this branch, so no FAILED child can be
    # BLOCKED_PRECONDITION: ``blocked_count`` is 0.
    if children and cancelled_count == len(children):
        from app.task_lifecycle import (
            _resolve_terminal_parent_verdict,
            _terminate_all_non_genuine_cancelled,
        )

        _verdict, _reason = await _resolve_terminal_parent_verdict(
            parent, children, 0
        )

        # Clear rollup transient deferral/retry counters before EITHER terminal
        # finalize commits so they don't leak into a future cycle. The shared
        # ``_terminate_all_non_genuine_cancelled`` helper commits internally and
        # does NOT clear rollup state or clean branches (the requeue path has no
        # such state), so the rollup path owns both around the helper here.
        parent.context = _clear_rollup_transient_state(parent.context)

        if _reason == "all_non_genuine_cancelled":
            # Every cancel is non-genuine (superseded / replaced-by-retry /
            # reassigned / scope-empty): the work may have landed elsewhere, so
            # this is FAILED + SCOPE_EMPTY with NO direction-health penalty and
            # NO failure alert — byte-for-byte the requeue/reaper finalize.
            await _terminate_all_non_genuine_cancelled(
                parent, children, session, broadcast_to_project
            )
            await _cleanup_branches(parent, session)
            logger.info(
                f"[ROLLUP] Parent {parent.id} FAILED (SCOPE_EMPTY) — all "
                f"{len(children)} children non-genuine cancelled"
            )
            return

        # At least one GENUINE cancel (e.g. an operator manually cancelling
        # every child, or a child cancelled under its own review/fix
        # divergence). DECISION: finalize FAILED — NOT the previous quiet
        # CANCELLED. The shared verdict counts a genuine cancel as a terminal
        # failure, and the requeue/reaper path (locked by
        # test_requeue_all_cancelled_children_marks_parent_failed) already
        # finalizes this exact child set to FAILED with a failure alert + a
        # direction-health signal. Because the recovery sweep wins the race to
        # this finalizer most of the time, it must produce the SAME terminal
        # state and the SAME side effects rather than the quieter CANCELLED that
        # silently disagreed with the reaper. (An all-operator-cancel parent has
        # always surfaced as a failure via the reaper; the parent's work
        # genuinely did not land, so the alert is correct, not noise.)
        from app.failure_classification import (
            AGENT_TIMEOUT,
            persist_failure_category,
        )

        parent.status = TaskStatus.FAILED
        parent.latest_status_message = (
            f"All {len(children)} subtask(s) cancelled, none succeeded — "
            f"nothing to roll up"
        )
        persist_failure_category(parent, AGENT_TIMEOUT)
        _record_rollup_failure_signals(parent, AGENT_TIMEOUT)
        session.add(parent)
        await _maybe_create_failure_alert(
            parent,
            session,
            f"All {len(children)} subtask(s) cancelled, none succeeded",
        )
        await session.commit()
        await _cleanup_branches(parent, session)
        await broadcast_to_project(
            str(parent.project_id),
            "task_updated",
            {"task_id": str(parent.id), "status": TaskStatus.FAILED},
        )
        logger.info(
            f"[ROLLUP] Parent {parent.id} FAILED — all {len(children)} children "
            f"cancelled with at least one genuine cancel"
        )
        return

    if all_done and (len(children) - cancelled_count) > 0:
        # All non-cancelled children merged — create rollup PR
        parent_ctx = parent.context or {}
        staging_branch = parent_ctx.get("plan_branch") or parent_ctx.get(
            "research_branch"
        )
        target_branch = parent_ctx.get("parent_base") or parent_ctx.get("original_base")

        failed_children = [c for c in children if c.status == TaskStatus.FAILED]
        failed_count = len(failed_children)
        completed_count = len(children) - cancelled_count - failed_count

        # Guard: _verify_pr_merged may have flipped COMPLETED children to FAILED
        # (e.g. PR closed without merging) after the all_done loop evaluated their
        # status. If every non-cancelled child is now FAILED, there is nothing
        # to roll up — fail the parent instead of creating an empty rollup PR
        # that would trip the zero-diff guard and spuriously mark it COMPLETED.
        if completed_count == 0 and failed_count > 0:
            parent.status = TaskStatus.FAILED
            parent.context = _clear_rollup_transient_state(parent.context)
            parent.latest_status_message = (
                f"{failed_count} of {len(children)} subtask(s) failed, none succeeded"
            )
            from app.failure_classification import (
                worst_child_failure_category,
                persist_failure_category,
            )

            _fc = worst_child_failure_category(failed_children)
            persist_failure_category(parent, _fc)
            _record_rollup_failure_signals(parent, _fc)
            session.add(parent)
            await _maybe_create_failure_alert(
                parent, session, f"{failed_count} subtasks failed, none succeeded"
            )
            await session.commit()
            await broadcast_to_project(
                str(parent.project_id),
                "task_updated",
                {"task_id": str(parent.id), "status": TaskStatus.FAILED},
            )
            logger.error(
                f"[ROLLUP] Parent {parent.id} — all {failed_count} non-cancelled "
                f"children failed after PR verification, marking parent FAILED"
            )
            return

        cap_exhausted_children = [
            c
            for c in failed_children
            if (c.context or {}).get("failure_detail") == "review_cap_exhausted"
        ]
        plain_failed_count = failed_count - len(cap_exhausted_children)
        rollup_note = ""
        if cancelled_count > 0:
            rollup_note += f"\n\n*{cancelled_count} subtask(s) were cancelled and excluded from this rollup.*"
        if plain_failed_count:
            rollup_note += f"\n\n*{plain_failed_count} subtask(s) failed and were excluded from this rollup.*"
        if cap_exhausted_children:
            rollup_note += (
                f"\n\n*{len(cap_exhausted_children)} subtask(s) hit the review-fix cap "
                f"(PRs auto-closed, manual review required) and were excluded from this rollup.*"
            )

        if staging_branch and target_branch and staging_branch != target_branch:
            gh_token = owner = repo = None
            project = await session.get(Project, parent.project_id)
            promotion_target = (
                _get_project_promotion_target(project) if project is not None else None
            )
            integration_branch = (
                _get_project_integration_branch(project)
                if project is not None
                else None
            )
            # Block ONLY when the rollup would land fleet work on the promotion
            # target AND that promotion target is genuinely distinct from the
            # project's integration branch. Single-trunk projects (integration ==
            # promotion, e.g. a project whose only branch is 'main') legitimately
            # land every rollup on that shared trunk; holding those would stall
            # the project entirely. The hotfix lane is the explicit exemption for
            # the distinct-branch case.
            #
            # A custom staged rollup root (e.g. a project whose staging family is
            # 'release/' rolling up onto 'release/main') is ALSO exempt: landing
            # on such a staging branch is the intended staged flow even when that
            # branch happens to coincide with promotion_target. Those merges are
            # not held but proceed and are classified downstream by
            # merged_pr_counts_as_integration_landing, which raises the
            # "Merged PR landed on promotion target before integration" alert and
            # leaves pr_merged unset. Holding them here would shadow that path and
            # strand the staged rollup (PR #7064 vs #7105 merge-lane reconciliation).
            if (
                promotion_target
                and target_branch == promotion_target
                and target_branch != integration_branch
                and not _promotion_target_rollup_allowed(parent, target_branch)
                and not is_custom_staged_rollup_root(parent, project, target_branch)
            ):
                await _hold_rollup_for_branch_safety(
                    parent,
                    project,
                    session,
                    (
                        f"Rollup blocked: target branch '{target_branch}' is this "
                        f"project's promotion target. Configure a distinct "
                        "integration branch before landing fleet work."
                    ),
                    issue_code="rollup_target_matches_promotion_target",
                )
                return
            # Track the rollup PR number/url across the try block so the
            # exception handler can rescue a newly-created PR whose marker
            # commit failed (see orphan-PR rescue below).
            rollup_pr_number: int | None = None
            rollup_pr_url: str = ""
            # Initialize before the try so the rescue path in the except
            # handler can name the parent even if `get_github_token_for_task`
            # below raises before the canonical capture at line ~981. Without
            # these, the except handler's `parent_id_local` reference would
            # raise UnboundLocalError, masking the original exception.
            parent_id_local = parent.id
            project_id_local = parent.project_id
            try:
                gh_token, owner, repo = await get_github_token_for_task(
                    parent.id, session
                )

                # ── Phase B3 lock-window break ──
                # The next three GitHub calls (get_open_pull_requests,
                # get_branch_sha x2) are read-only verifications that can
                # collectively hang 5-30s during rate-limit. Release the
                # parent lock so concurrent heartbeats on sibling children
                # don't queue behind us.
                #
                # Audit Phase C4 (M-C1): with `expire_on_commit=False` (see
                # database.py), attributes on `parent` are NOT refreshed after
                # the commit below. Code between this release and the
                # `_relock_parent` call at the bottom of this block MUST treat
                # `parent` as snapshot-stale: only stable identifiers (id,
                # project_id) and the captured `pre_gh_snapshot` are safe to
                # read. Any mutable attribute (status, context, payloads,
                # latest_status_message) is potentially out-of-date and MUST
                # be re-read from the freshly-locked instance returned by
                # `_relock_parent`. The `parent_id_local` / `project_id_local`
                # captures below exist precisely so error logs and follow-up
                # GitHub calls don't accidentally trigger a stale read.
                parent_id_local = parent.id
                project_id_local = parent.project_id
                pre_gh_snapshot = await _release_parent_lock(parent, session)

                # Verify all PRs targeting the staging branch are resolved before rolling up
                from app.github import get_open_pull_requests

                try:
                    open_prs = await get_open_pull_requests(
                        gh_token, owner, repo, staging_branch
                    )
                except HTTPException as api_err:
                    if api_err.status_code == 401:
                        from app.routers.agent import invalidate_github_token_cache

                        invalidate_github_token_cache(str(project_id_local))
                        logger.warning(
                            f"[ROLLUP] Auth failure listing open PRs for {owner}/{repo} — retrying with fresh token"
                        )
                        gh_token, owner, repo = await get_github_token_for_task(
                            parent_id_local, session, force_refresh=True
                        )
                        open_prs = await get_open_pull_requests(
                            gh_token, owner, repo, staging_branch
                        )
                    elif api_err.status_code in (403, 500, 502, 503):
                        logger.warning(
                            f"[ROLLUP] GitHub error {api_err.status_code} listing open PRs — deferring to next cycle"
                        )
                        return
                    else:
                        raise

                # ── Drop orphan PRs left by CANCELLED children ──
                # A child task that was cancelled (e.g. requeue-replaced after a
                # timeout, or SCOPE_EMPTY pre-empted) can leave its PR open: no
                # live task will ever drive it to merge, so blocking the rollup
                # on it strands the parent in EVALUATING forever (incident
                # 2026-05-28). The cancel path can't always close it — the PR ref
                # may live under a non-canonical context key (e.g. a safeguard
                # PR) so close_orphan_pr_if_any finds nothing. Attribute each
                # open PR to its owning child by work-branch id-prefix and close
                # the ones owned by a cancelled child, then block only on the
                # rest. This runs in the lock-released window: the child query is
                # read-only and the GitHub close is independent of the DB lock.
                if open_prs:
                    try:
                        _child_rows = (
                            await session.execute(
                                select(Task.id, Task.status).where(
                                    Task.parent_task_id == parent_id_local
                                )
                            )
                        ).all()
                        _cancelled_prefixes = {
                            str(_cid).replace("-", "")[:8].lower()
                            for _cid, _cstatus in _child_rows
                            if _cstatus == TaskStatus.CANCELLED
                        }
                        _live_prs, _orphan_prs = _partition_orphan_prs(
                            open_prs, _cancelled_prefixes
                        )
                        if _orphan_prs:
                            from app.github import close_pull_request

                            for _opr in _orphan_prs:
                                _onum = _opr.get("number")
                                _oref = (_opr.get("head") or {}).get("ref", "")
                                try:
                                    await close_pull_request(
                                        gh_token,
                                        owner,
                                        repo,
                                        _onum,
                                        project_id=str(project_id_local),
                                    )
                                    logger.info(
                                        f"[ROLLUP] Closed orphan PR #{_onum} (head "
                                        f"'{_oref}') from a cancelled child — was "
                                        f"blocking rollup of {parent_id_local}"
                                    )
                                except Exception as _oerr:
                                    logger.warning(
                                        f"[ROLLUP] Failed to close orphan PR #{_onum} "
                                        f"for {parent_id_local}: {_oerr} — excluding "
                                        f"from the blocking set anyway"
                                    )
                            open_prs = _live_prs
                    except Exception as _orphan_exc:
                        # Never let orphan-cleanup crash the rollup cycle; fall
                        # back to the raw open-PR set (original behavior).
                        logger.warning(
                            f"[ROLLUP] Orphan-PR filtering failed for "
                            f"{parent_id_local}: {_orphan_exc} — using raw open-PR set"
                        )

                # Verify target branch exists before creating PR.
                # Distinguish 404 (actually deleted) from transient GitHub errors
                # (5xx, rate-limit) — only fail on confirmed 404.
                try:
                    await get_branch_sha(gh_token, owner, repo, target_branch)
                except HTTPException as branch_err:
                    if branch_err.status_code == 404:
                        raise Exception(
                            f"Target branch '{target_branch}' does not exist — "
                            f"cannot create rollup PR. This indicates a branch was "
                            f"deleted out of order."
                        ) from branch_err
                    # Transient GitHub error — defer to next evaluation cycle
                    logger.warning(
                        f"[ROLLUP] Transient GitHub error checking target branch "
                        f"'{target_branch}': {branch_err.detail} — will retry"
                    )
                    return

                # Verify staging (head) branch exists — if it was deleted
                # (e.g., by a child rollup that found an empty diff and cleaned
                # up, or by GitHub auto-delete after a prior PR merge), check
                # whether all children completed successfully. If so, the work
                # already landed on the target branch and we can complete.
                staging_branch_missing = False
                try:
                    await get_branch_sha(gh_token, owner, repo, staging_branch)
                except HTTPException as http_err:
                    if http_err.status_code == 404:
                        # Use `parent_id_local` (captured pre-release) instead
                        # of `parent.id` to make the snapshot-stale contract
                        # explicit: see audit-C4 (M-C1) note above.
                        logger.warning(
                            f"[ROLLUP] Staging branch '{staging_branch}' not found (404) for task {parent_id_local}"
                        )
                        staging_branch_missing = True
                    elif http_err.status_code in (401, 403):
                        logger.exception(
                            f"[ROLLUP] Auth error {http_err.status_code} checking staging branch — deferring"
                        )
                        return
                    else:
                        logger.warning(
                            f"[ROLLUP] GitHub {http_err.status_code} checking staging branch — deferring"
                        )
                        return
                except Exception as e:
                    logger.warning(
                        f"[ROLLUP] Error checking staging branch '{staging_branch}': {e} — deferring"
                    )
                    return

                # ── Phase B3: re-acquire the lock before applying mutations ──
                # The three GitHub reads above were performed without the lock.
                # Validate updated_at hasn't changed; if it has, another writer
                # mutated the parent during our window — defer to next cycle.
                parent = await _relock_parent(
                    session,
                    parent_id_local,
                    pre_gh_snapshot,
                    parent_in_session=parent,
                )
                if parent is None:
                    return
                parent_ctx = parent.context or {}
                # Re-validate parent status — a peer may have moved it past EVALUATING.
                if parent.status != TaskStatus.EVALUATING:
                    return

                if open_prs:
                    pr_nums = [f"#{p['number']}" for p in open_prs]
                    parent.latest_status_message = f"Rollup waiting: {len(open_prs)} open PR(s) targeting {staging_branch}: {', '.join(pr_nums)}"
                    session.add(parent)
                    await session.commit()
                    logger.info(
                        f"[ROLLUP] Parent {parent.id} has {len(open_prs)} open PR(s) targeting {staging_branch}: {', '.join(pr_nums)} — waiting"
                    )
                    return

                if staging_branch_missing:
                    # Check if all children completed — if so, work already landed
                    all_children_ok = completed_count > 0 and failed_count == 0
                    if all_children_ok:
                        logger.info(
                            f"[ROLLUP] Staging branch '{staging_branch}' gone but all "
                            f"{completed_count} children completed — work already on {target_branch}"
                        )
                        parent.status = TaskStatus.COMPLETED
                        parent.context = _clear_rollup_transient_state(parent.context)
                        parent.latest_status_message = (
                            f"All {completed_count} subtasks completed with no actionable "
                            f"changes — research produced no new code to merge"
                        )
                        # Note on the direction that this cycle found nothing actionable
                        research_payload = parent.first_payload
                        dir_id = research_payload.get("current_direction_id")
                        if dir_id:
                            def _append_empty_diff_note(directions: list) -> None:
                                for d in directions:
                                    if d.get("id") == dir_id:
                                        notes = d.get("notes") or []
                                        notes.append(
                                            {
                                                "text": "Research cycle completed with no actionable changes — all findings already implemented or not applicable.",
                                                "added_at": _utcnow().isoformat(),
                                                "source": "rollup_empty_diff",
                                            }
                                        )
                                        d["notes"] = notes[-10:]  # Keep last 10
                                        break
                            await with_directions_locked(session, parent.project_id, _append_empty_diff_note, skip_locked=False)
                        session.add(parent)
                        await session.commit()
                        await _cleanup_branches(parent, session)
                        await broadcast_to_project(
                            str(parent.project_id),
                            "task_updated",
                            {"task_id": str(parent.id), "status": TaskStatus.COMPLETED},
                        )
                        return
                    raise Exception(
                        f"Staging branch '{staging_branch}' does not exist and "
                        f"{failed_count} child(ren) failed — cannot create rollup PR"
                    )

                is_target_intermediate = (
                    project is not None and project.is_staging_branch(target_branch)
                )

                # Resume from prior crash — skip PR creation if already done
                existing_pr = parent_ctx.get("rollup_pr_number")
                # ── Duplicate-create guard ──
                # No PR marker yet, but a peer invocation may be mid-create: it
                # stakes ``rollup_pr_creating_at`` under the lock just before the
                # release-window around create_pull_request below. Observing a
                # FRESH claim here means a concurrent worker is already POSTing the
                # rollup PR, so defer rather than POST a duplicate (GitHub's
                # one-open-PR rule is best-effort — incident #7350/#7351). A stale
                # claim (peer crashed mid-create) past the TTL falls through and we
                # re-create.
                if not existing_pr:
                    _claim_ts = parent_ctx.get("rollup_pr_creating_at")
                    if isinstance(_claim_ts, (int, float)) and (
                        time.time() - _claim_ts
                    ) < _ROLLUP_PR_CREATE_CLAIM_TTL_S:
                        logger.info(
                            f"[ROLLUP] Parent {parent_id_local} rollup PR creation "
                            f"already in progress (claim {time.time() - _claim_ts:.0f}s "
                            f"old) — deferring to avoid a duplicate PR"
                        )
                        return
                if existing_pr:
                    # ── Phase B3: release lock around get_pull_request ──
                    pre_existing_pr_snapshot = await _release_parent_lock(
                        parent, session
                    )
                    pr_state: dict | None = None
                    pr_state_err: Exception | None = None
                    try:
                        pr_state = await get_pull_request(
                            gh_token, owner, repo, existing_pr
                        )
                    except Exception as e:
                        pr_state_err = e
                    # Re-acquire and validate
                    parent = await _relock_parent(
                        session,
                        parent_id_local,
                        pre_existing_pr_snapshot,
                        parent_in_session=parent,
                    )
                    if parent is None:
                        return
                    parent_ctx = parent.context or {}
                    if parent.status != TaskStatus.EVALUATING:
                        return
                    # Re-read existing_pr from fresh context (a peer may have cleared it)
                    existing_pr = parent_ctx.get("rollup_pr_number")
                    if pr_state_err is not None:
                        logger.warning(
                            f"[ROLLUP] Could not verify PR #{existing_pr} state: {pr_state_err} — proceeding with existing marker"
                        )
                    elif existing_pr is not None and pr_state is not None:
                        if pr_state.get("merged"):
                            logger.info(
                                f"[ROLLUP] PR #{existing_pr} already merged — classifying landing"
                            )
                            (_landed, _terminal_rollup_merge) = (
                                await _classify_merged_rollup_landing(
                                    parent,
                                    project,
                                    session,
                                    pr_number=existing_pr,
                                    target_branch=target_branch,
                                    source="rollup_management.existing_pr_merged",
                                )
                            )
                            parent.status = (
                                TaskStatus.COMPLETED
                                if _terminal_rollup_merge
                                else TaskStatus.EVALUATING
                            )
                            parent.context = {
                                **_clear_rollup_transient_state(parent_ctx),
                            }
                            if _landed:
                                apply_merged_landed_signal(
                                    parent,
                                    project,
                                    base_branch=target_branch,
                                    hotfix_lane=_promotion_target_rollup_allowed(
                                        parent, target_branch
                                    ),
                                    non_merge_reason="merged_pr_non_integration_branch",
                                )
                            if _terminal_rollup_merge:
                                parent.latest_status_message = (
                                    f"Rollup PR #{existing_pr} already merged"
                                )
                            else:
                                parent.latest_status_message = (
                                    f"Rollup PR #{existing_pr} already merged into "
                                    f"{target_branch}; awaiting downstream landing"
                                )
                            session.add(parent)
                            await session.commit()
                            await _cleanup_branches(parent, session)
                            await broadcast_to_project(
                                str(parent.project_id),
                                "task_updated",
                                {
                                    "task_id": str(parent.id),
                                    "status": parent.status,
                                },
                            )
                            return
                        if pr_state.get("state") == "closed":
                            # C4 (4th same-class site): a closed-not-merged
                            # rollup PR may be a TRANSIENT self-close from the
                            # orchestrator's mergeability-reset loop (close+reopen
                            # to force GitHub to recompute mergeable=None — see
                            # orchestrator.py's CI poll). The orchestrator records
                            # its own reset window via review_loop.pending_reopen
                            # on THIS parent (the rollup PR's review_loop, seeded
                            # at the large-rollup → review handoff). Clearing the
                            # rollup_pr_number/rollup_pr_url markers during that
                            # window strands the parent into re-creating a
                            # DUPLICATE rollup PR next cycle. Mirror the other
                            # three handlers: only treat closed as abandoned when
                            # there is NO pending_reopen reset marker; during the
                            # reset window DEFER and leave the markers intact so
                            # the reopen completes and the resume path retries.
                            _parent_loop = parent_ctx.get("review_loop", {}) or {}
                            if _parent_loop.get("pending_reopen"):
                                logger.info(
                                    f"[ROLLUP] PR #{existing_pr} closed-not-merged with "
                                    f"pending_reopen set — transient mergeability-reset, "
                                    f"deferring (markers preserved, will retry next cycle)"
                                )
                                # Leave markers intact; do NOT clear, do NOT create
                                # a duplicate. Return so we don't re-process a
                                # momentarily-closed PR (re-scan / re-spawn review)
                                # this cycle — the orchestrator's reopen lands and
                                # the next rollup sweep resumes with the same PR.
                                parent.latest_status_message = (
                                    f"Rollup PR #{existing_pr} mergeability-reset in "
                                    f"progress (closed/reopen) — deferring"
                                )
                                session.add(parent)
                                await session.commit()
                                return
                            else:
                                logger.warning(
                                    f"[ROLLUP] PR #{existing_pr} was closed without merge — clearing marker, will retry"
                                )
                                parent.context = {
                                    k: v
                                    for k, v in parent_ctx.items()
                                    if k not in ("rollup_pr_number", "rollup_pr_url")
                                }
                                parent_ctx = parent.context
                                existing_pr = None
                                session.add(parent)
                                await session.commit()

                if existing_pr:
                    rollup_pr_number = existing_pr
                    rollup_pr_url = parent_ctx.get("rollup_pr_url", "")
                    logger.info(
                        f"[ROLLUP] Resuming with existing PR #{rollup_pr_number}"
                    )
                else:
                    _prefix = _get_project_pr_title_prefix(project)
                    pr_title = f"{_prefix}Rollup: {parent.title}"
                    _dir_id = parent.first_payload.get(
                        "current_direction_id"
                    ) or parent_ctx.get("direction_id")
                    _dir_title = parent_ctx.get("direction_title") or parent.title
                    _completed_children = [
                        c for c in children if c.status == TaskStatus.COMPLETED
                    ]
                    pr_body = _build_rollup_pr_body(
                        completed_count,
                        staging_branch,
                        rollup_note,
                        direction_id=str(_dir_id) if _dir_id else None,
                        direction_title=_dir_title,
                        children=_completed_children,
                    )

                    # ── Phase B3: release lock around create_pull_request ──
                    # PR creation is the longest-running call in this function:
                    # 5-15s on the happy path, minutes during rate-limit. The
                    # rollup_pr_number/rollup_pr_url written below are the
                    # marker for crash-recovery, so the post-create re-acquire
                    # MUST validate and persist before any other writer can
                    # observe an empty marker.
                    project_id_str = str(parent.project_id)
                    # Stake a creation claim UNDER the lock before releasing it
                    # around the slow create_pull_request call below. A concurrent
                    # invocation that grabs the lock during the create window will
                    # observe this claim (the guard near ``existing_pr`` above) and
                    # defer instead of POSTing a duplicate rollup PR (#7350/#7351).
                    # context_dirty=True forces the JSONB write so the claim is
                    # actually persisted before the lock is released.
                    parent.context = {
                        **parent_ctx,
                        "rollup_pr_creating_at": time.time(),
                    }
                    parent_ctx = parent.context
                    pre_create_snapshot = await _release_parent_lock(
                        parent, session, context_dirty=True
                    )

                    pr_result = await create_pull_request(
                        gh_token,
                        owner,
                        repo,
                        title=pr_title,
                        head=staging_branch,
                        base=target_branch,
                        body=pr_body,
                        project_id=project_id_str,
                    )

                    parent = await _relock_parent(
                        session,
                        parent_id_local,
                        pre_create_snapshot,
                        parent_in_session=parent,
                    )
                    if parent is None:
                        # Concurrent writer mutated the parent during PR creation.
                        # The orphan-PR rescue path in the except handler will
                        # persist the marker via a fresh session if we raise.
                        # Re-fetch without lock and bail safely.
                        logger.info(
                            f"[ROLLUP] Parent {parent_id_local} mutated during create_pull_request — "
                            f"orphan-PR rescue will persist marker"
                        )
                        rollup_pr_number = pr_result.get("number")
                        rollup_pr_url = pr_result.get("html_url", "")
                        # Trigger orphan-PR rescue path. The sentinel class lets
                        # the outer handler short-circuit past the post-rescue
                        # parent.context manipulation that would NPE here.
                        raise _ParentMutatedDuringPRCreation(
                            f"Parent {parent_id_local} mutated during create_pull_request — deferring"
                        )
                    parent_ctx = parent.context or {}

                    # Empty diff — branches are identical, nothing to merge
                    if pr_result.get("empty_diff"):
                        logger.info(
                            f"[ROLLUP] Empty diff for {staging_branch} → {target_branch} — marking COMPLETED"
                        )
                        parent.status = TaskStatus.COMPLETED
                        parent.context = _clear_rollup_transient_state(parent.context)
                        parent.latest_status_message = f"✓ All {completed_count} subtasks completed — no new changes to merge (fixes already on {target_branch})"
                        try:
                            await delete_branch(gh_token, owner, repo, staging_branch)
                        except Exception as e:
                            logger.warning(
                                f"[ROLLUP] Failed to delete staging branch '{staging_branch}' for task {parent.id}: {e}"
                            )
                        session.add(parent)
                        await session.commit()
                        await _cleanup_branches(parent, session)
                        await broadcast_to_project(
                            str(parent.project_id),
                            "task_updated",
                            {"task_id": str(parent.id), "status": TaskStatus.COMPLETED},
                        )
                        return

                    rollup_pr_url = pr_result.get("html_url", "")
                    rollup_pr_number = pr_result.get("number")
                    logger.info(
                        f"[ROLLUP] Created PR #{rollup_pr_number}: {staging_branch} → {target_branch}"
                    )

                    # Commit marker immediately — if we crash after this point,
                    # restart will find the marker and skip PR creation. Drop the
                    # creation claim now that the durable marker supersedes it.
                    parent.context = {
                        **{
                            k: v
                            for k, v in parent_ctx.items()
                            if k != "rollup_pr_creating_at"
                        },
                        "rollup_pr_number": rollup_pr_number,
                        "rollup_pr_url": rollup_pr_url,
                    }
                    parent_ctx = parent.context  # Update local ref
                    session.add(parent)
                    await session.commit()

                # Re-acquire lock on parent for merge phase (marker commit released it).
                # skip_locked: rollup PR marker is already persisted, so if a concurrent
                # sibling holds the lock we defer safely — reaper sweep will retry within 15 min.
                parent_id = parent.id
                parent = (
                    (
                        await session.execute(
                            select(Task)
                            .where(Task.id == parent_id)
                            .with_for_update(skip_locked=True)
                        )
                    )
                    .scalars()
                    .first()
                )
                if parent is None:
                    logger.info(
                        f"[ROLLUP] Skipped merge phase for parent {parent_id} — lock held, "
                        "reaper sweep will retry"
                    )
                    return
                parent_ctx = parent.context or {}

                # Guard: concurrent sibling may have created a different rollup PR in the gap
                if parent_ctx.get("rollup_pr_number") != rollup_pr_number:
                    logger.info(
                        f"[ROLLUP] Concurrent sibling created rollup PR for parent {parent.id} — yielding"
                    )
                    return

                # Check if any child task was high-risk — if so, require approval on the final merge.
                # Refactor tasks are structural (move code, no behavior change) and should not
                # trigger the high-risk gate even when classified as risk='high' by plan agents.
                # Gate is controlled by runtime setting (default True) — disable for repos
                # that don't need human-in-the-loop approval.
                has_high_risk_child = any(
                    (c.context or {}).get("risk") == "high"
                    for c in children
                    if c.status == TaskStatus.COMPLETED
                    and (c.first_payload or {}).get("type")
                    != "refactor"  # refactors are structural, not behavioral risk
                )
                if (
                    has_high_risk_child
                    and not is_target_intermediate
                    and _runtime_config.get("require_approval_gates", True)
                ):
                    parent.requires_approval = True
                    parent.latest_status_message = (
                        f"All {completed_count} subtasks merged, rollup PR #{rollup_pr_number} awaiting approval "
                        f"(contains high-risk changes)"
                    )
                    parent.context = {
                        **parent_ctx,
                        "rollup_pr_url": rollup_pr_url,
                        "rollup_pr_number": rollup_pr_number,
                    }
                    session.add(parent)
                    # Snapshot status before the alert dispatch: _dispatch_rollup_
                    # approval_alert can poison/roll back the session and expire
                    # `parent` (rollback expires even with expire_on_commit=False),
                    # so a post-commit parent.status read would sync lazy-load in
                    # async ctx → MissingGreenlet.
                    _bcast_status = parent.status
                    await _dispatch_rollup_approval_alert(
                        parent,
                        session,
                        f"Rollup PR #{rollup_pr_number} awaiting approval — contains high-risk changes",
                        pr_number=rollup_pr_number,
                    )
                    await session.commit()
                    await broadcast_to_project(
                        str(parent.project_id),
                        "task_updated",
                        {"task_id": str(parent.id), "status": _bcast_status},
                    )
                    logger.info(
                        f"[ROLLUP] PR #{rollup_pr_number} has high-risk children — requires approval before merge to {target_branch}"
                    )
                    return

                # Auto-merge small rollups on intermediate branches; review large ones
                # Measure by total lines changed (additions + deletions)
                # ── Phase B3: release lock around diff-size check + close on zero-diff ──
                # get_pull_request can hang 5-30s; close_pull_request + delete_branch
                # are also synchronous I/O. Releasing the lock around this cluster
                # lets concurrent heartbeats on sibling children proceed.
                project_id_str = str(parent.project_id)
                pre_diff_snapshot = await _release_parent_lock(parent, session)
                try:
                    pr_detail = await get_pull_request(
                        gh_token,
                        owner,
                        repo,
                        rollup_pr_number,
                        project_id=project_id_str,
                    )
                    lines_changed = pr_detail.get("additions", 0) + pr_detail.get(
                        "deletions", 0
                    )
                except Exception as e:
                    logger.warning(
                        f"[ROLLUP] Failed to fetch PR #{rollup_pr_number} diff size for task {parent_id_local}: {e}"
                    )
                    lines_changed = 999  # assume large if we can't check
                    pr_detail = None

                # Zero-diff guard: squash-merged commits can produce a PR with commits
                # but no file changes. Close before spawning review/fix VMs.
                if pr_detail is not None and lines_changed == 0:
                    logger.info(
                        f"[ROLLUP] PR #{rollup_pr_number} has zero lines changed — closing and classifying landing"
                    )
                    try:
                        await close_pull_request(
                            gh_token,
                            owner,
                            repo,
                            rollup_pr_number,
                            project_id=project_id_str,
                        )
                    except Exception as e:
                        logger.warning(
                            f"[ROLLUP] Failed to close zero-diff PR #{rollup_pr_number}: {e}"
                        )
                    try:
                        await delete_branch(gh_token, owner, repo, staging_branch)
                    except Exception as e:
                        logger.warning(
                            f"[ROLLUP] Failed to delete staging branch '{staging_branch}' for task {parent_id_local}: {e}"
                        )
                    # Re-acquire to apply mutation
                    parent = await _relock_parent(
                        session,
                        parent_id_local,
                        pre_diff_snapshot,
                        parent_in_session=parent,
                    )
                    if parent is None:
                        return
                    (_landed, _terminal_rollup_merge) = (
                        await _classify_merged_rollup_landing(
                            parent,
                            project,
                            session,
                            pr_number=rollup_pr_number,
                            target_branch=target_branch,
                            source="rollup_management.zero_diff_close",
                        )
                    )
                    parent.status = (
                        TaskStatus.COMPLETED
                        if _terminal_rollup_merge
                        else TaskStatus.EVALUATING
                    )
                    if _terminal_rollup_merge:
                        parent.latest_status_message = (
                            f"✓ All {completed_count} subtasks completed — rollup PR "
                            f"#{rollup_pr_number} had no file changes (already on "
                            f"{target_branch})"
                        )
                    else:
                        parent.latest_status_message = (
                            f"✓ All {completed_count} subtasks completed — rollup PR "
                            f"#{rollup_pr_number} had no file changes (already on "
                            f"{target_branch}); awaiting downstream landing"
                        )
                    _cleared = _clear_rollup_transient_state(parent.context)
                    parent.context = {
                        **{
                            k: v
                            for k, v in _cleared.items()
                            if k not in ("rollup_pr_number", "rollup_pr_url")
                        },
                    }
                    if _landed:
                        apply_merged_landed_signal(
                            parent,
                            project,
                            base_branch=target_branch,
                            hotfix_lane=_promotion_target_rollup_allowed(
                                parent, target_branch
                            ),
                            non_merge_reason="merged_pr_non_integration_branch",
                        )
                    session.add(parent)
                    await session.commit()
                    await _cleanup_branches(parent, session)
                    await broadcast_to_project(
                        str(parent.project_id),
                        "task_updated",
                        {"task_id": str(parent.id), "status": parent.status},
                    )
                    return

                # Non-zero-diff path: re-acquire for downstream mutations.
                parent = await _relock_parent(
                    session,
                    parent_id_local,
                    pre_diff_snapshot,
                    parent_in_session=parent,
                )
                if parent is None:
                    return
                parent_ctx = parent.context or {}
                if parent.status != TaskStatus.EVALUATING:
                    return

                # If diff size is unverifiable (metadata fetch failed), defer the
                # review-spawn decision to the next rollup cycle rather than
                # assuming large and burning VM time on a PR whose size is
                # unknown. Previously unverifiable → lines_changed=999 → review
                # spawned on potentially-empty PRs (2026-04-17 audit: ~15
                # VM-hours/day wasted on the empty-rollup class).
                if pr_detail is None:
                    logger.info(
                        f"[ROLLUP] Deferring review for PR #{rollup_pr_number} — could not verify "
                        f"diff size; will retry next cycle"
                    )
                    parent.latest_status_message = f"Rollup PR #{rollup_pr_number} — diff size unverified, retrying next cycle"
                    session.add(parent)
                    await session.commit()
                    return

                is_small_rollup = lines_changed <= 100
                logger.info(
                    f"[ROLLUP] PR #{rollup_pr_number}: {lines_changed} lines changed, {'auto-merge' if is_small_rollup and is_target_intermediate else 'review'}"
                )
                if is_target_intermediate and is_small_rollup:
                    # ── Phase B3: release lock around security scan + comment + merge ──
                    # The scan inspects PR diffs (multiple GH API calls), the comment
                    # is a write, and the merge is the slowest call of all. Holding
                    # the lock across this cluster previously starved sibling-child
                    # heartbeats during minutes-long rate-limit windows.
                    pre_merge_snapshot = await _release_parent_lock(parent, session)
                    rollup_scan: dict | None = None
                    rollup_scan_err: Exception | None = None
                    try:
                        rollup_scan = await scan_pr_for_injection(
                            gh_token, owner, repo, rollup_pr_number
                        )
                        logger.info(f"[SECURITY] Rollup scan: {rollup_scan['summary']}")
                    except Exception as e:
                        rollup_scan_err = e
                        logger.error(
                            f"[SECURITY] Rollup scan failed for PR #{rollup_pr_number}, blocking: {e}",
                            exc_info=True,
                        )

                    # Validate the combined Alembic migration chain on the rollup
                    # branch before auto-merge. Two child PRs can each add a
                    # migration with the same revision off the same head; each
                    # passes its own CI in isolation but the merged rollup then
                    # has duplicate revisions / multiple heads, breaking
                    # `alembic upgrade head` on the integration branch for every
                    # subsequent task. Fail-safe: fetch/parse failure does not
                    # auto-merge. Runs in this unlocked window (GitHub I/O).
                    alembic_status, alembic_reason = await _validate_rollup_migration_chain(
                        gh_token, owner, repo, rollup_pr_number, target_branch
                    )

                    # Re-acquire to apply mutations / dispatch alerts.
                    parent = await _relock_parent(
                        session,
                        parent_id_local,
                        pre_merge_snapshot,
                        parent_in_session=parent,
                    )
                    if parent is None:
                        return
                    parent_ctx = parent.context or {}
                    if parent.status != TaskStatus.EVALUATING:
                        return

                    # C5 invariant: every requires_approval-set site below is
                    # gated on require_approval_gates. With gates OFF the operator
                    # has opted out of manual approval, so setting the flag would
                    # only let services.py's 2h approval-gate timeout FAIL the
                    # otherwise-healthy parent and discard already-merged siblings.
                    # The two block classes have different gates-off behaviour:
                    #   - scan-error (transient/non-deterministic): DEFER and retry
                    #     next cycle — the scan may succeed on a re-run; never merge
                    #     unscanned and never permanently block.
                    #   - scan-blocked (deterministic injection finding): fall
                    #     through to merge with a LOUD operator alert — re-blocking
                    #     every cycle would be a permanent strand, and gates-off
                    #     means the operator accepts auto-merge with post-hoc review.
                    _gates_on = _runtime_config.get("require_approval_gates", True)
                    if rollup_scan_err is not None:
                        if _gates_on:
                            parent.requires_approval = True
                            parent.latest_status_message = f"Rollup security scan failed — requires manual approval: {type(rollup_scan_err).__name__}: {rollup_scan_err!r}"[
                                :500
                            ]
                            parent.context = {
                                **parent_ctx,
                                "rollup_pr_url": rollup_pr_url,
                                "rollup_pr_number": rollup_pr_number,
                            }
                            session.add(parent)
                            await _dispatch_rollup_approval_alert(
                                parent,
                                session,
                                f"Rollup PR #{rollup_pr_number} security scan failed — requires manual approval: {type(rollup_scan_err).__name__}",
                                pr_number=rollup_pr_number,
                            )
                            await session.commit()
                            return
                        # Gates off: defer to the next rollup cycle (the scan error
                        # is transient; retrying is correct, blocking is not).
                        logger.warning(
                            f"[ROLLUP] PR #{rollup_pr_number} security scan failed "
                            f"({type(rollup_scan_err).__name__}) and gates are off — "
                            f"deferring to next cycle for re-scan (not blocking)"
                        )
                        parent.latest_status_message = (
                            f"Rollup PR #{rollup_pr_number} — security scan failed "
                            f"({type(rollup_scan_err).__name__}); retrying next cycle"
                        )[:500]
                        parent.context = {
                            **parent_ctx,
                            "rollup_pr_url": rollup_pr_url,
                            "rollup_pr_number": rollup_pr_number,
                        }
                        session.add(parent)
                        await session.commit()
                        return

                    if rollup_scan["blocked"]:
                        if _gates_on:
                            parent.requires_approval = True
                            parent.latest_status_message = (
                                f"Rollup security scan blocked: {rollup_scan['summary']}"
                            )
                            parent.context = {
                                **parent_ctx,
                                "rollup_pr_url": rollup_pr_url,
                                "rollup_pr_number": rollup_pr_number,
                            }
                            # Persist + commit BEFORE the comment (releases lock for I/O).
                            session.add(parent)
                            await _dispatch_rollup_approval_alert(
                                parent,
                                session,
                                f"Rollup PR #{rollup_pr_number} blocked by security scan: {rollup_scan['summary']}",
                                pr_number=rollup_pr_number,
                            )
                            await session.commit()
                            # Comment after the lock is released — it's an external side-effect.
                            if rollup_scan["findings"]:
                                try:
                                    await create_pr_comment(
                                        gh_token,
                                        owner,
                                        repo,
                                        rollup_pr_number,
                                        rollup_scan["report"],
                                    )
                                except Exception as e:
                                    logger.warning(
                                        f"[ROLLUP] Failed to post scan-block comment on PR #{rollup_pr_number}: {e}"
                                    )
                            return
                        # Gates off: do NOT re-block every cycle. Fire a loud
                        # operator alert (so the deterministic finding is visible)
                        # and fall through to the auto-merge below. The scan report
                        # is still posted to the PR for post-hoc review.
                        logger.warning(
                            f"[ROLLUP] PR #{rollup_pr_number} security scan BLOCKED "
                            f"({rollup_scan['summary']}) but gates are off — alerting "
                            f"and proceeding to merge (post-hoc review via PR comment)"
                        )
                        await _dispatch_rollup_approval_alert(
                            parent,
                            session,
                            f"Rollup PR #{rollup_pr_number} security scan flagged "
                            f"({rollup_scan['summary']}) — auto-merging anyway "
                            f"(require_approval_gates off); review post-hoc",
                            pr_number=rollup_pr_number,
                        )
                        await session.commit()
                        if rollup_scan["findings"]:
                            try:
                                await create_pr_comment(
                                    gh_token,
                                    owner,
                                    repo,
                                    rollup_pr_number,
                                    rollup_scan["report"],
                                )
                            except Exception as e:
                                logger.warning(
                                    f"[ROLLUP] Failed to post scan-block comment on PR #{rollup_pr_number}: {e}"
                                )
                        # Re-acquire the parent lock dropped by the alert dispatch
                        # path before falling through to the alembic guard + merge.
                        parent = await _relock_parent(
                            session,
                            parent_id_local,
                            pre_merge_snapshot,
                            parent_in_session=parent,
                        )
                        if parent is None:
                            return
                        parent_ctx = parent.context or {}
                        if parent.status != TaskStatus.EVALUATING:
                            return
                    if rollup_scan["findings"]:
                        # Findings but not blocked — post comment outside lock window
                        # is fine; we don't mutate the parent for this case.
                        try:
                            await create_pr_comment(
                                gh_token,
                                owner,
                                repo,
                                rollup_pr_number,
                                rollup_scan["report"],
                            )
                        except Exception as e:
                            logger.warning(
                                f"[ROLLUP] Failed to post scan comment on PR #{rollup_pr_number}: {e}"
                            )

                    # Alembic combined-chain guard (parent is locked here).
                    if alembic_status == "defer":
                        logger.info(
                            f"[ROLLUP] Deferring auto-merge for PR #{rollup_pr_number} — "
                            f"could not validate Alembic chain: {alembic_reason}"
                        )
                        parent.latest_status_message = (
                            f"Rollup PR #{rollup_pr_number} — Alembic chain unverified, "
                            f"retrying next cycle"
                        )[:500]
                        session.add(parent)
                        await session.commit()
                        return
                    if alembic_status == "block":
                        # Before the operator-alert / agent-fix fallback, try a
                        # bounded automatic reconciliation of a PURE mechanical
                        # collision: a single added migration that simply needs a
                        # new revision number + a rechain onto the current head.
                        # Anything ambiguous (multi-migration, body conflict,
                        # parse failure, non-resolving renumber) yields no plan
                        # and falls through unchanged. The per-PR counter caps
                        # retries so a non-mechanical block never loops.
                        _ar_pr = parent_ctx.get("_alembic_autoreconcile_pr")
                        _ar_count = (
                            int(parent_ctx.get("_alembic_autoreconcile_count", 0))
                            if _ar_pr == rollup_pr_number
                            else 0
                        )
                        if _ar_count < _ALEMBIC_AUTORECONCILE_CAP:
                            _reconciled = await _attempt_alembic_autoreconcile(
                                parent,
                                session,
                                gh_token,
                                owner,
                                repo,
                                rollup_pr_number,
                                rollup_pr_url,
                                staging_branch,
                                target_branch,
                                parent_ctx,
                                _ar_count,
                            )
                            if _reconciled:
                                return
                        # C5: gate the approval flag on require_approval_gates.
                        # A broken combined migration chain is deterministic and
                        # would break `alembic upgrade head` for the whole project,
                        # so we must NOT auto-merge it regardless of the gate — but
                        # with gates OFF we also must NOT set requires_approval
                        # (services.py would 2h-timeout-FAIL the parent). Instead we
                        # always spawn the re-chain fix below; gates-off relies on
                        # that fix landing rather than on manual approval. The merge
                        # is held either way (this branch returns before the merge).
                        _gates_on = _runtime_config.get("require_approval_gates", True)
                        if _gates_on:
                            parent.requires_approval = True
                        parent.latest_status_message = (
                            f"Rollup PR #{rollup_pr_number} blocked — invalid Alembic "
                            f"migration chain: {alembic_reason}"
                        )[:500]
                        parent.context = {
                            **parent_ctx,
                            "rollup_pr_url": rollup_pr_url,
                            "rollup_pr_number": rollup_pr_number,
                        }
                        session.add(parent)
                        await _dispatch_rollup_approval_alert(
                            parent,
                            session,
                            f"Rollup PR #{rollup_pr_number} blocked — invalid Alembic "
                            f"migration chain: {alembic_reason}"
                            + ("" if _gates_on else " (gates off — re-chain fix spawned)"),
                            pr_number=rollup_pr_number,
                        )
                        await session.commit()
                        _alembic_fix_prompt = (
                            f"Rollup PR #{rollup_pr_number} merging {staging_branch} into "
                            f"{target_branch} has an invalid combined Alembic migration "
                            f"chain: {alembic_reason}. Two sibling PRs added migrations "
                            f"that collide. Do NOT modify any already-landed migration "
                            f"(applied history is immutable); instead rename and re-chain "
                            f"your migration to the next free NNNN revision with "
                            f"down_revision pointing at the current head, then bump "
                            f"EXPECTED_SCHEMA_REVISION in backend/app/main.py to match."
                        )
                        await _spawn_pipeline_task(
                            parent,
                            "fix",
                            session,
                            rollup_pr_number,
                            staging_branch,
                            target_branch,
                            fix_prompt=_alembic_fix_prompt,
                            original_task_id=str(parent.id),
                        )
                        logger.warning(
                            f"[ROLLUP] PR #{rollup_pr_number} blocked — invalid Alembic "
                            f"chain ({alembic_reason}) — held for approval, spawned re-chain fix"
                        )
                        return

                    # Release lock around the actual merge call (slowest of all).
                    pre_actual_merge_snapshot = await _release_parent_lock(parent, session)
                    # NOTE: A pre-merge "is the PR behind base?" check used to live
                    # here, gated on (target_tip_sha != head_sha). That gate was
                    # always true for any PR with commits (head is by definition
                    # ≥1 commit ahead of base), so it triggered update_branch on
                    # every sweep → GitHub 422 "no new commits on base" → counted
                    # as failure → spawned fix tasks with the false premise that
                    # the PR was behind. The authoritative "behind" check lives
                    # post-merge at the `_ms == "behind"` branch below — GitHub's
                    # mergeable_state distinguishes real behind from
                    # head-ahead-of-base. Removed 2026-05-26 (PR #6096 incident).
                    merge_result = await merge_pull_request(gh_token, owner, repo, rollup_pr_number, project_id=str(parent.project_id))
                    parent = await _relock_parent(
                        session,
                        parent_id_local,
                        pre_actual_merge_snapshot,
                        parent_in_session=parent,
                    )
                    if parent is None:
                        # Concurrent mutation; merge call already completed externally,
                        # next sweep will pick up the merged state.
                        return
                    parent_ctx = parent.context or {}
                    if merge_result.get("merged"):
                        (_landed, _terminal_rollup_merge) = (
                            await _classify_merged_rollup_landing(
                                parent,
                                project,
                                session,
                                pr_number=rollup_pr_number,
                                target_branch=target_branch,
                                source="rollup_management.auto_merge_success",
                            )
                        )
                        parent.status = (
                            TaskStatus.COMPLETED
                            if _terminal_rollup_merge
                            else TaskStatus.EVALUATING
                        )
                        parent.context = {
                            **_clear_rollup_transient_state(parent_ctx),
                            "rollup_pr_url": rollup_pr_url,
                        }
                        if _landed:
                            apply_merged_landed_signal(
                                parent,
                                project,
                                base_branch=target_branch,
                                hotfix_lane=_promotion_target_rollup_allowed(
                                    parent, target_branch
                                ),
                                non_merge_reason="merged_pr_non_integration_branch",
                            )
                        if _terminal_rollup_merge:
                            parent.latest_status_message = (
                                f"All {completed_count} subtasks merged, rollup PR auto-merged"
                            )
                        else:
                            parent.latest_status_message = (
                                f"All {completed_count} subtasks merged into "
                                f"{target_branch}; awaiting downstream landing"
                            )
                        session.add(parent)
                        await session.commit()
                        try:
                            await delete_branch(gh_token, owner, repo, staging_branch)
                        except Exception as del_err:
                            logger.warning(
                                f"[ROLLUP] Failed to delete staging branch {staging_branch}: {del_err}"
                            )
                        logger.info(f"[ROLLUP] Auto-merged rollup into {target_branch}")
                        try:
                            from app.notification_dispatch import post_pr_merged_message

                            _direction_id = (parent.context or {}).get("direction_id")
                            _direction_text = "unknown direction"
                            if _direction_id:
                                _proj = await session.get(Project, parent.project_id)
                                if _proj:
                                    for _d in _proj.directions or []:
                                        if str(_d.get("id", "")) == str(_direction_id):
                                            _direction_text = _d.get(
                                                "text", "unknown direction"
                                            )
                                            break
                            await post_pr_merged_message(
                                session,
                                parent.project_id,
                                rollup_pr_number,
                                _direction_text,
                                completed_count,
                                parent.id,
                            )
                        except Exception as _e:
                            logger.warning(
                                f"[NOTIFY] Failed to post PR merged message: {_e}"
                            )
                        try:
                            from app.github import get_pull_request_diff
                            from app.viewport_coverage import (
                                update_viewport_coverage_snapshot_from_diff,
                            )

                            _snap_proj = await session.get(Project, parent.project_id)
                            if _snap_proj is not None:
                                _diff_text = await get_pull_request_diff(
                                    gh_token, owner, repo, rollup_pr_number
                                )
                                await update_viewport_coverage_snapshot_from_diff(
                                    session, _snap_proj, rollup_pr_number, _diff_text
                                )
                        except Exception as _snap_e:
                            logger.warning(
                                f"[ROLLUP] Failed to update viewport_coverage_snapshot: {_snap_e}"
                            )
                        await session.commit()
                        if _terminal_rollup_merge:
                            await _cleanup_branches(parent, session)
                            await broadcast_to_project(
                                str(parent.project_id),
                                "task_updated",
                                {
                                    "task_id": str(parent.id),
                                    "status": TaskStatus.COMPLETED,
                                },
                            )
                        return
                    else:
                        # Merge failed (conflicts) — spawn review/fix cycle instead of giving up
                        merge_msg = merge_result.get("message", "unknown")
                        # Before spawning a fix task, check if the PR is merely
                        # behind its base (common when concurrent rollups land on
                        # the same target). If so, update-branch via GitHub API
                        # and let the next rollup sweep re-attempt the merge.
                        _pr_refetch_failed = False
                        try:
                            _pr_detail = await get_pull_request(
                                gh_token,
                                owner,
                                repo,
                                rollup_pr_number,
                                project_id=str(parent.project_id),
                            )
                        except Exception as _pr_exc:
                            _pr_detail = {}
                            _pr_refetch_failed = True
                            logger.debug(
                                f"[ROLLUP] Could not refetch PR #{rollup_pr_number} for behind-check: {_pr_exc}"
                            )
                        # PR refetch retry budget: transient GitHub API errors (500,
                        # 503, network timeouts) shouldn't burn a fix VM. Defer the
                        # sweep on early failures; only fall through to fix-spawn
                        # once consecutive failures exhaust the budget.
                        if _pr_refetch_failed:
                            _refetch_failures = (
                                parent_ctx.get("_refetch_failure_count", 0) + 1
                            )
                            _refetch_budget = _runtime_config.get(
                                "rollup_refetch_failure_retries", 3
                            )
                            if _refetch_failures < _refetch_budget:
                                parent.context = {
                                    **parent_ctx,
                                    "rollup_pr_url": rollup_pr_url,
                                    "rollup_pr_number": rollup_pr_number,
                                    "_refetch_failure_count": _refetch_failures,
                                }
                                parent.latest_status_message = (
                                    f"Rollup PR #{rollup_pr_number} refetch failed after merge attempt "
                                    f"— deferring to next sweep ({_refetch_failures}/{_refetch_budget})"
                                )
                                session.add(parent)
                                await session.commit()
                                logger.info(
                                    f"[ROLLUP] PR #{rollup_pr_number} refetch failed after failed merge "
                                    f"(attempt {_refetch_failures}/{_refetch_budget}) — deferring fix spawn"
                                )
                                return
                            parent_ctx = {
                                k: v
                                for k, v in parent_ctx.items()
                                if k != "_refetch_failure_count"
                            }
                            logger.warning(
                                f"[ROLLUP] PR #{rollup_pr_number} refetch failed {_refetch_failures} consecutive "
                                f"sweeps — falling through to fix spawn"
                            )
                        elif "_refetch_failure_count" in parent_ctx:
                            parent_ctx = {
                                k: v
                                for k, v in parent_ctx.items()
                                if k != "_refetch_failure_count"
                            }
                        # Classify mergeable_state before assuming this is a real
                        # conflict. Only 'dirty' (and 'behind' as fall-through if
                        # update_branch fails) warrant a fix VM. 'blocked' and
                        # 'unstable' resolve when checks/reviews complete; 'unknown'
                        # means GitHub is still computing — all should be retried by
                        # the next rollup sweep instead of burning a fix cycle.
                        _ms = (_pr_detail.get("mergeable_state") or "").lower()
                        # Budget-exhausted refetch-failed: _pr_detail is {} so _ms="".
                        # We already exhausted the retry budget above; treat it as an
                        # un-classifiable failed merge and fall through to fix-spawn
                        # instead of entering the deferral path (which would loop
                        # forever — next sweep would also have an empty _pr_detail
                        # if the API is persistently failing).
                        if _pr_refetch_failed:
                            logger.warning(
                                f"[ROLLUP] PR #{rollup_pr_number} refetch budget exhausted "
                                f"— skipping state-classification, falling through to fix-spawn"
                            )
                        elif _ms == "behind":
                            # Before escalating to update_branch, check if the
                            # ancestor test already considers this PR clean.
                            # mergeable_state="behind" doesn't short-circuit
                            # pr_is_not_behind so is_ancestor_of is consulted.
                            _behind_head_sha = (
                                _pr_detail.get("head", {}).get("sha") or None
                            )
                            _post_not_behind = await pr_is_not_behind_for(
                                gh_token,
                                owner,
                                repo,
                                pr_detail=_pr_detail,
                                base_branch=target_branch,
                                project_id=str(parent.project_id),
                                call_site="rollup_post_merge",
                            )
                            if _post_not_behind is True:
                                logger.info(
                                    f"[ROLLUP] PR #{rollup_pr_number} "
                                    f"mergeable_state=behind but "
                                    f"pr_is_not_behind=True — skipping "
                                    f"update_branch, deferring to next sweep"
                                )
                                return
                            _upd = await update_branch_with_sha_retry(
                                gh_token,
                                owner,
                                repo,
                                rollup_pr_number,
                                expected_head_sha=_behind_head_sha,
                                project_id=str(parent.project_id),
                            )
                            if _upd.get("updated"):
                                # Reset the behind-retry counter — update succeeded, so any
                                # prior transient failures were just that, transient.
                                _new_ctx = {
                                    **parent_ctx,
                                    "rollup_pr_url": rollup_pr_url,
                                    "rollup_pr_number": rollup_pr_number,
                                }
                                _new_ctx.pop("_behind_update_retries", None)
                                parent.context = _new_ctx
                                parent.latest_status_message = (
                                    f"Rollup PR #{rollup_pr_number} was behind {target_branch} — updated branch, "
                                    f"awaiting fresh CI; next rollup sweep will re-attempt merge"
                                )
                                session.add(parent)
                                await session.commit()
                                logger.info(
                                    f"[ROLLUP] PR #{rollup_pr_number} behind {target_branch} — update_branch succeeded, skipped fix spawn"
                                )
                                return
                            if _upd.get("not_behind"):
                                # GitHub's cached mergeable_state was stale — the PR
                                # is not actually behind (no new commits on base).
                                # Treat as a successful no-op: clear any prior retry
                                # counter and defer to the next sweep.
                                _new_ctx = {
                                    **parent_ctx,
                                    "rollup_pr_url": rollup_pr_url,
                                    "rollup_pr_number": rollup_pr_number,
                                }
                                _new_ctx.pop("_behind_update_retries", None)
                                parent.context = _new_ctx
                                parent.latest_status_message = (
                                    f"Rollup PR #{rollup_pr_number} not behind {target_branch} "
                                    f"(no new commits on base) — cached mergeable_state was stale, "
                                    f"deferring to next sweep"
                                )
                                session.add(parent)
                                await session.commit()
                                logger.info(
                                    f"[ROLLUP] PR #{rollup_pr_number} behind {target_branch} — "
                                    f"update_branch reports no new commits on base, deferring to next sweep"
                                )
                                return
                            # update_branch failed — this is a transient GitHub API error,
                            # not a real merge conflict. Defer like the non-dirty path
                            # (counter + status message), retry on the next sweep, and
                            # only fall through to fix spawn once the retry budget is
                            # exhausted. Prevents premature fix VMs from wasting a
                            # review/fix cycle on a PR that just needs another API call.
                            _behind_retries = (
                                parent_ctx.get("_behind_update_retries", 0) + 1
                            )
                            _behind_budget = _runtime_config.get(
                                "rollup_behind_update_retries", 3
                            )
                            _upd_msg = _upd.get("message") or "unknown"
                            if _behind_retries < _behind_budget:
                                parent.context = {
                                    **parent_ctx,
                                    "rollup_pr_url": rollup_pr_url,
                                    "rollup_pr_number": rollup_pr_number,
                                    "_behind_update_retries": _behind_retries,
                                }
                                parent.latest_status_message = (
                                    f"Rollup PR #{rollup_pr_number} behind {target_branch} — update_branch failed "
                                    f"({_upd_msg}); retrying next sweep ({_behind_retries}/{_behind_budget})"
                                )
                                session.add(parent)
                                await session.commit()
                                logger.info(
                                    f"[ROLLUP] PR #{rollup_pr_number} behind {target_branch} — update_branch failed "
                                    f"(attempt {_behind_retries}/{_behind_budget}): {_upd_msg} — deferring to next sweep"
                                )
                                return
                            await _maybe_create_failure_alert(
                                parent,
                                session,
                                f"Rollup PR #{rollup_pr_number} behind {target_branch} — update_branch failed "
                                f"{_behind_retries} consecutive sweeps ({_upd_msg}); falling through to fix spawn",
                            )
                            # Clear the counter so a future behind state starts fresh.
                            parent_ctx = {
                                k: v
                                for k, v in parent_ctx.items()
                                if k != "_behind_update_retries"
                            }
                            logger.warning(
                                f"[ROLLUP] PR #{rollup_pr_number} behind {target_branch} — update_branch failed "
                                f"{_behind_retries} times ({_upd_msg}); falling through to fix spawn"
                            )
                        elif _ms != "dirty":
                            # Non-conflict states: known stalls (blocked/unstable/unknown)
                            # or any unrecognized value GitHub may introduce. All defer
                            # to the next rollup sweep instead of spawning a fix. Track
                            # consecutive deferrals so silent stalls surface with an
                            # alert (and escalate to approval) instead of waiting out
                            # the 6h hard timeout. Refetch-failed+budget-exhausted cases
                            # are handled above and never reach this branch.
                            _ms_key = _ms or "empty"
                            _ms_display = _ms or "missing (field absent)"
                            _is_unrecognized = _ms_key not in (
                                "blocked",
                                "unstable",
                                "unknown",
                                "empty",
                            )
                            # For blocked/unstable, ask GitHub to auto-merge once gates
                            # clear — mirrors the task_lifecycle auto-merge fallback so a
                            # passing rollup PR lands without waiting on the reaper's
                            # 15-minute stale-parent sweep. Deferral counters below still
                            # run so a genuinely stuck PR still escalates to approval.
                            if _ms_key in ("blocked", "unstable"):
                                # Migration-carrying rollup PRs must NOT be handed to
                                # GitHub native auto-merge: the target branch can advance
                                # while checks are pending, so the eventual merge could land
                                # against a different migration chain than the one Blueprint
                                # validated. Skip delegation and fall through to the
                                # deferral / re-validation path so _validate_rollup_migration_chain
                                # re-runs against the live target_branch on the next sweep.
                                if await pr_touches_migrations(
                                    gh_token, owner, repo, rollup_pr_number
                                ):
                                    logger.info(
                                        f"[ROLLUP] PR #{rollup_pr_number} mergeable_state={_ms_key} "
                                        f"touches Alembic migrations — skipping GitHub auto-merge; "
                                        f"deferring for re-validation against {target_branch}"
                                    )
                                else:
                                    try:
                                        from app.github import enable_auto_merge

                                        am_result = await enable_auto_merge(
                                            gh_token,
                                            owner,
                                            repo,
                                            rollup_pr_number,
                                            project_id=str(parent.project_id),
                                        )
                                        if am_result.get("enabled"):
                                            logger.info(
                                                f"[ROLLUP] PR #{rollup_pr_number} mergeable_state={_ms_key} "
                                                f"— enabled GitHub auto-merge"
                                            )
                                        else:
                                            logger.info(
                                                f"[ROLLUP] PR #{rollup_pr_number} auto-merge enablement "
                                                f"declined: {am_result.get('message')}"
                                            )
                                    except Exception as _am_exc:
                                        logger.warning(
                                            f"[ROLLUP] Auto-merge enablement failed for PR "
                                            f"#{rollup_pr_number}: {_am_exc}"
                                        )
                            prev_state = parent_ctx.get("_merge_deferred_state")
                            deferred_count = (
                                (parent_ctx.get("_merge_deferred_count", 0) + 1)
                                if prev_state == _ms_key
                                else 1
                            )
                            # Total-deferral counter: increments on every deferral regardless
                            # of state change. Prevents a flapping PR (blocked↔unstable↔…) from
                            # evading the per-state threshold forever.
                            total_deferrals = (
                                parent_ctx.get("_merge_total_deferrals", 0) + 1
                            )
                            now = _utcnow()
                            first_deferred_at = (
                                parent_ctx.get("_merge_first_deferred_at")
                                or now.isoformat()
                            )
                            try:
                                from datetime import datetime as _dt

                                _first_dt = _dt.fromisoformat(first_deferred_at)
                                elapsed_minutes = (
                                    now - _first_dt
                                ).total_seconds() / 60.0
                            except (TypeError, ValueError):
                                elapsed_minutes = 0.0
                            # 'unknown' / empty = GitHub stuck computing mergeability or refetch
                            # failing; unrecognized = GitHub returned a new state we don't know
                            # how to interpret. All escalate faster than 'blocked'/'unstable'
                            # which legitimately wait for checks/reviews.
                            _fast_escalate = _ms_key not in ("blocked", "unstable")
                            alert_threshold = _runtime_config.get(
                                "rollup_merge_deferred_alert_unknown"
                                if _fast_escalate
                                else "rollup_merge_deferred_alert",
                                5 if _fast_escalate else 10,
                            )
                            approval_threshold = _runtime_config.get(
                                "rollup_merge_deferred_approval", 30
                            )
                            total_alert_threshold = alert_threshold * 2
                            total_approval_threshold = approval_threshold * 2
                            time_alert_minutes = _runtime_config.get(
                                "rollup_merge_deferred_time_alert_minutes", 30
                            )
                            time_approval_minutes = _runtime_config.get(
                                "rollup_merge_deferred_time_approval_minutes", 60
                            )
                            # Per-state alert latch: tracks which states have already
                            # fired an alert so a flapping PR (blocked→unstable→blocked)
                            # doesn't re-fire the per-state alert each time a state
                            # re-crosses its threshold. A new state triggers a fresh
                            # alert; a state that's already alerted stays quiet.
                            alerted_states = set(
                                parent_ctx.get("_merge_alerted_states") or []
                            )
                            parent.context = {
                                **parent_ctx,
                                "rollup_pr_url": rollup_pr_url,
                                "rollup_pr_number": rollup_pr_number,
                                "_merge_deferred_count": deferred_count,
                                "_merge_deferred_state": _ms_key,
                                "_merge_total_deferrals": total_deferrals,
                                "_merge_first_deferred_at": first_deferred_at,
                                "_merge_alerted_states": sorted(alerted_states),
                            }
                            parent.latest_status_message = (
                                f"Rollup PR #{rollup_pr_number} merge deferred (mergeable_state={_ms_display}, "
                                f"deferral {deferred_count}/total {total_deferrals}) — next rollup sweep will re-attempt"
                            )
                            # Per-state alert: fires once per state when its counter
                            # crosses the alert threshold. Use `>= threshold AND not
                            # already alerted` so a state that crosses the threshold
                            # in a previous sweep doesn't re-fire this sweep. Alert/
                            # approval dispatch is wrapped in try/except so dispatch
                            # failures don't prevent the counter-persisting commit —
                            # otherwise counters reset each sweep and thresholds never fire.
                            if (
                                deferred_count >= alert_threshold
                                and _ms_key not in alerted_states
                            ):
                                try:
                                    await _maybe_create_failure_alert(
                                        parent,
                                        session,
                                        f"Rollup PR #{rollup_pr_number} stuck in mergeable_state={_ms_display} "
                                        f"for {deferred_count} consecutive sweeps",
                                    )
                                except Exception as _alert_err:
                                    logger.warning(
                                        f"[ROLLUP] Failed to create per-state deferral alert for PR #{rollup_pr_number}: {_alert_err}"
                                    )
                                alerted_states.add(_ms_key)
                                parent.context = {
                                    **parent.context,
                                    "_merge_alerted_states": sorted(alerted_states),
                                }
                            # Total-count alert: fires once when total crosses threshold,
                            # catching state-flapping PRs that evade the per-state counter.
                            if (
                                total_deferrals >= total_alert_threshold
                                and not parent_ctx.get("_merge_total_alerted")
                            ):
                                try:
                                    await _maybe_create_failure_alert(
                                        parent,
                                        session,
                                        f"Rollup PR #{rollup_pr_number} merge deferred {total_deferrals} times across "
                                        f"states (flapping) — manual investigation required",
                                    )
                                except Exception as _alert_err:
                                    logger.warning(
                                        f"[ROLLUP] Failed to create total-deferral alert for PR #{rollup_pr_number}: {_alert_err}"
                                    )
                                parent.context = {
                                    **parent.context,
                                    "_merge_total_alerted": True,
                                }
                            # Time-based alert: fires once after total deferral time crosses threshold
                            if (
                                elapsed_minutes >= time_alert_minutes
                                and not parent_ctx.get("_merge_time_alerted")
                            ):
                                try:
                                    await _maybe_create_failure_alert(
                                        parent,
                                        session,
                                        f"Rollup PR #{rollup_pr_number} merge has been deferred for "
                                        f"{elapsed_minutes:.0f} minutes — manual investigation required",
                                    )
                                except Exception as _alert_err:
                                    logger.warning(
                                        f"[ROLLUP] Failed to create time-based deferral alert for PR #{rollup_pr_number}: {_alert_err}"
                                    )
                                parent.context = {
                                    **parent.context,
                                    "_merge_time_alerted": True,
                                }
                            # Approval gate: any of per-state, total, or time thresholds can fire it.
                            # Only one approval dispatch — the first condition wins. Discretionary:
                            # only gate when require_approval_gates is on. With gates off the
                            # per-state/total/time *alerts* above still fire (visibility preserved),
                            # but we do NOT approval-gate the parent — otherwise services.py's 2h
                            # approval-gate timeout would FAIL a healthy EVALUATING parent whose
                            # rollup PR is merely deferred. The evaluating_fail_hours backstop
                            # handles a genuinely stuck rollup. (Mirrors the review-cap/respawn-cap
                            # gating elsewhere in this file.)
                            if (
                                _runtime_config.get("require_approval_gates", True)
                                and not parent.requires_approval
                            ):
                                if deferred_count >= approval_threshold:
                                    parent.requires_approval = True
                                    try:
                                        await _dispatch_rollup_approval_alert(
                                            parent,
                                            session,
                                            f"Rollup PR #{rollup_pr_number} stuck in mergeable_state={_ms_display} "
                                            f"for {deferred_count} sweeps — manual investigation required",
                                            pr_number=rollup_pr_number,
                                        )
                                    except Exception as _alert_err:
                                        logger.warning(
                                            f"[ROLLUP] Failed to dispatch per-state approval alert for PR #{rollup_pr_number}: {_alert_err}"
                                        )
                                elif total_deferrals >= total_approval_threshold:
                                    parent.requires_approval = True
                                    try:
                                        await _dispatch_rollup_approval_alert(
                                            parent,
                                            session,
                                            f"Rollup PR #{rollup_pr_number} deferred {total_deferrals} times across "
                                            f"states — manual investigation required",
                                            pr_number=rollup_pr_number,
                                        )
                                    except Exception as _alert_err:
                                        logger.warning(
                                            f"[ROLLUP] Failed to dispatch total-deferral approval alert for PR #{rollup_pr_number}: {_alert_err}"
                                        )
                                elif elapsed_minutes >= time_approval_minutes:
                                    parent.requires_approval = True
                                    try:
                                        await _dispatch_rollup_approval_alert(
                                            parent,
                                            session,
                                            f"Rollup PR #{rollup_pr_number} has been deferred for "
                                            f"{elapsed_minutes:.0f} minutes — manual investigation required",
                                            pr_number=rollup_pr_number,
                                        )
                                    except Exception as _alert_err:
                                        logger.warning(
                                            f"[ROLLUP] Failed to dispatch time-based approval alert for PR #{rollup_pr_number}: {_alert_err}"
                                        )
                            session.add(parent)
                            await session.commit()
                            _log = logger.warning if _is_unrecognized else logger.info
                            _log(
                                f"[ROLLUP] PR #{rollup_pr_number} merge skipped: "
                                f"{'unrecognized ' if _is_unrecognized else ''}mergeable_state={_ms_display} "
                                f"(deferral {deferred_count}/total {total_deferrals}/{elapsed_minutes:.0f}min; "
                                f"alert@{alert_threshold}/{total_alert_threshold}/{time_alert_minutes}min, "
                                f"approval@{approval_threshold}/{total_approval_threshold}/{time_approval_minutes}min) "
                                f"— deferring to next rollup sweep, no fix spawn"
                            )
                            return
                        # Observability: check if concurrent rollups may explain this conflict
                        try:
                            # R4-W2 (M3): ``has_key`` uses the GIN ``?``
                            # operator and matches ``ix_task_context_gin``;
                            # the prior ``as_string().is_not(None)`` form
                            # bypassed the index.
                            recent_rollups = (
                                await session.execute(
                                    select(_func.count())
                                    .select_from(Task)
                                    .where(
                                        Task.status == TaskStatus.COMPLETED,
                                        Task.id != parent.id,
                                        Task.updated_at
                                        >= _utcnow() - timedelta(hours=1),
                                        Task.context.has_key("rollup_pr_url"),
                                    )
                                )
                            ).scalar() or 0
                            if recent_rollups > 0:
                                logger.warning(
                                    f"[ROLLUP] Conflict on PR #{rollup_pr_number} may be stale-window: "
                                    f"{recent_rollups} other rollup(s) merged to {target_branch} in the last hour"
                                )
                        except Exception as e:
                            logger.warning(
                                f"[ROLLUP] Failed to query concurrent rollup count for PR #{rollup_pr_number} (task {parent.id}): {e}"
                            )
                        parent.context = {
                            **parent_ctx,
                            "rollup_pr_url": rollup_pr_url,
                            "rollup_pr_number": rollup_pr_number,
                            "_is_rollup_review": True,
                        }
                        parent.latest_status_message = f"Rollup PR #{rollup_pr_number} merge blocked: {merge_msg} — spawning fix task"
                        try:
                            pr_files = await get_pr_files(
                                gh_token, owner, repo, rollup_pr_number
                            )
                            files_list = "\n".join(
                                f"  - {f['filename']} ({f['status']})" for f in pr_files
                            )
                        except Exception as e:
                            logger.warning(
                                f"[ROLLUP] Failed to fetch PR #{rollup_pr_number} file list for task {parent.id}: {e}"
                            )
                            files_list = "  (could not fetch file list)"
                            pr_files = []
                        has_migrations = any(
                            (f.get("filename") or "").startswith(
                                "backend/alembic/versions/"
                            )
                            for f in pr_files
                        )
                        migration_guidance = ""
                        if has_migrations:
                            migration_guidance = (
                                "\n\nMigration chain guidance:\n"
                                "This rollup contains database migrations from multiple subtasks. After resolving conflicts:\n"
                                "- The migration chain in backend/alembic/versions/ must be linear: each migration's `down_revision` "
                                "must point to the revision of the previous migration in the chain.\n"
                                "- All revision numbers (e.g., 0039, 0040, ...) must be unique and sequential — no gaps, no duplicates.\n"
                                "- Renumber/rewrite migration files as needed so the chain is contiguous from the latest pre-rollup revision.\n"
                                "- Update `EXPECTED_SCHEMA_REVISION` in backend/app/main.py to match the final (highest) migration's revision."
                            )
                        parent_goal = (parent.description or parent.title or "")[:500]
                        if _ms == "behind":
                            # Budget-exhausted behind path: no real conflicts, just a stale
                            # GitHub flag. The no-op guidance is critical — if the PR head is
                            # already ancestor of base, git rebase will be a no-op and the
                            # fix agent must not treat that as failure (PR #6153 incident).
                            fix_prompt = (
                                f"Rollup PR #{rollup_pr_number} merging {staging_branch} into {target_branch} "
                                f"is behind the target branch and the GitHub update-branch API failed across "
                                f"the retry budget ({_upd_msg}). Rebase manually onto origin/{target_branch}:\n\n"
                                f"1. git fetch origin {target_branch}\n"
                                f"2. git rebase origin/{target_branch}\n"
                                f"3. If no conflicts and no commits were replayed, the PR is already "
                                f"up-to-date — GitHub's mergeability flag is just stale. Report no "
                                f"changes needed (don't force-push); retries will clear it.\n"
                                f"4. Otherwise resolve conflicts and git push --force-with-lease."
                            )
                        else:
                            fix_prompt = (
                                f"This rollup PR (#{rollup_pr_number}) merging {staging_branch} into {target_branch} has conflicts.\n"
                                f"Error: {merge_msg}\n\n"
                                f"Rollup goal: {parent_goal}\n\n"
                                f"Files in this PR:\n{files_list}\n\n"
                                f"Steps to resolve:\n"
                                f"1. Run: git fetch origin {target_branch}\n"
                                f"2. Run: git rebase origin/{target_branch}\n"
                                f"3. Resolve any conflicts — {target_branch} is the source of truth. Keep its refactors and restructuring.\n"
                                f"   Re-apply only the changes that serve the rollup goal (described above) on top of {target_branch}'s current state.\n"
                                f"   If files were heavily rewritten on {target_branch}, adapt the rollup changes to the new structure.\n"
                                f"4. Run: git rebase --continue after resolving each file"
                                f"{migration_guidance}"
                            )
                        fix_task, _ = await _spawn_pipeline_task(
                            parent,
                            "fix",
                            session,
                            rollup_pr_number,
                            staging_branch,
                            target_branch,
                            fix_prompt=fix_prompt,
                            original_task_id=str(parent.id),
                        )
                        if fix_task:
                            logger.error(
                                f"[ROLLUP] Merge blocked for rollup PR #{rollup_pr_number}, spawned fix → task {fix_task.id}"
                            )
                        else:
                            from app.review_pipeline import (
                                SPAWN_REASON_KEY,
                                SPAWN_REASON_DEFERRED,
                                SPAWN_REASON_DEDUP,
                                SPAWN_REASON_ALREADY_MERGED,
                            )

                            spawn_reason = (parent.context or {}).get(SPAWN_REASON_KEY)
                            if spawn_reason in (
                                SPAWN_REASON_DEFERRED,
                                SPAWN_REASON_DEDUP,
                            ):
                                # Another review/fix is in flight on this PR — leave
                                # parent EVALUATING; next rollup sweep retries once the
                                # in-flight task finishes. latest_status_message was
                                # already set by _spawn_pipeline_task.
                                logger.info(
                                    f"[ROLLUP] Fix spawn deferred for rollup PR #{rollup_pr_number} "
                                    f"({spawn_reason}) — leaving parent EVALUATING, next sweep retries"
                                )
                            elif spawn_reason == SPAWN_REASON_ALREADY_MERGED:
                                # PR merged while we were preparing the fix — no-op.
                                logger.info(
                                    f"[ROLLUP] PR #{rollup_pr_number} merged mid-flight — no fix needed"
                                )
                            else:
                                # cap / stuck / unknown — terminal.
                                parent.status = TaskStatus.FAILED
                                parent.context = _clear_rollup_transient_state(
                                    parent.context
                                )
                                parent.latest_status_message = (
                                    f"Rollup PR #{rollup_pr_number} merge blocked, fix not spawned "
                                    f"({spawn_reason or 'unknown'})"
                                )
                                from app.failure_classification import (
                                    classify_failure,
                                    persist_failure_category,
                                )

                                _fc = classify_failure(parent)
                                persist_failure_category(parent, _fc)
                                _record_rollup_failure_signals(parent, _fc)
                                await _maybe_create_failure_alert(
                                    parent,
                                    session,
                                    f"Rollup PR #{rollup_pr_number} merge blocked — fix iteration cap exhausted, manual intervention required",
                                )
                                logger.warning(
                                    f"[ROLLUP] WARNING: Rollup fix spawn returned None for PR #{rollup_pr_number} (reason={spawn_reason})"
                                )
                            # NOTE: Do NOT delete staging_branch here — it contains
                            # all merged child work and requeueing should be possible.
                else:
                    # Large rollup → review path. Two flavours:
                    #   • integration-branch target (automain/main): ci.yml runs
                    #     on the PR, so we set ci_waiting=True. Both ci_passed
                    #     and review_approved must land on review_loop before
                    #     the merge gate fires (see app.ci_completion._review_approved
                    #     + the rollup gate handoff in app.task_lifecycle's
                    #     verdict==APPROVE branch).
                    #   • intermediate target (research/*, plan/*): ci.yml does
                    #     NOT trigger (its branches list is [main, automain]),
                    #     so a CI wait is unsatisfiable and times out at the
                    #     1h backstop. Skip ci_waiting; the review task's
                    #     verdict==APPROVE branch falls through to the direct
                    #     merge path, and the next rollup sweep marks this parent
                    #     COMPLETED when it sees the PR merged.
                    # Do NOT pre-seed review_approved here — the spawned review task
                    # writes it back to this parent's review_loop on APPROVE.
                    review_loop_seed = {
                        "pr_number": rollup_pr_number,
                        "branch": staging_branch,
                        "base_branch": target_branch,
                        "original_task_id": str(parent.id),
                    }
                    if not is_target_intermediate:
                        review_loop_seed["ci_waiting"] = True
                        review_loop_seed["ci_wait_started"] = _utcnow().isoformat()
                    parent.latest_status_message = f"All {completed_count} subtasks merged, rollup PR #{rollup_pr_number} awaiting review"
                    parent.context = {
                        **parent_ctx,
                        "rollup_pr_url": rollup_pr_url,
                        "rollup_pr_number": rollup_pr_number,
                        "_is_rollup_review": True,
                        "review_loop": review_loop_seed,
                    }
                    review_task, _ = await _spawn_pipeline_task(
                        parent,
                        "review",
                        session,
                        rollup_pr_number,
                        staging_branch,
                        target_branch,
                        original_prompt=parent.description or parent.title,
                        original_task_id=str(parent.id),
                    )
                    if review_task:
                        logger.info(
                            f"[ROLLUP] Spawned review for rollup PR #{rollup_pr_number} → task {review_task.id}"
                        )
                    else:
                        from app.review_pipeline import (
                            SPAWN_REASON_KEY,
                            SPAWN_REASON_DEFERRED,
                            SPAWN_REASON_DEDUP,
                            SPAWN_REASON_ALREADY_MERGED,
                        )

                        spawn_reason = (parent.context or {}).get(SPAWN_REASON_KEY)
                        if spawn_reason in (SPAWN_REASON_DEFERRED, SPAWN_REASON_DEDUP):
                            # An active review/fix already owns this PR — leave parent
                            # EVALUATING; next rollup sweep retries. latest_status_message
                            # was already set by _spawn_pipeline_task.
                            logger.info(
                                f"[ROLLUP] Review spawn deferred for rollup PR #{rollup_pr_number} "
                                f"({spawn_reason}) — leaving parent EVALUATING, next sweep retries"
                            )
                        elif spawn_reason == SPAWN_REASON_ALREADY_MERGED:
                            logger.info(
                                f"[ROLLUP] PR #{rollup_pr_number} merged mid-flight — no review needed"
                            )
                        else:
                            parent.status = TaskStatus.FAILED
                            parent.context = _clear_rollup_transient_state(
                                parent.context
                            )
                            parent.latest_status_message = (
                                f"Rollup PR #{rollup_pr_number} created but review not spawned "
                                f"({spawn_reason or 'unknown'})"
                            )
                            from app.failure_classification import (
                                classify_failure,
                                persist_failure_category,
                            )

                            _fc = classify_failure(parent)
                            persist_failure_category(parent, _fc)
                            _record_rollup_failure_signals(parent, _fc)
                            await _maybe_create_failure_alert(
                                parent,
                                session,
                                f"Rollup PR #{rollup_pr_number} created but review task could not be spawned — iteration cap exhausted",
                            )
                            logger.warning(
                                f"[ROLLUP] WARNING: Rollup review spawn returned None for PR #{rollup_pr_number} (reason={spawn_reason})"
                            )
                        # NOTE: Do NOT delete staging_branch here — it contains
                        # all merged child work and requeueing should be possible.
            except Exception as e:
                # Orphan PR rescue: if we created a rollup PR during this try
                # but the marker commit failed (connection loss, session invalid),
                # the local variable still holds the PR number while the DB has
                # no record.  Persist it via a separate session so the retry
                # cycle's crash-recovery check resumes the existing PR instead
                # of creating a duplicate.
                #
                # Production 2026-05-04: when the "Parent state changed during
                # create_pull_request" raise at line ~1211 fired, ``parent``
                # had been reassigned to None by ``_relock_parent`` at the
                # call above. Subsequent ``parent.id`` access in this except
                # block crashed with AttributeError, swallowing the rescue.
                # Use ``parent_id_local`` / ``project_id_local`` (captured
                # pre-release in this same scope at lines 981-982) which
                # always hold the original parent identifiers regardless of
                # whether ``parent`` got reassigned to None mid-flow.
                _err_parent_id = (
                    parent_id_local
                    if parent_id_local is not None
                    else (parent.id if parent is not None else None)
                )
                _err_project_id = (
                    project_id_local
                    if project_id_local is not None
                    else (parent.project_id if parent is not None else None)
                )
                if rollup_pr_number is not None and _err_parent_id is not None:
                    try:
                        async with AsyncSessionLocal() as rescue_session:
                            db_parent = await rescue_session.get(Task, _err_parent_id)
                            if db_parent is not None:
                                db_ctx = db_parent.context or {}
                                if not db_ctx.get("rollup_pr_number"):
                                    db_parent.context = {
                                        **db_ctx,
                                        "rollup_pr_number": rollup_pr_number,
                                        "rollup_pr_url": rollup_pr_url,
                                    }
                                    rescue_session.add(db_parent)
                                    await rescue_session.commit()
                                    logger.warning(
                                        f"[ROLLUP] Rescued rollup PR #{rollup_pr_number} marker "
                                        f"for parent {_err_parent_id} via separate session — "
                                        f"preventing orphan on retry"
                                    )
                    except Exception as rescue_err:
                        logger.error(
                            f"[ROLLUP] Failed to rescue rollup PR #{rollup_pr_number} marker "
                            f"for parent {_err_parent_id}: {rescue_err}",
                            exc_info=True,
                        )
                # Sentinel deferral: the rest of this handler (retry counter,
                # status flip, failure classification, alert, post-handler
                # finalization at line ~2049) all dereference ``parent``, which
                # is None on this path. The rescue above already persisted the
                # PR marker; the next sweep cycle re-enters with fresh parent
                # state and resumes from the existing-PR branch.
                if isinstance(e, _ParentMutatedDuringPRCreation):
                    return
                err_str = str(e).lower()
                # Auth failures: invalidate the cached token so the next retry uses a fresh one
                if (
                    isinstance(e, HTTPException)
                    and e.status_code == 401
                    and _err_project_id is not None
                ):
                    from app.routers.agent import invalidate_github_token_cache

                    invalidate_github_token_cache(str(_err_project_id))
                    logger.warning(
                        f"[ROLLUP] Auth failure (401) for parent {_err_parent_id} — invalidated token cache"
                    )
                is_transient = (
                    isinstance(e, HTTPException)
                    and e.status_code in (401, 500, 502, 503, 504)
                ) or any(
                    kw in err_str
                    for kw in [
                        "api.github.com",
                        "500 internal server error",
                        "502 bad gateway",
                        "503 service unavailable",
                        "504 gateway timeout",
                        "internal server error",
                        "connection",
                        "timeout",
                        "rate limit",
                        "ssl",
                        "name resolution",
                        # Staging branch may have been deleted by a race — retry
                        # to allow recovery or manual intervention
                        "staging branch",
                        "validation failed",
                        # Auth failures — token expired/revoked; cache is invalidated above
                        "unauthorized",
                        "bad credentials",
                        "token expired",
                    ]
                )
                rollup_retries = (parent.context or {}).get("_rollup_retries", 0) + 1
                if is_transient and rollup_retries <= (
                    max_rollup := _runtime_config.get("max_rollup_retries", 5)
                ):
                    # Transient GitHub error — leave parent EVALUATING, retry next
                    # cycle. Drop our own creation claim so the retry isn't blocked
                    # by it; create_pull_request's idempotent pre-check catches any
                    # PR that a lost-response POST actually created.
                    parent.context = {
                        **{
                            k: v
                            for k, v in (parent.context or {}).items()
                            if k != "rollup_pr_creating_at"
                        },
                        "_rollup_retries": rollup_retries,
                    }
                    parent.latest_status_message = f"Rollup PR creation failed (attempt {rollup_retries}/{max_rollup}), will retry: {type(e).__name__}"
                    logger.warning(
                        f"[ROLLUP] Transient error creating rollup PR (attempt {rollup_retries}): {e}"
                    )
                else:
                    logger.warning(
                        f"[ROLLUP] Failed to create rollup PR: {e}", exc_info=True
                    )
                    parent.status = TaskStatus.FAILED
                    parent.context = _clear_rollup_transient_state(parent.context)
                    parent.latest_status_message = f"All {completed_count} subtasks done but rollup PR creation failed: {type(e).__name__}: {e!r}"[
                        :500
                    ]
                    from app.failure_classification import (
                        classify_failure,
                        persist_failure_category,
                    )

                    _fc = classify_failure(parent)
                    persist_failure_category(parent, _fc)
                    _record_rollup_failure_signals(parent, _fc)
                    await _maybe_create_failure_alert(
                        parent,
                        session,
                        f"All {completed_count} subtasks completed but rollup PR creation failed after {rollup_retries} retries — manual recovery required",
                    )
                    # NOTE: Do NOT delete staging_branch here — it contains all
                    # merged child work. Preserving it allows manual recovery or
                    # requeue. Branches are cleaned up by _cleanup_branches when
                    # the parent eventually reaches a terminal state, or by the
                    # stale branch cleanup service after 7 days.
        else:
            parent.status = TaskStatus.COMPLETED
            parent.latest_status_message = (
                f"All {completed_count} subtasks completed and merged"
            )

        # Final guard: a parent with an unmerged rollup PR is not done yet —
        # the review/fix/CI pipeline is still working on landing the code.
        _pctx = parent.context or {}
        if (
            parent.status == TaskStatus.COMPLETED
            and _pctx.get("rollup_pr_number")
            and not _pctx.get("pr_merged")
        ):
            parent.status = TaskStatus.EVALUATING
            _rpr = _pctx["rollup_pr_number"]
            parent.latest_status_message = f"All {completed_count} subtasks done — rollup PR #{_rpr} awaiting merge"
            logger.info(
                f"[ROLLUP] Parent {parent.id} — blocked COMPLETED: rollup PR #{_rpr} not yet merged"
            )

        # Terminal transition — scrub transient deferral/retry counters so a
        # future re-entry (admin requeue, reused parent) starts from zero.
        if parent.status in (TaskStatus.COMPLETED, TaskStatus.FAILED):
            parent.context = _clear_rollup_transient_state(parent.context)

        session.add(parent)
        await session.commit()
        await broadcast_to_project(
            str(parent.project_id),
            "task_updated",
            {"task_id": str(parent.id), "status": parent.status},
        )
        logger.info(
            f"[ROLLUP] Parent {parent.id} — all children done (completed={completed_count}, cancelled={cancelled_count})"
        )

    elif any(s == TaskStatus.FAILED for s in child_statuses) and active_count == 0:
        # All done but some failed — mark parent as failed but keep staging branch
        # (it may contain merged work from successful children that can be salvaged)
        parent.status = TaskStatus.FAILED
        failed_count = sum(1 for s in child_statuses if s == TaskStatus.FAILED)
        parent.latest_status_message = (
            f"{failed_count} of {len(children)} subtasks failed"
        )
        from app.failure_classification import (
            worst_child_failure_category,
            persist_failure_category,
        )

        _failed_children = [c for c in children if c.status == TaskStatus.FAILED]
        _fc = worst_child_failure_category(_failed_children)
        persist_failure_category(parent, _fc)
        _record_rollup_failure_signals(parent, _fc)
        parent.context = _clear_rollup_transient_state(parent.context)
        session.add(parent)
        await session.commit()
        await broadcast_to_project(str(parent.project_id), "task_updated",
                                   {"task_id": str(parent.id), "status": TaskStatus.FAILED})
        logger.error(f"[ROLLUP] Parent {parent.id} failed — {failed_count} children failed")
# fmt: on
