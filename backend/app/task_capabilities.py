"""Task capability registry.

This module centralizes metadata about known task capabilities without
defining their payload shape. Payload validation stays in payload_schemas.py
and remains extra-field tolerant so new task fields can be introduced without
turning this registry into a product constraint.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Literal


class TaskType(str, Enum):
    create = "create"
    review = "review"
    fix = "fix"
    plan = "plan"
    research = "research"
    strategic_review = "strategic_review"
    design_doc_update = "design_doc_update"
    security_audit = "security_audit"
    qa = "qa"
    deps = "deps"
    docs = "docs"
    migrate = "migrate"
    smoketest = "smoketest"
    refactor = "refactor"
    debug = "debug"
    perf = "perf"
    investigate = "investigate"
    self_audit = "self_audit"


AgentMode = Literal["write", "read_only"]


@dataclass(frozen=True)
class TaskCapability:
    id: TaskType
    label: str
    agent_mode: AgentMode
    default_category: str | None = None
    user_launchable: bool = False
    provider_configurable: bool = True
    filter_visible: bool = True
    description: str = ""


TASK_CAPABILITIES: tuple[TaskCapability, ...] = (
    TaskCapability(
        TaskType.create,
        "Create",
        "write",
        default_category="build",
        user_launchable=True,
        description="Implement new functionality and open a PR.",
    ),
    TaskCapability(
        TaskType.review,
        "Review",
        "read_only",
        default_category="review",
        user_launchable=True,
        description="Review an existing PR.",
    ),
    TaskCapability(
        TaskType.fix,
        "Fix",
        "write",
        default_category="review",
        user_launchable=True,
        description="Fix a PR from review feedback or CI failures.",
    ),
    TaskCapability(
        TaskType.plan,
        "Plan",
        "read_only",
        default_category="strategic",
        user_launchable=True,
        description="Decompose work into subtasks.",
    ),
    TaskCapability(
        TaskType.research,
        "Research",
        "read_only",
        default_category="strategic",
        user_launchable=True,
        description="Analyze the codebase and produce findings.",
    ),
    TaskCapability(
        TaskType.strategic_review,
        "Strategic Review",
        "read_only",
        default_category="strategic",
        description="Evaluate project direction and propose strategy updates.",
    ),
    TaskCapability(
        TaskType.design_doc_update,
        "Design Doc",
        "read_only",
        default_category="strategic",
        description="Update the living project design document.",
    ),
    TaskCapability(
        TaskType.security_audit,
        "Security Audit",
        "read_only",
        default_category="review",
        description="Audit for security risks without changing code.",
    ),
    TaskCapability(
        TaskType.qa,
        "QA",
        "write",
        default_category="review",
        description="Add or improve test coverage.",
    ),
    TaskCapability(
        TaskType.deps,
        "Deps",
        "write",
        default_category="ops",
        description="Update dependencies and verify compatibility.",
    ),
    TaskCapability(
        TaskType.docs,
        "Docs",
        "write",
        default_category="ops",
        description="Write or update documentation.",
    ),
    TaskCapability(
        TaskType.migrate,
        "Migrate",
        "write",
        default_category="build",
        user_launchable=True,
        description="Create or update database migrations.",
    ),
    TaskCapability(
        TaskType.smoketest,
        "Smoketest",
        "read_only",
        default_category="ops",
        description="Run smoke checks against a deployment.",
    ),
    TaskCapability(
        TaskType.refactor,
        "Refactor",
        "write",
        default_category="build",
        user_launchable=True,
        description="Improve code structure without changing behavior.",
    ),
    TaskCapability(
        TaskType.debug,
        "Debug",
        "read_only",
        default_category="review",
        description="Investigate a failure or bug report.",
    ),
    TaskCapability(
        TaskType.perf,
        "Perf",
        "read_only",
        default_category="review",
        description="Measure and diagnose performance bottlenecks.",
    ),
    TaskCapability(
        TaskType.investigate,
        "Investigate",
        "read_only",
        description="Open-ended read-only investigation.",
    ),
    TaskCapability(
        TaskType.self_audit,
        "Self Audit",
        "read_only",
        description="Score system maturity and recommend operating mode.",
    ),
)

TASK_CAPABILITY_BY_TYPE: dict[TaskType, TaskCapability] = {
    capability.id: capability for capability in TASK_CAPABILITIES
}
TASK_CAPABILITY_BY_VALUE: dict[str, TaskCapability] = {
    capability.id.value: capability for capability in TASK_CAPABILITIES
}

# Backward-compatible constants consumed by existing code.
ALL_TASK_TYPES: frozenset[str] = frozenset(
    capability.id.value for capability in TASK_CAPABILITIES
)
READ_ONLY_TASK_TYPES: frozenset[str] = frozenset(
    capability.id.value
    for capability in TASK_CAPABILITIES
    if capability.agent_mode == "read_only"
)
WRITE_TASK_TYPES: frozenset[str] = frozenset(
    capability.id.value
    for capability in TASK_CAPABILITIES
    if capability.agent_mode == "write"
)
USER_LAUNCHABLE_TASK_TYPES: tuple[str, ...] = tuple(
    capability.id.value
    for capability in TASK_CAPABILITIES
    if capability.user_launchable
)
PROVIDER_CONFIGURABLE_TASK_TYPES: tuple[str, ...] = tuple(
    capability.id.value
    for capability in TASK_CAPABILITIES
    if capability.provider_configurable
)
FILTER_VISIBLE_TASK_TYPES: tuple[str, ...] = tuple(
    capability.id.value for capability in TASK_CAPABILITIES if capability.filter_visible
)


def get_task_capability(task_type: str | TaskType) -> TaskCapability | None:
    """Return metadata for a known task type, or None for future/unknown types."""
    value = task_type.value if isinstance(task_type, TaskType) else task_type
    return TASK_CAPABILITY_BY_VALUE.get(value)
