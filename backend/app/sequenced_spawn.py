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

DIRECT-SPAWN LIMITATION: the strategist ``spawn_task`` API (routers
``admin_agent_ops``) always cuts the spawned task from the project integration
branch, and nothing anywhere creates a shared/campaign branch for a strategist.
Those fields are therefore only admissible on that API when they name the
project integration branch; any other ref is rejected at admission with a 400
(the agent manifest and ``BasePayload`` documentation state the same). The
fields remain free-form on persisted payloads authored by the plan/research
spawners, which may repoint onto a branch the orchestrator actually maintains.
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


async def landing_gate_satisfied(
    dep_task: Task,
    branch: str,
    project: Project | None,
    session: Any,
    verify_pr_merged: Any,
) -> bool:
    """Gate check that can also consult VERIFIED merge truth, not just context.

    :func:`landing_gate_ok` answers from the dependency's PERSISTED context
    alone (``landed_on_branch`` bottoms out in ``has_landed_signal``, a pure
    context read). That is right whenever the signal was written -- but it
    makes a MISSING signal indistinguishable from "never landed", and nothing
    on the gate path can tell those apart. So a COMPLETED dependency whose PR
    provably merged into *branch* holds its dependent QUEUED forever.

    That is not hypothetical: the signal writer races. The acceptance-gate
    backfill sweep is a ONE-SHOT latch (it excludes rows already carrying
    ``_backfill_checked_at``), so a sweep that checks a task seconds BEFORE
    its PR merges stamps the latch, loses the race, and permanently excludes
    that task from ever being re-checked. The dependent then sits QUEUED with
    no reconciler that can reach it, and it is INVISIBLE: it is filtered out
    of ``eligible_ids`` before the orchestrator's "N queued" line is computed.

    The heuristic path below this gate already carries the equivalent
    defense-in-depth for its own COMPLETED-non-landed case -- the cross-base
    branch consults lineage and shared-staging rather than holding on a bare
    missing signal. The gate path ``continue``s above all of that, so it never
    got one. This is that missing counterpart: consult the SAME GitHub-aware
    ``verify_pr_merged`` the ungated same-base path already uses.

    FAIL-CLOSED AND BRANCH-EXACT BY CONSTRUCTION. ``verify_pr_merged`` answers
    "this PR merged", NOT "merged into *branch*" -- its boolean alone must
    never release a gate. It is used only to drive the authoritative landing
    stamp: it calls ``merged_pr_counts_as_integration_landing`` and stamps via
    ``corroborate_and_mark_landed_on_integration`` ONLY when the merge really
    puts the dep's work where the gate requires it. The original context
    predicate is then re-asked, and that re-ask is the release condition. A
    merge that landed on some other branch stamps nothing and still holds; so
    does a dep with no PR artifact at all.

    Do not "simplify" the trailing re-ask into ``return await
    verify_pr_merged(...)``: that would make the gate branch-blind and release
    a dependent onto a base its dependency never reached.
    """
    if landing_gate_ok(dep_task, branch, project):
        return True
    # Only a COMPLETED dep can be blocked on a missing signal; anything else is
    # legitimately still in flight and must keep holding without a GitHub call.
    if dep_task.status != TaskStatus.COMPLETED:
        return False
    if not await verify_pr_merged(dep_task, session):
        return False
    return landing_gate_ok(dep_task, branch, project)


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
