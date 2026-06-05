"""Operator-triggered pipeline action endpoints (admin router).

Exposes the manual levers operators pull on a project or task from the admin
UI: spawning a ``self-audit``, kicking off the release ``qualify`` gate,
clearing wave dependencies, triggering a rollup for a task, and listing a
project's findings. Each route resolves the project/task with a role check via
``admin_helpers`` and validates internal payloads through ``payload_schemas``.
"""

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select
from typing import List
import uuid
import logging

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


class SelfAuditRequest(BaseModel):
    """Optional body for POST /projects/{project_id}/self-audit.

    Both fields are optional — when omitted, the agent picks up to ``max_systems``
    systems in round-robin order from the project design_doc.
    """

    focus_systems: List[str] | None = Field(default=None, max_length=20)
    max_systems: int | None = Field(default=None, ge=1, le=10)


@router.post("/projects/{project_id}/self-audit")
async def trigger_self_audit(
    project_id: uuid.UUID,
    body: SelfAuditRequest | None = None,
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(get_current_user),
):
    """Enqueue a self_audit task for this project.

    Members and above can trigger a maturity audit. Returns the new task_id.
    Rejects with 409 if a self_audit is already in flight for this project.
    """
    project = await _get_user_project_with_role(
        project_id, current_user["user_id"], session, "member"
    )

    # In-flight guard: never spawn a second self_audit when one is already running.
    # Two concurrent audits produce duplicate findings and waste VM budget.
    active_audits = (
        (
            await session.execute(
                select(Task).where(
                    Task.project_id == project.id,
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
    for audit_task in active_audits:
        audit_payload = audit_task.first_payload
        if audit_payload.get("type") == "self_audit":
            raise HTTPException(
                status_code=409,
                detail=(
                    f"Self-audit is already in progress for this project "
                    f"(task {str(audit_task.id)[:8]}, status {audit_task.status}). "
                    f"Wait for it to complete before starting another."
                ),
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

    new_task = Task(
        project_id=project.id,
        title=f"Self audit{title_suffix}"[:200],
        description="Self maturity audit triggered manually",
        payloads=[validated_payload],
        vm_instance_id=f"agent-vm-{uuid.uuid4().hex}",
        status=TaskStatus.QUEUED,
        created_by="USER",
        requires_approval=False,
    )
    session.add(new_task)
    await session.commit()
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
    return result


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
    and ``total_queued``.
    """
    await _get_user_project_with_role(
        project_id, current_user["user_id"], session, "member"
    )
    result = await session.execute(
        select(Task).where(
            Task.project_id == project_id, Task.status == TaskStatus.QUEUED
        )
    )
    tasks = result.scalars().all()

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
            await session.execute(select(Task).where(Task.id.in_(foundation_ids)))
        ).scalars().all()
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
    return {"cleared": cleared, "preserved": preserved, "total_queued": len(tasks)}


@router.post("/tasks/{task_id}/rollup")
async def trigger_rollup(
    task_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(get_current_user),
):
    """Manually trigger rollup PR for an EVALUATING parent task."""
    from app.routers.agent import get_github_token_for_task
    from app.review_pipeline import _spawn_pipeline_task
    from app.github import create_pull_request, merge_pull_request, delete_branch

    task = await _get_user_task_with_role(
        task_id, current_user["user_id"], session, "member"
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

    gh_token, owner, repo = await get_github_token_for_task(task.id, session)

    # Count children
    children = (
        (await session.execute(select(Task).where(Task.parent_task_id == task.id)))
        .scalars()
        .all()
    )

    from app.rollup_management import _build_rollup_pr_body

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

    pr_result = await create_pull_request(
        gh_token,
        owner,
        repo,
        title=pr_title,
        head=staging_branch,
        base=target_branch,
        body=pr_body,
    )
    rollup_pr_url = pr_result.get("html_url", "")
    rollup_pr_number = pr_result.get("number")

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

        _ok_to_merge, _hold_reason = await enforce_premerge_migration_guard(
            gh_token, owner, repo, rollup_pr_number, target_branch
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
                TaskStatus.COMPLETED
                if terminal_rollup_merge
                else TaskStatus.EVALUATING
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
