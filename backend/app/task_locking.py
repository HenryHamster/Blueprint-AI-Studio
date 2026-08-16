"""Row-lock helpers for task / deployment-state mutations.

Audit 2026-04-25 (W1) found that the orchestrator dispatch loop and CI
polling loop load tasks once and then mutate them repeatedly across the
loop body without re-acquiring row locks. Concurrent dispatch instances,
the reaper, and status POST handlers race on the same in-memory copy and
overwrite each other's mutations. This drove the ~30-40% baseline failure
rate flagged in ``project_deferred_robustness.md``.

Use ``task_lock`` anywhere a task field is mutated under contention. The
yielded Task is freshly loaded inside the lock; the lock releases when
the surrounding session commits or rolls back.

For the singleton ``DeploymentState`` row, use ``deployment_state_lock``
which adds an optimistic-concurrency ``version`` bump alongside the
SELECT FOR UPDATE so callers that mutate without holding the lock for
the entire write window can still detect lost-update races.

Audit 2026-04-29 (Phase A1) added project-row helpers for the same
rationale: 9 sites used plain ``with_for_update()`` without
``skip_locked``, so the 30s ``lock_timeout`` could fire and propagate
``LockNotAvailableError`` as a 500 — Cloud Run then quarantines the
instance. ``lock_project_or_429`` is the canonical helper for handler
paths (returns 429 Retry-After on contention); ``project_lock`` is the
context-manager variant for background / post-commit chains that need
``skip_locked`` + structured retry.
"""

from __future__ import annotations

import asyncio
import json
import logging
from contextlib import asynccontextmanager
from typing import AsyncIterator, Callable

import uuid
from fastapi import HTTPException
from sqlalchemy import func, inspect as sa_inspect, select, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import load_only
from sqlalchemy.orm.attributes import flag_modified

from app.database import resolve_dialect_name
from app.models import DeploymentState, Project, Task
from app.sr_analytics_store import LEGACY_SR_ANALYTICS_KEYS

logger = logging.getLogger(__name__)

# Agent-owned write ceiling for project.design_doc. The God-column
# decomposition (design 52f97baa) is migrating the code-written telemetry blobs
# out to dedicated tables so design_doc converges on architectural content only
# (<50KB target). The cochange_* blobs are still the largest remaining
# transitional payload in design_doc, so the ceiling stays at 500KB until the
# remaining keys are cut over; it can be lowered once design_doc holds
# architectural content alone.
DESIGN_DOC_MAX_BYTES = 500_000

# Automatic task admission decisions.  Keep these strings stable: callers use
# them for status/telemetry without importing a second policy enum.
AUTOMATIC_TASK_CREATION_ALLOWED = "allowed"
AUTOMATIC_TASK_CREATION_PROJECT_PAUSED = "project_paused"
AUTOMATIC_TASK_CREATION_PROJECT_ARCHIVED = "project_archived"
AUTOMATIC_TASK_CREATION_AUTONOMY_DISABLED = "autonomy_disabled"
AUTOMATIC_TASK_CREATION_PROJECT_UNAVAILABLE = "project_unavailable"


def automatic_task_creation_decision(
    project: Project | None,
    *,
    require_autonomous_enabled: bool = False,
) -> str:
    """Return automatic-task admission from an already locked Project.

    This is the policy half of :func:`acquire_automatic_task_creation_authority`.
    Completion pipelines that already hold the canonical Project -> Task lock
    must use this function instead of trying to re-lock Project behind Task.
    The caller is responsible for keeping the Project lock through every Task
    INSERT admitted by the returned decision.
    """

    if project is None:
        return AUTOMATIC_TASK_CREATION_PROJECT_UNAVAILABLE
    if project.archived:
        return AUTOMATIC_TASK_CREATION_PROJECT_ARCHIVED
    if project.paused:
        return AUTOMATIC_TASK_CREATION_PROJECT_PAUSED
    if require_autonomous_enabled and not project.autonomous_enabled:
        return AUTOMATIC_TASK_CREATION_AUTONOMY_DISABLED
    return AUTOMATIC_TASK_CREATION_ALLOWED


class DesignDocSizeLimitExceeded(Exception):
    """Raised by with_design_doc_locked when a write would grow the agent-owned
    portion of design_doc past DESIGN_DOC_MAX_BYTES and raise_on_size_limit=True.

    Callers that need to surface the failure to the strategist (e.g. HTTP
    handlers) should pass raise_on_size_limit=True and catch this exception
    to return a visible error rather than a silent False.
    """

    def __init__(self, project_id, old_size: int, new_size: int, ceiling: int):
        self.project_id = project_id
        self.old_size = old_size
        self.new_size = new_size
        self.ceiling = ceiling
        super().__init__(
            f"design_doc size limit exceeded for project {project_id}: "
            f"would grow from {old_size} to {new_size} bytes (ceiling {ceiling})"
        )

# Keys in project.design_doc that are owned by internal orchestration (code-side
# writers) and are excluded from the agent-write size cap. These keys have their
# own internal retention caps and must not consume the agent-write budget.
# This is the authoritative set — mirror of the guard in
# admin_agent_ops.py (which imports from here). Keep in sync with the set/delete
# guard in that module so the two never diverge.
#
# Specialized stores are authoritative for efficiency, lane, and SR analytics,
# but their legacy design_doc values remain as per-key/rollback fallbacks during
# the rolling cutover. Those retained code-owned values stay exempt from the
# agent-write cap until the fallbacks are intentionally removed.
_RESERVED_DESIGN_DOC_KEYS: frozenset = frozenset(
    {
        # Originally reserved
        "playbook_archive",
        "proposed_changes",
        "module_feedback",
        "retired_diagnoses_archive",
        # Co-change index written by direction_outcomes.py (~38KB); excluded so
        # it doesn't consume the agent-write 500KB budget.
        "cochange_index",
        "cochange_applied_prs",
        # Per-direction first-attempt review feedback tallies.  Dual-written to
        # project_telemetry; exempted from agent budget during the transition.
        "direction_first_attempt_feedback",
        # Durable compute-image build lease/state; maintained by product code.
        "compute_image_build_state",
        # Completion-debt-ledger system description seeded by completion_debt.py.
        "systems/completion-debt-ledger",
        # Analytics/sweep keys written by background code (not by agents)
        "repo_overview",
        "review_effectiveness",
        "playbook_deleted_keys",
        # Generic-KV telemetry blobs dual-written to project_telemetry during the
        # final God-column cutover tranche; exempt from the agent-write cap while
        # their design_doc copies are retained for rollback compatibility.
        "_broadcast_limit",
        "_auto_remediation",
        # Retained specialized-store fallbacks.
        "efficiency_baseline",
        "lane_baselines",
    }
    | LEGACY_SR_ANALYTICS_KEYS
)


@asynccontextmanager
async def task_lock(
    session: AsyncSession,
    task_id,
    *,
    skip_locked: bool = True,
    timeout: float = 30.0,
) -> AsyncIterator[Task | None]:
    """Acquire SELECT FOR UPDATE on a task row, yield freshly-loaded Task or None.

    Args:
        session: AsyncSession to issue the lock against. The lock is held
            until this session commits or rolls back — the caller MUST
            commit/rollback before exiting the ``async with`` block.
        task_id: UUID or str of the task.
        skip_locked: If True (default), yield ``None`` immediately when the row
            is held by another session. Caller must check for None and skip
            the iteration. If False, wait up to ``timeout`` seconds and raise
            ``OperationalError`` on timeout.
        timeout: Max seconds to wait when ``skip_locked=False``. Ignored
            when skip_locked=True (the SELECT returns immediately either
            way; the asyncio timeout is only meaningful for the blocking
            variant or to bound the wait if the DB itself is slow).

    Yields:
        The freshly-loaded Task object inside the lock, or None if the row
        was held and skip_locked=True.

    Example:
        async with task_lock(session, task.id) as t:
            if t is None:
                continue  # held by another worker
            t.retry_after_until = None
            t.requeue_in_flight = True
        await session.commit()  # release lock

    Round 5 W1 (audit CRITICAL): ``populate_existing=True`` is required.
    The orchestrator dispatch loop loads candidate Tasks via
    ``load_only(<projection>, raiseload=True)``; those projection-deferred instances enter
    the session's identity map. A subsequent ``task_lock`` re-SELECT
    without ``populate_existing`` returns the cached projected instance
    (SQLAlchemy's identity-map default), so reads of non-projected columns
    (``vm_instance_id``, ``title``, ...) trigger sync lazy-load →
    MissingGreenlet under async, crashing the entire dispatch cycle.
    Setting ``populate_existing=True`` forces SQLA to refresh attributes
    from this SELECT regardless of identity-map state — the FOR UPDATE
    re-acquire returns fresh data semantics, not a cached projection.
    """
    stmt = (
        select(Task)
        .where(Task.id == task_id)
        .with_for_update(skip_locked=skip_locked)
        .execution_options(populate_existing=True)
    )
    try:
        result = await asyncio.wait_for(session.execute(stmt), timeout=timeout)
    except asyncio.TimeoutError as e:
        logger.warning(
            "[task_lock] timeout acquiring lock on task %s after %.1fs (skip_locked=%s)",
            task_id,
            timeout,
            skip_locked,
        )
        raise OperationalError("task_lock acquisition timed out", None, e) from e
    yield result.scalar_one_or_none()


@asynccontextmanager
async def deployment_state_lock(
    session: AsyncSession,
    *,
    skip_locked: bool = True,
    timeout: float = 30.0,
) -> AsyncIterator[DeploymentState | None]:
    """Acquire SELECT FOR UPDATE on the singleton DeploymentState row.

    Bumps ``version`` automatically when the caller exits the block normally
    (i.e. didn't set ``ds.version_unchanged = True``). Callers that mutate
    DeploymentState should use this helper everywhere; mixing locked and
    unlocked writes is the F4 race condition flagged in the audit.

    See ``task_lock`` for arg semantics — same shape.
    """
    stmt = (
        select(DeploymentState)
        .where(DeploymentState.id == 1)
        .with_for_update(skip_locked=skip_locked)
    )
    try:
        result = await asyncio.wait_for(session.execute(stmt), timeout=timeout)
    except asyncio.TimeoutError as e:
        logger.warning(
            "[deployment_state_lock] timeout after %.1fs (skip_locked=%s)",
            timeout,
            skip_locked,
        )
        raise OperationalError(
            "deployment_state_lock acquisition timed out", None, e
        ) from e
    ds = result.scalar_one_or_none()
    if ds is None:
        yield None
        return
    starting_version = ds.version
    yield ds
    # Auto-bump version on exit if the row was actually modified. Detect
    # modification by comparing the SQLAlchemy session.dirty set; if the
    # caller explicitly set version_unchanged=True (read-only inside lock)
    # skip the bump.
    if getattr(ds, "_version_unchanged", False):
        return
    if ds in session.dirty or ds.version == starting_version:
        ds.version = starting_version + 1


def _undo_projection_downgrade(instance) -> None:
    """Undo the loader damage a ``load_only(..., raiseload=True)`` lock does to an
    instance that was ALREADY more fully loaded than the projection.

    A projection lock is supposed to be a cheap read of a few columns. What it
    actually does to a shared identity-mapped instance is permanent: the SELECT
    stores its loader options on the ``InstanceState`` (``state.load_options``)
    and installs a deferred/raiseload callable for every non-projected column
    (``state.callables``). The column VALUES already in ``state.dict`` survive —
    so nothing looks wrong, and sync reads keep working right after the lock.

    The damage only detonates later. Anything that expires the instance — an
    explicit ``session.expire()``, a ``session.refresh()``, a ``rollback()`` —
    hands control to those stored loaders, so the reload fetches ONLY the
    projected columns and leaves everything else expired. (A bare ``commit()``
    does NOT expire here: the app configures ``expire_on_commit=False``, which
    is why several completion paths call ``session.expire()`` by hand to drop a
    stale snapshot — those are the detonation sites.) The next SYNC read then
    lazy-loads
    outside the asyncpg greenlet → ``MissingGreenlet``. Worse, if the lock also
    left a column dirty, the reload SELECT autoflushes that UPDATE from sync
    context, and the resulting MG poisons the whole session so the traceback
    surfaces at an unrelated downstream reader.

    This restores the invariant a lock owes its caller: **acquiring a lock must
    not leave the instance less loaded than it found it.** Dropping the stored
    options and the now-stale callables is pure in-memory bookkeeping — no IO,
    no extra SELECT — and a later refresh/expire then behaves normally.

    Deliberately a no-op for a genuinely cold instance (one the lock itself
    brought into the session). There, every non-projected column is legitimately
    unloaded, the raiseload is the intended guard, and keeping the projection is
    the whole point of ``load_columns``. The discriminator is exact: a stale
    callable is one whose attribute ALSO has a live value in ``state.dict``,
    which can only happen if the instance was loaded more fully before the lock.
    """
    if instance is None:
        return
    state = sa_inspect(instance)
    callables = getattr(state, "callables", None) or {}
    stale = [key for key in callables if key in state.dict]
    if not stale:
        # Cold instance (or no projection applied) — nothing was downgraded.
        return
    for key in stale:
        del callables[key]
    # The callables above are re-derived from the stored loader options on every
    # expire cycle, so clearing them alone is not enough — the projection grows
    # back on the next refresh. This is the actual persistent carrier.
    state.load_options = ()


async def lock_project_or_429(
    session: AsyncSession,
    project_id: uuid.UUID,
    *,
    load_columns: list | None = None,
) -> Project:
    """Acquire a row lock on a Project for atomic read-modify-write (handler path).

    Uses SKIP LOCKED so concurrent agent writes don't queue behind the 30s
    lock_timeout set in database.py. If another handler currently holds the
    lock, returns 429 Retry-After so the client retries (same contract as
    agent_status.py uses for task-row contention; see commit 88a77903).
    Cloud Run flags any 5xx as ContainerError and quarantines the instance,
    so 429 is deliberate — it conveys "temporary, retry" without tripping
    container-fault classification.

    Use this from request handlers where the user can retry on 429. For
    background loops or post-commit chains that should not bubble up an
    HTTPException, use ``project_lock`` instead.

    ``load_columns``: when provided, only those columns are loaded from the
    Project row (``raiseload=True`` guards against accidental access of other
    columns). Pass only the columns the caller actually reads or mutates to
    avoid deserializing the 500KB+ design_doc on paths that don't need it.
    Default ``None`` loads all columns (safe for callers with varied needs).
    """
    stmt = (
        select(Project)
        .where(Project.id == project_id)
        # FOR NO KEY UPDATE — see project_lock: serializes project-row writers
        # without blocking FK child-row inserts (FOR KEY SHARE). PK never changes.
        .with_for_update(skip_locked=True, key_share=True)
        .execution_options(populate_existing=True)
    )
    if load_columns is not None:
        stmt = stmt.options(load_only(*load_columns, raiseload=True))
    result = await session.execute(stmt)
    project = result.scalars().first()
    if project is not None:
        _undo_projection_downgrade(project)
        return project
    # Distinguish "row locked" from "row missing" with a non-locking read.
    exists = (
        await session.execute(select(Project.id).where(Project.id == project_id))
    ).first()
    if exists is None:
        raise HTTPException(status_code=404, detail="Project not found")
    raise HTTPException(
        status_code=429,
        detail="Project row locked by concurrent write, retry",
        headers={"Retry-After": "2"},
    )


async def acquire_pr_spawn_lock(
    session: AsyncSession,
    project_id: object,
    pr_number: int,
    *,
    repo_identity: str | None = None,
) -> None:
    """Serialize task admission for one repository PR via an advisory lock.

    The dedup logic at the top of ``_spawn_pipeline_task`` reads existing
    pipeline tasks (SELECT … status IN active), decides whether to dedup /
    supersede / proceed, then inserts a new task. Two concurrent callers
    for the same PR can both pass the SELECT before either commits — and
    because each spawn computes its own ``idempotency_key``, the existing
    partial UNIQUE on ``ux_task_active_idempotency_key`` does NOT catch
    that race. The result is a double-spawn that wastes an agent slot and
    has both review/fix tasks running against the same PR.

    A repository-scoped key makes the SELECT-decide-INSERT window structurally
    serial even when duplicate Blueprint projects point at the same GitHub PR,
    without coupling unrelated repositories that happen to use the same PR
    number. When repository identity is unavailable, project id is the safe
    fallback. The lock is
    held until the surrounding transaction commits or rolls back, so the caller
    MUST commit (or rollback) at the end of the admission flow to release it.

    No-op on non-Postgres dialects (SQLite tests rely on
    serialization-by-event-loop within a single process and don't need
    the lock).

    Args:
        session: AsyncSession to issue the lock against. Must be the same
            session that performs the subsequent SELECT/INSERT — the lock
            is transaction-scoped, not session-scoped.
        project_id: Fallback identity and source for repository resolution.
        pr_number: Integer PR number within that project.
    """
    if resolve_dialect_name(session) != "postgresql":
        return
    normalized_repo = str(repo_identity or "").strip().lower()
    if not normalized_repo:
        resolved_repo = (
            await session.execute(
                select(Project.github_owner, Project.github_repo).where(
                    Project.id == project_id
                )
            )
        ).first()
        if resolved_repo and resolved_repo[0] and resolved_repo[1]:
            normalized_repo = f"{resolved_repo[0]}/{resolved_repo[1]}".lower()
    lock_identity = (
        f"repo:{normalized_repo}" if normalized_repo else f"project:{project_id}"
    )
    await session.execute(
        text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
        {"key": f"review-pr:{lock_identity}:{int(pr_number)}"},
    )


async def acquire_merge_submission_locks(
    session: AsyncSession,
    *,
    project_id: object,
    repo_identity: str,
    base_branch: str,
    pr_number: int,
) -> None:
    """Serialize authoritative merge submissions for one base, then one PR.

    Lock order is deliberately global: every merge takes the project/repository
    base-branch lock first and the project-scoped PR admission lock second.
    Reviews, fixes, CI, and other work remain parallel; only the final merge
    submission boundary for PRs targeting the same base is serialized.
    """
    if resolve_dialect_name(session) != "postgresql":
        return
    normalized_repo = str(repo_identity or project_id).strip().lower()
    normalized_base = str(base_branch or "").strip()
    await session.execute(
        text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
        {"key": f"review-merge-base:{normalized_repo}:{normalized_base}"},
    )
    await acquire_pr_spawn_lock(
        session,
        project_id,
        pr_number,
        repo_identity=normalized_repo,
    )


async def acquire_direction_research_spawn_lock(
    session: AsyncSession,
    project_id,
    direction_id: str | None,
) -> None:
    """Serialize same-direction research spawns across sessions/processes.

    Research uses a per-direction idempotency claim to close the general
    duplicate-spawn race, but the deliberate pipelining exception still needs a
    separate serializer: the pipelined task claims ``...:pipelined`` while a
    later primary/manual spawn claims ``...``. Without a shared lock around the
    read-decide-insert window, those different keys can coexist when the later
    caller's pre-insert scan misses the just-committed pipelined task.

    A transaction-scoped advisory lock keyed by ``project_id`` + ``direction``
    makes that window structurally serial for both the autonomous loop and the
    manual ``POST /directions/{id}/research`` endpoint. Non-Postgres dialects
    no-op; SQLite tests rely on single-process scheduling and explicit
    regression fixtures for coverage.
    """
    if resolve_dialect_name(session) != "postgresql":
        return
    await session.execute(
        text("SELECT pg_advisory_xact_lock(hashtext(:key))"),
        {
            "key": f"research-direction-spawn:{project_id}:{direction_id or 'cross-cutting'}"
        },
    )


@asynccontextmanager
async def project_lock(
    session: AsyncSession,
    project_id,
    *,
    skip_locked: bool = True,
    timeout: float = 30.0,
    load_columns: list | None = None,
) -> AsyncIterator[Project | None]:
    """Acquire SELECT FOR UPDATE on a project row, yield freshly-loaded Project or None.

    Mirror of ``task_lock`` for the project row. Use from background
    loops or post-commit chains that should react to contention rather
    than surface a 5xx — when ``skip_locked=True`` (the default) the
    contextmanager yields ``None`` immediately if the row is held by
    another session, and the caller must check for None and choose
    whether to skip or retry.

    For request handlers that should bubble a retry-friendly 429 to the
    client, use ``lock_project_or_429`` instead.

    Args:
        session: AsyncSession to issue the lock against. Lock is held
            until commit/rollback.
        project_id: UUID or str of the project.
        skip_locked: If True (default), yield ``None`` when the row is
            held; if False, wait up to ``timeout`` and raise on timeout.
        timeout: Max seconds to wait when ``skip_locked=False``.
        load_columns: When provided, only those columns are fetched from
            the Project row (``raiseload=True`` guards accidental access).
            Pass only the columns the caller actually reads or mutates to
            avoid deserializing the 500KB+ design_doc unnecessarily.
            Default ``None`` loads all columns (safe for generic callers).

    Yields:
        The freshly-loaded Project object, or None if held under skip_locked.
    """
    stmt = (
        select(Project)
        .where(Project.id == project_id)
        # FOR NO KEY UPDATE (key_share=True), not FOR UPDATE: still serializes
        # concurrent project-row writers (it conflicts with itself and FOR
        # UPDATE), but does NOT conflict with the FOR KEY SHARE lock that every
        # insert of a project-child row (ProjectMessage / Task / Alert ...) takes
        # on the parent for its FK. Plain FOR UPDATE deadlocks across connections
        # when a lock holder (or a chain it awaits) inserts such a child row — the
        # release-qualifier infra-failure path hit exactly this (operator-message
        # INSERT blocked on a held project lock). The project PK is never updated,
        # so FOR NO KEY UPDATE is the correct, strictly-safer lock here.
        .with_for_update(skip_locked=skip_locked, key_share=True)
        .execution_options(populate_existing=True)
    )
    if load_columns is not None:
        stmt = stmt.options(load_only(*load_columns, raiseload=True))
    try:
        result = await asyncio.wait_for(session.execute(stmt), timeout=timeout)
    except asyncio.TimeoutError as e:
        logger.warning(
            "[project_lock] timeout acquiring lock on project %s after %.1fs (skip_locked=%s)",
            project_id,
            timeout,
            skip_locked,
        )
        raise OperationalError("project_lock acquisition timed out", None, e) from e
    project = result.scalar_one_or_none()
    try:
        yield project
    finally:
        # In a finally, not just on the success path: the error path is where
        # this matters most. An exception inside the lock body is usually
        # followed by a rollback, and a rollback is exactly the expire cycle
        # that detonates a leaked projection. This is IO-free in-memory
        # bookkeeping, so it is safe to run while the stack is unwinding.
        # See ``_undo_projection_downgrade`` — a projection lock must not leave
        # the shared instance less loaded than it found it.
        _undo_projection_downgrade(project)


async def acquire_automatic_task_creation_authority(
    session: AsyncSession,
    project_id,
    *,
    skip_locked: bool = True,
    timeout: float = 30.0,
    require_autonomous_enabled: bool = False,
) -> str:
    """Linearize an automatic Task INSERT against project pause/archive.

    ``Project.paused`` is the universal no-new-work authority.  Dispatch-side
    filtering alone is insufficient: an automatic continuation can otherwise
    insert latent QUEUED rows while paused and release them as a burst on
    resume.  This helper takes the canonical Project ``FOR NO KEY UPDATE`` lock
    and returns a stable decision while leaving that lock held until the
    surrounding transaction commits or rolls back.  A concurrent pause write
    and a task insert therefore have one deterministic order:

    * pause wins the Project lock -> this admission fails closed;
    * admission wins -> its INSERT commits before the pause can become visible.

    Call this **before any child-row lock or mutation** in an automatic spawn
    transaction (Project -> child is the canonical lock order), and keep the
    transaction open through the Task INSERT.  Project-row contention and a
    missing project both fail closed as ``project_unavailable``; a later sweep
    may retry after contention clears.  Callers whose work is specifically
    autonomy-owned may set ``require_autonomous_enabled=True`` to linearize that
    policy check under the same Project lock.  Manual, explicitly user-authored
    task creation must not call this helper merely to bypass pause semantics.
    """

    # ``load_only`` is intentionally cheap, but an ORM projection introduced
    # into a previously-cold identity map can poison a later ``session.get``:
    # it returns the same underloaded Project and a synchronous full-field read
    # raises MissingGreenlet.  Remember whether the caller already owned this
    # identity.  After the decision, a clean projection introduced solely by
    # this helper can be detached without releasing the transaction-scoped DB
    # row lock.  Preloaded identities stay attached and project_lock's
    # _undo_projection_downgrade restores their prior loader state.
    sync_session = session.sync_session
    project_id_text = str(project_id)
    project_preloaded = any(
        isinstance(candidate, Project)
        and str(candidate.__dict__.get("id")) == project_id_text
        for candidate in (*sync_session.identity_map.values(), *sync_session.new)
    )
    decision = AUTOMATIC_TASK_CREATION_PROJECT_UNAVAILABLE
    async with project_lock(
        session,
        project_id,
        skip_locked=skip_locked,
        timeout=timeout,
        load_columns=[
            Project.id,
            Project.paused,
            Project.archived,
            *([Project.autonomous_enabled] if require_autonomous_enabled else []),
        ],
    ) as project_authority:
        decision = automatic_task_creation_decision(
            project_authority,
            require_autonomous_enabled=require_autonomous_enabled,
        )
    if (
        project_authority is not None
        and not project_preloaded
        and sa_inspect(project_authority).persistent
        and not sa_inspect(project_authority).modified
    ):
        session.expunge(project_authority)
    return decision


@asynccontextmanager
async def automatic_task_creation_parent_lock(
    session: AsyncSession,
    project_id,
    task_id,
    *,
    skip_locked: bool = True,
    timeout: float = 30.0,
    require_autonomous_enabled: bool = False,
) -> AsyncIterator[tuple[str, Project | None, Task | None]]:
    """Lock Project -> parent Task and return one automatic-spawn decision.

    Use this boundary after any commit/rollback or unlocked external-I/O window
    invalidated a completion pipeline's original locks.  Pause/archive is read
    while Project is locked, then the parent Task is locked regardless of the
    decision so a denied spawn can still reconcile its parent terminally.  Both
    locks remain transaction-scoped, so callers must keep the transaction open
    through child INSERTs (normally committing inside the context).  Project or
    Task contention/missing rows fail closed as ``project_unavailable``.

    The full Project row is intentionally loaded.  Callers commonly need branch
    configuration after a prior commit, and returning a projected identity would
    poison a later ``session.get(Project, ...)`` with MissingGreenlet.
    """

    async with project_lock(
        session,
        project_id,
        skip_locked=skip_locked,
        timeout=timeout,
    ) as project:
        decision = automatic_task_creation_decision(
            project,
            require_autonomous_enabled=require_autonomous_enabled,
        )
        if project is None:
            yield decision, None, None
            return
        async with task_lock(
            session,
            task_id,
            skip_locked=skip_locked,
            timeout=timeout,
        ) as task:
            if task is None:
                yield AUTOMATIC_TASK_CREATION_PROJECT_UNAVAILABLE, project, None
                return
            yield decision, project, task


@asynccontextmanager
async def lock_project_then_task(
    session: AsyncSession,
    project_id,
    task_id,
    *,
    skip_locked: bool = True,
    timeout: float = 30.0,
) -> AsyncIterator[tuple[Project | None, Task | None]]:
    """Acquire row locks on Project then Task in canonical order.

    Phase C1 (CRITICAL C5): the audit found cross-table lock-ordering
    deadlock potential. Some paths locked Project then Task; others
    locked Task then Project. Two flows holding opposite orders deadlock
    until the 30s ``lock_timeout`` fires and surfaces ``LockNotAvailable``
    as 500 — Cloud Run then quarantines the instance.

    The canonical order is **Project before Task, always**. This helper
    enforces it. Sites that need both locks should use this helper rather
    than rolling their own pair.

    Args:
        session: AsyncSession to issue the locks against. Both locks are
            held until commit/rollback.
        project_id: UUID or str of the project.
        task_id: UUID or str of the task.
        skip_locked: If True (default), yield ``(None, None)`` when
            either row is held by a peer; on contention with the project
            row the task lock is never attempted (so the caller backs off
            cleanly without holding a stale half-pair). If False, wait up
            to ``timeout`` per row and raise on timeout.
        timeout: Max seconds to wait per row when ``skip_locked=False``.

    Yields:
        Tuple ``(project, task)``. Either may be ``None`` if the
        corresponding row is held under ``skip_locked=True`` or missing;
        the caller must check both before mutating.

    Example:
        async with lock_project_then_task(session, pid, tid) as (project, task):
            if project is None or task is None:
                return  # contended or missing; defer to next cycle
            ...
        await session.commit()  # release both locks atomically
    """
    project_stmt = (
        select(Project)
        .where(Project.id == project_id)
        # FOR NO KEY UPDATE — see project_lock: serializes project-row writers
        # without blocking FK child-row inserts (FOR KEY SHARE). PK never changes.
        .with_for_update(skip_locked=skip_locked, key_share=True)
        .execution_options(populate_existing=True)
    )
    try:
        project_result = await asyncio.wait_for(
            session.execute(project_stmt),
            timeout=timeout,
        )
    except asyncio.TimeoutError as e:
        logger.warning(
            "[lock_project_then_task] timeout acquiring project %s after %.1fs (skip_locked=%s)",
            project_id,
            timeout,
            skip_locked,
        )
        raise OperationalError("project lock acquisition timed out", None, e) from e
    project = project_result.scalar_one_or_none()
    if project is None:
        # Don't attempt the task lock — caller backs off without holding
        # a half-pair. This preserves the canonical ordering invariant
        # under contention (project never released while task held).
        yield (None, None)
        return
    task_stmt = (
        select(Task)
        .where(Task.id == task_id)
        .with_for_update(skip_locked=skip_locked)
        .execution_options(populate_existing=True)
    )
    try:
        task_result = await asyncio.wait_for(
            session.execute(task_stmt),
            timeout=timeout,
        )
    except asyncio.TimeoutError as e:
        logger.warning(
            "[lock_project_then_task] timeout acquiring task %s after %.1fs (skip_locked=%s)",
            task_id,
            timeout,
            skip_locked,
        )
        raise OperationalError("task lock acquisition timed out", None, e) from e
    task = task_result.scalar_one_or_none()
    yield (project, task)


async def lock_project_for_strategic_transition(
    session: AsyncSession,
    project_id,
    *,
    max_retries: int = 3,
) -> Project | None:
    """Acquire a project lock for the strategic post-commit chain.

    Phase C3 (HIGH H-C4): ``_handle_design_doc_completion`` and
    ``_handle_strategic_completion`` (formerly ``_record_strategic_*``)
    each acquired a project-row lock. They're called sequentially from
    the same post-commit chain, so two simultaneous strategic completions
    in the same project would queue lock attempts back-to-back — and the
    chain's downstream calls (``_spawn_strategic_review`` →
    ``_has_active_strategic_task``) re-attempted the same lock again,
    deepening the queue. This helper centralises the acquisition.

    Returns the locked Project under skip_locked semantics with a small
    bounded retry, or None when the row is held past the retry window
    (caller defers; the next cycle re-attempts). On a genuinely missing
    project, returns None and the caller surfaces the appropriate error.

    The legacy retry pattern (3 attempts × 0.5s backoff) is preserved so
    existing tests that simulate "row held under contention" continue to
    pass deterministically.

    Args:
        session: AsyncSession to issue the lock against.
        project_id: UUID or str of the project.
        max_retries: Number of skip_locked attempts before giving up.

    Returns:
        Locked Project, or None if held past max_retries / missing.
    """
    project: Project | None = None
    for attempt in range(max_retries):
        result = await session.execute(
            select(Project)
            .where(Project.id == project_id)
            # FOR NO KEY UPDATE — see project_lock: serializes project-row writers
            # without blocking FK child-row inserts (FOR KEY SHARE). PK never changes.
            .with_for_update(skip_locked=True, key_share=True)
            .execution_options(populate_existing=True)
        )
        project = result.scalars().first()
        if project is not None:
            return project
        # Distinguish "held" from "missing" with a non-locking probe.
        exists = (
            await session.execute(select(Project.id).where(Project.id == project_id))
        ).first()
        if exists is None:
            return None  # genuinely missing
        await asyncio.sleep(0.5 * (attempt + 1))
    return None  # row held past retry window — caller defers


async def _with_project_locked_list_mutation(
    session: AsyncSession,
    project_id,
    mutate_fn: Callable[[list], list | None],
    *,
    skip_locked: bool,
) -> bool:
    async with project_lock(
        session,
        project_id,
        skip_locked=skip_locked,
        load_columns=[Project.id, Project.directions],
    ) as project:
        if project is None:
            return False
        directions = list(project.directions or [])
        result = mutate_fn(directions)
        final = result if result is not None else directions
        project.directions = final
        flag_modified(project, "directions")
        session.add(project)
        # Persist the authoritative blob mutation inside this transaction
        # before a caller acquires another projected Project lock. Those locks
        # use populate_existing=True; with autoflush disabled (a valid session
        # configuration) a follow-on vision/problem-model SELECT would
        # otherwise overwrite this still-dirty attribute with its pre-mutation
        # database value while the normalized Direction rows below survived.
        await session.flush([project])
        # P2 (p2-direction-table): dual-write the normalised ``direction`` table
        # so it stays consistent with the authoritative blob during the
        # transition. Best-effort — a sync failure must never break the blob
        # write (which is still the source of truth every read site uses).
        try:
            from app.directions_repo import sync_directions

            await sync_directions(session, project_id, final)
        # BLE001: intentional log-and-continue — the authoritative blob write
        # above must never be broken by a dual-write sync failure (the blob
        # remains the source of truth every read site uses).
        except Exception as exc:  # noqa: BLE001  # pragma: no cover - defensive
            logger.warning(
                "[with_directions_locked] direction-table dual-write sync failed "
                "for project %s (blob write unaffected): %s",
                project_id,
                exc,
            )
    return True


async def with_design_doc_locked(
    session: AsyncSession,
    project_id,
    mutate_fn: Callable[[dict], None],
    *,
    skip_locked: bool = False,
    raise_on_size_limit: bool = True,
) -> bool:
    """Atomic read-modify-write of project.design_doc under a project row lock.

    Acquires SELECT FOR UPDATE, calls mutate_fn(doc) with a copy of the
    current doc dict, then writes the result back. Caller is responsible for
    committing the session.

    Args:
        skip_locked: When False (default), block until the lock is acquired
            (suitable for background callers that must not silently drop
            updates). When True, return False immediately if the row is held
            by another session — preserves best-effort / non-blocking
            semantics for hot dispatch paths where lock contention should
            degrade gracefully rather than block.
        raise_on_size_limit: When True (the default), raise
            DesignDocSizeLimitExceeded when the agent-owned portion of
            design_doc would exceed DESIGN_DOC_MAX_BYTES — ensuring callers
            do not silently believe a write persisted when it was dropped.
            Pass False explicitly to opt back into the legacy silent-return
            behavior.

    Returns:
        True if mutate_fn was applied, False if the row was held and
        skip_locked=True caused an immediate skip (or raise_on_size_limit is
        explicitly False and the size cap was hit — use the default
        raise_on_size_limit=True to distinguish cap failures from lock-skip
        failures via the exception type).

    This is the canonical helper for all design_doc mutations. Using it
    consistently prevents concurrent writers from clobbering each other's
    keys via stale read-modify-write races.
    """
    async with project_lock(
        session,
        project_id,
        skip_locked=skip_locked,
        load_columns=[Project.id, Project.design_doc],
    ) as project:
        if project is None:
            return False
        doc = dict(project.design_doc or {})

        # Compute agent-owned size: reserved keys (code-side blobs with their own
        # retention caps) must not block agent writes.  Measure only the
        # non-reserved portion so the cap reflects what agents can control.
        def _agent_owned_size(d: dict) -> int:
            return len(
                json.dumps(
                    {k: v for k, v in d.items() if k not in _RESERVED_DESIGN_DOC_KEYS}
                )
            )

        old_agent_size = _agent_owned_size(doc)
        mutate_fn(doc)
        new_agent_size = _agent_owned_size(doc)
        if new_agent_size > DESIGN_DOC_MAX_BYTES and new_agent_size > old_agent_size:
            logger.warning(
                "[with_design_doc_locked] skipping write for project %s: "
                "would grow agent-owned portion from %d to %d bytes (ceiling %d)",
                project_id,
                old_agent_size,
                new_agent_size,
                DESIGN_DOC_MAX_BYTES,
            )
            if raise_on_size_limit:
                raise DesignDocSizeLimitExceeded(
                    project_id, old_agent_size, new_agent_size, DESIGN_DOC_MAX_BYTES
                )
            return False
        # Strip migrated telemetry keys before persist — these are now written
        # exclusively via project_telemetry.set_project_telemetry / upsert_telemetry.
        from app.project_telemetry import strip_migrated_doc_keys as _strip_migrated_doc_keys

        _strip_migrated_doc_keys(doc)
        project.design_doc = doc
        flag_modified(project, "design_doc")
        session.add(project)
    return True


@asynccontextmanager
async def project_telemetry_value_lock(
    session: AsyncSession,
    project_id,
    key: str,
    *,
    design_doc_fallback: dict | None = None,
    skip_locked: bool = False,
) -> AsyncIterator[dict | None]:
    """Lock and yield one mutable ``project_telemetry`` value.

    Missing rows are seeded from the legacy design-doc value. The updated
    dictionary is written back before the context exits; the database row lock
    remains held until the caller commits or rolls back.
    """
    from app.models import ProjectTelemetry
    from sqlalchemy.dialects.postgresql import insert as pg_insert

    seed: dict = {}
    if design_doc_fallback is not None:
        seed = dict(design_doc_fallback.get(key) or {})

    await session.execute(
        pg_insert(ProjectTelemetry)
        .values(
            id=uuid.uuid4(),
            project_id=project_id,
            key=key,
            value=seed,
            updated_at=func.now(),
        )
        .on_conflict_do_nothing(index_elements=["project_id", "key"])
    )
    result = await session.execute(
        select(ProjectTelemetry)
        .where(
            ProjectTelemetry.project_id == project_id,
            ProjectTelemetry.key == key,
        )
        .with_for_update(skip_locked=skip_locked)
    )
    row = result.scalar_one_or_none()
    if row is None:
        yield None
        return

    value = dict(row.value or {})
    yield value
    row.value = value
    row.updated_at = func.now()
    flag_modified(row, "value")
    session.add(row)


async def with_project_telemetry_locked(
    session: AsyncSession,
    project_id,
    key: str,
    mutate_fn: Callable[[dict], None],
    *,
    design_doc_fallback: dict | None = None,
    skip_locked: bool = False,
) -> bool:
    """Atomic read-modify-write of a ``project_telemetry`` value.

    The row is keyed by ``(project_id, key)``. If it does not exist, seed it
    from ``design_doc_fallback[key]`` so the first post-migration mutation
    preserves a legacy container instead of replacing it with an empty dict.
    Caller commits.
    """
    async with project_telemetry_value_lock(
        session,
        project_id,
        key,
        design_doc_fallback=design_doc_fallback,
        skip_locked=skip_locked,
    ) as value:
        if value is None:
            return False
        mutate_fn(value)
    return True


async def with_sr_analytics_locked(
    session: AsyncSession,
    project_id,
    mutate_fn: Callable[[dict], None],
    *,
    skip_locked: bool = False,
) -> bool:
    """Atomic read-modify-write of ProjectSRAnalytics.data.

    Mirrors with_design_doc_locked but targets the dedicated SR analytics row.
    During a rolling cutover, a missing or empty dedicated row is seeded from
    documented legacy ``design_doc`` SR keys before mutation so a new write
    cannot mask data that has not been backfilled yet.
    """
    from app.models import Project, ProjectSRAnalytics
    from app.sr_analytics_store import legacy_sr_analytics_payload
    from sqlalchemy.dialects.postgresql import insert as pg_insert

    legacy_seed: dict = {}
    project = await session.get(Project, project_id)
    if project is not None:
        legacy_seed = legacy_sr_analytics_payload(project.design_doc)

    await session.execute(
        pg_insert(ProjectSRAnalytics)
        .values(project_id=project_id, data=legacy_seed, updated_at=func.now())
        .on_conflict_do_nothing(
            index_elements=[ProjectSRAnalytics.project_id]
        )
    )

    stmt = (
        select(ProjectSRAnalytics)
        .where(ProjectSRAnalytics.project_id == project_id)
        .with_for_update(skip_locked=skip_locked)
    )
    result = await session.execute(stmt)
    sr_row = result.scalar_one_or_none()
    if sr_row is None:
        return False

    data = dict(sr_row.data or {})
    for key, value in legacy_seed.items():
        data.setdefault(key, value)
    mutate_fn(data)
    sr_row.data = data
    sr_row.updated_at = func.now()
    flag_modified(sr_row, "data")
    session.add(sr_row)
    return True


async def with_vision_locked(
    session: AsyncSession,
    project_id,
    mutate_fn: Callable[[dict], None],
    *,
    skip_locked: bool = False,
    timeout: float = 30.0,
    _locked_project: Project | None = None,
) -> bool:
    """Atomic read-modify-write of project.vision under a project row lock.

    Mirror of ``with_design_doc_locked`` for the sibling ``vision`` JSON
    column. Acquires SELECT FOR UPDATE (skip_locked=False — background callers
    wait rather than silently skip), hands ``mutate_fn`` the current vision
    dict loaded *inside* the lock, then writes the result back. Caller is
    responsible for committing.

    ``timeout`` is forwarded to :func:`project_lock` and bounds how long a
    blocking acquire (``skip_locked=False``) waits before raising
    ``OperationalError`` — operator-steering callers pass a short timeout so a
    contended project surfaces a retryable error instead of stalling.

    This is the canonical helper for serializing vision mutations from
    background task completions (strategic_completion, research_completion,
    etc.). Using it consistently prevents concurrent background writers from
    clobbering each other's updates via stale read-modify-write races — the
    same lost-update class already eliminated for ``design_doc`` and
    ``directions``.
    """

    # _locked_project is the narrow re-entrant bridge for code that already
    # holds the canonical Project row lock while also holding a dependent Task
    # lock. It prevents a second Project acquire and the resulting self-deadlock
    # while keeping every vision assignment centralized in this helper. Callers
    # must only pass the exact project row yielded by project_lock.
    def _apply(project: Project) -> None:
        vision = dict(project.vision or {})
        mutate_fn(vision)
        project.vision = vision
        flag_modified(project, "vision")
        session.add(project)

    if _locked_project is not None:
        if str(_locked_project.id) != str(project_id):
            raise ValueError("_locked_project does not match project_id")
        _apply(_locked_project)
        # A caller can next acquire a different narrow Project projection in
        # this same transaction. Those locks use populate_existing=True; with
        # autoflush disabled, that refresh would otherwise restore the old DB
        # value over this still-dirty vision attribute. Persist the canonical
        # JSON mutation before returning, matching the directions and
        # problem-model helpers' cross-projection guarantee.
        await session.flush([_locked_project])
        return True

    async with project_lock(
        session,
        project_id,
        skip_locked=skip_locked,
        timeout=timeout,
        load_columns=[Project.id, Project.vision],
    ) as project:
        if project is None:
            return False
        _apply(project)
        await session.flush([project])
    return True


async def with_task_context_locked(
    session: AsyncSession,
    task_id,
    mutate_fn: Callable[[dict], None],
    *,
    skip_locked: bool = False,
    timeout: float = 30.0,
) -> bool:
    """Atomic read-modify-write of ``Task.context`` under a task row lock.

    Mirror of :func:`with_vision_locked` for the per-task ``context`` JSON
    column. Acquires SELECT FOR UPDATE on the task row (via :func:`task_lock`),
    hands ``mutate_fn`` the current ``context`` dict loaded *inside* the lock,
    then writes the result back via ``flag_modified``. Caller commits.

    ``Task.context`` is a whole-column JSON blob written from several
    strategic-API paths (``routers/admin_agent_ops.py``) and the
    heartbeat ledger/cursor mirror. Each writer
    sets a *disjoint* set of keys (``_strategic_api_called`` /
    ``_strategic_vision_set`` on one side, ``_exam_ledger`` /
    ``message_cursor`` on the other) but did so via a stale-read whole-column
    overwrite, so two concurrent writers dropped each other's keys (lost
    update). Loading the dict *inside* the FOR UPDATE lock is what serializes
    them and forces a fresh read — the same lost-update class already
    eliminated for ``vision`` / ``design_doc`` / ``directions``.

    CANONICAL LOCK ORDER: this helper takes ONLY the task row lock. Callers that
    also need the project row lock MUST acquire the project lock FIRST
    (Project→Task — see :func:`lock_project_then_task`); taking the task lock
    first risks the C1 cross-table deadlock. The heartbeat caller
    already holds the project lock (via :func:`with_vision_locked`) when it
    invokes this, so ordering is preserved.

    Args:
        session: AsyncSession to issue the lock against. Lock is held until
            the caller commits/rolls back.
        task_id: UUID or str of the task.
        mutate_fn: Called with the freshly-loaded ``context`` dict; mutate it
            in place (add/overwrite keys).
        skip_locked: When False (default), wait up to ``timeout`` for the row
            (durable callers that must not silently drop the write); when True,
            return False immediately if the row is held by another session.
        timeout: Max seconds to wait when ``skip_locked=False``.

    Returns:
        True if ``mutate_fn`` was applied, False if the row was held
        (``skip_locked=True``) or the task is missing.
    """
    async with task_lock(
        session, task_id, skip_locked=skip_locked, timeout=timeout
    ) as task:
        if task is None:
            return False
        ctx = dict(task.context or {})
        mutate_fn(ctx)
        task.context = ctx
        flag_modified(task, "context")
        session.add(task)
    return True


async def with_problem_model_locked(
    session: AsyncSession,
    project_id,
    mutate_fn: Callable[[dict], None],
    *,
    skip_locked: bool = False,
    timeout: float = 30.0,
) -> bool:
    """Atomic read-modify-write of project.problem_model under a project row lock.

    Mirror of ``with_vision_locked`` for the sibling ``problem_model`` JSON
    column. Acquires SELECT FOR UPDATE (``skip_locked=False`` by default so
    background callers wait rather than silently skip), hands ``mutate_fn`` the
    current problem_model dict loaded *inside* the lock, then writes the result
    back via ``flag_modified``. Caller is responsible for committing.

    This is the canonical helper for serializing problem-model mutations that
    are not full agent ``set_problem_model`` rewrites — e.g. the in-transaction
    gap reconciliation on direction retire
    (:func:`app.problem_model.reconcile_gaps_for_retired_directions`). Loading
    the dict inside the lock is what prevents a concurrent strategic write from
    clobbering the reconciliation via a stale read-modify-write race — the same
    lost-update class already eliminated for ``vision`` and ``directions``.
    """
    async with project_lock(
        session,
        project_id,
        skip_locked=skip_locked,
        timeout=timeout,
        load_columns=[Project.id, Project.problem_model],
    ) as project:
        if project is None:
            return False
        from app.problem_model import sanitize_problem_model_raw  # noqa: PLC0415
        problem_model = sanitize_problem_model_raw(dict(project.problem_model or {}))
        mutate_fn(problem_model)
        project.problem_model = problem_model
        flag_modified(project, "problem_model")
        session.add(project)
        # Match the directions helper's transactional guarantee: a later
        # populate_existing Project projection (commonly the vision lock in a
        # retire chain) must not refresh this unflushed mutation back to its
        # pre-reconciliation value when autoflush is disabled.
        await session.flush([project])
    return True


async def with_directions_locked(
    session: AsyncSession,
    project_id,
    mutate_fn: Callable[[list], list | None],
    *,
    skip_locked: bool = False,
) -> bool:
    """Atomic read-modify-write of project.directions under a project row lock.

    Mirror of ``with_design_doc_locked`` for the sibling ``directions`` JSON
    column. Acquires SELECT FOR UPDATE, hands ``mutate_fn`` the current
    directions list loaded *inside* the lock, then writes the result back.
    ``mutate_fn`` may mutate the list in place and return None, or return a
    replacement list (for whole-array overwrites). Caller is responsible for
    committing.

    Args:
        skip_locked: When False (default), block until the lock is acquired
            (suitable for background callers that must not silently drop
            updates). When True, return False immediately if the row is held
            by another session — preserves best-effort / non-blocking
            semantics for hot dispatch paths.

    Returns:
        True if mutate_fn was applied, False if the row was held and
        skip_locked=True caused an immediate skip.

    This is the canonical helper for serializing directions mutations. Loading
    the array inside the lock is what prevents a whole-array overwrite from
    silently dropping a direction another writer appended concurrently — the
    same lost-update class already eliminated for ``design_doc``.
    """
    return await _with_project_locked_list_mutation(
        session,
        project_id,
        mutate_fn,
        skip_locked=skip_locked,
    )
