"""Server-owned research execution state carried in task context.

These keys select the producer contract, short-circuit completion, or route
durable deferred work between research attempts. They are execution authority,
not agent- or member-authored task metadata. Keep the policy in a small leaf
module so every untrusted context ingress uses the same rule.
"""

from __future__ import annotations

import contextlib
import logging
from collections.abc import Iterable, Mapping, Sequence

from sqlalchemy import inspect as sa_inspect
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm.attributes import flag_modified
from sqlmodel import select

from app.models import Project, Task, TaskStatus
from app.task_locking import (
    AUTOMATIC_TASK_CREATION_ALLOWED,
    acquire_automatic_task_creation_authority,
    lock_project_then_task,
)

logger = logging.getLogger(__name__)

MAX_RESEARCH_DEPTH = 3

_IDEMPOTENCY_INDEX_ACTIVE_STATUSES = (
    TaskStatus.QUEUED,
    TaskStatus.PROVISIONING,
    TaskStatus.IN_PROGRESS,
    TaskStatus.EVALUATING,
    TaskStatus.BLOCKED,
    TaskStatus.PARKED,
)

RESEARCH_SERVER_OWNED_CONTEXT_KEYS: frozenset[str] = frozenset(
    {
        "research_output_contract_version",
        "research_findings_finalized",
        "_research_spawn_pending",
        "prior_deferred_findings",
        "deferred_findings",
        "deferred_finding_consumption_acks",
        # Research lineage controls the recursion ceiling and downstream
        # provenance.  It is derived from persisted, same-project task
        # ancestry at creation time; neither agents nor the generic member
        # PATCH seam may author or retarget it.
        "research_depth",
        "parent_research_task_id",
        "research_task_id",
        # Binds an authenticated delivery row to the exact candidate finding
        # it authorizes at finalization.  Letting a member/agent retarget this
        # hash on a genuine child would make a different, undelivered finding
        # appear to have survived merely because the row's lineage is valid.
        "finding_hash",
    }
)

# These three records describe one execution attempt. Deferred findings are
# deliberately absent: ``prior_deferred_findings`` is durable input that must
# survive a retry, while ``deferred_findings`` and its receipts are durable
# cross-cycle handoff state.
RESEARCH_FRESH_ATTEMPT_CONTEXT_KEYS: frozenset[str] = frozenset(
    {
        "research_output_contract_version",
        "research_findings_finalized",
        "_research_spawn_pending",
    }
)


def is_server_owned_research_context_key(key: object) -> bool:
    """Return whether ``key`` controls backend research execution state."""

    return isinstance(key, str) and key in RESEARCH_SERVER_OWNED_CONTEXT_KEYS


def is_authenticated_research_child(parent_task: object, child_task: object) -> bool:
    """Return whether ``child_task`` is an exact backend-routed research child.

    ``parent_task_id`` remains generic member-writable metadata, so it is not
    sufficient proof that a row was admitted by research completion.  The
    paired lineage keys and finding hash are server-owned at every untrusted
    ingress; require the lineage together with durable creator/origination and
    tenant linkage before a child can authorize replay or finalized findings.
    """

    parent_id = getattr(parent_task, "id", None)
    parent_project_id = getattr(parent_task, "project_id", None)
    child_context = getattr(child_task, "context", None)
    if (
        parent_id is None
        or parent_project_id is None
        or not isinstance(child_context, Mapping)
    ):
        return False

    parent_id_text = str(parent_id)
    return (
        getattr(child_task, "project_id", None) == parent_project_id
        and getattr(child_task, "parent_task_id", None) == parent_id
        and getattr(child_task, "created_by", None) == "ORCHESTRATOR"
        and getattr(child_task, "origination", None) == "RESEARCH"
        and child_context.get("research_task_id") == parent_id_text
        and child_context.get("parent_research_task_id") == parent_id_text
    )


def research_delivery_identities(
    parent_task: Task, children: Iterable[object]
) -> set[tuple[str, str]]:
    """Return stable identities for replay-deduplicable research deliveries."""

    identities: set[tuple[str, str]] = set()
    for child in children:
        if not is_authenticated_research_child(parent_task, child):
            continue
        finding_hash = (getattr(child, "context", None) or {}).get("finding_hash")
        if finding_hash:
            identities.add(("finding", str(finding_hash)))
        elif (getattr(child, "first_payload", None) or {}).get("type") == (
            "salvage_bundle"
        ):
            identities.add(("salvage_bundle", str(parent_task.id)))
    return identities


async def authenticated_research_children(
    session,
    parent_task: Task,
    *,
    lock: bool = False,
) -> list[Task]:
    """Read exact backend-routed children for one research parent.

    Authoritative final snapshots pass ``lock=True`` while already holding
    Project then parent locks. Stable child-id order keeps the multi-row lock
    compatible across retries.
    """

    statement = (
        select(Task)
        .where(
            Task.project_id == parent_task.project_id,
            Task.parent_task_id == parent_task.id,
        )
        .order_by(Task.id)
    )
    if lock:
        statement = statement.with_for_update().execution_options(populate_existing=True)
    children = (await session.execute(statement)).scalars().all()
    return [
        child
        for child in children
        if is_authenticated_research_child(parent_task, child)
    ]


async def finalized_research_findings_for_survivors(
    session,
    parent_task: Task,
    candidates: Sequence[tuple[int, dict, str]],
) -> list[dict]:
    """Join candidate findings to the locked, non-cancelled delivery set."""

    children = await authenticated_research_children(
        session, parent_task, lock=True
    )
    surviving_hashes = {
        str((child.context or {}).get("finding_hash"))
        for child in children
        if child.status != TaskStatus.CANCELLED
        and (child.context or {}).get("finding_hash")
    }
    finalized: list[dict] = []
    finalized_hashes: set[str] = set()
    for _, finding, finding_hash in candidates:
        if finding_hash in surviving_hashes and finding_hash not in finalized_hashes:
            finalized.append(finding)
            finalized_hashes.add(finding_hash)
    return finalized


def reconcile_replayed_salvage_delivery(
    parent_task: Task,
    pending_tasks: list[Task],
    prior_children: Sequence[Task],
    *,
    salvaged_at: str,
) -> None:
    """Publish at most one authenticated salvage delivery for this parent."""

    def _salvage(row: Task) -> bool:
        return is_authenticated_research_child(parent_task, row) and (
            row.first_payload or {}
        ).get("type") == "salvage_bundle"

    pending = next((row for row in pending_tasks if _salvage(row)), None)
    prior = next((row for row in prior_children if _salvage(row)), None)
    if prior is not None and pending is not None:
        pending_tasks.remove(pending)
    delivery = prior or pending
    if delivery is None:
        return
    previous = (parent_task.context or {}).get("salvage_activity") or {}
    delivery_id = str(delivery.id)
    activity_at = (
        previous.get("salvaged_at")
        if previous.get("salvage_task_id") == delivery_id
        else salvaged_at
    )
    parent_task.context = {
        **(parent_task.context or {}),
        "salvage_activity": {
            "salvage_task_id": delivery_id,
            "salvaged_count": int(
                (delivery.context or {}).get("salvage_finding_count") or 0
            ),
            "salvaged_at": activity_at,
        },
    }
    flag_modified(parent_task, "context")


async def lock_research_spawn_boundary(
    session,
    task: Task,
    *,
    local_context: Mapping,
    removed_context_keys: Iterable[str] = (),
    plan_output: object,
    latest_status_message: str | None,
) -> tuple[Task | None, list[Task]]:
    """Re-lock spawn authority and return authenticated prior children.

    Intermediate completion commits release the entrypoint locks.  Refresh the
    Project and parent Task in canonical order, fail closed when a concurrent
    terminal transition won, and merge the locally-authored completion state
    back onto the locked row.  Locks remain transaction-scoped for the caller's
    atomic child-insert/topology commit.
    """

    project_id, task_id = task.project_id, task.id
    with session.no_autoflush:
        admission = await acquire_automatic_task_creation_authority(
            session, project_id, skip_locked=False
        )
        if admission != AUTOMATIC_TASK_CREATION_ALLOWED:
            return None, []
        async with lock_project_then_task(
            session,
            project_id,
            task_id,
            skip_locked=False,
        ) as (project, locked_task):
            if project is None or locked_task is None or locked_task.status not in (
                TaskStatus.COMPLETED,
                TaskStatus.EVALUATING,
            ):
                return None, []
            merged_context = {
                **(locked_task.context or {}),
                **dict(local_context),
            }
            for key in removed_context_keys:
                merged_context.pop(key, None)
            locked_task.context = merged_context
            locked_task.plan_output = plan_output
            locked_task.latest_status_message = latest_status_message
            flag_modified(locked_task, "context")
            return locked_task, await authenticated_research_children(
                session, locked_task
            )


async def relock_research_spawn_retry(session, project_id, task_id) -> Task | None:
    """Reauthorize a spawn retry after rollback released its original locks."""
    with session.no_autoflush:
        admission = await acquire_automatic_task_creation_authority(
            session, project_id, skip_locked=False
        )
        if admission != AUTOMATIC_TASK_CREATION_ALLOWED:
            return None
        async with lock_project_then_task(
            session, project_id, task_id, skip_locked=False
        ) as (project, task):
            if project is None or task is None or task.status not in (
                TaskStatus.COMPLETED,
                TaskStatus.EVALUATING,
            ):
                return None
            return task


def _is_active_idempotency_integrity_error(exc: IntegrityError) -> bool:
    message = str(exc)
    return (
        "ux_task_active_idempotency_key" in message
        or "UNIQUE constraint failed: task.idempotency_key" in message
    )


async def commit_or_handle_duplicate_research_spawn(
    session,
    task: Task,
    *,
    stage: str,
    relock_retry=relock_research_spawn_retry,
) -> bool:
    """Commit an atomic spawn batch and recover an own-claim collision.

    A terminal research parent temporarily leaves the active idempotency index,
    allowing a peer to claim its direction before the parent flips back to
    EVALUATING. Preserve unique pending work in that case, but yield when a
    same-parent retry published the same authenticated delivery during the
    rollback-to-relock gap.
    """

    task_pk, project_pk = task.id, task.project_id
    task_id_text = str(task_pk)
    pending_objects = list(session.new)
    pending_delivery_ids = research_delivery_identities(task, pending_objects)
    parent_state = {
        attr: task.__dict__[attr]
        for attr in ("status", "context", "latest_status_message")
        if attr in task.__dict__
    }
    own_key = task.__dict__.get("idempotency_key")
    try:
        await session.commit()
    except IntegrityError as duplicate_error:
        if not _is_active_idempotency_integrity_error(duplicate_error):
            raise
        await session.rollback()
        with contextlib.suppress(Exception), session.no_autoflush:
            await session.execute(
                select(Task)
                .where(Task.id == task_pk)
                .execution_options(populate_existing=True)
            )
        peer_id = None
        if own_key:
            with contextlib.suppress(Exception):
                peer_id = (
                    (
                        await session.execute(
                            select(Task.id).where(
                                Task.idempotency_key == own_key,
                                Task.id != task_pk,
                                Task.status.in_(_IDEMPOTENCY_INDEX_ACTIVE_STATUSES),
                            )
                        )
                    )
                    .scalars()
                    .first()
                )
        if peer_id is not None:
            task = await relock_retry(session, project_pk, task_pk)
            if task is None:
                await session.commit()
                return False
            peer_deliveries = await authenticated_research_children(session, task)
            if pending_delivery_ids & research_delivery_identities(
                task, peer_deliveries
            ):
                await session.commit()
                return False
            task.idempotency_key = None
            for attr, value in parent_state.items():
                setattr(task, attr, value)
            if "context" in parent_state:
                flag_modified(task, "context")
            for pending in pending_objects:
                session.add(pending)
            session.add(task)
            try:
                await session.commit()
            except IntegrityError as retry_error:
                if not _is_active_idempotency_integrity_error(retry_error):
                    raise
                await session.rollback()
                with contextlib.suppress(Exception), session.no_autoflush:
                    await session.execute(
                        select(Task)
                        .where(Task.id == task_pk)
                        .execution_options(populate_existing=True)
                    )
            else:
                logger.warning(
                    "[RESEARCH] Task %s: %s commit collided on its OWN "
                    "direction-claim key; peer %s owns the claim. Re-committed "
                    "%d pending row(s).",
                    task_id_text,
                    stage,
                    peer_id,
                    len(pending_objects),
                )
                return True
        logger.warning(
            "[RESEARCH] Task %s: %s hit active idempotency uniqueness; "
            "rolled back the duplicate batch: %s",
            task_id_text,
            stage,
            str(duplicate_error)[:200],
        )
        return False
    return True


async def refresh_research_spawn_references(
    session,
    *,
    project: Project | None,
    scanned_tasks: list[Task],
    child_tasks: list[Task],
) -> list[Task]:
    """Refresh references expired by duplicate self-claim recovery."""

    with session.no_autoflush:
        if project is not None and sa_inspect(project).expired:
            await session.execute(
                select(Project)
                .where(Project.id == sa_inspect(project).identity[0])
                .execution_options(populate_existing=True)
            )
        stale_ids = [
            sa_inspect(row).identity[0]
            for row in scanned_tasks
            if sa_inspect(row).expired
        ]
        if stale_ids:
            await session.execute(
                select(Task)
                .where(Task.id.in_(stale_ids))
                .execution_options(populate_existing=True)
            )
    if not child_tasks:
        return child_tasks
    child_ids = [child.id for child in child_tasks]
    rows = (
        await session.execute(select(Task).where(Task.id.in_(child_ids)))
    ).scalars().all()
    fresh = {str(row.id): row for row in rows}
    return [fresh.get(str(child.id), child) for child in child_tasks]
