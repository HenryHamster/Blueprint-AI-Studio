"""Autonomous spawn-budget accounting.

This module is the shared source of truth for classifying autonomously created
tasks in a trailing window and for evaluating the global initiative/backstop
spawn budgets.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Mapping, Sequence

from sqlalchemy import and_, or_
from sqlmodel import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Task, _utcnow
from app.task_capabilities import (
    ALL_TASK_TYPES,
    AUTONOMOUS_SPAWN_INITIATIVE_TASK_TYPES,
    AUTONOMOUS_SPAWN_SERVICING_TASK_TYPES,
)

AUTONOMOUS_SPAWN_BUDGET_MODES = frozenset({"off", "observe", "enforce"})
DEFAULT_AUTONOMOUS_SPAWN_BUDGET_MODE = "observe"
DEFAULT_AUTONOMOUS_INITIATIVE_TASKS_PER_HOUR = 50
DEFAULT_AUTONOMOUS_BACKSTOP_TASKS_PER_HOUR = 50
AUTONOMOUS_SPAWN_BUDGET_CATEGORY_KEYS = frozenset({"initiative", "servicing"})
AUTONOMOUS_DIRECT_SPAWN_PROVENANCE = "strategic_direct"


def autonomous_spawn_task_clause():
    """SQL predicate for work authored by Blueprint's autonomous control plane.

    Most autonomous tasks are created by ``ORCHESTRATOR``.  Resident Strategic
    roots deliberately use ``STRATEGIC_AGENT`` so their provenance remains
    distinct, but they consume the same fleet and model budget.  Count only the
    direct-spawn provenance within that creator class so fixtures/manual rows
    labelled ``STRATEGIC_AGENT`` do not accidentally enter the budget ledger.
    """

    return or_(
        Task.created_by == "ORCHESTRATOR",
        and_(
            Task.created_by == "STRATEGIC_AGENT",
            Task.context["spawned_by"].as_string()
            == AUTONOMOUS_DIRECT_SPAWN_PROVENANCE,
        ),
    )


@dataclass(frozen=True)
class AutonomousSpawnBudgetConfig:
    mode: str = DEFAULT_AUTONOMOUS_SPAWN_BUDGET_MODE
    initiative_cap: int = DEFAULT_AUTONOMOUS_INITIATIVE_TASKS_PER_HOUR
    global_cap: int = DEFAULT_AUTONOMOUS_BACKSTOP_TASKS_PER_HOUR
    initiative_task_types: frozenset[str] = field(
        default_factory=lambda: AUTONOMOUS_SPAWN_INITIATIVE_TASK_TYPES
    )
    servicing_task_types: frozenset[str] = field(
        default_factory=lambda: AUTONOMOUS_SPAWN_SERVICING_TASK_TYPES
    )


@dataclass(frozen=True)
class AutonomousSpawnBudgetSnapshot:
    mode: str
    window_started_at: datetime
    window_ended_at: datetime
    initiative_cap: int
    global_cap: int
    counts: dict[str, int]
    task_type_counts: dict[str, dict[str, int]]

    @property
    def total_count(self) -> int:
        return sum(self.counts.values())

    @property
    def initiative_count(self) -> int:
        return self.counts.get("initiative", 0)

    @property
    def servicing_count(self) -> int:
        return self.counts.get("servicing", 0)

    @property
    def other_count(self) -> int:
        return self.counts.get("other", 0)

    @property
    def initiative_headroom(self) -> int:
        return max(0, self.initiative_cap - self.initiative_count)

    @property
    def global_headroom(self) -> int:
        return max(0, self.global_cap - self.total_count)

    def as_log_fields(self) -> dict[str, Any]:
        return {
            "mode": self.mode,
            "initiative": self.initiative_count,
            "servicing": self.servicing_count,
            "other": self.other_count,
            "initiative_cap": self.initiative_cap,
            "initiative_headroom": self.initiative_headroom,
            "global_cap": self.global_cap,
            "global_headroom": self.global_headroom,
            "total": self.total_count,
        }


def normalize_autonomous_spawn_budget_mode(raw: Any) -> str:
    mode = str(raw or DEFAULT_AUTONOMOUS_SPAWN_BUDGET_MODE).strip().lower()
    if mode not in AUTONOMOUS_SPAWN_BUDGET_MODES:
        raise ValueError(
            "autonomous_spawn_budget_mode must be one of: "
            f"{', '.join(sorted(AUTONOMOUS_SPAWN_BUDGET_MODES))}"
        )
    return mode


def normalize_autonomous_initiative_cap(raw: Any) -> int:
    try:
        cap = int(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError(
            "max_global_autonomous_initiative_tasks_per_hour must be a number"
        ) from exc
    if not (0 <= cap <= 500):
        raise ValueError(
            "max_global_autonomous_initiative_tasks_per_hour must be between 0 and 500"
        )
    return cap


def normalize_autonomous_backstop_cap(raw: Any) -> int:
    try:
        cap = int(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError(
            "max_global_autonomous_tasks_per_hour must be a number"
        ) from exc
    if not (1 <= cap <= 500):
        raise ValueError(
            "max_global_autonomous_tasks_per_hour must be between 1 and 500"
        )
    return cap


def _normalize_type_list(field_name: str, raw: Any) -> list[str]:
    if isinstance(raw, str):
        raw = [part.strip() for part in raw.split(",") if part.strip()]
    if not isinstance(raw, (list, tuple, set)):
        raise ValueError(f"{field_name} must be a list of task-type strings")
    if not all(isinstance(item, str) for item in raw):
        raise ValueError(f"{field_name} entries must be strings")
    values = sorted({item.strip() for item in raw if item.strip()})
    invalid = [item for item in values if item not in ALL_TASK_TYPES]
    if invalid:
        raise ValueError(
            f"{field_name} contains unknown task type(s) {invalid}; "
            f"must be one of {sorted(ALL_TASK_TYPES)}"
        )
    return values


def normalize_autonomous_spawn_budget_categories(raw: Any) -> dict[str, list[str]]:
    if raw is None:
        return default_autonomous_spawn_budget_categories()
    if not isinstance(raw, Mapping):
        raise ValueError("autonomous_spawn_budget_categories must be a dict")
    invalid_categories = sorted(
        str(key) for key in raw if str(key) not in AUTONOMOUS_SPAWN_BUDGET_CATEGORY_KEYS
    )
    if invalid_categories:
        raise ValueError(
            "autonomous_spawn_budget_categories has unknown category key(s) "
            f"{invalid_categories}; must be one of "
            f"{sorted(AUTONOMOUS_SPAWN_BUDGET_CATEGORY_KEYS)}"
        )
    categories = default_autonomous_spawn_budget_categories()
    for key in sorted(AUTONOMOUS_SPAWN_BUDGET_CATEGORY_KEYS):
        if key in raw:
            categories[key] = _normalize_type_list(
                f"autonomous_spawn_budget_categories[{key}]", raw[key]
            )
    overlap = sorted(set(categories["initiative"]) & set(categories["servicing"]))
    if overlap:
        raise ValueError(
            "autonomous_spawn_budget_categories initiative/servicing sets overlap: "
            f"{overlap}"
        )
    return categories


def default_autonomous_spawn_budget_categories() -> dict[str, list[str]]:
    return {
        "initiative": sorted(AUTONOMOUS_SPAWN_INITIATIVE_TASK_TYPES),
        "servicing": sorted(AUTONOMOUS_SPAWN_SERVICING_TASK_TYPES),
    }


def autonomous_spawn_budget_config_from_runtime(
    runtime_config: Mapping[str, Any],
) -> AutonomousSpawnBudgetConfig:
    categories = normalize_autonomous_spawn_budget_categories(
        runtime_config.get("autonomous_spawn_budget_categories")
    )
    initiative_cap = runtime_config.get(
        "max_global_autonomous_initiative_tasks_per_hour",
        runtime_config.get(
            "max_global_autonomous_tasks_per_hour",
            DEFAULT_AUTONOMOUS_INITIATIVE_TASKS_PER_HOUR,
        ),
    )
    global_cap = runtime_config.get(
        "max_global_autonomous_tasks_per_hour",
        DEFAULT_AUTONOMOUS_BACKSTOP_TASKS_PER_HOUR,
    )
    return AutonomousSpawnBudgetConfig(
        mode=normalize_autonomous_spawn_budget_mode(
            runtime_config.get("autonomous_spawn_budget_mode")
        ),
        initiative_cap=normalize_autonomous_initiative_cap(initiative_cap),
        global_cap=normalize_autonomous_backstop_cap(global_cap),
        initiative_task_types=frozenset(categories["initiative"]),
        servicing_task_types=frozenset(categories["servicing"]),
    )


def _has_servicing_lineage(
    *,
    payload: Mapping[str, Any],
    context: Mapping[str, Any],
) -> bool:
    if payload.get("pr_number") or context.get("pr_number"):
        return True
    if isinstance(context.get("review_loop"), Mapping):
        return True
    spawned_by = str(context.get("spawned_by") or "").lower()
    if "remediation" in spawned_by or "review" in spawned_by:
        return True
    return any(
        key in payload
        for key in (
            "source_task_id",
            "origin_task_id",
            "review_task_id",
            "fix_task_id",
            "parent_pr_number",
        )
    )


def classify_autonomous_spawn_budget_category(
    *,
    task_type: str | None,
    config: AutonomousSpawnBudgetConfig | None = None,
    parent_task_id: Any = None,
    depends_on: Sequence[Any] | None = None,
    payload: Mapping[str, Any] | None = None,
    context: Mapping[str, Any] | None = None,
) -> str:
    config = config or AutonomousSpawnBudgetConfig()
    task_type_value = str(task_type or "").strip()
    payload = payload or {}
    context = context or {}
    has_dependency_lineage = bool(parent_task_id) or bool(depends_on)
    has_servicing_lineage = _has_servicing_lineage(payload=payload, context=context)

    if task_type_value in config.servicing_task_types:
        return "servicing"
    if has_servicing_lineage or has_dependency_lineage:
        return "servicing"
    if task_type_value in config.initiative_task_types:
        return "initiative"
    return "other"


async def compute_autonomous_spawn_budget_snapshot(
    session: AsyncSession,
    *,
    runtime_config: Mapping[str, Any],
    now: datetime | None = None,
    window: timedelta = timedelta(hours=1),
) -> AutonomousSpawnBudgetSnapshot:
    window_end = now or _utcnow()
    window_start = window_end - window
    config = autonomous_spawn_budget_config_from_runtime(runtime_config)
    counts = {"initiative": 0, "servicing": 0, "other": 0}
    task_type_counts: dict[str, dict[str, int]] = {
        "initiative": {},
        "servicing": {},
        "other": {},
    }

    # Project only the JSON the classifier actually reads instead of the whole
    # ``payloads``/``context`` JSONB columns. ``context`` accretes multi-hundred-KB
    # agent state over a task's lifetime, and this snapshot runs on HOT spawn paths
    # (every idle-research tick per project, every guarded strategic spawn) over ALL
    # autonomous tasks in the trailing hour — at the 500/hr backstop a full-column
    # fetch dragged tens of MB per check. The classifier needs only payloads[0] (its
    # ``type`` plus servicing-lineage key PRESENCE) and three small context keys, so
    # pull exactly those. The whole first payload element is projected (rather than
    # rebuilt key-by-key) to preserve the ``key in payload`` presence semantics in
    # _has_servicing_lineage; context is read only via ``.get()``/isinstance, so a
    # slim three-key context dict is behaviourally exact.
    result = await session.execute(
        select(
            Task.payloads[0],
            Task.context["pr_number"],
            Task.context["review_loop"],
            Task.context["spawned_by"],
            Task.parent_task_id,
            Task.depends_on,
        ).where(
            autonomous_spawn_task_clause(),
            Task.created_at >= window_start,
            Task.created_at <= window_end,
        )
    )
    for (
        first_payload,
        ctx_pr_number,
        ctx_review_loop,
        ctx_spawned_by,
        parent_task_id,
        depends_on,
    ) in result.all():
        payload = first_payload if isinstance(first_payload, dict) else {}
        task_type = str(payload.get("type") or "").strip()
        context = {
            "pr_number": ctx_pr_number,
            "review_loop": ctx_review_loop,
            "spawned_by": ctx_spawned_by,
        }
        category = classify_autonomous_spawn_budget_category(
            task_type=task_type,
            config=config,
            parent_task_id=parent_task_id,
            depends_on=depends_on,
            payload=payload,
            context=context,
        )
        counts[category] += 1
        bucket = task_type_counts[category]
        bucket[task_type or "unknown"] = bucket.get(task_type or "unknown", 0) + 1

    return AutonomousSpawnBudgetSnapshot(
        mode=config.mode,
        window_started_at=window_start,
        window_ended_at=window_end,
        initiative_cap=config.initiative_cap,
        global_cap=config.global_cap,
        counts=counts,
        task_type_counts=task_type_counts,
    )


async def compute_autonomous_spawn_budget_counterfactual(
    session: AsyncSession,
    *,
    runtime_config: Mapping[str, Any],
    now: datetime | None = None,
    window: timedelta = timedelta(hours=24),
) -> dict[str, Any]:
    """Compute the observe-mode counterfactual for ``autonomous_spawn_budget_mode``.

    This is the MEASURED evidence the shared mode-knob graduation harness
    (:mod:`app.mode_knob_graduation`) consumes to decide GRADUATE / CUT / HOLD for
    the initiative spawn-budget knob.  It answers "if the tighter initiative cap
    were flipped to ``enforce`` over the trailing window, how often would it have
    actually bound?" — the real signal that the enforce branch has lift.

    Delegates the accounting to :func:`compute_autonomous_spawn_budget_snapshot`
    (the single classification source of truth) and derives, without mutating any
    counter:

    * ``initiative_count`` / ``servicing_count`` / ``other_count`` / ``total_count``
    * ``initiative_cap`` / ``initiative_headroom``
    * ``would_have_blocked`` — how many initiative spawns exceeded the cap in the
      window (``max(0, initiative_count - initiative_cap)``); the count the enforce
      branch WOULD have blocked
    * ``pressure`` — ``initiative_count / initiative_cap`` (the scalar graduation
      signal compared against thresholds), or ``None`` when the cap is zero so the
      ratio is undefined and the harness must HOLD rather than divide by zero.

    Read-only and bounded (inherits the snapshot query's bounds).
    """
    snapshot = await compute_autonomous_spawn_budget_snapshot(
        session, runtime_config=runtime_config, now=now, window=window
    )
    initiative_cap = snapshot.initiative_cap
    would_have_blocked = max(0, snapshot.initiative_count - initiative_cap)
    pressure = (
        snapshot.initiative_count / initiative_cap if initiative_cap > 0 else None
    )
    return {
        "mode": snapshot.mode,
        "window_hours": window.total_seconds() / 3600.0,
        "initiative_count": snapshot.initiative_count,
        "servicing_count": snapshot.servicing_count,
        "other_count": snapshot.other_count,
        "total_count": snapshot.total_count,
        "initiative_cap": initiative_cap,
        "initiative_headroom": snapshot.initiative_headroom,
        "would_have_blocked": would_have_blocked,
        "pressure": pressure,
    }


def build_autonomous_spawn_budget_guard(
    *,
    snapshot: AutonomousSpawnBudgetSnapshot,
    spawn_category: str,
    enforce_initiative: bool,
) -> dict[str, Any] | None:
    """Return the blocking guard for an enforced spawn, if any.

    The global backstop remains the hard ceiling; the initiative budget only
    binds when the caller has explicitly flipped to enforce mode.
    """

    if snapshot.total_count >= snapshot.global_cap:
        return {
            "guard": "global_rate_limit_exceeded",
            "reason": (
                f"Global autonomous backstop reached: "
                f"{snapshot.total_count}/{snapshot.global_cap} "
                f"(initiative={snapshot.initiative_count}, "
                f"servicing={snapshot.servicing_count}, other={snapshot.other_count})"
            ),
            "current": snapshot.total_count,
            "limit": snapshot.global_cap,
            "headroom": snapshot.global_headroom,
            "mode": snapshot.mode,
            "scope": "global",
            "category": spawn_category,
        }
    if (
        enforce_initiative
        and spawn_category == "initiative"
        and snapshot.initiative_count >= snapshot.initiative_cap
    ):
        return {
            "guard": "initiative_rate_limit_exceeded",
            "reason": (
                f"Initiative autonomous budget reached: "
                f"{snapshot.initiative_count}/{snapshot.initiative_cap} "
                f"(servicing={snapshot.servicing_count}, other={snapshot.other_count})"
            ),
            "current": snapshot.initiative_count,
            "limit": snapshot.initiative_cap,
            "headroom": snapshot.initiative_headroom,
            "mode": snapshot.mode,
            "scope": "initiative",
            "category": spawn_category,
        }
    return None
