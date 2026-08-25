"""Server-owned research execution state carried in task context.

These keys select the producer contract, short-circuit completion, or route
durable deferred work between research attempts. They are execution authority,
not agent- or member-authored task metadata. Keep the policy in a small leaf
module so every untrusted context ingress uses the same rule.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import logging
from collections.abc import Iterable, Mapping, Sequence
from typing import NamedTuple

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

# Stamped on a research parent's context only once the completion handler has
# durably finalized its survivor set and child topology.  Defined in this leaf
# module -- ``app.findings_index`` re-exports it -- because ``app.task_blocked``
# imports this module, so importing the findings index back into it is a real
# cycle (it fails at ``app.main`` load, not in some rare path).
FINALIZED_RESEARCH_FINDINGS_KEY = "research_findings_finalized"

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
        FINALIZED_RESEARCH_FINDINGS_KEY,
        "_research_spawn_pending",
        "_research_spawn_recovery",
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
        # The bounded record of findings that were routed or filtered instead
        # of delivered.  The discovery decision packet surfaces it as evidence
        # a reviewer binds advance/park/stop on, so a member or agent that
        # could author it could fabricate the very content the review reads.
        "withheld_findings",
    }
)

# These three records describe one execution attempt. Deferred findings are
# deliberately absent: ``prior_deferred_findings`` is durable input that must
# survive a retry, while ``deferred_findings`` and its receipts are durable
# cross-cycle handoff state.
RESEARCH_FRESH_ATTEMPT_CONTEXT_KEYS: frozenset[str] = frozenset(
    {
        "research_output_contract_version",
        FINALIZED_RESEARCH_FINDINGS_KEY,
        "_research_spawn_pending",
        "_research_spawn_recovery",
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


# ---------------------------------------------------------------------------
# Withheld-finding carry
#
# A research cycle can produce a finding that never becomes a child task, for
# two structurally different reasons:
#
#   * TYPE ROUTING -- ``design_doc_observation`` and ``direction_update``
#     findings are routed into durable project state (the design doc's
#     ``proposed_changes``, or a direction's ``pending_update``) instead of
#     spawning work. That routing is correct; spawning an implementation task
#     for "the design doc should say X" would be wrong.
#   * DEDUP FILTERING -- a finding that overlaps in-flight, shipped, or
#     already-known work is dropped from the spawn set.
#
# In both cases the finding has no child task, so it carries no
# ``finding_hash`` into ``finalized_research_findings_for_survivors`` and is
# dropped from ``plan_output``. The discovery decision packet reads exactly
# ``plan_output``, so the reviewer saw zero evidence coverage while the
# packet's manifest -- which can only report on evidence it knows about --
# reported ``required_evidence_omitted: false``. That is a completeness signal
# reporting clean because it could not see: the same vacuous-guard shape this
# codebase keeps paying for. Observed live on direction ``d-1210b978``
# generations 5 and 9, and across six research parents in a single 60-row task
# page on 2026-08-25.
#
# The invariant these records exist to hold: a finding is either turned into a
# task, or it reaches the review packet as withheld-with-reason INCLUDING ITS
# CONTENT. Never dropped-and-declared-complete.
WITHHELD_FINDINGS_CONTEXT_KEY = "withheld_findings"
# Bounded so one pathological cycle can neither grow ``task.context`` without
# limit nor blow the discovery packet's fixed evidence budget (overflowing that
# budget revokes binding authority and strands the direction, so the carry must
# have a known worst case). Overflow past the cap is RECORDED on the envelope
# -- ``omitted_count`` plus a checksum of the dropped set -- never silent.
WITHHELD_FINDINGS_CAP = 12
# Per-string excerpt bound. A cut is stamped on the record
# (``content_truncated``) and the full finding's checksum is carried alongside,
# so a bounded excerpt is provably an excerpt of that exact finding rather than
# a silent rewrite.
WITHHELD_FINDING_EXCERPT_CHARS = 700
# Whole-record content bound, across every carried field. Per-field caps alone
# are not enough: six fields at the per-field bound render past what the
# discovery packet will serialize for one item, and that packet-side bound
# collapses an over-long value into an excerpt blob -- which would lose the
# per-field structure without the record ever saying so. Bounding the record
# here keeps the cut where it can be named: fields that do not fit are listed
# in ``content_omitted_keys`` rather than quietly disappearing.
WITHHELD_FINDING_CONTENT_CHARS = 900

WITHHELD_DISPOSITION_TYPE_ROUTED = "type_routed"
WITHHELD_DISPOSITION_DEDUP_FILTERED = "dedup_filtered"

WITHHELD_ROUTE_DESIGN_DOC = "project_design_doc.proposed_changes"
WITHHELD_ROUTE_DIRECTION_PENDING_UPDATE = "direction.pending_update"

# The decision-bearing prose of a withheld finding. Everything else on a raw
# finding (scope hints, related_files, internal gate verdicts) is machinery the
# reviewer does not judge, and carrying it would spend the bounded packet
# budget on noise.
_WITHHELD_CONTENT_KEYS: tuple[str, ...] = (
    "title",
    "description",
    "section",
    "suggested_text",
    "suggested_priority",
    "rationale",
)


def _withheld_checksum(value: object) -> str:
    rendered = json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(rendered.encode("utf-8")).hexdigest()


def build_withheld_finding_record(
    finding: Mapping | None,
    *,
    disposition: str,
    reason: str,
    finding_type: str | None = None,
    routed_to: str | None = None,
    destination_id: str | None = None,
    direction_id: str | None = None,
    routing_applied: bool = False,
) -> dict:
    """Build one bounded, structured record of a finding that spawned no task.

    ``reason`` is the human-readable *why*; ``routing_applied`` is the machine
    answer to "did the side effect actually land?".  A routed finding whose
    side effect no-opped (a duplicate design-doc observation, a
    ``direction_update`` whose target direction is gone) is the strictly worse
    case -- before this record existed it incremented no counter at all and did
    not even reach the parent's status string -- so it is recorded exactly like
    an applied one, distinguished only by this flag and its reason.
    """

    source = finding if isinstance(finding, Mapping) else {}
    content: dict[str, object] = {}
    content_omitted_keys: list[str] = []
    content_truncated = False
    remaining = WITHHELD_FINDING_CONTENT_CHARS
    # ``_WITHHELD_CONTENT_KEYS`` is in decision-value order, so the record
    # spends its budget on the prose a reviewer judges first and names what it
    # could not afford last.
    for key in _WITHHELD_CONTENT_KEYS:
        raw = source.get(key)
        if raw is None or raw == "":
            continue
        text = raw if isinstance(raw, str) else str(raw)
        limit = min(WITHHELD_FINDING_EXCERPT_CHARS, remaining)
        if limit <= 0:
            content_omitted_keys.append(key)
            content_truncated = True
            continue
        if len(text) > limit:
            text = text[:limit]
            content_truncated = True
        remaining -= len(text)
        content[key] = text
    return {
        "disposition": str(disposition),
        "reason": str(reason)[:240],
        "finding_type": str(finding_type or source.get("type") or "improvement"),
        "routed_to": routed_to,
        "destination_id": str(destination_id) if destination_id else None,
        "direction_id": str(direction_id) if direction_id else None,
        "routing_applied": bool(routing_applied),
        "content": content,
        "content_truncated": content_truncated,
        "content_omitted_keys": content_omitted_keys,
        "finding_checksum": _withheld_checksum(source),
    }


def bounded_withheld_findings_carry(records: Sequence[Mapping]) -> dict | None:
    """Cap the carry and state the overflow outright.

    Type-routed records sort ahead of dedup-filtered ones: a dedup-filtered
    finding already has other durable surfaces (the direction's
    ``research_history`` filter breakdown and filtered-title list, and the
    salvage bundle), while a type-routed one has none, so it is the record the
    reviewer would otherwise lose entirely.
    """

    usable = [dict(record) for record in records if isinstance(record, Mapping)]
    if not usable:
        return None
    ordered = sorted(
        usable,
        key=lambda record: 0
        if record.get("disposition") == WITHHELD_DISPOSITION_TYPE_ROUTED
        else 1,
    )
    kept = ordered[:WITHHELD_FINDINGS_CAP]
    omitted = ordered[WITHHELD_FINDINGS_CAP:]
    return {
        "total_count": len(ordered),
        "records": kept,
        "omitted_count": len(omitted),
        "omitted_checksum": _withheld_checksum(omitted) if omitted else None,
    }


def withheld_findings_carry_from_state(
    context: object,
    *,
    status: object,
    task_type: object,
) -> dict | None:
    """Return the finalized withheld-finding carry, or ``None``.

    Gated exactly like ``finalized_research_findings``: the carry is authority
    only once research completion stamped its finalized marker inside the
    Project -> parent -> children lock boundary.  A pre-gate copy could still
    be rewritten by a retry, and evidence a reviewer binds on must not move
    under it.
    """

    if not isinstance(context, dict):
        return None
    if context.get(FINALIZED_RESEARCH_FINDINGS_KEY) is not True:
        return None
    if status not in (TaskStatus.COMPLETED, TaskStatus.EVALUATING):
        return None
    if str(task_type or "").strip().lower() != "research":
        return None
    carry = context.get(WITHHELD_FINDINGS_CONTEXT_KEY)
    if not isinstance(carry, dict):
        return None
    if not isinstance(carry.get("records"), list):
        return None
    records = [
        record for record in carry["records"] if isinstance(record, dict)
    ]
    return {
        "total_count": int(carry.get("total_count") or len(records)),
        "records": records,
        "omitted_count": int(carry.get("omitted_count") or 0),
        "omitted_checksum": carry.get("omitted_checksum"),
    }


def withheld_findings_carry(task: Task) -> dict | None:
    """Read one research task's finalized withheld-finding carry."""

    return withheld_findings_carry_from_state(
        getattr(task, "context", None),
        status=getattr(task, "status", None),
        task_type=(getattr(task, "first_payload", None) or {}).get("type"),
    )


class ResearchFinalizationDelivery(NamedTuple):
    """What survived finalization, and what was withheld with its content.

    Two disjoint streams, deliberately not merged: ``findings`` is the
    authoritative surviving set that becomes ``plan_output`` (and so the
    findings index, the survival signal, and every actionable-evidence
    surface), while ``withheld`` carries findings that were routed or filtered
    rather than delivered.  Folding the second into the first would make a
    design-doc observation look like shipped work; dropping it -- what this
    join did before -- is what made the decision packet's completeness
    manifest lie.
    """

    findings: list[dict]
    withheld: list[dict]


async def finalized_research_findings_for_survivors(
    session,
    parent_task: Task,
    candidates: Sequence[tuple[int, dict, str]],
    *,
    withheld_records: Sequence[Mapping] = (),
) -> ResearchFinalizationDelivery:
    """Join candidates to the locked delivery set, carrying the withheld set.

    The hash join answers exactly one question -- which candidate findings
    have a surviving, non-cancelled child task -- and it must keep answering
    only that: ``findings`` becomes ``plan_output``, the findings index, and
    the survival signal, so a routed observation folded in there would read as
    shipped work.

    But "no surviving child" is not the same fact as "nothing happened".  A
    ``design_doc_observation`` was routed into the design doc; a
    ``direction_update`` was written onto a direction row; a dedup-filtered
    finding was suppressed against known work.  Each of those has no child and
    therefore no ``finding_hash``, so the join used to drop it on the floor --
    and because the discovery decision packet reads ``plan_output`` and
    nothing else, the reviewer got zero evidence coverage under a manifest
    asserting nothing was omitted.  ``withheld`` is the second stream that
    closes that hole: bounded, structured, content-carrying, and reported.
    """

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
    withheld = [
        dict(record) for record in withheld_records if isinstance(record, Mapping)
    ]
    return ResearchFinalizationDelivery(findings=finalized, withheld=withheld)


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
