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
  * ``seed_inventory``      — a standing backlog item seeded once from the HEAD
                              dormant-state inventory.

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
from typing import Any, Iterable

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.time_utils import _utcnow

logger = logging.getLogger(__name__)

# --- Debt kinds ---
KIND_DORMANT_KNOB = "dormant_knob"
KIND_CAPABILITY_TRIAL = "capability_trial"
KIND_NO_CI_GATE = "no_ci_gate"
KIND_RETIREMENT_RESIDUAL = "retirement_residual"
KIND_SEED_INVENTORY = "seed_inventory"

# --- Statuses ---
STATUS_OPEN = "open"
STATUS_RESOLVED = "resolved"
STATUS_DISMISSED = "dismissed"

# Bound the serialized JSONB metadata so the column cannot grow unbounded.
_MAX_DETAIL_BYTES = 4000
_MAX_TITLE_LEN = 500
_OPEN_COMPLETION_DEBT_LIMIT = 10


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
    """Return a size-bounded, JSON-serializable metadata dict (or ``None``)."""
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
                CompletionDebtEntry.status == STATUS_OPEN,
            )
        )
    else:
        q = (
            select(func.count())
            .select_from(CompletionDebtEntry)
            .where(
                CompletionDebtEntry.project_id.is_(None),
                CompletionDebtEntry.status == STATUS_OPEN,
            )
        )
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
):
    from app.models import CompletionDebtEntry

    entry = await session.get(CompletionDebtEntry, debt_id)
    if entry is None or (
        entry.project_id is not None and entry.project_id != project_id
    ):
        raise ValueError("not_found")
    if entry.status != STATUS_OPEN:
        raise ValueError("not_open")
    async with session.begin_nested():
        entry.status = STATUS_RESOLVED
        entry.resolution_reason = None
        entry.resolved_at = _utcnow()
        entry.updated_at = _utcnow()
        session.add(entry)
        await session.flush()
    return entry


def debt_to_dict(entry) -> dict[str, Any]:
    return _serialize_debt(entry)


async def get_open_completion_debt(session: Any, project_id: Any) -> dict[str, Any]:
    """Return a bounded, project-scoped open-debt snapshot for strategist use.

    Returns ``{"items": [...], "total_open": N}`` where ``total_open`` is the
    true count of open rows visible to this project (may exceed ``len(items)``
    when the ledger has more than ``_OPEN_COMPLETION_DEBT_LIMIT`` open rows).
    Project-scoped rows appear before global seed rows in the snapshot.
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
    now = _utcnow()
    items: list[dict[str, Any]] = []
    for entry in entries:
        created_at = entry.created_at
        items.append(
            {
                "id": str(entry.id),
                "kind": entry.kind,
                "title": entry.title,
                "description": _detail_to_text(entry.detail) or entry.title,
                "created_at": created_at.isoformat() if created_at else None,
                "age_days": (
                    max(0, (now - created_at).days) if created_at is not None else None
                ),
            }
        )
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


# ── Seed inventory ───────────────────────────────────────────────────────────


def _standing_inventory_debts() -> list[dict]:
    """Static, deployment-global backlog items present on HEAD that are not
    already enumerable from runtime-config knobs."""
    return [
        {
            "dedupe_key": "seed:no_ci_gate_live_trial",
            "kind": KIND_NO_CI_GATE,
            "title": (
                "No-CI merge gate live-trial: the gate may fall open to "
                "UNVERIFIED merges until an enforce policy is proven"
            ),
            "project_id": None,
            "source": KIND_SEED_INVENTORY,
            "detail": {"backlog_item": "no_ci_gate_live_trial"},
        },
        {
            "dedupe_key": "seed:capability_trial_rail",
            "kind": KIND_CAPABILITY_TRIAL,
            "title": (
                "Capability-trial/adoption rail: observe-mode capabilities "
                "lacking a decided enforce verdict"
            ),
            "project_id": None,
            "source": KIND_SEED_INVENTORY,
            "detail": {"backlog_item": "capability_trial_adoption"},
        },
        {
            "dedupe_key": "seed:adversarial_observe_watch",
            "kind": KIND_RETIREMENT_RESIDUAL,
            "title": (
                "Adversarial / retirement observe-watch rail: challenges are "
                "recorded but never enforced pending graduation"
            ),
            "project_id": None,
            "source": KIND_SEED_INVENTORY,
            "detail": {"backlog_item": "adversarial_observe_watch"},
        },
    ]


def _iter_seed_debts() -> Iterable[dict]:
    """All seed rows: the static standing inventory plus the currently-dormant
    runtime-config knobs enumerated by ``runtime_config_state``."""
    yield from _standing_inventory_debts()
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


def _build_completion_debt_design_doc_section() -> dict:
    """Build the completion-debt-ledger system description for the design doc.

    Returns a dict suitable for insertion into ``design_doc["systems/completion-debt-ledger"]``.
    """
    return {
        "summary": "Durable ledger that makes merged-but-unfinished work first-class, recording obligations created by dormant knobs, capability trials, no-CI merges, and retirement residuals.",
        "status": "active",
        "debt_kinds": [
            KIND_DORMANT_KNOB,
            KIND_CAPABILITY_TRIAL,
            KIND_NO_CI_GATE,
            KIND_RETIREMENT_RESIDUAL,
            KIND_SEED_INVENTORY,
        ],
        "recorders": {
            "dormant_knob": "backend/app/runtime_config_state.py",
            "capability_trial": "backend/app/capability_trial_verdict.py",
            "no_ci_gate": "backend/app/ci_resolution.py",
            "retirement_residual": "backend/app/strategic_completion.py",
            "seed_inventory": "backend/app/completion_debt.py",
        },
        "reconciliation_surfaces": [
            "knob graduation via admin settings endpoint resolves dormant-knob debt",
            "decided capability verdicts resolve capability-trial debt",
        ],
        "strategist_surface": "open_completion_debt payload section / ./fetch completion-debt verb",
        "admin_surface": "PATCH /api/admin/completion-debt endpoints (list, dismiss, resolve)",
        "module": "backend/app/completion_debt.py",
    }


async def seed_completion_debt_design_doc_section(session: Any, project_id: Any) -> bool:
    """Seed the design-doc section for completion-debt ledger (idempotent per project).

    Writes to ``project.design_doc["systems/completion-debt-ledger"]`` if absent,
    and refreshes code-owned fields (summary, status, debt_kinds, etc.) without
    clobbering operator- or strategist-authored additions.

    Returns ``True`` if a new section was written or code fields were refreshed,
    ``False`` if the section already exists with current code state.
    """
    if session is None or project_id is None:
        return False

    from app.task_locking import with_design_doc_locked

    section_key = "systems/completion-debt-ledger"
    new_section = _build_completion_debt_design_doc_section()
    changed = [False]  # Use list to allow modification in nested function

    def _mutate(doc: dict) -> None:
        now = _utcnow().isoformat()
        existing = doc.get(section_key)
        if existing is None:
            # No section yet — seed it as code-authored.
            doc[section_key] = {**new_section, "_authored_by": "code"}
            changed[0] = True
        else:
            # Section exists — start with existing (preserves operator/strategist keys).
            # Then refresh code-owned fields only (those present in new_section).
            refreshed = dict(existing)
            for key, value in new_section.items():
                if refreshed.get(key) != value:
                    refreshed[key] = value
                    changed[0] = True
            # Mark authorship if missing.
            if "_authored_by" not in refreshed:
                refreshed["_authored_by"] = "code"
            doc[section_key] = refreshed

        # Update _meta if section changed.
        if changed[0]:
            meta = dict(doc.get("_meta") or {"revision": 0})
            meta["revision"] = meta.get("revision", 0) + 1
            meta["last_updated"] = now
            doc["_meta"] = meta

    try:
        await with_design_doc_locked(session, project_id, _mutate, skip_locked=True)
        return changed[0]
    except Exception:  # noqa: BLE001
        logger.warning(
            "[COMPLETION_DEBT] failed to seed design-doc section for project %s",
            project_id,
            exc_info=True,
        )
        return False
