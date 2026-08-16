"""Named recovery authorities for BLOCKED task requeue decisions.

Elapsed time and free-form blocker text are not retry authority.  This module
re-evaluates exact file/rollup/dependency preconditions and mints a stable
server-owned receipt for a verified execution-capability epoch.
"""

from __future__ import annotations

import contextlib
import copy
import uuid

from sqlalchemy import or_
from sqlmodel import select

from app.models import Task, TaskStatus

BLOCKED_RETRY_PRECONDITION_RECEIPT_KEY = "_blocked_retry_precondition_receipt"
_VIEWPORT_ADVISORY_POLICY_RECEIPT = {
    "authority": "server_advisory_policy_v1",
    "kind": "legacy_viewport_advisory",
    "policy": "viewport_capture_ci_advisory_v1",
}
_FILE_CONFLICT_HANDOFF_AUTHORITY = "server_file_conflict_fifo_v1"


def _blocked_retry_mutation_snapshot(task: Task) -> tuple:
    """Freeze the task authority that justified a prospective transition."""

    return (
        task.status,
        task.updated_at,
        task.blocker_kind,
        task.blocked_reason,
        task.blocked_at,
        task.recheck_after,
        task.latest_status_message,
        tuple(str(dep) for dep in (task.depends_on or [])),
        copy.deepcopy(task.context or {}),
    )


async def relock_blocked_retry_mutation_authority(session, task: Task) -> Task | None:
    """Release advisory work, then linearize Project -> Task before mutation."""

    from app.task_locking import lock_project_then_task

    task_id = task.id
    project_id = task.project_id
    # Flush any DB-local advisory bookkeeping first so an ORM on-update stamp is
    # part of the expected state rather than mistaken for a concurrent change.
    await session.flush()
    expected = _blocked_retry_mutation_snapshot(task)
    await session.commit()
    async with lock_project_then_task(
        session, project_id, task_id, skip_locked=True
    ) as (project, locked_task):
        if project is None or locked_task is None:
            await session.rollback()
            return None
        if (
            not project.autonomous_enabled
            or project.paused
            or project.archived
            or locked_task.status != TaskStatus.BLOCKED
            or _blocked_retry_mutation_snapshot(locked_task) != expected
        ):
            await session.rollback()
            return None
        return locked_task


def _bounded_text(value: object, limit: int = 64) -> str | None:
    if not isinstance(value, str):
        return None
    text = value.strip()
    return text[:limit] if text else None


def file_conflict_precondition_files(task: Task) -> frozenset[str]:
    """Return the exact file set named by a file-conflict blocker."""

    from app.orchestrator_filters import _prod_files

    context = task.context or {}
    defer = context.get("_precondition_defer")
    if isinstance(defer, dict) and isinstance(defer.get("files"), list):
        files = _prod_files(defer["files"])
        if files:
            return frozenset(files)

    reason = str(task.blocked_reason or "")
    marker = "file conflict on "
    marker_index = reason.lower().find(marker)
    if marker_index < 0:
        return frozenset()
    raw_files = reason[marker_index + len(marker) :]
    raw_files = raw_files.split(" (blocked by:", 1)[0]
    return frozenset(_prod_files(part.strip() for part in raw_files.split(",")))


def file_conflict_handoff_receipt(task: Task) -> dict[str, object] | None:
    """Mint the bounded FIFO reservation used between unblock and claim."""

    files = file_conflict_precondition_files(task)
    if not files:
        return None
    return {
        "authority": _FILE_CONFLICT_HANDOFF_AUTHORITY,
        "kind": "file_conflict_handoff",
        "files": sorted(files),
    }


async def file_conflict_precondition_cleared(task: Task, session) -> bool:
    """Return true only after no authoritative writer owns the named files."""

    from app.orchestrator_filters import (
        _file_conflict_reserved_files,
        _is_more_senior,
        _is_read_only_task,
        _is_senior_file_conflict_waiter,
        _prod_files,
    )

    named_files = file_conflict_precondition_files(task)
    if not named_files:
        return False
    possible_blockers = (
        (
            await session.execute(
                select(Task).where(
                    Task.project_id == task.project_id,
                    Task.id != task.id,
                    or_(
                        Task.status == TaskStatus.IN_PROGRESS,
                        Task.status == TaskStatus.QUEUED,
                        (
                            (Task.status == TaskStatus.BLOCKED)
                            & (Task.blocker_kind == "file_conflict")
                        ),
                    ),
                )
            )
        )
        .scalars()
        .all()
    )
    for blocker in possible_blockers:
        if _is_read_only_task(blocker):
            continue
        if blocker.status == TaskStatus.IN_PROGRESS:
            blocker_files = _prod_files(
                (blocker.context or {}).get("related_files", [])
            )
        elif _is_senior_file_conflict_waiter(blocker) and _is_more_senior(
            blocker, task
        ):
            blocker_files = _file_conflict_reserved_files(blocker)
        else:
            continue
        if named_files.intersection(blocker_files):
            return False
    return True


async def pending_rollup_precondition_cleared(task: Task, session) -> bool:
    """Return true only after the exact named pending-rollup authority is gone."""

    defer = (task.context or {}).get("_precondition_defer")
    blocker_key = defer.get("key") if isinstance(defer, dict) else None
    if not isinstance(blocker_key, str) or not blocker_key.startswith(
        "pending rollup '"
    ):
        reason = str(task.blocked_reason or "")
        marker = "overlaps pending rollup '"
        marker_index = reason.lower().find(marker)
        if marker_index < 0:
            return False
        title = reason[marker_index + len(marker) :].split("'", 1)[0]
        blocker_key = f"pending rollup '{title}'"

    evaluating_parent_ids = (
        (
            await session.execute(
                select(Task.id).where(
                    Task.project_id == task.project_id,
                    Task.status == TaskStatus.EVALUATING,
                )
            )
        )
        .scalars()
        .all()
    )
    if not evaluating_parent_ids:
        return True
    pending_children = (
        (
            await session.execute(
                select(Task).where(
                    Task.project_id == task.project_id,
                    Task.parent_task_id.in_(evaluating_parent_ids),
                    Task.status == TaskStatus.COMPLETED,
                )
            )
        )
        .scalars()
        .all()
    )
    for child in pending_children:
        child_scope = (child.context or {}).get("scope", "medium")
        child_type = str((child.first_payload or {}).get("type") or "")
        if child_scope != "large" and child_type not in ("refactor", "plan"):
            continue
        if blocker_key == f"pending rollup '{(child.title or str(child.id))[:60]}'":
            return False
    return True


async def execution_capability_retry_receipt(
    task: Task,
    session,
) -> dict[str, object] | None:
    """Return the stable runtime authority that can admit one retry.

    Volatile heartbeat/load fields are excluded.  A repeated sweep over the
    same runner revision and functional proof therefore stays parked; a changed
    runner, served revision, or proof is a distinct capability epoch.
    """

    from app.execution_capability_requirements import (
        CAPABILITY_BACKEND_VERIFICATION,
        learned_execution_capabilities,
    )
    from app.orchestrator_runner_dispatch import (
        _coerce_min_compute_capability,
        _coerce_vram_gb,
        _load_active_runners_cache,
        _select_eligible_runner,
        _task_requires_browser,
    )
    from app.runner_decline import (
        decline_exclusion_is_advisory,
        project_verification_proof_fingerprint,
        recently_declined_runner_ids,
    )

    learned_capabilities = learned_execution_capabilities(task.context)
    if not learned_capabilities:
        return None
    active_runners = await _load_active_runners_cache(session)
    payload = task.first_payload or {}
    task_type = str(payload.get("type") or "")
    accelerator = payload.get("accelerator") or (
        payload.get("compute_experiment") or {}
    ).get("accelerator")
    container_image = payload.get("container_image") or (
        payload.get("compute_experiment") or {}
    ).get("container_image")
    vram_gb, vram_error = _coerce_vram_gb(payload)
    min_compute_capability, min_cc_error = _coerce_min_compute_capability(payload)
    if vram_error is not None or min_cc_error is not None:
        # Dispatch rejects malformed numeric constraints before runner selection;
        # such a task cannot gain retry authority from a looser selector here.
        return None
    runner = _select_eligible_runner(
        active_runners,
        task.project_id,
        task_type,
        accelerator,
        {},
        preferred_runner_target_id=(task.context or {}).get(
            "preferred_runner_target_id"
        ),
        dispatch_seed=task.id.int,
        container_image=container_image,
        vram_gb=vram_gb,
        min_compute_capability=min_compute_capability,
        declined_runner_ids=recently_declined_runner_ids(task),
        declined_exclusion_advisory=decline_exclusion_is_advisory(task, task_type),
        requires_project_verification=(
            CAPABILITY_BACKEND_VERIFICATION in learned_capabilities
        ),
        requires_browser=_task_requires_browser(task),
    )
    if runner is None:
        return None

    runner_row = None
    get_row = getattr(session, "get", None)
    if callable(get_row):
        from app.models import LocalRunner

        with contextlib.suppress(Exception):
            runner_row = await get_row(LocalRunner, runner.id)
    proof_fingerprint = None
    if CAPABILITY_BACKEND_VERIFICATION in learned_capabilities:
        proof_fingerprint = project_verification_proof_fingerprint(
            (runner.capabilities or {}).get("project_verification")
        )
    source = runner_row or runner
    return {
        "authority": "server_runtime_selection_v1",
        "kind": "execution_capability",
        "capabilities": sorted(learned_capabilities),
        "runner_id": str(runner.id),
        "runner_source_sha": _bounded_text(getattr(source, "source_sha", None)),
        "runner_agent_source_sha": _bounded_text(
            getattr(source, "agent_source_sha", None)
        ),
        "proof_fingerprint": proof_fingerprint,
    }


async def blocked_precondition_state(
    task,
    session,
    verify_pr_merged,
    *,
    retry_receipt_out: dict | None = None,
) -> str:
    """Classify a BLOCKED row from machine-checkable current authority."""

    deps = task.depends_on or []
    blocker_meta = (task.context or {}).get("blocker") or {}
    blocker_source = (
        blocker_meta.get("source") if isinstance(blocker_meta, dict) else None
    )
    if task.blocker_kind == "file_conflict":
        if not await file_conflict_precondition_cleared(task, session):
            return "waiting"
        if not deps:
            receipt = file_conflict_handoff_receipt(task)
            if retry_receipt_out is not None and receipt is not None:
                retry_receipt_out["receipt"] = receipt
            return "clear"
    if task.blocker_kind == "pending_rollup":
        if not await pending_rollup_precondition_cleared(task, session):
            return "waiting"
        if not deps:
            return "clear"
    if not deps:
        if blocker_source == "parent":
            children = (
                (
                    await session.execute(
                        select(Task).where(
                            Task.project_id == task.project_id,
                            Task.parent_task_id == task.id,
                        )
                    )
                )
                .scalars()
                .all()
            )
            for child in children:
                child_ctx = child.context or {}
                if child.status == TaskStatus.BLOCKED:
                    return "waiting"
                if child.status == TaskStatus.FAILED and (
                    child_ctx.get("failure_category") == "BLOCKED_PRECONDITION"
                    or child_ctx.get("failure_class") == "BLOCKED_PRECONDITION"
                    or (child.latest_status_message or "")
                    .lstrip()
                    .lower()
                    .startswith("blocker:")
                ):
                    return "waiting"
            return "re_evaluate"
        if verify_pr_merged is None:
            return "waiting"
        if blocker_source == "agent":
            blocker_text = " ".join(
                str(value or "")
                for value in (
                    task.blocked_reason,
                    task.latest_status_message,
                    blocker_meta.get("reason"),
                )
            )
            if "VIEWPORT_GATE_INFRA_UNAVAILABLE" in blocker_text:
                # The retired VM-side viewport gate is advisory; clearing legacy
                # rows is migration cleanup, not a reusable infra retry. Persist
                # the server policy receipt so the same old runner cannot consume
                # another attempt by emitting the unchanged sentinel again.
                receipt = dict(_VIEWPORT_ADVISORY_POLICY_RECEIPT)
                prior_receipt = (task.context or {}).get(
                    BLOCKED_RETRY_PRECONDITION_RECEIPT_KEY
                )
                if receipt == prior_receipt:
                    return "waiting"
                if retry_receipt_out is not None:
                    retry_receipt_out["receipt"] = receipt
                return "clear"
            receipt = await execution_capability_retry_receipt(task, session)
            prior_receipt = (task.context or {}).get(
                BLOCKED_RETRY_PRECONDITION_RECEIPT_KEY
            )
            if receipt is not None and receipt != prior_receipt:
                if retry_receipt_out is not None:
                    retry_receipt_out["receipt"] = receipt
                return "retry"
        return "waiting"
    if verify_pr_merged is None:
        return "waiting"

    from app.orchestrator_health import _check_candidate_task_deps

    dep_uuids = []
    for dependency in deps:
        try:
            dep_uuids.append(uuid.UUID(str(dependency)))
        except (ValueError, AttributeError, TypeError):
            continue
    if not dep_uuids:
        return "clear"
    dep_tasks = (
        (await session.execute(select(Task).where(Task.id.in_(dep_uuids))))
        .scalars()
        .all()
    )
    result, _ = await _check_candidate_task_deps(
        task, dep_tasks, dep_uuids, session, verify_pr_merged
    )
    if result == "eligible":
        return "clear"
    if result == "cascade_cancel":
        return "dead"
    return "waiting"
