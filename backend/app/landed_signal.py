"""Canonical integration-landing and rollup-unblock signals.

Historically a single ``task.context["pr_merged"]`` flag did two distinct jobs:

  1. mark a PR that truly merged onto the project's *integration* branch, and
  2. mark merge-*equivalent* successes that did NOT land on the integration
     branch — staging-only rollup children (whose PR merges into the rollup
     staging branch), no-PR / no-op completions, and deliberate success-closes
     where the work landed elsewhere.

Conflating the two forced shipped / merge-rate / direction-outcome analytics to
read a flag that overcounts, and forced strategic code to reconstruct the
"really on the integration branch" answer with GitHub ancestry heuristics.

This module gives those two jobs *named, separate* entry points that every
writer funnels through, plus canonical readers for each meaning. The legacy
``pr_merged`` field remains the rollup-unblock / merge-equivalent bit, while
``landed_on_integration`` is the explicit "this work actually landed on the
integration branch" signal used by analytics.
"""

import logging
from typing import Any, Literal

from sqlalchemy import and_, cast, func, or_
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql import ColumnElement

from app.ci_resolution import is_degraded_unverified_reason
from app.database import resolve_dialect_name
from app.models import Task, TaskStatus

logger = logging.getLogger(__name__)

# The context key both meanings currently share. Centralised here so the field
# name lives in exactly one place.
LANDED_SIGNAL_KEY = "pr_merged"
LANDED_ON_INTEGRATION_KEY = "landed_on_integration"
NON_MERGE_UNBLOCK_REASON_KEY = "_non_merge_unblock_reason"

# Where the exact merge commit SHA of an integration landing is recorded. The
# discovery-review receipt (``discovery_review_packet._merge_sha``) reads this
# key first; without it a landed write task reports "lacks exact commit
# identity" and pins the direction's discovery cycle in EVIDENCE_PENDING.
LANDED_MERGE_SHA_KEY = "merge_commit_sha"

# Durable negative marker: a rollup whose merge commit was confirmed NOT to be an
# ancestor of the project's integration branch (``is_ancestor_of`` -> False). The
# rollup->integration merge never landed, so the task must NOT carry the shipped
# badge. Recorded analogously to the landed signals above so the stranded state is
# durable and visible to operators / analytics.
STRANDED_LANDING_REASON_KEY = "rollup_landing_stranded"

# These merge-equivalent markers mean a child was deliberately dropped from a
# rollup rather than carried by it. Keep the values beside the canonical
# landing signal so readers cannot accidentally credit discarded work when
# reconstructing a rollup-carried landing.
FORCE_DROP_UNBLOCK_REASONS = frozenset(
    {"rollup_no_pr_artifact", "malformed_pr_url", "review_respawn_cap"}
)

# Where the no-CI merge gate stamps WHY a PR merged without real test
# verification. Lives under ``task.context["review_loop"]``; absent on a
# verified/normal merge. The reason KEYS are minted in ``ci_resolution`` and
# classified benign-vs-degraded there (the single source of truth).
REVIEW_LOOP_KEY = "review_loop"
UNVERIFIED_MERGE_REASON_KEY = "unverified_merge_reason"

# Task-level verified-merge classification consumed by success-scoring analytics
# (task_outcomes / direction_outcomes / pipeline_health).
MergeVerificationStatus = Literal[
    "verified", "unverified_benign", "unverified_degraded"
]

# Where the durable reproduce-on-HEAD verdict is persisted for
# bug-report-/finding-derived work. Written by the agent terminal-status path
# (``routers/agent_status``) and seeded by research/bug-report ingestion. The
# canonical defect-truth signal success scoring reads to tell a *proven* fix
# from a merely-shipped one. Mirrors ``schemas.ReproduceVerdict``.
REPRODUCE_VERDICT_KEY = "reproduce_verdict"
ReproduceVerdictStatus = Literal["reproduced", "already_resolved", "indeterminate"]
_REPRODUCE_VERDICT_STATUSES = frozenset(
    ("reproduced", "already_resolved", "indeterminate")
)


def has_merge_unblock_signal(task: Task) -> bool:
    """Canonical read of the rollup-unblock / merge-equivalent signal."""
    return (task.context or {}).get(LANDED_SIGNAL_KEY) is True


def has_landed_signal_context(context: dict[str, Any] | None) -> bool:
    """Canonical landing predicate evaluated against a raw context mapping.

    Callers that hold a plain ``task.context``-shaped dict (admin task-list
    payloads, replay/diagnostic readers) must use this rather than
    re-deriving the clauses, so the three-clause rule below lives in exactly
    one place:

      1. explicit ``landed_on_integration is True`` → landed,
      2. explicit ``landed_on_integration is False`` → not landed,
      3. otherwise the legacy ``pr_merged is True`` fallback, valid only while
         the explicit non-merge marker written by
         :func:`mark_unblocked_non_merge` is ABSENT.
    """
    ctx = context or {}
    landed = ctx.get(LANDED_ON_INTEGRATION_KEY)
    if landed is True:
        return True
    if landed is False:
        return False
    return (
        ctx.get(LANDED_SIGNAL_KEY) is True and NON_MERGE_UNBLOCK_REASON_KEY not in ctx
    )


def has_landed_signal(task: Task) -> bool:
    """Canonical read of the integration-branch landing signal.

    New writes stamp ``landed_on_integration`` explicitly. Legacy rows that
    predate the split fall back to ``pr_merged is True`` unless they carry the
    explicit non-merge marker introduced by :func:`mark_unblocked_non_merge`.
    """
    return has_landed_signal_context(task.context)


async def has_landed_signal_via_rollup(task: Task, session: AsyncSession) -> bool:
    """Return whether *task* landed directly or through a landed rollup.

    A rollup child can be absent from the integration branch's ancestry after a
    squash merge: the rollup's single merge commit carries the child's content,
    while the child's own PR commit does not. The normal rollup propagation
    path stamps :func:`has_landed_signal` on those children, but this reader
    also closes the crash/legacy gap by walking the existing parent lineage.

    The fallback is deliberately conservative. The child must have a genuine
    merge-equivalent signal, every lineage edge must remain in the same
    project, and every inspected ancestor must be merge-equivalent until a
    canonical landed ancestor is found. Missing parents and force-dropped
    children remain unlanded rather than being inferred as clean.
    """
    if has_landed_signal(task):
        return True
    if not has_merge_unblock_signal(task):
        return False

    current = task
    visited: set[Any] = set()
    while current.parent_task_id is not None:
        parent_id = current.parent_task_id
        if parent_id in visited:
            return False
        visited.add(parent_id)
        parent = await session.get(Task, parent_id)
        if parent is None or parent.project_id != task.project_id:
            return False

        current_context = current.context or {}
        if current_context.get(NON_MERGE_UNBLOCK_REASON_KEY) in FORCE_DROP_UNBLOCK_REASONS:
            return False
        parent_context = parent.context or {}
        if parent_context.get(NON_MERGE_UNBLOCK_REASON_KEY) in FORCE_DROP_UNBLOCK_REASONS:
            return False
        if has_landed_signal(parent):
            return True
        if not has_merge_unblock_signal(parent):
            return False
        current = parent
    return False


def landed_on_branch(task: Task, branch: str, project: Any = None) -> bool:
    """True when *task*'s merged work landed on *branch*.

    Generalizes :func:`has_landed_signal` from "landed on the project's
    integration branch" to an arbitrary caller-named branch, for the
    ``require_deps_landed_on`` dispatch gate (sequenced campaign tasks that
    build on a prerequisite's merged state).

      * *branch* == the project's integration branch → delegate to
        :func:`has_landed_signal`, the canonical (rollup-propagation-aware)
        integration signal.
      * any other branch (e.g. a campaign/staging branch) → require the
        merge-equivalent unblock signal AND that the canonical landing-branch
        resolver reports the PR landed on *branch*. Uses the same
        :func:`branch_resolution.resolve_pr_landing_branch` every other
        ``pr_merged`` decision uses, so the two cannot drift.

    Deferred imports keep this module a leaf (branch_resolution imports it).
    """
    if not branch:
        return has_landed_signal(task)
    if project is not None:
        from app.shared_state import get_project_integration_branch

        if branch == get_project_integration_branch(project):
            return has_landed_signal(task)
    if not has_merge_unblock_signal(task):
        return False
    from app.branch_resolution import resolve_pr_landing_branch

    return resolve_pr_landing_branch(task, project) == branch


def is_merged_completion(task: Task) -> bool:
    """The canonical "shipped" predicate for analytics.

    True when *task* is COMPLETED and carries the landed signal — the
    ``status == COMPLETED and (ctx or {}).get("pr_merged")`` idiom that
    shipped / merge-rate / direction-outcome logic repeats throughout the
    codebase.
    """
    return task.status == TaskStatus.COMPLETED and has_landed_signal(task)


def _unverified_merge_reason(task: Task) -> str | None:
    """Read ``review_loop["unverified_merge_reason"]`` defensively.

    Returns the stable reason KEY a no-CI fall-open stamped, or ``None`` when the
    merge was verified (no reason) or the context shape is missing/malformed.
    """
    review_loop = (task.context or {}).get(REVIEW_LOOP_KEY)
    if not isinstance(review_loop, dict):
        return None
    reason = review_loop.get(UNVERIFIED_MERGE_REASON_KEY)
    return reason if isinstance(reason, str) and reason else None


def merge_verification_status(task: Task) -> "MergeVerificationStatus | None":
    """Classify HOW a merged completion's PR was verified, for success scoring.

    A PR merging onto the integration branch is necessary but NOT sufficient to
    call a done/fixed claim *proven*: the no-CI merge gate legitimately "falls
    open" and merges some PRs WITHOUT running real tests, recording why in
    ``review_loop["unverified_merge_reason"]``. This predicate reads that reason
    and returns one of:

      - ``"verified"`` — merged completion with NO fall-open reason: a real CI /
        test verdict gated the merge.
      - ``"unverified_benign"`` — merged via a fall-open whose reason is benign
        (the project has no runnable suite, or only positive execution signals
        were obtained). A benign project must NOT be penalized for having no tests.
      - ``"unverified_degraded"`` — merged via a fall-open whose reason is
        *degraded* (verification was expected but an environment/setup failure
        blocked it) per :func:`ci_resolution.is_degraded_unverified_reason`.

    Returns ``None`` for any task that is not a merged completion (the question
    does not apply), so callers can cheaply skip non-shipped tasks. Reuses
    :func:`is_merged_completion` so the "shipped" definition stays identical to
    the rest of analytics; an unknown / unclassified reason key defaults to
    ``"unverified_benign"`` (mirroring the WARNING severity default).
    """
    if not is_merged_completion(task):
        return None
    reason = _unverified_merge_reason(task)
    if reason is None:
        return "verified"
    if is_degraded_unverified_reason(reason):
        return "unverified_degraded"
    return "unverified_benign"


def is_verified_merge(task: Task) -> bool:
    """True iff *task* is a merged completion whose PR was really verified.

    Convenience wrapper over :func:`merge_verification_status`: only the
    ``"verified"`` status counts. Both unverified buckets and non-merged tasks
    return False, so success-scoring can gate a "proven fix" credit on this.
    """
    return merge_verification_status(task) == "verified"


def reproduce_verdict_status(task: Task) -> "ReproduceVerdictStatus | None":
    """Read the canonical reproduce-on-HEAD verdict status off *task*, or None.

    Returns the persisted ``task.context["reproduce_verdict"]["status"]`` when it
    is one of the canonical values (``reproduced`` / ``already_resolved`` /
    ``indeterminate``), else ``None``. ``None`` deliberately covers three cases
    that must all fall back to merge-based scoring: a task that predates the
    verdict (historical), a task that is not bug-report-/finding-derived (no
    defect claim to prove), and any malformed/partial context shape. Mirrors the
    defensive nested-read of :func:`_unverified_merge_reason`.
    """
    verdict = (task.context or {}).get(REPRODUCE_VERDICT_KEY)
    if not isinstance(verdict, dict):
        return None
    status = verdict.get("status")
    return status if status in _REPRODUCE_VERDICT_STATUSES else None


def suppresses_redispatch(task: Task) -> bool:
    """True when a non-shipping completion affirmatively determined its defect is
    already resolved / non-reproducing on HEAD, so its source finding must NOT be
    re-dispatched.

    The canonical signal is a reproduce verdict of ``already_resolved``. The
    legacy ``no_op_reason`` marker (stamped by ``./done --already-done`` before
    the verdict existed) is honored as a fallback so historical no-op completions
    keep suppressing re-dispatch. An ``indeterminate`` verdict is deliberately
    NOT suppressed — the defect could not be ruled out, so the finding stays
    eligible for retry.
    """
    if reproduce_verdict_status(task) == "already_resolved":
        return True
    return bool((task.context or {}).get("no_op_reason"))


def landed_signal_predicate(session: AsyncSession) -> ColumnElement[bool]:
    """SQL predicate for rows that truly landed on the integration branch.

    Mirrors :func:`has_landed_signal`, including the legacy fallback for rows
    that predate ``landed_on_integration`` and the explicit opt-out marker for
    merge-equivalent non-landings.
    """
    # Async sessions created from a sessionmaker can expose a None ``bind``
    # attribute even though the session still resolves an engine. Routing through
    # the centralised resolver keeps the None-bind-safe resolution (``.bind`` →
    # ``get_bind()`` → ``"postgresql"`` default) in exactly one place so the
    # SQLite branch is chosen correctly on a SQLite-bound session.
    dialect_name = resolve_dialect_name(session)
    if dialect_name == "sqlite":
        landed = func.json_extract(Task.context, f"$.{LANDED_ON_INTEGRATION_KEY}")
        pr_merged = func.json_extract(Task.context, f"$.{LANDED_SIGNAL_KEY}")
        non_merge_reason = func.json_extract(
            Task.context, f"$.{NON_MERGE_UNBLOCK_REASON_KEY}"
        )
        return or_(
            landed == True,  # noqa: E712
            and_(
                landed.is_(None),
                pr_merged == True,  # noqa: E712
                non_merge_reason.is_(None),
            ),
        )

    landed_true = Task.context.op("@>")(cast({LANDED_ON_INTEGRATION_KEY: True}, JSONB))
    legacy_pr_merged = Task.context.op("@>")(cast({LANDED_SIGNAL_KEY: True}, JSONB))
    return or_(
        landed_true,
        and_(
            legacy_pr_merged,
            # ``has_key`` here is SQLAlchemy's JSONB ``?`` operator, not the
            # removed dict method.
            ~Task.context.has_key(LANDED_ON_INTEGRATION_KEY),
            ~Task.context.has_key(NON_MERGE_UNBLOCK_REASON_KEY),
        ),
    )


def mark_landed_on_integration(
    task: Task, project: Any = None, *, merge_commit_sha: str | None = None
) -> None:
    """Stamp the landed signal for a PR that merged onto the integration branch.

    This is the canonical write for a *real* integration-branch merge. Callers
    that need the integration-vs-promotion-target guard should use
    :func:`apply_merged_landed_signal` instead, which routes through here.

    When *merge_commit_sha* is provided, the exact merge commit is recorded under
    ``LANDED_MERGE_SHA_KEY`` (truncated to 64 chars, matching the discovery
    receipt reader's bound). This gives the discovery-review packet the commit
    identity it needs to close the "lacks exact commit identity" evidence gap
    without an extra GitHub round-trip.

    Also records the code-task acceptance verdict when the task declared
    acceptance evidence, so every integration-landing path (rollup propagation,
    PR verification, backfill) produces a verdict — not only the
    ``apply_merged_landed_signal`` path. Fail-safe: a verdict-recording failure
    is logged and swallowed so it can never undo a real merge landing. Lazy
    import avoids the module-load cycle (``acceptance_gate`` imports this module
    for its constants).

    When ``project`` is supplied, also mints the capability proof for any
    capability the task advertises with a proven acceptance verdict, so the SAME
    "every integration-landing path" guarantee that already covers the verdict
    now covers the capability proof too. Previously the proof was recorded only
    on the ``apply_merged_landed_signal`` path, so direct landing paths (rollup
    child propagation, PR verification, recovery strands) stamped the verdict but
    never minted the proof — leaving the provider's dependents blocked under the
    enforce gate until an out-of-band sweep re-recorded it. Fail-safe: a
    proof-recording failure is logged and swallowed so it can never undo a real
    merge landing. ``project`` defaults to ``None`` (proof skipped) so legacy
    task-only callers keep their exact current behavior.
    """
    had_non_merge_unblock = NON_MERGE_UNBLOCK_REASON_KEY in (task.context or {})
    ctx = {
        **(task.context or {}),
        LANDED_SIGNAL_KEY: True,
        LANDED_ON_INTEGRATION_KEY: True,
    }
    ctx.pop(NON_MERGE_UNBLOCK_REASON_KEY, None)
    if merge_commit_sha:
        ctx[LANDED_MERGE_SHA_KEY] = str(merge_commit_sha)[:64]
    # When a staging-merged child is promoted to integration-landed, clear the
    # stale unverified_merge_reason the staging CI monitor stamped.  Staging
    # branches genuinely lack CI, but the task now reaches integration through
    # the rollup parent, whose CI serves as the real verification gate.  Only
    # clear when _non_merge_unblock_reason was present (the task was previously
    # staging-merged), so direct integration merges with legitimate unverified
    # reasons are unaffected.
    if had_non_merge_unblock:
        review_loop = ctx.get(REVIEW_LOOP_KEY)
        if isinstance(review_loop, dict):
            review_loop.pop(UNVERIFIED_MERGE_REASON_KEY, None)
    task.context = ctx
    try:
        from app.acceptance_gate import record_code_acceptance_verdict

        record_code_acceptance_verdict(task)
    except Exception:  # noqa: BLE001 — verdict recording must never undo a real merge
        logger.warning(
            "[LANDED] code-acceptance verdict recording failed for task %s",
            getattr(task, "id", None),
            exc_info=True,
        )
    if project is not None:
        try:
            from app.capability_proof import record_capability_proof

            record_capability_proof(task, project)
        except Exception:  # noqa: BLE001 — proof recording must never undo a real merge
            logger.warning(
                "[LANDED] capability proof recording failed for task %s",
                getattr(task, "id", None),
                exc_info=True,
            )


def mark_unblocked_non_merge(task: Task, *, reason: str) -> None:
    """Stamp the unblock marker for a merge-*equivalent* success.

    Used for staging-only rollup children, no-PR / no-op completions, and
    deliberate success-closes where the work landed elsewhere. These never
    landed on the integration branch but must still unblock dependents and
    rollup convergence, so they keep ``pr_merged=True`` while explicitly
    recording ``landed_on_integration=False``.
    """
    logger.debug(
        "[LANDED] non-merge unblock for task %s: %s",
        getattr(task, "id", None),
        reason,
    )
    task.context = {
        **(task.context or {}),
        LANDED_SIGNAL_KEY: True,
        LANDED_ON_INTEGRATION_KEY: False,
        NON_MERGE_UNBLOCK_REASON_KEY: reason,
    }


def mark_stranded_non_landing(task: Task, *, reason: str) -> None:
    """Stamp the durable negative marker for a rollup that did NOT land.

    Used when an ancestry check definitively proves the rollup's merge commit is
    not reachable from the integration branch (``is_ancestor_of`` -> False): the
    rollup->integration merge never completed, so the work is stranded on a
    staging branch. Records ``landed_on_integration=False`` and the stranded
    reason, and clears any stale ``pr_merged`` unblock bit so confirmed
    non-landings never act as merge-equivalent success. Shipped / merge-rate
    analytics never count it and the rollup does not converge on a false landing.

    Idempotent: re-stamping the same definitive negative only re-writes the same
    two keys and re-clears the unblock bit. Callers gate on a DEFINITIVE negative
    before calling this, so a previously-recorded true landing (whose ancestry
    check returns True) is never routed here and cannot be flipped.
    """
    logger.warning(
        "[LANDED] stranded non-landing for task %s: %s",
        getattr(task, "id", None),
        reason,
    )
    ctx = {
        **(task.context or {}),
        LANDED_ON_INTEGRATION_KEY: False,
        STRANDED_LANDING_REASON_KEY: reason,
    }
    ctx.pop(LANDED_SIGNAL_KEY, None)
    task.context = ctx


def apply_merged_landed_signal(
    task: Task,
    project,
    *,
    base_branch: str,
    hotfix_lane: bool,
    non_merge_reason: str | None = None,
    merge_commit_sha: str | None = None,
) -> bool:
    """Guarded write path for a merged PR.

    Stamps the landed signal only when the merged PR landed on the project's
    integration branch. When the base branch resolves to a distinct promotion
    target, the signal is withheld and the same branch-safety alert the rollup
    boundary raises is staged.

    Returns True iff the merged PR landed on the integration branch.

    When *non_merge_reason* is provided, merged PRs that landed somewhere other
    than the integration branch are still marked as merge-equivalent rollup
    unblocks via :func:`mark_unblocked_non_merge`.

    *merge_commit_sha* is forwarded to :func:`mark_landed_on_integration` on a
    genuine integration landing so the exact commit identity is recorded
    alongside the landed flags.
    """
    from app.branch_resolution import _signal_blocking_project_branch_configuration
    from app.shared_state import (
        get_project_integration_branch,
        get_project_promotion_target,
    )

    integration_branch = get_project_integration_branch(project)
    promotion_target = get_project_promotion_target(project)
    landed_on_integration = bool(
        integration_branch and base_branch == integration_branch
    )
    if (
        promotion_target
        and base_branch == promotion_target
        and base_branch != integration_branch
        and not hotfix_lane
        and project is not None
    ):
        _signal_blocking_project_branch_configuration(
            project,
            "rollup_target_matches_promotion_target",
            (
                f"Rollup blocked: target branch '{base_branch}' is this "
                "project's promotion target. Configure a distinct "
                "integration branch before landing fleet work."
            ),
        )
    if landed_on_integration:
        # record_code_acceptance_verdict AND record_capability_proof are both
        # called inside mark_landed_on_integration (project supplied here), so
        # every landing path records the verdict and mints the capability proof.
        mark_landed_on_integration(task, project, merge_commit_sha=merge_commit_sha)
        try:
            from app.capability_proof import record_gap_evidence

            record_gap_evidence(task, project)
        except Exception:  # noqa: BLE001 — gap evidence stamp must never undo a real merge
            logger.warning(
                "[LANDED] gap evidence recording failed for task %s",
                getattr(task, "id", None),
                exc_info=True,
            )
        # Onboarding-proof: record the first PR that landed on the project's
        # integration branch. This is the central merge-landed choke point that
        # already records the capability proof, so it mirrors that pattern
        # exactly — no session parameter, the caller's session already tracks
        # ``project``. Best-effort and idempotent; never undoes a real merge.
        try:
            from app.onboarding_proof import stamp_first_pr_merged

            stamp_first_pr_merged(project)
        except Exception:  # noqa: BLE001 — observability stamp must never undo a real merge
            logger.warning(
                "[LANDED] onboarding first-pr-merged stamp failed for task %s",
                getattr(task, "id", None),
                exc_info=True,
            )
        # Onboarding-proof: record the first autonomous code improvement
        # (create/fix/refactor) that landed on an external (non-self-repo)
        # project. Makes the g-generality direction retirement condition
        # machine-checkable. Same pattern: best-effort, idempotent.
        try:
            from app.onboarding_proof import stamp_first_autonomous_improvement

            stamp_first_autonomous_improvement(task, project)
        except Exception:  # noqa: BLE001 — stamp must never undo a real merge
            logger.warning(
                "[LANDED] first-autonomous-improvement stamp failed for task %s",
                getattr(task, "id", None),
                exc_info=True,
            )
    elif non_merge_reason:
        mark_unblocked_non_merge(task, reason=non_merge_reason)
    return landed_on_integration


def _pinned_corroboration_sha(merge_commit_sha: Any) -> str | None:
    """Normalize a caller-supplied merge sha for a PINNED corroboration read.

    Landing sites forward the sha straight off a GitHub payload, where it can be
    absent or ``None`` (and, for a squash/rebase merge not yet finalized, an
    empty string). Only a non-empty string can pin
    :func:`app.github_repo_snapshot.file_text_at_commit` to an immutable tree;
    anything else must degrade to ``None`` so corroboration is not silently
    handed a junk ref.
    """
    if isinstance(merge_commit_sha, str) and merge_commit_sha:
        return merge_commit_sha[:64]
    return None


async def _stamp_changed_files_before_landing(
    task: Task,
    session: AsyncSession,
    project: Any,
    *,
    base_branch: str,
    pr_number: int | str | None = None,
    github_credentials: tuple[str, str, str] | None = None,
) -> None:
    """Acquire authoritative PR provenance immediately before land-time scoring."""
    if project is None:
        return
    from app.shared_state import get_project_integration_branch

    if base_branch != get_project_integration_branch(project):
        return
    from app.acceptance_gate import stamp_pr_changed_files_at_landing

    await stamp_pr_changed_files_at_landing(
        task,
        session,
        pr_number=pr_number,
        github_credentials=github_credentials,
    )


async def _verify_finding_clearance_before_landing(
    task: Task,
    session: AsyncSession,
    *,
    merge_commit_sha: str | None,
    github_credentials: tuple[str, str, str] | None = None,
) -> None:
    """Stamp current-tree proof for a merged finding-clearing claim.

    Runs on the same choke point as changed-file provenance and acceptance
    corroboration so every async merged-landing lane produces the marker
    ``finding_clearance_blocks_success`` reads. Best-effort: a failure leaves the
    claim unproven (no credit) and never aborts the landing.
    """
    from app.acceptance_gate import verify_finding_clearance_at_landing

    try:
        await verify_finding_clearance_at_landing(
            task,
            session,
            merge_commit_sha=_pinned_corroboration_sha(merge_commit_sha),
            github_credentials=github_credentials,
        )
    except Exception:  # noqa: BLE001 — proof recording must never undo a merge
        logger.warning(
            "[LANDED] finding-clearance verification failed for task %s",
            getattr(task, "id", None),
            exc_info=True,
        )


async def corroborate_and_mark_landed_on_integration(
    task: Task,
    session: AsyncSession,
    project: Any = None,
    *,
    branch: str | None = None,
    merge_commit_sha: str | None = None,
    pr_number: int | str | None = None,
    github_credentials: tuple[str, str, str] | None = None,
) -> None:
    """Async twin of :func:`mark_landed_on_integration` with backend corroboration.

    Runs the backend's own check that the task's declared acceptance test exists
    in the merged tree (:func:`app.acceptance_gate.corroborate_acceptance_for_landing`,
    pinning the read to *merge_commit_sha* when known) BEFORE stamping the landed
    signal and recording the code-acceptance verdict, so a not-in-diff declared
    test that genuinely exists at the merge commit mints machine-proven credit
    instead of soft/unproven. This is the fix for the non-CI-monitor landing
    paths (rollup PR verification, recovery strands, propagation) that recorded
    verdicts without ever stamping the corroboration marker.

    Corroboration is best-effort — never raises, never aborts the landing. The
    caller is responsible for committing. Sync callers keep using the sync
    :func:`mark_landed_on_integration` unchanged.

    PASS *merge_commit_sha* WHENEVER THE CALLER KNOWS IT. It pins the tree read
    to the immutable merge commit; without it the read falls back to the
    branch-tip cache, whose 60s TTL can serve the PRE-merge tip and would
    otherwise stamp a durable "test absent". Corroboration refuses to stamp an
    unpinned absence for exactly that reason, so an omitted sha silently costs
    the landing its machine-proven credit.
    """
    await _stamp_changed_files_before_landing(
        task,
        session,
        project,
        base_branch=branch or "",
        pr_number=pr_number,
        github_credentials=github_credentials,
    )
    from app.acceptance_gate import corroborate_acceptance_for_landing

    await corroborate_acceptance_for_landing(
        task,
        session,
        project=project,
        branch=branch,
        commit_sha=_pinned_corroboration_sha(merge_commit_sha),
    )
    await _verify_finding_clearance_before_landing(
        task,
        session,
        merge_commit_sha=merge_commit_sha,
        github_credentials=github_credentials,
    )
    mark_landed_on_integration(task, project, merge_commit_sha=merge_commit_sha)


async def corroborate_and_apply_merged_landed_signal(
    task: Task,
    project,
    session: AsyncSession,
    *,
    base_branch: str,
    hotfix_lane: bool,
    non_merge_reason: str | None = None,
    merge_commit_sha: str | None = None,
    pr_number: int | str | None = None,
    github_credentials: tuple[str, str, str] | None = None,
) -> bool:
    """Async twin of :func:`apply_merged_landed_signal` with backend corroboration.

    Same guarded write contract as the sync function — returns True iff the
    merged PR landed on the project's integration branch — but corroborates the
    declared acceptance test at the merge commit BEFORE routing through the sync
    write, so the landing mints machine-proven credit for a not-in-diff declared
    test exactly like the CI monitor's recheck branch. Corroboration is
    best-effort and never aborts the landing.

    This is the write EVERY async merged-landing flow calls: CI completion
    (``ci_completion``), task lifecycle auto-merge and review-merge, rollup
    merge/repoll/escalation (``rollup_management``), recovery
    (``recovery_evaluating_helpers`` / ``_reaped_owner``), request-changes
    cleanup, and the GitHub-webhook / agent-status / operator-trigger /
    admin-approval routers. The one remaining sync
    :func:`apply_merged_landed_signal` caller is
    ``orchestrator_ci_monitor._apply_ci_merged_landed_signal``, whose own call
    site already awaits
    :func:`app.acceptance_gate.corroborate_acceptance_for_landing` immediately
    before the write — routing it through here too would only duplicate that
    GitHub read.

    ``project`` is REQUIRED for corroboration to do anything: the tree read
    resolves the repo's GitHub credentials from it, so a ``None`` project
    degrades corroboration to indeterminate (marker left absent) while the
    landed-signal write itself behaves exactly as the sync function does.

    PASS *merge_commit_sha* WHENEVER THE CALLER KNOWS IT — it pins the tree read
    to the immutable merge commit instead of the 60s-TTL branch-tip cache, which
    right after a merge can still serve the PRE-merge tip. Corroboration refuses
    to stamp an unpinned absence, so an omitted sha costs the landing its
    machine-proven credit rather than recording a false one.
    """
    await _stamp_changed_files_before_landing(
        task,
        session,
        project,
        base_branch=base_branch,
        pr_number=pr_number,
        github_credentials=github_credentials,
    )
    from app.acceptance_gate import corroborate_acceptance_for_landing

    await corroborate_acceptance_for_landing(
        task,
        session,
        project=project,
        branch=base_branch,
        commit_sha=_pinned_corroboration_sha(merge_commit_sha),
    )
    await _verify_finding_clearance_before_landing(
        task,
        session,
        merge_commit_sha=merge_commit_sha,
        github_credentials=github_credentials,
    )
    return apply_merged_landed_signal(
        task,
        project,
        base_branch=base_branch,
        hotfix_lane=hotfix_lane,
        non_merge_reason=non_merge_reason,
        merge_commit_sha=merge_commit_sha,
    )
