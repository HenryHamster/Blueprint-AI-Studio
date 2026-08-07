"""Completion-debt ledger — make merged-but-unfinished work first-class.

The ledger records obligations created when the system lands something in a
dormant / default-off / observe-only / residual state that still owes
follow-through:

  * ``dormant_knob``        — a runtime/config knob shipped default-off or in
                              observe mode (enumerated from ``runtime_config``);
  * ``capability_trial``    — a capability running in observe/trial mode with no
                              decided (adopt/reject) enforce verdict;
  * ``no_ci_gate``          — a no-CI merge gate that fell open to an UNVERIFIED
                              merge;
  * ``retirement_residual`` — a strategic retirement/deferral that left a
                              residual handoff behind;
Every entry is IDEMPOTENT on ``dedupe_key`` (globally unique): repeated recorder
or seed invocations upsert the same logical obligation rather than duplicating a
row.  Recording is BEST-EFFORT at every write site — the recorders never raise
and use a SAVEPOINT so a ledger failure rolls back only the debt insert, never
the caller's transaction.  A ledger failure must never break the merge,
capability-verdict, or strategic-retirement path that created the debt.

Most live write paths run on ``AsyncSession`` (the ``a*`` coroutine variants);
sync ``Session`` callers (and unit tests) use the plain variants.  A sync
recorder handed an ``AsyncSession`` cleanly no-ops (returns ``False``) rather
than issuing sync I/O against an async engine — such call sites rely on the
async recorders or the startup seed instead.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Iterable, Mapping

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm.attributes import flag_modified

from app.time_utils import _utcnow

logger = logging.getLogger(__name__)

# --- Debt kinds ---
KIND_DORMANT_KNOB = "dormant_knob"
KIND_CAPABILITY_TRIAL = "capability_trial"
KIND_NO_CI_GATE = "no_ci_gate"
# A release-machinery fix that was fast-tracked through the operator hotfix-deploy
# channel (skipping the scoring/audit gate). The bypass is owed follow-through:
# it stays open until the NORMAL gate re-qualifies the same content, so the
# hotfix contributes to debt-pressure rather than being silently blessed.
KIND_HOTFIX_DEPLOY_PENDING_BACKFILL = "hotfix_deploy_pending_backfill"
KIND_RETIREMENT_RESIDUAL = "retirement_residual"
# A working capability that already carries a structural path_proven proof
# (backing_path tier) but has NO discoverable covering test — permanently stuck
# because the execution-trial earn path requires a convention-named test file.
# The obligation surfaces this to the operator/strategist so a test can be
# added, unblocking the trial path and enabling graduation to ``proven``.
KIND_CAPABILITY_EARN_OBLIGATION = "capability_earn_obligation"
# An acceptance category (VersionAcceptanceCriterion / CategoryVerdict) the
# blind merge-grade judge graded NOT met (status != "met") for a candidate.
# Gives an UNMET category a durable consequence instead of letting the verdict
# evaporate: the entry stays open until a later grade run reports the SAME
# category as ``met``, at which point it auto-resolves.
KIND_OBJECTIVE_GAP = "objective_gap"

# --- Statuses ---
STATUS_OPEN = "open"
STATUS_RESOLVED = "resolved"
STATUS_DISMISSED = "dismissed"

# Bound the serialized JSONB metadata so the column cannot grow unbounded.
_MAX_DETAIL_BYTES = 4000
_MAX_TITLE_LEN = 500
_OPEN_COMPLETION_DEBT_LIMIT = 10


def hotfix_deploy_debt_dedupe_key(project_id: Any, candidate_sha: str) -> str:
    """Stable identity for the normal-gate backfill owed by one hotfix deploy."""
    return f"hotfix_deploy:{project_id}:{candidate_sha}"


def hotfix_deploy_alert_dedupe_key(project_id: Any, candidate_sha: str) -> str:
    """Stable identity for the operator alert accompanying one hotfix deploy."""
    return f"hotfix_deploy_alert:{project_id}:{candidate_sha}"


def capability_earn_obligation_dedupe_key(project_id: Any, cap_name: str) -> str:
    """Stable per-capability identity for a trial-ineligible earn obligation.

    A capability earns this obligation when it already carries a ``backing_path``
    (path_proven) proof but has NO discoverable covering test — so the execution-
    trial earn path is blocked until a convention-named test file is added.
    The key is deterministic and globally unique across projects and caps so
    repeated sweep ticks upsert the same obligation row rather than duplicating.
    """
    return f"capability_earn_obligation:{project_id}:{cap_name}"


def objective_gap_dedupe_key(version_id: Any, category_key: Any) -> str:
    """Stable identity for the objective_gap debt owed by one acceptance category.

    Keyed on ``(version_id, category_key)`` only — NOT ``candidate_sha`` — so a
    re-grade of the same category on a newer candidate tree upserts the SAME
    debt row (resolving it on ``met``, reopening it on regression) instead of
    accumulating a new row per candidate.
    """
    return f"objective_gap:{version_id}:{category_key}"


def _is_async_session(session: Any) -> bool:
    """True for an ``AsyncSession`` (which exposes ``.sync_session``).

    A plain sync ``Session`` has no ``sync_session`` attribute, so this cleanly
    distinguishes the two without importing the async class.
    """
    return hasattr(session, "sync_session")


def _bound_title(title: Any) -> str:
    text = str(title or "").strip() or "(unspecified completion debt)"
    return text[:_MAX_TITLE_LEN]


def _bound_detail(detail: Any) -> dict | None:
    """Return a size-bounded, JSON-serializable metadata dict (or ``None``).

    Bounding is STRUCTURE-PRESERVING for the growing ``linked_tasks`` list: when
    the serialized detail exceeds :data:`_MAX_DETAIL_BYTES`, the oversized
    ``linked_tasks`` list is trimmed (dropping the oldest records, always keeping
    the most-recent one) rather than collapsing the whole dict.  This keeps the
    recorder metadata and the ``linked_tasks`` list SHAPE intact so repeated
    :func:`link_task_to_debt` calls keep finding a list to append to instead of
    restarting from empty.  Only detail with no trimmable ``linked_tasks`` list
    (a pathologically large recorder payload) falls back to the flat truncated
    preview.
    """
    if detail is None:
        return None
    if not isinstance(detail, dict):
        detail = {"value": str(detail)}
    try:
        encoded = json.dumps(detail, default=str)
    except Exception:  # noqa: BLE001
        return {"_note": "detail-unserializable"}
    if len(encoded) <= _MAX_DETAIL_BYTES:
        return dict(detail)

    # Over budget. Prefer a structure-preserving trim: shrink the unbounded
    # ``linked_tasks`` list (drop oldest first) while keeping every other key —
    # recorder metadata and the list itself — so subsequent link_task_to_debt
    # calls still see a list to append to.  json.dumps here cannot raise since
    # the full detail already serialized above and ``bounded`` is a subset.
    linked = detail.get("linked_tasks")
    if isinstance(linked, list) and linked:
        bounded = dict(detail)
        trimmed = list(linked)
        original_linked_len = len(linked)
        while True:
            bounded["linked_tasks"] = trimmed
            if (
                len(json.dumps(bounded, default=str)) <= _MAX_DETAIL_BYTES
                or len(trimmed) <= 1
            ):
                # Record how many entries were dropped so callers can observe
                # that trimming occurred (useful for debugging and metrics).
                if len(trimmed) < original_linked_len:
                    bounded["_linked_tasks_trimmed"] = original_linked_len - len(trimmed)
                return bounded
            trimmed = trimmed[1:]  # drop the oldest linked record and retry

    return {
        "_truncated": True,
        "_original_bytes": len(encoded),
        "preview": encoded[:_MAX_DETAIL_BYTES],
    }


def _new_entry(
    *,
    dedupe_key: str,
    kind: str,
    title: str,
    project_id: Any,
    source: str | None,
    detail: Any,
):
    """Build an unsaved ``CompletionDebtEntry`` (imported lazily to avoid a
    models import cycle at module load)."""
    from app.models import CompletionDebtEntry

    return CompletionDebtEntry(
        project_id=project_id,
        kind=str(kind),
        dedupe_key=str(dedupe_key),
        status=STATUS_OPEN,
        title=_bound_title(title),
        source=(str(source) if source else None),
        detail=_bound_detail(detail),
    )


def _detail_to_text(detail: Any) -> str | None:
    if detail is None:
        return None
    if isinstance(detail, dict):
        for key in ("description", "summary", "backlog_item", "value"):
            value = detail.get(key)
            if value not in (None, ""):
                return str(value)
        try:
            return json.dumps(detail, sort_keys=True, default=str)
        except Exception:  # noqa: BLE001
            return str(detail)
    return str(detail)


def _serialize_debt(entry) -> dict[str, Any]:
    created_at = entry.created_at.isoformat() if entry.created_at else None
    resolved_at = entry.resolved_at.isoformat() if entry.resolved_at else None
    dismissed_at = resolved_at if entry.status == STATUS_DISMISSED else None
    return {
        "id": str(entry.id),
        "project_id": str(entry.project_id) if entry.project_id is not None else None,
        "kind": entry.kind,
        "title": entry.title,
        "detail": _detail_to_text(entry.detail),
        "status": entry.status,
        "dismiss_reason": (
            entry.resolution_reason if entry.status == STATUS_DISMISSED else None
        ),
        "created_at": created_at,
        "dismissed_at": dismissed_at,
        "resolved_at": resolved_at,
    }


async def _query_completion_debts(
    session: Any,
    *,
    project_id: Any,
    status: str | None = None,
    kind: str | None = None,
    limit: int = 100,
    offset: int = 0,
    newest_first: bool = True,
    project_first: bool = False,
) -> list[Any]:
    from app.models import CompletionDebtEntry
    from sqlalchemy import case, or_

    if project_id is not None:
        # Include both project-scoped rows and deployment-global rows (project_id
        # IS NULL — e.g. seed inventory and dormant runtime knobs that have no
        # owning project).
        q = select(CompletionDebtEntry).where(
            or_(
                CompletionDebtEntry.project_id == project_id,
                CompletionDebtEntry.project_id.is_(None),
            )
        )
    else:
        q = select(CompletionDebtEntry).where(
            CompletionDebtEntry.project_id.is_(None)
        )
    if status is not None:
        q = q.where(CompletionDebtEntry.status == status)
    if kind is not None:
        q = q.where(CompletionDebtEntry.kind == kind)
    if project_first and project_id is not None:
        # Float project-scoped rows above global (project_id IS NULL) rows so
        # the bounded snapshot shows per-project debt before global seed debt.
        scope_rank = case(
            (CompletionDebtEntry.project_id.is_(None), 1),
            else_=0,
        )
        if newest_first:
            q = q.order_by(
                scope_rank,
                CompletionDebtEntry.created_at.desc(),
                CompletionDebtEntry.id.desc(),
            )
        else:
            q = q.order_by(
                scope_rank,
                CompletionDebtEntry.created_at.asc(),
                CompletionDebtEntry.id.asc(),
            )
    elif newest_first:
        q = q.order_by(
            CompletionDebtEntry.created_at.desc(),
            CompletionDebtEntry.id.desc(),
        )
    else:
        q = q.order_by(
            CompletionDebtEntry.created_at.asc(),
            CompletionDebtEntry.id.asc(),
        )
    q = q.offset(max(0, int(offset))).limit(max(0, min(int(limit), 200)))
    result = await session.execute(q)
    return list(result.scalars().all())


async def _count_open_completion_debts(session: Any, project_id: Any) -> int:
    """Return the total count of open debt rows visible to this project."""
    return await count_completion_debts(session, project_id=project_id, status=STATUS_OPEN)


async def count_completion_debts(
    session: Any,
    *,
    project_id: Any,
    status: str | None = None,
    kind: str | None = None,
) -> int:
    """Return the total count of debt rows matching the given filters."""
    from app.models import CompletionDebtEntry
    from sqlalchemy import func, or_

    if project_id is not None:
        q = (
            select(func.count())
            .select_from(CompletionDebtEntry)
            .where(
                or_(
                    CompletionDebtEntry.project_id == project_id,
                    CompletionDebtEntry.project_id.is_(None),
                ),
            )
        )
    else:
        q = (
            select(func.count())
            .select_from(CompletionDebtEntry)
            .where(
                CompletionDebtEntry.project_id.is_(None),
            )
        )
    if status is not None:
        q = q.where(CompletionDebtEntry.status == status)
    if kind is not None:
        q = q.where(CompletionDebtEntry.kind == kind)
    return (await session.execute(q)).scalar_one()


async def list_completion_debts(
    session: Any,
    *,
    project_id: Any,
    status: str | None = None,
    kind: str | None = None,
    limit: int = 100,
    offset: int = 0,
) -> list[Any]:
    """Return completion-debt rows for a project, newest first."""
    return await _query_completion_debts(
        session,
        project_id=project_id,
        status=status,
        kind=kind,
        limit=limit,
        offset=offset,
        newest_first=True,
    )


async def dismiss_debt(
    session: Any,
    debt_id: Any,
    *,
    project_id: Any,
    reason: str,
):
    from app.models import CompletionDebtEntry

    entry = await session.get(CompletionDebtEntry, debt_id)
    if entry is None or (
        entry.project_id is not None and entry.project_id != project_id
    ):
        raise ValueError("not_found")
    if entry.status != STATUS_OPEN:
        raise ValueError("not_open")
    reason_text = str(reason).strip()
    if not reason_text:
        raise ValueError("invalid_reason")
    async with session.begin_nested():
        entry.status = STATUS_DISMISSED
        entry.resolution_reason = reason_text[:_MAX_TITLE_LEN]
        entry.resolved_at = _utcnow()
        entry.updated_at = _utcnow()
        session.add(entry)
        await session.flush()
    return entry


async def resolve_debt(
    session: Any,
    debt_id: Any,
    *,
    project_id: Any,
    reason: str | None = None,
):
    """Flip an OPEN debt row to resolved, optionally stamping an evidence reason.

    ``reason`` is keyword-optional so the admin resolve endpoint (which passes no
    evidence) keeps working unchanged; when a strategist supplies disposition
    evidence it is persisted (bounded) to ``resolution_reason``.
    """
    from app.models import CompletionDebtEntry

    entry = await session.get(CompletionDebtEntry, debt_id)
    if entry is None or (
        entry.project_id is not None and entry.project_id != project_id
    ):
        raise ValueError("not_found")
    if entry.status != STATUS_OPEN:
        raise ValueError("not_open")
    reason_text = str(reason).strip() if reason is not None else ""
    async with session.begin_nested():
        entry.status = STATUS_RESOLVED
        entry.resolution_reason = reason_text[:_MAX_TITLE_LEN] if reason_text else None
        entry.resolved_at = _utcnow()
        entry.updated_at = _utcnow()
        session.add(entry)
        await session.flush()
    return entry


# Bound the number of linked-task records stamped onto a debt entry's detail so
# repeated linkage can never grow the JSONB column unbounded.
_MAX_LINKED_TASKS = 25


async def link_task_to_debt(
    session: Any,
    debt_id: Any,
    *,
    project_id: Any,
    task_id: Any,
    note: str | None = None,
):
    """Record that a spawned task addresses an OPEN completion-debt entry.

    Appends ``{task_id, note?}`` to the entry's ``detail['linked_tasks']`` list
    (deduplicated on ``task_id`` and bounded to :data:`_MAX_LINKED_TASKS`). Reuses
    the ``not_found`` / ``not_open`` ValueError contract shared with
    :func:`resolve_debt` / :func:`dismiss_debt`. The row stays OPEN — linkage is a
    forward pointer, not a disposition; the strategist closes the row separately
    once the work merges.
    """
    from app.models import CompletionDebtEntry

    task_ref = str(task_id).strip()
    if not task_ref:
        raise ValueError("invalid_task")
    # Use SELECT ... FOR UPDATE inside the savepoint so concurrent callers
    # serialize on this row.  Without the lock, two concurrent links to the same
    # entry both read detail, compute their own merged list, and the later writer
    # silently discards the earlier one's linked-task record.
    async with session.begin_nested():
        result = await session.execute(
            select(CompletionDebtEntry)
            .where(CompletionDebtEntry.id == debt_id)
            .with_for_update()
        )
        entry = result.scalar_one_or_none()
        if entry is None or (
            entry.project_id is not None and entry.project_id != project_id
        ):
            raise ValueError("not_found")
        if entry.status != STATUS_OPEN:
            raise ValueError("not_open")
        detail = dict(entry.detail) if isinstance(entry.detail, dict) else {}
        linked = detail.get("linked_tasks")
        linked = [x for x in linked if isinstance(x, dict)] if isinstance(linked, list) else []
        if not any(x.get("task_id") == task_ref for x in linked):
            record: dict[str, Any] = {"task_id": task_ref}
            note_text = str(note).strip() if note else ""
            if note_text:
                record["note"] = note_text[:_MAX_TITLE_LEN]
            linked.append(record)
        detail["linked_tasks"] = linked[-_MAX_LINKED_TASKS:]
        entry.detail = _bound_detail(detail)
        entry.updated_at = _utcnow()
        flag_modified(entry, "detail")
        session.add(entry)
        await session.flush()
    return entry


async def astamp_cut_recommendation(
    session: Any,
    dedupe_key: str,
    *,
    recommendation: dict,
) -> bool:
    """Stamp a legible CUT recommendation onto an OPEN debt row's detail (ASYNC).

    The default-off PRINCIPLE demands that a dormant knob be either graduated on
    measured lift OR cut with measured numbers — but there was no CUT surface on
    the ledger.  This is that surface: the shared mode-knob graduation harness
    (:mod:`app.mode_knob_graduation`) calls it when a knob's counterfactual shows
    the enforce branch would never bind, merging ``recommendation`` (the measured
    signal — would-have-blocked counts, pressure, window) under
    ``detail['cut_recommendation']`` so an operator reviewing the completion-debt
    ledger sees WHY the knob should be removed, not just that it is dormant.

    Idempotent-by-overwrite: re-running replaces the recommendation with the
    latest measurement.  The row stays OPEN — a CUT recommendation is advisory
    signal, not a disposition; the operator (or a follow-up removal PR) closes the
    row.  Best-effort and non-fatal: a missing/non-open row or any error returns
    ``False`` and never raises, mirroring the other ledger mutators.  Returns
    ``True`` iff the recommendation was stamped.
    """
    if not dedupe_key or session is None or not isinstance(recommendation, dict):
        return False
    try:
        from app.models import CompletionDebtEntry

        async with session.begin_nested():
            entry = (
                await session.execute(
                    select(CompletionDebtEntry)
                    .where(
                        CompletionDebtEntry.dedupe_key == dedupe_key,
                        CompletionDebtEntry.status == STATUS_OPEN,
                    )
                    .with_for_update()
                )
            ).scalar_one_or_none()
            if entry is None:
                return False
            detail = dict(entry.detail) if isinstance(entry.detail, dict) else {}
            detail["cut_recommendation"] = dict(recommendation)
            entry.detail = _bound_detail(detail)
            entry.updated_at = _utcnow()
            flag_modified(entry, "detail")
            session.add(entry)
            await session.flush()
        return True
    except Exception:  # noqa: BLE001
        logger.warning(
            "[COMPLETION_DEBT] failed to stamp cut recommendation (key=%s)",
            dedupe_key,
            exc_info=True,
        )
        return False


def debt_to_dict(entry) -> dict[str, Any]:
    return _serialize_debt(entry)


async def resolve_linked_task_statuses(
    session: Any,
    task_ids: list[str],
) -> dict[str, str]:
    """Return a mapping of task_id -> status string for the given task ids.

    Missing ids (no matching Task row) are absent from the returned dict.
    Performs a single bounded query so callers can batch across many debt entries.
    """
    if not task_ids:
        return {}
    import uuid as _uuid

    from app.models import Task

    valid_ids: list[_uuid.UUID] = []
    for tid in task_ids:
        try:
            valid_ids.append(_uuid.UUID(str(tid)))
        except (ValueError, AttributeError, TypeError):
            continue
    if not valid_ids:
        return {}
    rows = (
        await session.execute(select(Task.id, Task.status).where(Task.id.in_(valid_ids)))
    ).all()
    result: dict[str, str] = {}
    for row in rows:
        status = row.status
        result[str(row.id)] = status.value if hasattr(status, "value") else str(status)
    return result


async def get_open_completion_debt(session: Any, project_id: Any) -> dict[str, Any]:
    """Return a bounded, project-scoped open-debt snapshot for strategist use.

    Returns ``{"items": [...], "total_open": N}`` where ``total_open`` is the
    true count of open rows visible to this project (may exceed ``len(items)``
    when the ledger has more than ``_OPEN_COMPLETION_DEBT_LIMIT`` open rows).
    Project-scoped rows appear before global seed rows in the snapshot.

    Each item carries ``linked_tasks`` (a list of ``{task_id, status}`` dicts)
    when the debt entry has referenced tasks so the strategist can see whether
    follow-up work is still in flight or already completed.  Dormant-knob rows
    surface their human title as the description instead of the bare knob value.
    """
    total_open = await _count_open_completion_debts(session, project_id)
    entries = await _query_completion_debts(
        session,
        project_id=project_id,
        status=STATUS_OPEN,
        limit=_OPEN_COMPLETION_DEBT_LIMIT,
        offset=0,
        newest_first=False,
        project_first=True,
    )

    # Collect all linked task ids from all entries in one pass so we can
    # resolve their live statuses in a single DB round-trip.
    all_linked_ids: list[str] = []
    entry_linked_records: dict[str, list[dict]] = {}
    for entry in entries:
        detail_dict = entry.detail if isinstance(entry.detail, dict) else {}
        raw_linked = detail_dict.get("linked_tasks")
        recs = (
            [x for x in raw_linked if isinstance(x, dict)]
            if isinstance(raw_linked, list)
            else []
        )
        entry_linked_records[str(entry.id)] = recs
        for x in recs:
            tid = x.get("task_id")
            if tid:
                all_linked_ids.append(str(tid))
    status_map = await resolve_linked_task_statuses(session, all_linked_ids)

    now = _utcnow()
    items: list[dict[str, Any]] = []
    for entry in entries:
        created_at = entry.created_at

        # For dormant_knob rows the detail["value"] is just "off"/"observe" —
        # return the human title instead so it is never shadowed by a bare knob
        # state.  For other kinds, use the detail text when it adds information.
        if entry.kind == KIND_DORMANT_KNOB:
            description = entry.title
        else:
            raw_desc = _detail_to_text(entry.detail)
            description = raw_desc if raw_desc else entry.title

        # Build linked tasks list with resolved live statuses.
        recs = entry_linked_records.get(str(entry.id), [])
        linked_tasks: list[dict[str, Any]] = []
        for x in recs:
            tid = x.get("task_id")
            if not tid:
                continue
            lt: dict[str, Any] = {
                "task_id": str(tid),
                "status": status_map.get(str(tid), "unknown"),
            }
            if x.get("note"):
                lt["note"] = x["note"]
            linked_tasks.append(lt)

        item: dict[str, Any] = {
            "id": str(entry.id),
            "kind": entry.kind,
            "title": entry.title,
            "description": description,
            "project_id": str(entry.project_id) if entry.project_id is not None else None,
            "created_at": created_at.isoformat() if created_at else None,
            "age_days": (
                max(0, (now - created_at).days) if created_at is not None else None
            ),
        }
        if linked_tasks:
            item["linked_tasks"] = linked_tasks
        items.append(item)
    return {"items": items, "total_open": total_open}


# ── Recorders ────────────────────────────────────────────────────────────────


def record_completion_debt(
    session: Any,
    *,
    dedupe_key: str,
    kind: str,
    title: str,
    project_id: Any = None,
    source: str | None = None,
    detail: Any = None,
) -> bool:
    """Best-effort idempotent open of a completion-debt entry (SYNC session).

    Returns ``True`` iff a NEW open row was created, ``False`` if an entry with
    the same ``dedupe_key`` already existed, the session is async, or recording
    failed.  Never raises.
    """
    if not dedupe_key or session is None:
        return False
    if _is_async_session(session):
        # Called from a sync code path that was, in production, handed an async
        # session (e.g. the no-CI gate runtime recorder). Sync I/O against an
        # async engine would raise; defer to the async recorders / startup seed.
        return False
    try:
        from app.models import CompletionDebtEntry

        existing_open = session.execute(
            select(CompletionDebtEntry).where(
                CompletionDebtEntry.dedupe_key == dedupe_key,
                CompletionDebtEntry.status == STATUS_OPEN,
            )
        ).scalar_one_or_none()
        if existing_open is not None:
            return False
        # Check for a resolved row to reopen. DISMISSED rows must NOT be
        # reopened — the operator's dismissal reason and timestamp are permanent
        # against startup seeding and recurring obligation re-records.
        existing_any = session.execute(
            select(CompletionDebtEntry).where(
                CompletionDebtEntry.dedupe_key == dedupe_key
            )
        ).scalar_one_or_none()
        if existing_any is not None:
            if existing_any.status == STATUS_DISMISSED:
                return False
            try:
                with session.begin_nested():
                    existing_any.status = STATUS_OPEN
                    existing_any.resolved_at = None
                    existing_any.resolution_reason = None
                    existing_any.updated_at = _utcnow()
                    session.add(existing_any)
                    session.flush()
            except Exception:  # noqa: BLE001
                return False
            return True
        entry = _new_entry(
            dedupe_key=dedupe_key,
            kind=kind,
            title=title,
            project_id=project_id,
            source=source,
            detail=detail,
        )
        try:
            with session.begin_nested():
                session.add(entry)
                session.flush()
        except IntegrityError:
            # A concurrent writer inserted the same dedupe_key first — the
            # "one open row per obligation" invariant still holds.
            return False
        return True
    except Exception:  # noqa: BLE001
        logger.warning(
            "[COMPLETION_DEBT] failed to record debt (kind=%s key=%s)",
            kind,
            dedupe_key,
            exc_info=True,
        )
        return False


async def arecord_completion_debt(
    session: Any,
    *,
    dedupe_key: str,
    kind: str,
    title: str,
    project_id: Any = None,
    source: str | None = None,
    detail: Any = None,
) -> bool:
    """Best-effort idempotent open of a completion-debt entry (ASYNC session).

    Async analogue of :func:`record_completion_debt`; never raises.
    """
    if not dedupe_key or session is None:
        return False
    try:
        from app.models import CompletionDebtEntry

        existing_open = (
            await session.execute(
                select(CompletionDebtEntry).where(
                    CompletionDebtEntry.dedupe_key == dedupe_key,
                    CompletionDebtEntry.status == STATUS_OPEN,
                )
            )
        ).scalar_one_or_none()
        if existing_open is not None:
            return False
        # Check for a resolved row to reopen. DISMISSED rows must NOT be
        # reopened — the operator's dismissal reason and timestamp are permanent
        # against startup seeding and recurring obligation re-records.
        existing_any = (
            await session.execute(
                select(CompletionDebtEntry).where(
                    CompletionDebtEntry.dedupe_key == dedupe_key
                )
            )
        ).scalar_one_or_none()
        if existing_any is not None:
            if existing_any.status == STATUS_DISMISSED:
                return False
            try:
                async with session.begin_nested():
                    existing_any.status = STATUS_OPEN
                    existing_any.resolved_at = None
                    existing_any.resolution_reason = None
                    existing_any.updated_at = _utcnow()
                    session.add(existing_any)
                    await session.flush()
            except Exception:  # noqa: BLE001
                return False
            return True
        entry = _new_entry(
            dedupe_key=dedupe_key,
            kind=kind,
            title=title,
            project_id=project_id,
            source=source,
            detail=detail,
        )
        try:
            async with session.begin_nested():
                session.add(entry)
                await session.flush()
        except IntegrityError:
            return False
        return True
    except Exception:  # noqa: BLE001
        logger.warning(
            "[COMPLETION_DEBT] failed to record debt (kind=%s key=%s)",
            kind,
            dedupe_key,
            exc_info=True,
        )
        return False


# ── Resolvers ────────────────────────────────────────────────────────────────


def _apply_resolution(entry, reason: str | None) -> None:
    entry.status = STATUS_RESOLVED
    entry.resolved_at = _utcnow()
    entry.updated_at = _utcnow()
    if reason:
        entry.resolution_reason = str(reason)[:_MAX_TITLE_LEN]


def resolve_completion_debt(
    session: Any,
    dedupe_key: str,
    *,
    reason: str | None = None,
) -> bool:
    """Best-effort resolve of an OPEN debt entry (SYNC session).

    Returns ``True`` iff an open row was found and flipped to resolved. Never
    raises. A missing or already-resolved entry is a no-op. A sync session
    handed an ``AsyncSession`` cleanly no-ops (returns ``False``).
    """
    if not dedupe_key or session is None:
        return False
    if _is_async_session(session):
        return False
    try:
        from app.models import CompletionDebtEntry

        entry = session.execute(
            select(CompletionDebtEntry).where(
                CompletionDebtEntry.dedupe_key == dedupe_key,
                CompletionDebtEntry.status == STATUS_OPEN,
            )
        ).scalar_one_or_none()
        if entry is None:
            return False
        with session.begin_nested():
            _apply_resolution(entry, reason)
            session.add(entry)
            session.flush()
        return True
    except Exception:  # noqa: BLE001
        logger.warning(
            "[COMPLETION_DEBT] failed to resolve debt (key=%s)",
            dedupe_key,
            exc_info=True,
        )
        return False


async def aresolve_completion_debt(
    session: Any,
    dedupe_key: str,
    *,
    reason: str | None = None,
) -> bool:
    """Best-effort resolve of an OPEN debt entry (ASYNC session).

    Returns ``True`` iff an open row was found and flipped to resolved. Never
    raises. A missing or already-resolved entry is a no-op.
    """
    if not dedupe_key or session is None:
        return False
    try:
        from app.models import CompletionDebtEntry

        entry = (
            await session.execute(
                select(CompletionDebtEntry).where(
                    CompletionDebtEntry.dedupe_key == dedupe_key,
                    CompletionDebtEntry.status == STATUS_OPEN,
                )
            )
        ).scalar_one_or_none()
        if entry is None:
            return False
        # Flush the resolution inside a SAVEPOINT so it is visible to later
        # queries in this session regardless of the session's autoflush setting,
        # and so a failure rolls back only this update (never the caller's txn).
        async with session.begin_nested():
            _apply_resolution(entry, reason)
            session.add(entry)
            await session.flush()
        return True
    except Exception:  # noqa: BLE001
        logger.warning(
            "[COMPLETION_DEBT] failed to resolve debt (key=%s)",
            dedupe_key,
            exc_info=True,
        )
        return False


def deferred_retirement_dedupe_key(project_id: Any, direction_id: Any) -> str:
    """Return the stable dedupe key for a gate-DEFERRED retirement residual.

    This is the ``:deferred`` sibling of the ordinary retirement handoff key
    ``retirement_residual:{project}:{direction}`` written by
    ``_spawn_retirement_memos``.  The two are DISTINCT obligations: the deferred
    row is the "the retire is still owed once the gate clears" residual that this
    module resolves on success; the un-suffixed row is the retirement handoff
    memo debt, which must stay open.
    """
    return f"retirement_residual:{project_id}:{direction_id}:deferred"


async def aresolve_deferred_retirement_debt(
    session: Any,
    project_id: Any,
    direction_id: Any,
) -> bool:
    """Best-effort resolve of the gate-DEFERRED retirement residual (ASYNC).

    Called when a previously-deferred retirement finally succeeds (via the
    strategic ``retire_direction`` endpoint or the log-parse fallback). Resolves
    ONLY the ``:deferred`` key; the ordinary ``retirement_residual:{p}:{d}`` memo
    handoff obligation is intentionally left untouched. Delegates to
    :func:`aresolve_completion_debt`, which is non-fatal, skips missing/already
    resolved rows, and never reopens a dismissed row. Never raises.
    """
    return await aresolve_completion_debt(
        session,
        deferred_retirement_dedupe_key(project_id, direction_id),
        reason="retirement succeeded",
    )


async def aresolve_hotfix_deploy_backfill_debts(
    session: Any,
    project_id: Any,
    *,
    reason: str | None = None,
) -> int:
    """Best-effort discharge of a project's open hotfix-deploy backfill debts.

    The operator hotfix-deploy channel opens a
    :data:`KIND_HOTFIX_DEPLOY_PENDING_BACKFILL` row (keyed
    ``hotfix_deploy:{project}:{candidate_sha}``) recording that a fast-tracked
    release-machinery promotion bypassed the audit/scoring gate and is owed
    re-qualification by the NORMAL gate.  Nothing else resolves it, so it stays
    open forever and permanently depresses the debt-pressure sub-score.

    Called when the normal qualification gate clears for the project — that IS
    the owed re-qualification.  Resolves EVERY open hotfix-deploy backfill row
    for ``project_id`` (a project may have accumulated several fast-tracks before
    the next normal gate ran) and returns the number resolved.  Delegates each
    flip to :func:`aresolve_completion_debt`, which is non-fatal, skips
    already-resolved rows, and never reopens a dismissed row.  Never raises.
    """
    if session is None or project_id is None:
        return 0
    try:
        from app.models import CompletionDebtEntry

        open_rows = (
            await session.execute(
                select(CompletionDebtEntry).where(
                    CompletionDebtEntry.project_id == project_id,
                    CompletionDebtEntry.kind
                    == KIND_HOTFIX_DEPLOY_PENDING_BACKFILL,
                    CompletionDebtEntry.status == STATUS_OPEN,
                )
            )
        ).scalars().all()
        resolved = 0
        for entry in open_rows:
            if await aresolve_completion_debt(
                session, entry.dedupe_key, reason=reason
            ):
                resolved += 1
        return resolved
    except Exception:  # noqa: BLE001
        logger.warning(
            "[COMPLETION_DEBT] failed to resolve hotfix-deploy backfill debts "
            "(project=%s)",
            project_id,
            exc_info=True,
        )
        return 0


# ── Seed inventory ───────────────────────────────────────────────────────────


def _iter_seed_debts() -> Iterable[dict]:
    """All seed rows: the currently-dormant runtime-config knobs enumerated by
    ``runtime_config_state``."""
    try:
        from app.runtime_config_state import iter_dormant_knob_debts

        yield from iter_dormant_knob_debts()
    except Exception:  # noqa: BLE001
        logger.warning(
            "[COMPLETION_DEBT] failed to enumerate dormant knob debts for seed",
            exc_info=True,
        )


def seed_completion_debt_inventory(session: Any) -> int:
    """Seed the ledger from the HEAD dormant-state inventory (SYNC session).

    Idempotent: each item upserts on its stable ``dedupe_key`` so repeated
    startup/test calls never duplicate rows. Returns the count of NEW rows
    created this call.
    """
    created = 0
    for item in _iter_seed_debts():
        if record_completion_debt(session, **item):
            created += 1
    return created


async def aseed_completion_debt_inventory(session: Any) -> int:
    """Seed the ledger from the HEAD dormant-state inventory (ASYNC session).

    Async analogue of :func:`seed_completion_debt_inventory`.
    """
    created = 0
    for item in _iter_seed_debts():
        if await arecord_completion_debt(session, **item):
            created += 1
    return created


async def areconcile_dormant_knob_debts(
    session: Any, runtime_config: Mapping[str, Any] | None = None
) -> dict[str, int]:
    """Best-effort reconcile dormant-knob debt rows against runtime config.

    Current dormant knobs are (re)opened if needed. Dormant-knob rows whose
    key is no longer dormant are resolved in place so startup and live settings
    changes converge immediately instead of waiting for a later seed pass.
    """
    stats = {"opened": 0, "resolved": 0}
    if session is None:
        return stats
    try:
        from app.models import CompletionDebtEntry
        from app.runtime_config_state import (
            _DORMANT_KNOB_VALUES,
            _DORMANT_MODE_KNOBS,
            _DORMANT_OFF_FLAGS,
        )

        # Dedupe keys the knob registry owns. Stale resolution below must only
        # touch these (or rows the registry itself wrote) — dormant_knob debts
        # recorded through other flows (strategist/admin, project-scoped) are
        # NOT ours to resolve just because no current knob produces their key.
        registry_keys = {
            f"dormant_knob:{key}"
            for key in (*_DORMANT_MODE_KNOBS, *_DORMANT_OFF_FLAGS)
        }

        if runtime_config is None:
            from app.runtime_config_state import iter_dormant_knob_debts

            dormant_by_key = {
                item["dedupe_key"]: item for item in iter_dormant_knob_debts()
            }
        else:
            dormant_by_key = {}
            for key, title in _DORMANT_MODE_KNOBS.items():
                value = str(runtime_config.get(key, "")).lower()
                if value in _DORMANT_KNOB_VALUES:
                    dormant_by_key[f"dormant_knob:{key}"] = {
                        "dedupe_key": f"dormant_knob:{key}",
                        "kind": KIND_DORMANT_KNOB,
                        "title": title,
                        "project_id": None,
                        "source": "runtime_config_state",
                        "detail": {"knob": key, "value": value},
                    }
            for key, title in _DORMANT_OFF_FLAGS.items():
                if not runtime_config.get(key, False):
                    dormant_by_key[f"dormant_knob:{key}"] = {
                        "dedupe_key": f"dormant_knob:{key}",
                        "kind": KIND_DORMANT_KNOB,
                        "title": title,
                        "project_id": None,
                        "source": "runtime_config_state",
                        "detail": {"knob": key, "value": bool(runtime_config.get(key))},
                    }

        for item in dormant_by_key.values():
            if await arecord_completion_debt(session, **item):
                stats["opened"] += 1

        stale_rows = (
            await session.execute(
                select(CompletionDebtEntry).where(
                    CompletionDebtEntry.kind == KIND_DORMANT_KNOB,
                    CompletionDebtEntry.status == STATUS_OPEN,
                )
            )
        ).scalars().all()
        for entry in stale_rows:
            if entry.dedupe_key in dormant_by_key:
                continue
            registry_owned = (
                entry.dedupe_key in registry_keys
                or entry.source == "runtime_config_state"
            )
            if not registry_owned:
                continue
            if await aresolve_completion_debt(
                session, entry.dedupe_key, reason="graduated:runtime-config"
            ):
                stats["resolved"] += 1
        return stats
    except Exception:  # noqa: BLE001
        logger.warning(
            "[COMPLETION_DEBT] failed to reconcile dormant knob debts",
            exc_info=True,
        )
        return stats


def _build_completion_debt_design_doc_section() -> dict:
    """Build the completion-debt-ledger system description section.

    Returns the section dict stored under the dedicated ``project_telemetry``
    row (key ``systems/completion-debt-ledger``).
    """
    return {
        "summary": "Durable ledger that makes merged-but-unfinished work first-class, recording obligations created by dormant knobs, capability trials, no-CI merges, hotfix deploy backfills, retirement residuals, and capability earn obligations.",
        "status": "active",
        "debt_kinds": [
            KIND_DORMANT_KNOB,
            KIND_CAPABILITY_TRIAL,
            KIND_NO_CI_GATE,
            KIND_HOTFIX_DEPLOY_PENDING_BACKFILL,
            KIND_RETIREMENT_RESIDUAL,
            KIND_CAPABILITY_EARN_OBLIGATION,
        ],
        "recorders": {
            "dormant_knob": "backend/app/runtime_config_state.py",
            "capability_trial": "backend/app/capability_trial_verdict.py",
            "no_ci_gate": "backend/app/ci_resolution.py",
            "hotfix_deploy_pending_backfill": "backend/app/release_qualifier.py",
            "retirement_residual": "backend/app/strategic_completion.py",
            "capability_earn_obligation": "backend/app/metrics_integrity_sweep.py",
        },
        "reconciliation_surfaces": [
            "knob graduation via admin settings endpoint resolves dormant-knob debt",
            "decided capability verdicts resolve capability-trial debt",
            "a verified real-test run via record_no_ci_gate_runtime auto-resolves no_ci_gate debt",
            "a passing normal qualification gate resolves hotfix-deploy backfill debt",
            "adding a discoverable covering test file resolves capability earn obligation debt",
        ],
        "strategist_surface": "open_completion_debt payload section / ./fetch completion-debt verb",
        "admin_surface": "PATCH /api/admin/completion-debt endpoints (list, dismiss, resolve)",
        "module": "backend/app/completion_debt.py",
    }


async def seed_completion_debt_design_doc_section(session: Any, project_id: Any) -> bool:
    """Seed the completion-debt ledger system section (idempotent per project).

    God-column cutover: the section is stored SOLELY in the dedicated
    ``project_telemetry`` row (key ``systems/completion-debt-ledger``) — the
    design_doc write leg is cut, so this no longer takes the Project row lock.
    Seeds the row when absent and refreshes code-owned fields (summary, status,
    debt_kinds, etc.) without clobbering operator- or strategist-authored
    additions carried on the existing row.

    Returns ``True`` if the row was newly written or code fields were refreshed,
    ``False`` if the row already exists with current code state.
    """
    if session is None or project_id is None:
        return False

    from app.project_telemetry import read_telemetry, set_project_telemetry

    section_key = "systems/completion-debt-ledger"
    new_section = _build_completion_debt_design_doc_section()

    try:
        existing = await read_telemetry(session, project_id, section_key)
        changed = False
        if existing is None:
            # No row yet — seed it as code-authored.
            section = {**new_section, "_authored_by": "code"}
            changed = True
        else:
            # Row exists — start with existing (preserves operator/strategist
            # keys). Then refresh code-owned fields only (those in new_section).
            section = dict(existing)
            for key, value in new_section.items():
                if section.get(key) != value:
                    section[key] = value
                    changed = True
            # Mark authorship if missing (does not by itself count as a change,
            # matching the pre-cutover semantics).
            if "_authored_by" not in section:
                section["_authored_by"] = "code"

        if changed:
            await set_project_telemetry(session, project_id, section_key, section)
        return changed
    except Exception:  # noqa: BLE001
        logger.warning(
            "[COMPLETION_DEBT] failed to seed completion-debt ledger section for project %s",
            project_id,
            exc_info=True,
        )
        return False
