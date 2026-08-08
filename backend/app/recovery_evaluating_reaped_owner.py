"""Reaped-owner reconciliation handlers for the EVALUATING-timeout sweep.

Extracted from ``app.recovery_evaluating`` to keep each module below its size
cap.  Both handlers are called from ``_check_evaluating_timeouts`` in that
module; they share the same conceptual sub-domain (reaped EVALUATING owners
whose PR merged out-of-band or whose sub-pipeline finished while still open).

Neither function is part of any public API — they are module-private helpers
that return ``True`` when they fully handled the row (the caller should
``continue``) and ``False`` to fall through to downstream logic.
"""

import contextlib
import logging

from sqlmodel import select

from app import services
from app.branch_resolution import classify_rollup_merge_outcome
from app.landed_signal import corroborate_and_apply_merged_landed_signal
from app.models import Artifact, ArtifactType, Task, TaskStatus
from app.recovery_evaluating_helpers import _heal_direction_outcomes_best_effort

logger = logging.getLogger(__name__)


async def _handle_reaped_owner_fallback(
    task: Task,
    ctx: dict,
    session,
    now,
    elapsed: float,
    _heartbeat,
    _eval_task_id,
    maybe_create_failure_alert,
) -> bool:
    """REAPED-OWNER MARKER-LESS FALLBACK (Defect 2).

    An EVALUATING task with a PULL_REQUEST artifact but no ``review_loop`` or
    ``_pr_state_check_pending`` is invisible to every existing sweep path once
    the reaper clears the write-ahead flag on successful Review spawn.  When
    the PR merges out-of-band while the pipeline is still active this handler
    catches it before ``all_children_terminal`` ever becomes True.  The grace
    period avoids a GH API call on every cycle for freshly-handed-off owners.

    Returns True if the row was fully handled (caller should ``continue``).
    """
    from app.rollup_management import _check_and_trigger_rollup

    _REAPED_OWNER_FALLBACK_GRACE_S = 1800  # 30 min grace before GH poll

    if not (
        not ctx.get("review_loop")
        and not ctx.get("_pr_state_check_pending")
        and not ctx.get("pr_merged")
        and not ctx.get("rollup_pr_number")
        and task.vm_instance_id is None
        and elapsed >= _REAPED_OWNER_FALLBACK_GRACE_S
    ):
        return False

    _fb_artifact = (
        (
            await session.execute(
                select(Artifact).where(
                    Artifact.task_id == task.id,
                    Artifact.artifact_type == ArtifactType.PULL_REQUEST,
                )
            )
        )
        .scalars()
        .first()
    )
    if not (_fb_artifact and _fb_artifact.pr_url):
        return False

    _fb_pr_num: int | None = None
    with contextlib.suppress(ValueError, IndexError):
        _fb_pr_num = int(_fb_artifact.pr_url.rstrip("/").rsplit("/", 1)[-1])
    if not _fb_pr_num:
        return False

    _fb_pr_data: dict | None = None
    try:
        from app.routers.agent import get_github_token_for_task
        from app.github import get_pull_request

        (
            _fb_tok,
            _fb_owner,
            _fb_repo,
        ) = await services._bounded(
            get_github_token_for_task(task.id, session),
            services._GH_CALL_TIMEOUT_S,
            what=f"reaped-fallback token {str(task.id)[:8]}",
        )
        _fb_pr_data = await services._bounded(
            get_pull_request(_fb_tok, _fb_owner, _fb_repo, _fb_pr_num),
            services._GH_CALL_TIMEOUT_S,
            what=f"reaped-fallback get_pull_request #{_fb_pr_num}",
        )
    except Exception as _fb_exc:
        logger.warning(
            f"[EVAL TIMEOUT reaped-fallback] Task "
            f"{str(task.id)[:8]} PR #{_fb_pr_num} lookup "
            f"failed: {type(_fb_exc).__name__}: {_fb_exc} — "
            f"deferring to next sweep"
        )

    if _fb_pr_data is not None and _fb_pr_data.get("merged"):
        from sqlalchemy.orm.attributes import flag_modified
        from app.models import Project

        _fb_proj = await session.get(Project, task.project_id)
        (
            _fb_record,
            _fb_terminal,
            _fb_landed,
        ) = await classify_rollup_merge_outcome(
            task,
            _fb_proj,
            session,
            pr_number=_fb_pr_num,
            merged_base_branch=((_fb_pr_data.get("base") or {}).get("ref")),
            source="recovery_evaluating.reaped_owner_fallback",
        )
        task.status = TaskStatus.COMPLETED if _fb_terminal else TaskStatus.EVALUATING
        task.vm_instance_id = None
        task.last_heartbeat = None
        task.latest_status_message = (
            f"Reaped-owner fallback: PR #{_fb_pr_num} "
            "merged out-of-band; owner reconciled"
        )
        if _fb_record:
            task.context = dict(ctx)
            await corroborate_and_apply_merged_landed_signal(
                task,
                _fb_proj,
                session,
                base_branch=_fb_landed,
                hotfix_lane=bool(
                    (task.first_payload or {}).get("hotfix") or ctx.get("hotfix")
                ),
                # The PR payload this branch already fetched carries the merge
                # commit — pin acceptance corroboration to that immutable tree
                # rather than the 60s-TTL branch-tip cache.
                merge_commit_sha=_fb_pr_data.get("merge_commit_sha"),
            )
            flag_modified(task, "context")
        session.add(task)
        await session.commit()
        logger.info(
            f"[EVAL TIMEOUT reaped-fallback] Task "
            f"{str(task.id)[:8]} reconciled → {task.status} "
            f"(PR #{_fb_pr_num} merged out-of-band)"
        )
        if _fb_record:
            await _heal_direction_outcomes_best_effort(
                _eval_task_id, label="reaped-fallback"
            )
        await services.broadcast_to_project(
            str(task.project_id),
            "task_updated",
            {"task_id": str(task.id), "status": task.status},
        )
        if _fb_terminal and task.parent_task_id:
            with contextlib.suppress(Exception):
                await services._bounded(
                    _check_and_trigger_rollup(
                        task,
                        session,
                        progress_cb=_heartbeat,
                    ),
                    services._GH_CALL_TIMEOUT_S,
                    what=f"reaped-fallback parent rollup {str(task.id)[:8]}",
                )
        return True

    if _fb_pr_data is not None:
        # Review/Fix children may be attached to the owner's rollup ancestor,
        # so ``all_children_terminal`` never reaches the normal reconcile
        # handler. An open marker-less PR must still re-arm its state consumer.
        from sqlalchemy.orm.attributes import flag_modified

        _fb_ctx = dict(ctx)
        _fb_ctx["_pr_state_check_pending"] = True
        task.context = _fb_ctx
        task.latest_status_message = (
            f"Reaped-owner fallback: PR #{_fb_pr_num} still open; "
            "re-armed review reconciliation"
        )
        flag_modified(task, "context")
        session.add(task)
        await session.commit()
        await services.broadcast_to_project(
            str(task.project_id),
            "task_updated",
            {"task_id": str(task.id), "status": TaskStatus.EVALUATING},
        )
        return True

    return False


async def _handle_reaped_owner_reconcile(
    task: Task,
    ctx: dict,
    session,
    _eval_task_id,
    _heartbeat,
) -> bool:
    """REAPED-OWNER RECONCILE (Defect 1).

    When all Review/Fix children are terminal but the EVALUATING owner still
    has a PULL_REQUEST artifact and no ``review_loop`` of its own, check GitHub:
    merged → reconcile to COMPLETED and trigger parent rollup; open (Fix pushed
    a commit but review not yet done) → re-arm ``_pr_state_check_pending`` so
    the existing consumer spawns the next Review iteration.

    Returns True if the row was fully handled (caller should ``continue``).
    """
    from app.rollup_management import _check_and_trigger_rollup

    if ctx.get("review_loop") or ctx.get("pr_merged") or ctx.get("rollup_pr_number"):
        return False

    _ro_artifact = (
        (
            await session.execute(
                select(Artifact).where(
                    Artifact.task_id == task.id,
                    Artifact.artifact_type == ArtifactType.PULL_REQUEST,
                )
            )
        )
        .scalars()
        .first()
    )
    if not (_ro_artifact and _ro_artifact.pr_url):
        return False

    _ro_pr_num: int | None = None
    with contextlib.suppress(ValueError, IndexError):
        _ro_pr_num = int(_ro_artifact.pr_url.rstrip("/").rsplit("/", 1)[-1])
    if not _ro_pr_num:
        return False

    _ro_pr_data: dict | None = None
    try:
        from app.routers.agent import get_github_token_for_task
        from app.github import get_pull_request

        (
            _ro_tok,
            _ro_owner,
            _ro_repo,
        ) = await services._bounded(
            get_github_token_for_task(task.id, session),
            services._GH_CALL_TIMEOUT_S,
            what=f"reaped-owner token {str(task.id)[:8]}",
        )
        _ro_pr_data = await services._bounded(
            get_pull_request(_ro_tok, _ro_owner, _ro_repo, _ro_pr_num),
            services._GH_CALL_TIMEOUT_S,
            what=f"reaped-owner get_pull_request #{_ro_pr_num}",
        )
    except Exception as _ro_exc:
        logger.warning(
            f"[EVAL TIMEOUT reaped-owner] Task "
            f"{str(task.id)[:8]} PR #{_ro_pr_num} lookup "
            f"failed: {type(_ro_exc).__name__}: {_ro_exc} "
            f"— falling through to standard rollup trigger"
        )

    if _ro_pr_data is None:
        return False

    from sqlalchemy.orm.attributes import flag_modified

    if _ro_pr_data.get("merged"):
        from app.models import Project

        _ro_proj = await session.get(Project, task.project_id)
        (
            _ro_record,
            _ro_terminal,
            _ro_landed,
        ) = await classify_rollup_merge_outcome(
            task,
            _ro_proj,
            session,
            pr_number=_ro_pr_num,
            merged_base_branch=((_ro_pr_data.get("base") or {}).get("ref")),
            source="recovery_evaluating.reaped_owner_reconcile",
        )
        task.status = TaskStatus.COMPLETED if _ro_terminal else TaskStatus.EVALUATING
        task.vm_instance_id = None
        task.last_heartbeat = None
        task.latest_status_message = (
            f"Reaped-owner reconcile: PR #{_ro_pr_num} "
            "merged; review/fix sub-pipeline complete"
        )
        if _ro_record:
            task.context = dict(ctx)
            await corroborate_and_apply_merged_landed_signal(
                task,
                _ro_proj,
                session,
                base_branch=_ro_landed,
                hotfix_lane=bool(
                    (task.first_payload or {}).get("hotfix") or ctx.get("hotfix")
                ),
                # Same as the fallback branch above: the already-fetched PR
                # payload pins corroboration to the immutable merge commit.
                merge_commit_sha=_ro_pr_data.get("merge_commit_sha"),
            )
            flag_modified(task, "context")
        session.add(task)
        await session.commit()
        logger.info(
            f"[EVAL TIMEOUT reaped-owner] Task "
            f"{str(task.id)[:8]} reconciled → "
            f"{task.status} (PR #{_ro_pr_num} merged, "
            f"sub-pipeline complete)"
        )
        if _ro_record:
            await _heal_direction_outcomes_best_effort(
                _eval_task_id, label="reaped-owner"
            )
        await services.broadcast_to_project(
            str(task.project_id),
            "task_updated",
            {"task_id": str(task.id), "status": task.status},
        )
        if _ro_terminal and task.parent_task_id:
            with contextlib.suppress(Exception):
                await services._bounded(
                    _check_and_trigger_rollup(
                        task,
                        session,
                        progress_cb=_heartbeat,
                    ),
                    services._GH_CALL_TIMEOUT_S,
                    what=f"reaped-owner parent rollup {str(task.id)[:8]}",
                )
        return True
    else:
        # PR still open, all pipeline children done →
        # re-arm so the existing consumer spawns the
        # next Review iteration on the next sweep.
        _ro_ctx = dict(ctx)
        _ro_ctx["_pr_state_check_pending"] = True
        task.context = _ro_ctx
        flag_modified(task, "context")
        session.add(task)
        await session.commit()
        logger.info(
            f"[EVAL TIMEOUT reaped-owner] Task "
            f"{str(task.id)[:8]} PR #{_ro_pr_num} "
            f"open, sub-pipeline done — re-arming "
            f"_pr_state_check_pending"
        )
        await services.broadcast_to_project(
            str(task.project_id),
            "task_updated",
            {"task_id": str(task.id), "status": TaskStatus.EVALUATING},
        )
        return True
