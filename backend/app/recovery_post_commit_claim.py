"""Atomic ownership helpers for post-commit recovery claims."""

from __future__ import annotations

import uuid

from sqlalchemy import text
from sqlmodel import select

from app import services
from app.database import resolve_dialect_name
from app.models import Task, TaskStatus
from app.time_utils import _utcnow


async def _claim_and_bump_total_attempts(
    task_id,
    pending_meta,
    *,
    expected_status: TaskStatus,
    expected_generation_token: str | None = None,
) -> dict | None:
    """Try to claim the task row and advance total_attempts atomically.

    Opens an isolated session, dialect-gates SET LOCAL lock_timeout, and issues
    SELECT ... FOR UPDATE SKIP LOCKED on the task row. Returns None if the row
    is held by another writer (caller should defer this marker without doing any
    work — zero lock_timeout burned, zero DB connections wasted). Returns a dict
    with the bumped ``total_attempts`` value when the lock is acquired; the commit
    releases the row lock in milliseconds so _process_post_commit's own
    skip_locked attempt succeeds immediately after.

    Write-ahead durability: the claim-commit advances total_attempts BEFORE work
    starts. A hard kill after this commit but before _process_post_commit means
    the next sweep sees total_attempts already incremented — correct; the ceiling
    fires in bounded cycles regardless of whether the work ran.
    """
    async with services.AsyncSessionLocal() as claim_session:
        if resolve_dialect_name(claim_session) == "postgresql":
            await claim_session.execute(text("SET LOCAL lock_timeout = '5s'"))
        locked = (
            (
                await claim_session.execute(
                    select(Task)
                    .where(
                        Task.id == task_id,
                        Task.post_commit_pending.is_(True),
                        Task.archived.is_(False),
                        Task.status == expected_status,
                    )
                    .with_for_update(skip_locked=True)
                )
            )
            .scalars()
            .first()
        )
        if locked is None:
            # Row held by a peer or no longer carries the exact ownership state
            # selected by the sweep. Either case is a clean defer.
            return None

        new_ctx = dict(locked.context or {})
        new_meta = dict(new_ctx.get("_post_commit_pending_meta") or pending_meta)
        current_generation = str(
            new_meta.get("generation_token") or new_meta.get("at") or ""
        )
        if (
            expected_generation_token is not None
            and current_generation != expected_generation_token
        ):
            # Same status + pending=True can describe a later completion
            # generation. Never claim the newer payload using an older sweep
            # snapshot (ABA).
            return None
        # Seed from on-disk value; fall back to attempts for pre-rework markers
        # that have no total_attempts field yet.
        _current_ta = int(
            new_meta.get("total_attempts") or new_meta.get("attempts", 0) or 0
        )
        new_ta = _current_ta + 1
        claim_token = uuid.uuid4().hex
        new_meta["total_attempts"] = new_ta
        new_meta["claim_token"] = claim_token
        new_ctx["_post_commit_pending_meta"] = new_meta
        locked.context = new_ctx
        from sqlalchemy.orm.attributes import flag_modified

        flag_modified(locked, "context")
        claim_session.add(locked)
        await claim_session.commit()
        return {
            "total_attempts": new_ta,
            "claim_token": claim_token,
            "generation_token": current_generation,
            "payload": new_meta.get("payload"),
            "attempts": int(new_meta.get("attempts", 0) or 0),
            "reason": new_meta.get("reason"),
        }


def _post_commit_claim_is_current(
    task: Task,
    claim_token: str,
    *,
    expected_status: TaskStatus | None = None,
) -> bool:
    """Return whether a claimed continuation still owns this exact row."""

    meta = dict((task.context or {}).get("_post_commit_pending_meta") or {})
    return bool(
        task.post_commit_pending
        and not task.archived
        and (expected_status is None or task.status == expected_status)
        and meta.get("claim_token") == claim_token
    )


def _build_post_commit_abandoned_context(
    ctx: dict,
    *,
    after_attempts: int | None = None,
    after_total_attempts: int | None = None,
    after_pending_age_s: int | None = None,
    reason: str | None = None,
    last_reason: str | None = None,
) -> dict:
    """Build context dict with _post_commit_abandoned marker.

    Returns a new context dict with:
    - _post_commit_abandoned key set with provided fields (always includes "at")
    - _post_commit_pending_meta key removed

    Does not touch any session or database.
    """
    new_ctx = dict(ctx or {})
    abandoned = {"at": _utcnow().isoformat()}

    if after_attempts is not None:
        abandoned["after_attempts"] = after_attempts
    if after_total_attempts is not None:
        abandoned["after_total_attempts"] = after_total_attempts
    if after_pending_age_s is not None:
        abandoned["after_pending_age_s"] = after_pending_age_s
    if reason is not None:
        abandoned["reason"] = reason
    if last_reason is not None:
        abandoned["last_reason"] = last_reason

    new_ctx["_post_commit_abandoned"] = abandoned
    new_ctx.pop("_post_commit_pending_meta", None)
    return new_ctx
