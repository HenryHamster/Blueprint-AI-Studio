"""Operator-triggered pipeline action endpoints (admin router).

Exposes the manual levers operators pull on a project or task from the admin
UI: spawning a ``self-audit``, kicking off the release ``qualify`` gate,
clearing wave dependencies, triggering a rollup for a task, and listing a
project's findings. Each route resolves the project/task with a role check via
``admin_helpers`` and validates internal payloads through ``payload_schemas``.
"""

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import func as _func
from sqlalchemy.exc import IntegrityError, InvalidRequestError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import load_only
from sqlalchemy.orm.attributes import flag_modified
from sqlmodel import select
from typing import List, Literal
import uuid
import logging
import time

from app.database import get_session
from app.depends_on_provenance import has_structural_depends_on
from app.branch_resolution import classify_rollup_merge_outcome
from app.landed_signal import apply_merged_landed_signal
from app.shared_state import (
    get_project_integration_branch,
    get_project_pr_title_prefix as _get_project_pr_title_prefix,
)
from app.models import (
    Task,
    TaskStatus,
    TaskHistory,
    Project,
    _utcnow,
)
from app.auth import get_current_user
from app.payload_schemas import validate_internal_payload
from app.admin_helpers import (
    _get_user_project_with_role,
    _get_user_task_with_role,
)
from app import release_qualifier, release_version

logger = logging.getLogger(__name__)

router = APIRouter(tags=["admin"])

_MAX_QUEUED_WAVE_EDGE_REPAIR_SCAN = 500


async def _clear_manual_rollup_create_claim(
    session: AsyncSession,
    *,
    task_id: uuid.UUID,
    claim_ts: float,
    pre_create_snapshot: dict | None,
    task_in_session: Task | None,
) -> Task | None:
    """Best-effort cleanup for this request's manual rollup create claim.

    Only removes the claim when the task still carries THIS request's
    ``rollup_pr_creating_at`` value and no durable ``rollup_pr_number`` marker
    exists, so a retry-safe cleanup never clobbers a peer's newer claim or an
    already-recorded PR.
    """
    from app.rollup_locking import _relock_parent

    task = await _relock_parent(
        session,
        task_id,
        pre_create_snapshot,
        parent_in_session=task_in_session,
    )
    if task is None:
        return None

    ctx = task.context or {}
    if ctx.get("rollup_pr_number") or ctx.get("rollup_pr_creating_at") != claim_ts:
        return task

    task.context = {k: v for k, v in ctx.items() if k != "rollup_pr_creating_at"}
    session.add(task)
    await session.commit()
    return task


def _record_failure_result(task: Task, failure_class: str) -> None:
    """Record a task failure for deployment monitoring and direction health.

    Safe to call alongside persist_failure_category; best-effort (logs on error).
    """
    try:
        from app.routers.agent_status import record_task_result
        from app.shared_state import record_direction_result

        record_task_result(
            success=False,
            failure_class=failure_class,
            task_id=str(task.id),
            task_type=(task.first_payload or {}).get("type", ""),
        )
        direction_id = str((task.context or {}).get("direction_id") or "")
        if direction_id:
            record_direction_result(
                direction_id,
                success=False,
                failure_class=failure_class,
                project_id=str(task.project_id) if task.project_id else "",
            )
    except Exception:
        logger.exception(f"[ADMIN] Failed to record failure result for task {task.id}")


class _SelfAuditRequest(BaseModel):
    """Optional body for POST /projects/{project_id}/self-audit.

    Both fields are optional — when omitted, the agent picks up to ``max_systems``
    systems in round-robin order from the project design_doc.
    """

    focus_systems: List[str] | None = Field(default=None, max_length=20)
    max_systems: int | None = Field(default=None, ge=1, le=10)


@router.post("/projects/{project_id}/self-audit")
async def trigger_self_audit(
    project_id: uuid.UUID,
    body: _SelfAuditRequest | None = None,
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(get_current_user),
):
    """Enqueue a self_audit task for this project.

    Members and above can trigger a maturity audit. Returns the new task_id.
    Rejects with 409 if a self_audit is already in flight for this project.
    Idempotent: concurrent/duplicate submissions use idempotency_key to prevent
    duplicate audit spawns — a second near-simultaneous trigger coalesces onto
    the in-flight audit instead of spawning a second one.
    """
    project = await _get_user_project_with_role(
        project_id, current_user["user_id"], session, "member"
    )

    payload_dict: dict = {
        "type": "self_audit",
        "base_branch": get_project_integration_branch(project),
    }
    if body is not None:
        if body.focus_systems is not None:
            payload_dict["focus_systems"] = [
                s for s in body.focus_systems if isinstance(s, str) and s.strip()
            ]
        if body.max_systems is not None:
            payload_dict["max_systems"] = body.max_systems
    validated_payload = validate_internal_payload(payload_dict, "admin:self_audit")

    title_suffix = ""
    if body and body.focus_systems:
        title_suffix = f" ({', '.join(body.focus_systems[:3])}{'...' if len(body.focus_systems) > 3 else ''})"

    # Check for any active self_audit tasks (including legacy tasks without idempotency_key).
    # This check is non-transactional but will be backed by the idempotency_key constraint
    # if two concurrent requests both pass this check — the database constraint prevents
    # duplicate inserts and forces coalesce via IntegrityError handling below.
    active_statuses = [
        TaskStatus.QUEUED,
        TaskStatus.PROVISIONING,
        TaskStatus.IN_PROGRESS,
        TaskStatus.EVALUATING,
    ]
    # The task type is stored in the first payload element. Push that exact
    # predicate into PostgreSQL and fetch only the row needed for the 409.
    active_audit = (
        await session.execute(
            select(Task.id, Task.status)
            .where(
                Task.project_id == project.id,
                Task.status.in_(active_statuses),
                Task.payloads[0]["type"].as_string() == "self_audit",
            )
            .limit(1)
        )
    ).first()
    if active_audit is not None:
        raise HTTPException(
            status_code=409,
            detail=(
                f"Self-audit is already in progress for this project "
                f"(task {str(active_audit.id)[:8]}, status {active_audit.status}). "
                f"Wait for it to complete before starting another."
            ),
        )

    # Use idempotency_key to prevent concurrent duplicates. The key is project-scoped
    # so concurrent triggers on the same project use the same key; the database
    # partial unique index ux_task_active_idempotency_key prevents duplicate inserts.
    idempotency_key = f"operator_self_audit:{project.id}"

    new_task = Task(
        project_id=project.id,
        title=f"Self audit{title_suffix}"[:200],
        description="Self maturity audit triggered manually",
        payloads=[validated_payload],
        vm_instance_id=f"agent-vm-{uuid.uuid4().hex}",
        status=TaskStatus.QUEUED,
        created_by="USER",
        requires_approval=False,
        idempotency_key=idempotency_key,
    )
    session.add(new_task)
    # Snapshot project.id before commit: the IntegrityError rollback below
    # expires `project` (rollback expires regardless of expire_on_commit), so
    # reading project.id in the coalesce log/return would fire a sync lazy-load
    # -> MissingGreenlet, 500-ing instead of coalescing onto the in-flight audit.
    _project_id = project.id
    try:
        await session.commit()
    except IntegrityError:
        # Concurrent submission: another request inserted a task with the same
        # idempotency_key before this one committed. Fetch the existing active audit
        # and return it instead of raising an error — coalesce onto the in-flight audit.
        await session.rollback()
        existing = (
            (
                await session.execute(
                    select(Task).where(
                        Task.idempotency_key == idempotency_key,
                        Task.status.in_(active_statuses),
                    )
                )
            )
            .scalars()
            .first()
        )
        if existing is None:
            raise HTTPException(
                status_code=409,
                detail="Self-audit is already in progress for this project.",
            )
        logger.info(
            f"[ADMIN] Concurrent self-audit trigger for project {_project_id} "
            f"coalesced onto existing audit {existing.id}"
        )
        return {
            "task_id": str(existing.id),
            "project_id": str(_project_id),
            "status": existing.status,
        }
    await session.refresh(new_task)

    history = TaskHistory(
        task_id=new_task.id,
        action="CREATED",
        details={"created_by": "USER", "method": "self_audit_manual"},
    )
    session.add(history)
    await session.commit()

    return {
        "task_id": str(new_task.id),
        "project_id": str(project.id),
        "status": new_task.status,
    }


@router.post("/projects/{project_id}/qualify")
async def trigger_qualify(
    project_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(get_current_user),
):
    """Manually trigger the release qualifier for this project's open version.

    Members and above can trigger qualification on demand (used during the
    Phase 4 dry-run calibration window).  Resolves the current open
    ``ReleaseVersion``, runs ``release_qualifier.run_qualification``, and
    returns its result dict.  Returns 404 when there is no open version.
    """
    await _get_user_project_with_role(
        project_id, current_user["user_id"], session, "member"
    )

    open_version = await release_version.current_open_version(session, project_id)
    if open_version is None:
        raise HTTPException(
            status_code=404,
            detail="No open release version found for this project.",
        )

    result = await release_qualifier.run_qualification(
        session, project_id, open_version
    )
    await session.commit()
    # Promotion-in-flight: the audit passed and a promotion PR is open and
    # auto-merging.  Re-qualifying would spawn a fresh heavy audit/smoketest pair
    # and clobber the promotion-pending provenance, so run_qualification refuses
    # (no-op).  Surface that to the operator as a 409, mirroring the version-cut
    # lever, so all the levers over an in-flight promotion agree.
    if result.get("status") == "promotion_in_flight":
        detail = (
            "a promotion PR is open and auto-merging — wait for it to land or "
            "cancel it before re-qualifying"
        )
        pr_number = result.get("promotion_pr_number")
        if pr_number is not None:
            detail += f" (PR #{pr_number}"
            pr_url = result.get("promotion_pr_url")
            if pr_url:
                detail += f" {pr_url}"
            detail += ")"
        detail += release_version.format_promotion_freshness(result)
        raise HTTPException(status_code=409, detail=detail)
    return result


class _ForcePromoteRequest(BaseModel):
    candidate_sha: str | None = None
    reason: str | None = None


@router.post("/projects/{project_id}/promote/force")
async def trigger_force_promote(
    project_id: uuid.UUID,
    body: _ForcePromoteRequest | None = None,
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(get_current_user),
):
    """FORCE-PROMOTE the project's open version — SKIP the audit/qualify gate.

    Still cuts the release branch, opens the ``release-vN → promotion-target`` PR,
    and lets the normal CI-wait / managed-check-wait machinery merge it once the
    promotion target's required checks (branch-protection CI) pass — 'force' skips
    the AUDIT, not CI.

    OWNER-only: this bypasses the quality gate and merges toward production
    ``main``, so it is the most privileged operator lever.  Optionally pins an
    explicit ``candidate_sha`` (default: current integration-branch HEAD).
    Refuses (409) when a promotion is already in flight; 404 when there is no open
    version.  Reuses ``promote()`` + the Fix-3 under-lock claim, so it is
    idempotent and cannot double-promote.
    """
    await _get_user_project_with_role(
        project_id, current_user["user_id"], session, "owner"
    )
    _body = body or _ForcePromoteRequest()
    result = await release_qualifier.force_promote_current_version(
        session,
        project_id,
        candidate_sha=_body.candidate_sha,
        actor=str(current_user["user_id"]),
        reason=_body.reason,
    )
    await session.commit()
    status = result.get("status")
    if status == "no_open_version":
        raise HTTPException(
            status_code=404,
            detail="No open release version found for this project.",
        )
    if status == "promotion_in_flight":
        detail = (
            "a promotion PR is open and auto-merging — wait for it to land or "
            "cancel it before force-promoting"
        )
        pr_number = result.get("promotion_pr_number")
        if pr_number is not None:
            detail += f" (PR #{pr_number})"
        raise HTTPException(status_code=409, detail=detail)
    return result


class _HotfixPromoteRequest(BaseModel):
    candidate_sha: str | None = None
    # REQUIRED, non-empty: this narrower lane's reason IS the audit justification.
    reason: str = Field(..., min_length=1)
    # The operator's DECLARED scope — cross-checked/audited against the actual
    # diff, NEVER the authorization source (the diff allowlist is).
    scope_modules: List[str] = Field(default_factory=list)
    # The operator must affirm they understand the self-reference gap: the fix
    # stays inert until the deploy lands the promoted content.
    ack_inert_until_deploy: bool = False


@router.post("/projects/{project_id}/promote/hotfix")
async def trigger_hotfix_promote(
    project_id: uuid.UUID,
    body: _HotfixPromoteRequest,
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(get_current_user),
):
    """HOTFIX-PROMOTE the project's open version — a NARROW, audited force-promote.

    Fast-tracks a release-machinery fix ahead of the normal qualification gate,
    but STRICTLY narrower than ``/promote/force``: it can only ship allowlisted
    release-machinery files (never product code or migrations), the allowlist is
    enforced fail-closed against the candidate's ACTUAL changed files, and every
    hotfix is recorded as owed completion debt + an operator Alert.  Skips the
    AUDIT/scoring gate ONLY — CI and promote()'s merge-time migration-chain
    backstop still gate the merge.

    OWNER-only.  ``reason`` is mandatory (it is the audit justification) and
    ``ack_inert_until_deploy`` must be true.  Maps: scope_rejected → 400 (with the
    offending files), no_open_version → 404, promotion_in_flight → 409.
    """
    await _get_user_project_with_role(
        project_id, current_user["user_id"], session, "owner"
    )
    if not body.reason or not body.reason.strip():
        raise HTTPException(
            status_code=422,
            detail="A non-empty 'reason' is required — it is the hotfix audit justification.",
        )
    if not body.ack_inert_until_deploy:
        raise HTTPException(
            status_code=400,
            detail=(
                "ack_inert_until_deploy must be true — affirm you understand the "
                "hotfix fix stays inert until the deploy lands the promoted content."
            ),
        )
    result = await release_qualifier.hotfix_promote_current_version(
        session,
        project_id,
        candidate_sha=body.candidate_sha,
        actor=str(current_user["user_id"]),
        reason=body.reason.strip(),
        scope_modules=list(body.scope_modules),
    )
    await session.commit()
    status = result.get("status")
    if status == "scope_rejected":
        raise HTTPException(
            status_code=400,
            detail={
                "message": (
                    "Hotfix scope rejected — the candidate diff is not a "
                    "release-machinery-only change: " + str(result.get("reason") or "")
                ),
                "offending_files": result.get("offending_files") or [],
            },
        )
    if status == "no_open_version":
        raise HTTPException(
            status_code=404,
            detail="No open release version found for this project.",
        )
    if status == "promotion_in_flight":
        detail = (
            "a promotion PR is open and auto-merging — wait for it to land or "
            "cancel it before hotfix-promoting"
        )
        pr_number = result.get("promotion_pr_number")
        if pr_number is not None:
            detail += f" (PR #{pr_number})"
        raise HTTPException(status_code=409, detail=detail)
    return result


@router.post("/projects/{project_id}/ack-env-will-be-bound")
async def trigger_ack_env_will_be_bound(
    project_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(get_current_user),
):
    """OWNER-only: acknowledge that the missing boot env/secret will be bound in
    the SAME deploy revision, overriding exactly the ``boot_prereq_missing`` block.

    Slice 4 of design 245212a4.  The deployed-side env-preflight judge compares a
    candidate's declared boot prerequisites against THIS deployed process's own
    environ.  When the operator intends to bind the missing secret as part of the
    same deploy, the current environ does not yet reflect it and the judge would
    false-block.  This arms a ONE-SHOT acknowledgment on the open version so the
    NEXT qualification treats ``boot_prereq_missing`` as an explicit, audited
    override (recorded in the qualification blob — who/when/that it was overridden,
    NAMES only, never a value) rather than a silent bypass.

    Scoped strictly to ``boot_prereq_missing``: the ack never suppresses any other
    blocker.  It stays armed across re-qualification until it actually overrides a
    block once, then clears.  OWNER-only — the most privileged deploy-safety lever.
    Returns 404 when there is no open version.
    """
    await _get_user_project_with_role(
        project_id, current_user["user_id"], session, "owner"
    )

    open_version = await release_version.current_open_version(session, project_id)
    if open_version is None:
        raise HTTPException(
            status_code=404,
            detail="No open release version found for this project.",
        )

    acknowledged_at = _utcnow().isoformat()
    # Read-modify-write on the JSONB column: preserve every other field of the
    # existing qualification record and only add/replace the ack marker.  A None
    # last_qualification (no qualification yet) becomes a minimal dict carrying
    # just the ack, which run_qualification's prior_qual read tolerates.
    qual = dict(open_version.last_qualification or {})
    qual["ack_env_will_be_bound"] = {
        "actor": str(current_user["user_id"]),
        # Presence-only: who + when, never any env value.
        "acknowledged_at": acknowledged_at,
    }
    open_version.last_qualification = qual
    flag_modified(open_version, "last_qualification")
    session.add(open_version)
    await session.commit()

    return {
        "status": "acknowledged",
        "scope": "boot_prereq_missing",
        "acknowledged_at": acknowledged_at,
        "note": (
            "one-shot override of the boot_prereq_missing block; clears once it "
            "overrides that block or stays armed until it fires"
        ),
    }


# Blocker provenance sources that mark a depends_on edge as a real, structural
# prerequisite rather than legacy queue-wave serialization.
_PREREQUISITE_BLOCKER_SOURCES = {"research_prerequisite", "parent"}


def _has_edge_prerequisite_provenance(dependent: Task, dep_id: str) -> bool:
    """True if *dep_id* is durably marked as a structural prerequisite edge."""
    dep_text = str(dep_id).strip()
    if not dep_text:
        return False
    if has_structural_depends_on(dependent, dep_text):
        return True

    ctx = dependent.context or {}
    converged = ctx.get("_foundation_converged")
    if isinstance(converged, dict) and str(converged.get("foundation_id")) == dep_text:
        return True

    unresolved = ctx.get("unresolved_prerequisite") or []
    if isinstance(unresolved, dict):
        unresolved = [unresolved]
    if isinstance(unresolved, list):
        for item in unresolved:
            if isinstance(item, dict) and str(item.get("ref")) == dep_text:
                return True

    blocker = ctx.get("blocker")
    if (
        isinstance(blocker, dict)
        and blocker.get("source") in _PREREQUISITE_BLOCKER_SOURCES
    ):
        need = blocker.get("need")
        if isinstance(need, dict) and str(need.get("ref")) == dep_text:
            return True
    return False


async def _is_clearable_wave_edge(
    dependent: Task,
    dep_id: str,
    foundation: Task | None,
    session: AsyncSession,
) -> bool:
    """True when a single edge is legacy sequencing and safe to clear.

    The route must share the dispatcher's dependency semantics instead of using
    foundation status as a proxy. We therefore ask the canonical dependency gate
    whether THIS edge is already non-blocking for dispatch:

    * ``waiting``       → preserve (still a live prerequisite)
    * ``cascade_cancel`` → preserve (real failed/deleted prerequisite)
    * ``eligible``      → only clear if the edge is ordering-only and carries no
      structural provenance
    """
    from app.orchestrator_health import (
        _check_candidate_task_deps,
        is_ordering_only_dependency,
    )
    from app.rollup_pr_verification import _verify_pr_merged

    if _has_edge_prerequisite_provenance(dependent, dep_id):
        return False

    dep_tasks = [foundation] if foundation is not None else []
    result, _ = await _check_candidate_task_deps(
        dependent,
        dep_tasks,
        [dep_id],
        session,
        _verify_pr_merged,
    )
    if result != "eligible" or foundation is None:
        return False
    return is_ordering_only_dependency(dependent, foundation)


@router.post("/projects/{project_id}/clear-wave-deps")
async def clear_wave_deps(
    project_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(get_current_user),
):
    """Clear only STALE/legacy wave depends_on edges on QUEUED tasks.

    Recovers stale queue-wave serialization without destroying the prerequisite
    graph: each ``depends_on`` edge on a QUEUED task is classified by the SAME
    dependency gate the dispatcher uses plus per-edge structural provenance.
    Only edges that are already dispatch-eligible AND ordering-only are removed,
    so a task mixing legacy sequencing and a real prerequisite keeps the latter.

    Response: ``cleared`` (tasks whose stale edges were removed — kept for
    backward compat), ``preserved`` (tasks that retained ≥1 prerequisite edge),
    ``total_queued`` (exact queued count), and ``processed_queued`` (the capped
    number scanned this run).
    """
    await _get_user_project_with_role(
        project_id, current_user["user_id"], session, "member"
    )
    total_queued = (
        await session.execute(
            select(_func.count())
            .select_from(Task)
            .where(Task.project_id == project_id, Task.status == TaskStatus.QUEUED)
        )
    ).scalar() or 0
    result = await session.execute(
        select(Task)
        .where(Task.project_id == project_id, Task.status == TaskStatus.QUEUED)
        .order_by(Task.created_at.asc())
        .limit(_MAX_QUEUED_WAVE_EDGE_REPAIR_SCAN)
    )
    tasks = result.scalars().all()
    truncated = total_queued > _MAX_QUEUED_WAVE_EDGE_REPAIR_SCAN
    processed_queued = len(tasks)
    skipped_queued = total_queued - processed_queued if truncated else 0

    # Batch-load every referenced foundation task in one query so the per-edge
    # classifier can check landed status without N round-trips.
    foundation_ids: set[uuid.UUID] = set()
    for task in tasks:
        for dep in task.depends_on or []:
            try:
                foundation_ids.add(uuid.UUID(str(dep)))
            except (ValueError, TypeError):
                continue
    foundation_map: dict[str, Task] = {}
    if foundation_ids:
        frows = (
            (await session.execute(select(Task).where(Task.id.in_(foundation_ids))))
            .scalars()
            .all()
        )
        foundation_map = {str(f.id): f for f in frows}

    cleared = 0
    preserved = 0
    for task in tasks:
        deps = task.depends_on or []
        if not deps:
            continue
        kept: list[str] = []
        for dep in deps:
            dep_text = str(dep)
            foundation = foundation_map.get(dep_text)
            if not await _is_clearable_wave_edge(task, dep_text, foundation, session):
                kept.append(dep)
        if len(kept) != len(deps):
            task.depends_on = kept
            session.add(task)
            cleared += 1
        if kept:
            preserved += 1
    await session.commit()
    return {
        "cleared": cleared,
        "preserved": preserved,
        "total_queued": total_queued,
        "processed_queued": processed_queued,
        "truncated": truncated,
        "skipped_queued": skipped_queued,
    }


@router.post("/tasks/{task_id}/rollup")
async def trigger_rollup(
    task_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(get_current_user),
):
    """Manually trigger rollup PR for an EVALUATING parent task.

    Idempotent and mutually exclusive with the automated rollup path
    (``rollup_management._check_and_trigger_rollup``). Both coordinate through
    the parent-row ``FOR UPDATE`` lock and the SHARED rollup markers
    (``rollup_pr_creating_at`` / ``rollup_pr_number``), so a manual trigger
    fired while the background path is mid-create — or two manual triggers, or
    a manual trigger after the PR already exists — cannot POST a duplicate
    rollup PR:

    * the parent row is re-fetched ``FOR UPDATE`` (the role check above does an
      unlocked read), so a held row → 409 ``rollup already in progress``;
    * a recorded ``rollup_pr_number`` short-circuits and returns that PR
      idempotently — no second POST/merge;
    * a FRESH ``rollup_pr_creating_at`` claim (peer mid-create) → 409;
    * otherwise the creation claim is staked UNDER the lock and committed
      before the slow ``create_pull_request`` call, releasing the lock around
      it so a concurrent worker observes the claim and defers (mirrors the
      auto path's marker discipline — incident #7350/#7351).
    """
    from app.routers.agent import get_github_token_for_task
    from app.review_pipeline import _spawn_pipeline_task
    from app.github import create_pull_request, merge_pull_request, delete_branch
    from app.rollup_management import (
        _build_rollup_pr_body,
        _ROLLUP_PR_CREATE_CLAIM_TTL_S,
    )
    from app.rollup_state import _clear_rollup_transient_state
    from app.rollup_locking import _release_parent_lock, _relock_parent

    # Role check is an unlocked read; re-fetch the parent UNDER A ROW LOCK so
    # the precondition re-check and claim-stake below race neither the
    # automated rollup path nor a second concurrent manual trigger.
    #
    # _get_user_task_with_role loaded the row via session.get, so it now lives
    # in this session's identity map. With expire_on_commit=False the FOR
    # UPDATE re-SELECT below would otherwise return that CACHED instance
    # WITHOUT refreshing its attributes — ctx/existing_pr/_claim_ts would all
    # evaluate against the pre-lock snapshot and miss a peer that committed
    # rollup_pr_number/rollup_pr_creating_at after the role check. Expire the
    # cached instance and force populate_existing on the locked SELECT so the
    # precondition re-check reads the row AS OF the lock (mirrors
    # rollup_locking._relock_parent / _lock_original_task).
    cached_task = await _get_user_task_with_role(
        task_id, current_user["user_id"], session, "member"
    )
    try:
        session.expire(cached_task)
    except InvalidRequestError:
        session.expire_all()
    task = (
        (
            await session.execute(
                select(Task)
                .where(Task.id == task_id)
                .with_for_update(skip_locked=True)
                .execution_options(populate_existing=True)
            )
        )
        .scalars()
        .first()
    )
    if task is None:
        # Row held by the automated rollup path or a concurrent manual trigger
        # mid-create — defer rather than racing into a duplicate PR.
        raise HTTPException(
            status_code=409,
            detail="A rollup is already in progress for this task — try again shortly.",
        )
    if task.status != TaskStatus.EVALUATING:
        raise HTTPException(
            status_code=400, detail=f"Task is {task.status}, not EVALUATING"
        )

    ctx = task.context or {}
    staging_branch = ctx.get("plan_branch") or ctx.get("research_branch")
    target_branch = ctx.get("parent_base") or ctx.get("original_base")

    if not staging_branch or not target_branch:
        raise HTTPException(
            status_code=400, detail="Task has no staging/target branch info in context"
        )

    # ── Idempotency guard: a rollup PR is already recorded for this task. ──
    # Return it instead of POSTing a second PR (the auto path may have created
    # it, or this is a duplicate client retry).
    existing_pr = ctx.get("rollup_pr_number")
    if existing_pr:
        return {
            "rollup_pr_url": ctx.get("rollup_pr_url", ""),
            "rollup_pr_number": existing_pr,
            "target": target_branch,
            "status": task.status,
        }

    # ── Duplicate-create guard: a FRESH creation claim is in flight. ──
    # A peer (auto path or another manual trigger) staked the claim under the
    # lock and released it around its own create_pull_request; observing a
    # fresh claim with no recorded PR means a duplicate POST is imminent, so
    # defer. A claim older than the TTL is stale (peer crashed mid-create) and
    # falls through so creation is never wedged.
    _claim_ts = ctx.get("rollup_pr_creating_at")
    if (
        isinstance(_claim_ts, (int, float))
        and (time.time() - _claim_ts) < _ROLLUP_PR_CREATE_CLAIM_TTL_S
    ):
        raise HTTPException(
            status_code=409,
            detail=(
                "A rollup PR creation is already in progress for this task "
                "— try again shortly."
            ),
        )

    gh_token, owner, repo = await get_github_token_for_task(task.id, session)

    # Count children — only load the fields needed by _build_rollup_pr_body.
    children = (
        (
            await session.execute(
                select(Task)
                .where(Task.parent_task_id == task.id)
                .options(
                    load_only(
                        Task.id,
                        Task.payloads,
                        Task.vm_provisioned_at,
                        Task.vm_stopped_at,
                        Task.vm_machine_type,
                        Task.vm_spot,
                        raiseload=True,
                    )
                )
            )
        )
        .scalars()
        .all()
    )

    _project = await session.get(Project, task.project_id)
    _prefix = _get_project_pr_title_prefix(_project)
    pr_title = f"{_prefix}Rollup: {task.title}"
    _dir_id = task.first_payload.get("current_direction_id") or ctx.get("direction_id")
    _dir_title = ctx.get("direction_title") or task.title
    pr_body = _build_rollup_pr_body(
        len(children),
        staging_branch,
        direction_id=str(_dir_id) if _dir_id else None,
        direction_title=_dir_title,
        children=list(children),
    )

    # Stake the creation claim UNDER the lock, then release it (commit) around
    # the slow create_pull_request call so a concurrent worker observes the
    # claim and defers instead of POSTing a duplicate rollup PR.
    claim_ts = time.time()
    task.context = {**ctx, "rollup_pr_creating_at": claim_ts}
    pre_create_snapshot = await _release_parent_lock(task, session, context_dirty=True)

    try:
        pr_result = await create_pull_request(
            gh_token,
            owner,
            repo,
            title=pr_title,
            head=staging_branch,
            base=target_branch,
            body=pr_body,
        )
    except Exception:
        await _clear_manual_rollup_create_claim(
            session,
            task_id=task_id,
            claim_ts=claim_ts,
            pre_create_snapshot=pre_create_snapshot,
            task_in_session=task,
        )
        raise
    rollup_pr_url = pr_result.get("html_url", "")
    rollup_pr_number = pr_result.get("number")
    empty_diff = bool(pr_result.get("empty_diff"))

    # Re-acquire the lock and record the durable marker, dropping the now
    # superseded creation claim. The marker is the crash-recovery signal both
    # paths key on, so persist it before any other writer can observe an empty
    # marker on an EVALUATING parent.
    task = await _relock_parent(
        session, task_id, pre_create_snapshot, parent_in_session=task
    )
    if task is None:
        # A peer mutated the parent during PR creation. For a real PR, persist
        # the durable marker best-effort so it is not orphaned. For the empty-
        # diff sentinel there is no PR to rescue, so never stamp PR #0.
        session.expire_all()
        fresh = (
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
        if fresh is not None:
            if empty_diff:
                fresh_ctx = fresh.context or {}
                if not fresh_ctx.get("rollup_pr_number"):
                    fresh.status = TaskStatus.COMPLETED
                    fresh.context = _clear_rollup_transient_state(fresh_ctx)
                    fresh.latest_status_message = (
                        f"✓ All {len(children)} subtasks completed — no new changes "
                        f"to merge (fixes already on {target_branch})"
                    )[:500]
                    session.add(fresh)
                    await session.commit()
                    try:
                        await delete_branch(gh_token, owner, repo, staging_branch)
                    except Exception as e:
                        logger.warning(
                            f"[ADMIN] Failed to delete staging branch '{staging_branch}' "
                            f"for task {task_id}: {e}"
                        )
            else:
                fctx = {
                    k: v
                    for k, v in (fresh.context or {}).items()
                    if k != "rollup_pr_creating_at"
                }
                fresh.context = {
                    **fctx,
                    "rollup_pr_number": rollup_pr_number,
                    "rollup_pr_url": rollup_pr_url,
                }
                session.add(fresh)
                await session.commit()
        return {
            "rollup_pr_url": rollup_pr_url,
            "rollup_pr_number": rollup_pr_number,
            "target": target_branch,
            "status": fresh.status if fresh is not None else TaskStatus.EVALUATING,
        }

    if empty_diff:
        task.status = TaskStatus.COMPLETED
        task.context = _clear_rollup_transient_state(task.context)
        task.latest_status_message = (
            f"✓ All {len(children)} subtasks completed — no new changes "
            f"to merge (fixes already on {target_branch})"
        )[:500]
        session.add(task)
        await session.commit()
        try:
            await delete_branch(gh_token, owner, repo, staging_branch)
        except Exception as e:
            logger.warning(
                f"[ADMIN] Failed to delete staging branch '{staging_branch}' "
                f"for task {task.id}: {e}"
            )
        return {
            "rollup_pr_url": "",
            "rollup_pr_number": rollup_pr_number,
            "target": target_branch,
            "status": task.status,
        }

    ctx = {
        k: v for k, v in (task.context or {}).items() if k != "rollup_pr_creating_at"
    }
    task.context = {
        **ctx,
        "rollup_pr_number": rollup_pr_number,
        "rollup_pr_url": rollup_pr_url,
    }
    ctx = task.context

    is_target_intermediate = _project is not None and _project.is_staging_branch(
        target_branch
    )

    if is_target_intermediate:
        # Pre-merge Alembic dual-head guard. This operator-triggered autonomous
        # rollup auto-merge is the same multi-migration vector as the guarded
        # autonomous path. On a block-or-defer verdict, skip the merge, surface
        # the reason, and leave the task EVALUATING so a later sweep can retry
        # (or an operator can reconcile) rather than wedging the branch.
        from app.rollup_migration_validation import enforce_premerge_migration_guard

        from app.shared_state import (
            get_project_promotion_target as _get_project_promotion_target,
        )

        _ok_to_merge, _hold_reason = await enforce_premerge_migration_guard(
            gh_token,
            owner,
            repo,
            rollup_pr_number,
            target_branch,
            promotion_target=_get_project_promotion_target(_project),
        )
        if not _ok_to_merge:
            task.context = {
                **ctx,
                "rollup_pr_number": rollup_pr_number,
                "rollup_pr_url": rollup_pr_url,
            }
            task.latest_status_message = (
                f"Rollup merge held — Alembic dual-head guard: {_hold_reason}"
            )[:500]
            session.add(task)
            await session.commit()
            return {
                "rollup_pr_url": rollup_pr_url,
                "rollup_pr_number": rollup_pr_number,
                "target": target_branch,
                "status": task.status,
                "merge_blocked_reason": _hold_reason,
            }
        merge_result = await merge_pull_request(gh_token, owner, repo, rollup_pr_number)
        if merge_result.get("merged"):
            (
                record_pr_merged,
                terminal_rollup_merge,
                landed_branch,
            ) = await classify_rollup_merge_outcome(
                task,
                _project,
                session,
                pr_number=rollup_pr_number,
                merged_base_branch=target_branch,
                source="admin_operator_triggers.rollup_auto_merge",
            )
            task.status = (
                TaskStatus.COMPLETED if terminal_rollup_merge else TaskStatus.EVALUATING
            )
            task.context = {
                **ctx,
                "rollup_pr_number": rollup_pr_number,
                "rollup_pr_url": rollup_pr_url,
            }
            if record_pr_merged:
                apply_merged_landed_signal(
                    task,
                    _project,
                    base_branch=landed_branch,
                    hotfix_lane=bool(
                        (task.first_payload or {}).get("hotfix")
                        or (task.context or {}).get("hotfix")
                    ),
                    non_merge_reason="merged_pr_non_integration_branch",
                )
            if terminal_rollup_merge:
                task.latest_status_message = (
                    f"Rollup PR auto-merged into {landed_branch}"
                )
            else:
                task.latest_status_message = (
                    f"Rollup PR auto-merged into {landed_branch}; awaiting "
                    "downstream landing"
                )
            await delete_branch(gh_token, owner, repo, staging_branch)
        else:
            task.latest_status_message = (
                f"Rollup merge failed: {merge_result.get('message')}"
            )
    else:
        task.context = {
            **ctx,
            "rollup_pr_url": rollup_pr_url,
            "rollup_pr_number": rollup_pr_number,
        }
        review_task, _ = await _spawn_pipeline_task(
            task,
            "review",
            session,
            rollup_pr_number,
            staging_branch,
            target_branch,
            original_prompt=task.description or task.title,
            original_task_id=str(task.id),
        )
        if review_task:
            task.latest_status_message = f"Rollup PR #{rollup_pr_number} created, review spawned → {review_task.id}"
        else:
            # _spawn_pipeline_task sets source_task.latest_status_message to a
            # specific reason (already merged / iteration cap / deferred behind
            # in-flight task / stuck cycle) and writes a SPAWN_REASON code into
            # context so callers can distinguish recoverable from terminal cases.
            from app.review_pipeline import (
                SPAWN_REASON_KEY,
                SPAWN_REASON_DEFERRED,
                SPAWN_REASON_DEDUP,
                SPAWN_REASON_ALREADY_MERGED,
            )

            spawn_reason_code = (task.context or {}).get(SPAWN_REASON_KEY)
            spawn_reason_msg = task.latest_status_message
            if spawn_reason_code == SPAWN_REASON_ALREADY_MERGED:
                (
                    record_pr_merged,
                    terminal_rollup_merge,
                    landed_branch,
                ) = await classify_rollup_merge_outcome(
                    task,
                    _project,
                    session,
                    pr_number=rollup_pr_number,
                    merged_base_branch=target_branch,
                    source="admin_operator_triggers.rollup_already_merged",
                )
                task.status = (
                    TaskStatus.COMPLETED
                    if terminal_rollup_merge
                    else TaskStatus.EVALUATING
                )
                task.context = {
                    **(task.context or {}),
                }
                if record_pr_merged:
                    apply_merged_landed_signal(
                        task,
                        _project,
                        base_branch=landed_branch,
                        hotfix_lane=bool(
                            (task.first_payload or {}).get("hotfix")
                            or (task.context or {}).get("hotfix")
                        ),
                        non_merge_reason="merged_pr_non_integration_branch",
                    )
                if terminal_rollup_merge:
                    task.latest_status_message = (
                        f"Rollup PR #{rollup_pr_number} already merged"
                    )
                else:
                    task.latest_status_message = (
                        f"Rollup PR #{rollup_pr_number} already merged into "
                        f"{landed_branch}; awaiting downstream landing"
                    )
            elif spawn_reason_code in (SPAWN_REASON_DEFERRED, SPAWN_REASON_DEDUP):
                # Recoverable: a sibling task is in-flight on the same PR. Keep
                # the parent EVALUATING so the orchestrator retries review-spawn
                # once the in-flight task completes.
                task.latest_status_message = f"Rollup PR #{rollup_pr_number} created — review deferred ({spawn_reason_msg})"
            else:
                # Terminal failure (iteration cap, stuck cycle, blocked repeat,
                # or unknown). Operator must intervene.
                task.status = TaskStatus.FAILED
                if task.vm_provisioned_at:
                    task.vm_stopped_at = _utcnow()
                task.latest_status_message = (
                    f"Rollup PR #{rollup_pr_number} created but review not spawned — {spawn_reason_msg}"
                    if spawn_reason_msg
                    else f"Rollup PR #{rollup_pr_number} created but review not spawned (unknown reason)"
                )
                from app.failure_classification import persist_failure_category

                persist_failure_category(task, "AGENT_TIMEOUT")
                _record_failure_result(task, "AGENT_TIMEOUT")

    session.add(task)
    await session.commit()

    return {
        "rollup_pr_url": rollup_pr_url,
        "rollup_pr_number": rollup_pr_number,
        "target": target_branch,
        "status": task.status,
    }


@router.post("/projects/{project_id}/cancel-strategic-refresh")
async def cancel_strategic_refresh_endpoint(
    project_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(get_current_user),
):
    """Retract a pending (armed) strategic steer for this project.

    Clears the operator-armed strategic-refresh marker so the next strategic
    cycle will NOT be woken by it — the operator's lever to undo a steer they
    sent by mistake or immediately reconsidered. Members and above (same level
    as the other manual operator triggers).

    Safe to retry: cancelling when nothing is armed is an idempotent no-op
    (``cleared=False``), not an error, so a duplicate or late client retry is
    harmless. Returns ``cleared`` — ``True`` when an armed marker was removed.
    """
    await _get_user_project_with_role(
        project_id, current_user["user_id"], session, "member"
    )
    from app.strategic_trigger import cancel_strategic_refresh

    cleared = await cancel_strategic_refresh(session, project_id)
    await session.commit()
    return {"cleared": cleared}


class _RecordFindingResolutionBody(BaseModel):
    """Body for POST /projects/{project_id}/finding-resolution."""

    finding_key: str = Field(..., min_length=1, max_length=64)
    release_version_id: uuid.UUID
    state: Literal["resolved", "stale"]
    severity: str | None = Field(default=None, max_length=32)
    note: str | None = Field(default=None, max_length=2000)
    resolving_ref: str | None = Field(default=None, max_length=200)
    verified_against_sha: str | None = Field(default=None, max_length=200)


@router.post("/projects/{project_id}/finding-resolution")
async def record_finding_resolution(
    project_id: uuid.UUID,
    body: _RecordFindingResolutionBody,
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(get_current_user),
):
    """Manually write a resolution-ledger entry for a carried audit finding.

    Members and above can record a ``resolved`` or ``stale`` entry for a
    ``(finding_key, release_version_id)`` pair without requiring an agent token.
    Intended as the operator override when the automated sweep fails or a
    one-time manual resolution is needed to unblock re-qualification.

    Returns the upserted ``AuditFindingResolution`` row as a dict.
    """
    from app.models import ReleaseVersion
    from app.release_readiness import apply_finding_resolution

    await _get_user_project_with_role(
        project_id, current_user["user_id"], session, "member"
    )

    # Validate that the release_version_id belongs to this project.
    rv = (
        (
            await session.execute(
                select(ReleaseVersion).where(
                    ReleaseVersion.id == body.release_version_id,
                    ReleaseVersion.project_id == project_id,
                )
            )
        )
        .scalars()
        .first()
    )
    if rv is None:
        raise HTTPException(
            status_code=404,
            detail="Release version not found for this project.",
        )

    row = await apply_finding_resolution(
        session,
        project_id,
        body.release_version_id,
        body.finding_key,
        body.state,
        severity=body.severity,
        note=body.note,
        resolving_ref=body.resolving_ref,
        verified_against_sha=body.verified_against_sha,
    )
    await session.commit()
    await session.refresh(row)
    return {
        "id": str(row.id),
        "project_id": str(row.project_id),
        "release_version_id": str(row.release_version_id),
        "finding_key": row.finding_key,
        "state": row.state,
        "severity": row.severity,
        "note": row.note,
        "resolving_ref": row.resolving_ref,
        "verified_against_sha": row.verified_against_sha,
    }


@router.get("/projects/{project_id}/findings")
async def get_project_findings(
    project_id: uuid.UUID,
    direction_id: str | None = Query(None),
    limit: int = Query(50, ge=1, le=200),
    days: int = Query(30, ge=1, le=365),
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(get_current_user),
):
    """Return structured research findings for a project, with claims and sources.

    Optional direction_id filter scopes results to a single direction.
    Ordered newest-task-first; bounded by days recency and limit total rows.
    """
    await _get_user_project_with_role(
        project_id, current_user["user_id"], session, "viewer"
    )
    from app.research_completion import query_findings_index

    rows = await query_findings_index(
        session, project_id, direction_id=direction_id, limit=limit, days=days
    )
    return {"findings": rows, "count": len(rows)}
