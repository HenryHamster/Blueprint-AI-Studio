"""Declared dependency sequencing for campaign-style task chains.

Two payload fields (``payload_schemas.BasePayload``) make "build task B on top
of task A's merged state" expressible:

- ``require_deps_landed_on``: branch every ``depends_on`` dependency must have
  LANDED on before the task dispatches. This is a deterministic gate that
  replaces the genuine-code-dependency file-overlap heuristic for the
  declaring task's edges — an ordering-only cross-base dep is non-blocking by
  design in the heuristic path, and the same-base path only waits for the
  merge of create-type deps, which is exactly why sequenced chains were not
  expressible before.
- ``release_base_branch``: branch the task's ``base_branch`` is repointed to
  at dependency release time. The agent resolves the branch TIP at claim, so
  the repointed task starts from the named branch's state INCLUDING its
  dependencies' merged work — starting state, not just scheduling.

The helpers here are called from ``orchestrator_health`` (the dispatch gate in
``_check_candidate_task_deps`` and the release chokepoint
``ensure_dependent_base_recut``); they live in this module because
``orchestrator_health`` is a chronic size offender and this logic is a
self-contained policy unit.
"""

import logging
from typing import Any

from sqlalchemy.orm.attributes import flag_modified

from app.landed_signal import landed_on_branch, suppresses_redispatch
from app.models import Project, Task, TaskStatus

logger = logging.getLogger(__name__)


async def landing_gate(
    task: Task, session: Any
) -> tuple[str | None, Project | None]:
    """Read the candidate's declared landing gate, fetching the project only
    when a gate is declared (the project disambiguates the integration branch,
    where a plain landed signal suffices)."""
    branch = (task.first_payload or {}).get("require_deps_landed_on") or None
    if not branch:
        return None, None
    return branch, await session.get(Project, task.project_id)


def landing_gate_ok(
    dep_task: Task, branch: str, project: Project | None
) -> bool:
    """True when this dependency edge no longer holds the declaring task.

    Called per dependency edge from ``_check_candidate_task_deps``; when the
    candidate declares a gate, this REPLACES the heuristic path for its edges
    (strictly stronger than the same-base wait, and deliberately overriding
    the ordering-only non-blocking classification).

    Satisfied when the dependency is COMPLETED and its merged work is on the
    named branch. A no-op completion that affirmatively determined its change
    ALREADY EXISTS on HEAD (``suppresses_redispatch``) is satisfied too — it
    will never land anywhere, and holding on it is the known
    COMPLETED-non-landed permanent-wedge class. Callers handle cancelled/failed
    deps before consulting the gate (non-death cancellations such as a clean
    SCOPE_EMPTY are already treated as satisfied there, so they can never
    wedge the sequence; a dead dep still cascade-cancels).
    """
    if dep_task.status != TaskStatus.COMPLETED:
        return False
    if suppresses_redispatch(dep_task):
        return True
    return landed_on_branch(dep_task, branch, project)


async def apply_declared_release_base(dependent: Task, session: Any) -> None:
    """Repoint ``base_branch`` to a declared ``release_base_branch``.

    Runs inside ``ensure_dependent_base_recut`` — the single chokepoint every
    dependency-release path (dispatch driver, completion-side warm-claim,
    reaper resume) funnels through, and which the dispatch loop runs BEFORE
    stamping ``_deps_satisfied``, so a warm claim can never read the
    pre-repoint base. When the repointed base equals the recut source,
    ``recut_dependent_base_from_branch`` returns ``"ok"`` without a merge
    call, so the repoint keeps the recut a no-op and never arms the recut
    backoff. Idempotent: a no-change repoint writes nothing; creation-time
    base inheritance (``_maybe_inherit_base``) is suppressed for these tasks
    so nothing fights the declared pin.
    """
    payload = dependent.first_payload or {}
    release_base = payload.get("release_base_branch") or None
    if not release_base or (payload.get("base_branch") or None) == release_base:
        return
    dependent.payloads[0]["base_branch"] = release_base
    flag_modified(dependent, "payloads")
    session.add(dependent)
    await session.commit()
    logger.info(
        "[CROSS-BASE] Repointed task %s base_branch to declared "
        "release_base_branch %r at dependency release",
        dependent.id,
        release_base,
    )
