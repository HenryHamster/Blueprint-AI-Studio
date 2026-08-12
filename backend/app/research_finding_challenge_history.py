"""Durable per-finding challenge receipts used across parked research re-entry."""

from __future__ import annotations

import contextlib
from typing import Any

from sqlalchemy.orm.attributes import flag_modified

from app.finding_deduplication import finding_hash
from app.models import Task
from app.task_evaluators import FINDING_CHALLENGE_VERDICT_KEY

FINDING_CHALLENGE_HISTORY_KEY = "finding_challenge_records"
FINDING_CHALLENGE_RECORD_KEY = "finding_challenge"


def _record_matches(
    record: object,
    *,
    boundary: str,
    first_verdict: str,
    current_hash: str,
    allow_missing_hash: bool,
) -> bool:
    if not isinstance(record, dict):
        return False
    if record.get("challenged_boundary") != boundary:
        return False
    if str(record.get("first_verdict") or "") != str(first_verdict):
        return False
    record_hash = record.get("finding_hash")
    return (record_hash is None and allow_missing_hash) or record_hash == current_hash


def attach_historical_finding_challenge_record(
    task: Task, finding: dict[str, Any]
) -> dict[str, Any] | None:
    """Rehydrate the newest exact-hash receipt onto a reconstructed finding."""

    current_hash = finding_hash(
        finding.get("title", ""),
        finding.get("description") or finding.get("prompt", ""),
    )
    history = (task.context or {}).get(FINDING_CHALLENGE_HISTORY_KEY) or []
    record = next(
        (
            item
            for item in reversed(history)
            if isinstance(item, dict) and item.get("finding_hash") == current_hash
        ),
        None,
    )
    if record is None:
        return None
    attached = dict(record)
    finding[FINDING_CHALLENGE_RECORD_KEY] = attached
    with contextlib.suppress(Exception):
        flag_modified(task, "plan_output")
    return attached


def consume_finding_challenge_record(
    task: Task,
    finding: dict[str, Any],
    *,
    boundary: str,
    first_verdict: str,
) -> dict[str, Any] | None:
    """Attach an active or previously consumed verdict to its exact finding.

    Carried findings are appended after the pre-gate ``plan_output`` commit. If
    one verdict is consumed and a later finding parks the same pass, only this
    bounded history survives the sidecar commit. Rehydrating by canonical hash
    keeps each earlier decision authoritative across sequential challenges.
    """

    ctx = dict(task.context or {})
    current_hash = finding_hash(
        finding.get("title", ""),
        finding.get("description") or finding.get("prompt", ""),
    )
    active = ctx.get(FINDING_CHALLENGE_VERDICT_KEY)
    consumed_active = _record_matches(
        active,
        boundary=boundary,
        first_verdict=first_verdict,
        current_hash=current_hash,
        allow_missing_hash=True,
    )
    history = [
        item
        for item in (ctx.get(FINDING_CHALLENGE_HISTORY_KEY) or [])
        if isinstance(item, dict)
    ]
    record = active if consumed_active else next(
        (
            item
            for item in reversed(history)
            if _record_matches(
                item,
                boundary=boundary,
                first_verdict=first_verdict,
                current_hash=current_hash,
                allow_missing_hash=False,
            )
        ),
        None,
    )
    if not isinstance(record, dict):
        return None

    attached = dict(record)
    finding[FINDING_CHALLENGE_RECORD_KEY] = attached
    with contextlib.suppress(Exception):
        flag_modified(task, "plan_output")
    if consumed_active:
        if attached not in history:
            history.append(attached)
        ctx[FINDING_CHALLENGE_HISTORY_KEY] = history[-20:]
        ctx.pop(FINDING_CHALLENGE_VERDICT_KEY, None)
        ctx.pop("awaiting_finding_challenge", None)
        task.context = ctx
        flag_modified(task, "context")
    return attached
