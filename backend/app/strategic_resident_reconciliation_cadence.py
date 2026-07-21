"""Pure cadence policy for reconciled Strategic Resident follow-ups."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from app.strategic_resident_reconciliation_validation import (
    ReconciliationResultInvalid,
)
from app.strategic_resident_turn_results import StrategicResidentTurnResult


DEFAULT_CONTINUATION_DELAY = timedelta(minutes=15)
MAX_CONTINUATION_DELAY = timedelta(hours=6)
DEADLINE_STATE_KEYS = (
    "deadline_at",
    "deadline_reason",
    "deadline_source",
    "deadline_source_turn_task_id",
    "deadline_scheduled_at",
    "paused_next_wakeup_at",
    "continuation_fingerprint",
    "continuation_unchanged_count",
    "continuation_backoff_seconds",
)
_RUNNABLE_OWNER_LABELS = frozenset(
    {"resident", "agent", "system", "unknown", "unassigned"}
)


@dataclass(frozen=True)
class CadenceDecision:
    effective_at: datetime
    reason: str
    source: str
    fingerprint: str
    unchanged_count: int
    backoff: timedelta


def _parse_timestamp(value: Any, *, field: str) -> datetime:
    if not isinstance(value, str) or not value.strip():
        raise ReconciliationResultInvalid(f"{field} must be an ISO-8601 timestamp")
    try:
        # The Agent emits a canonical strict subset. Keep the authority-side
        # parser tolerant of valid Python ISO forms for older/failover workers.
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        raise ReconciliationResultInvalid(
            f"{field} must be an ISO-8601 timestamp"
        ) from None
    if parsed.tzinfo is not None:
        return parsed.astimezone(timezone.utc).replace(tzinfo=None)
    return parsed


def _effective_open_loops(body: Mapping[str, Any]) -> list[dict[str, Any]]:
    strategy = body.get("working_strategy")
    if not isinstance(strategy, Mapping):
        raise ReconciliationResultInvalid("working_strategy is malformed")
    loops: list[dict[str, Any]] = []
    for index, item in enumerate(list(strategy.get("open_loops") or [])[:100]):
        if not isinstance(item, Mapping):
            raise ReconciliationResultInvalid(
                f"working_strategy.open_loops[{index}] is malformed"
            )
        loops.append(dict(item))
    return loops


def _fingerprint(
    open_loops: list[dict[str, Any]],
    *,
    explicit_intent: str | None,
) -> str:
    if open_loops:
        projection: Any = sorted(
            open_loops,
            key=lambda item: (
                str(item.get("loop_id") or ""),
                json.dumps(item, sort_keys=True, separators=(",", ":"), default=str),
            ),
        )
    elif explicit_intent is not None:
        # Exclude the requested timestamp so a rolling now+1m request cannot
        # evade backoff, while a genuinely different intent starts new lineage.
        projection = {
            "explicit_follow_up_intent": explicit_intent.strip(),
        }
    else:
        raise ValueError("cadence fingerprint requires work")
    encoded = json.dumps(
        projection,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _bounded_backoff(step: int) -> timedelta:
    multiplier = 2 ** min(max(step - 1, 0), 8)
    return min(DEFAULT_CONTINUATION_DELAY * multiplier, MAX_CONTINUATION_DELAY)


def _human_owned(item: Mapping[str, Any]) -> bool:
    owner = item.get("owner")
    if not isinstance(owner, str) or not owner.strip():
        return False
    normalized = owner.strip().casefold()
    # Canonical operator/human/user values and any other named actor are
    # explicitly human-owned. Unknown/null remains runnable to avoid stranding.
    return not (
        normalized in _RUNNABLE_OWNER_LABELS
        or normalized.startswith(tuple(f"{label}:" for label in _RUNNABLE_OWNER_LABELS))
    )


def decide_cadence(
    *,
    body: Mapping[str, Any],
    result: StrategicResidentTurnResult,
    scheduling_state: Mapping[str, Any],
    now: datetime,
) -> CadenceDecision | None:
    """Choose the next meaningful wake from the effective checkpoint."""

    explicit = None
    if result.follow_up and result.follow_up.next_wakeup_at:
        explicit = _parse_timestamp(
            result.follow_up.next_wakeup_at,
            field="follow_up.next_wakeup_at",
        )
    open_loops = _effective_open_loops(body)
    loop_candidates: list[tuple[datetime, str]] = []
    for index, item in enumerate(open_loops):
        if item.get("due_at"):
            loop_candidates.append(
                (
                    _parse_timestamp(
                        item["due_at"],
                        field=f"working_strategy.open_loops[{index}].due_at",
                    ),
                    "open_loop_deadline",
                )
            )
    if explicit is None and not loop_candidates and not open_loops:
        return None

    future_loops = [item for item in loop_candidates if item[0] > now]
    overdue = [item for item in loop_candidates if item[0] <= now]
    human_waiting = (
        bool(open_loops)
        and bool(result.required_attention)
        and all(_human_owned(item) for item in open_loops)
        and (explicit is None or explicit <= now)
    )
    if human_waiting:
        # A future commitment remains meaningful. Once every human-owned
        # commitment is due and has been inspected, wait for the operator
        # instead of repeatedly spending model calls on unchanged evidence.
        if not future_loops:
            return None
        explicit = None
        overdue = []

    explicit_intent = (
        (result.follow_up.summary or "")
        if explicit is not None and result.follow_up is not None
        else None
    )
    fingerprint = _fingerprint(
        open_loops,
        explicit_intent=explicit_intent,
    )
    raw_count = scheduling_state.get("continuation_unchanged_count")
    prior_count = (
        raw_count
        if isinstance(raw_count, int)
        and not isinstance(raw_count, bool)
        and raw_count >= 0
        else 0
    )
    unchanged = scheduling_state.get("continuation_fingerprint") == fingerprint
    unchanged_count = prior_count + 1 if unchanged else 0
    backoff = timedelta(0)

    if explicit is not None and explicit > now:
        effective, source = min(
            [(explicit, "follow_up"), *future_loops],
            key=lambda item: (item[0], item[1]),
        )
        reason = source
        if unchanged and source == "follow_up":
            backoff = _bounded_backoff(unchanged_count)
            floor = now + backoff
            if effective < floor:
                effective = min(
                    floor,
                    min((item[0] for item in future_loops), default=floor),
                )
                reason = "unchanged_follow_up_backoff"
    elif overdue or (explicit is not None and explicit <= now):
        overdue_candidates = [*overdue]
        if explicit is not None:
            overdue_candidates.append((explicit, "follow_up"))
        _deadline, source = min(
            overdue_candidates,
            key=lambda item: (item[0], item[1]),
        )
        backoff = _bounded_backoff(unchanged_count) if unchanged else timedelta(0)
        effective = now + backoff
        if future_loops:
            effective = min(effective, min(item[0] for item in future_loops))
        reason = "overdue_commitment" if not backoff else "unchanged_overdue_backoff"
    elif future_loops:
        effective, source = min(future_loops, key=lambda item: (item[0], item[1]))
        reason = source
    else:
        source = "effective_open_loops"
        reason = "open_loop_continuation"
        backoff = _bounded_backoff(unchanged_count + 1)
        effective = now + backoff

    return CadenceDecision(
        effective_at=effective,
        reason=reason,
        source=source,
        fingerprint=fingerprint,
        unchanged_count=unchanged_count,
        backoff=backoff,
    )


__all__ = ["DEADLINE_STATE_KEYS", "CadenceDecision", "decide_cadence"]
