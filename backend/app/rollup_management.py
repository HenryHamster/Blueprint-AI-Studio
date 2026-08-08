"""Rollup PR management and branch cleanup.

Extracted from task_lifecycle.py to reduce its size.  Contains the three
rollup-related helpers: _verify_pr_merged, _cleanup_branches, and
_check_and_trigger_rollup.

All deferred imports from ``app.main`` are intentional — they avoid circular
imports (rollup_management is imported BY main indirectly via task_lifecycle)
and preserve test-patchability.
"""

# fmt: off
import contextlib
import logging
import time
from datetime import timedelta

from fastapi import HTTPException
from sqlalchemy import func as _func
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from app.database import AsyncSessionLocal, resolve_dialect_name
from app.rollup_locking import (  # noqa: F401 — re-exported for callers/tests
    _ParentMutatedDuringPRCreation,
    _capture_rollup_parent_snapshot,
    _release_parent_lock,
    _relock_parent,
)
from app.models import (
    Task,
    Artifact,
    Project,
    TaskStatus,
    ArtifactType,
)
from app.review_pipeline import (
    _spawn_pipeline_task,
    _is_pipeline_task_stale,
    _load_pipeline_liveness_lookups,
    _task_is_actively_ci_waiting,
)
from app.review_policy_merge import attempt_policy_bound_merge
from app.rollup_branch_cleanup import (  # noqa: F401 — re-exported for callers/tests
    _branch_task_id_prefix,
    _classify_prs_authoritative,
    _cleanup_branches,
    _extract_task_pr_numbers,
    _is_owner_chain_live,
    _partition_orphan_prs,
)
from app.rollup_migration_validation import (  # noqa: F401 - re-exported for callers/tests
    _extract_rollup_migration_sources,
    _plan_rollup_migration_reconciliation,
    _touches_alembic_versions,
    _validate_rollup_migration_chain,
)
from app.rollup_pr_verification import (  # noqa: F401 — re-exported for callers/tests
    MAX_REVIEW_RESPAWNS,
    _PR_MERGE_NEGATIVE_TTL_S,
    _pr_merge_negative_cache,
    _record_rollup_failure_signals,
    _review_respawn_count,
    _verify_pr_merged,
    verify_rollup_merge_landed,
)
from app.rollup_state import (  # noqa: F401 — re-exported for legacy callers/tests
    _ALEMBIC_AUTORECONCILE_INFLIGHT_AT_KEY,
    _ALEMBIC_AUTORECONCILE_INFLIGHT_TTL_S,
    _MERGE_QUEUE_UPDATE_AT_KEY,
    _ROLLUP_TRANSIENT_CTX_KEYS,
    _SCAN_REPORT_POSTED_PR_KEY,
    _alembic_autoreconcile_in_flight,
    _clear_rollup_transient_state,
    _is_rollup_parent_awaiting_merge,
    _merge_queue_update_in_flight,
)
from app.branch_resolution import (
    InvalidProjectBranchConfiguration,
    _signal_blocking_project_branch_configuration,
    is_custom_staged_rollup_root,
    merged_pr_counts_as_integration_landing,
)
from app.landed_signal import (
    LANDED_MERGE_SHA_KEY,
    corroborate_and_apply_merged_landed_signal,
    mark_unblocked_non_merge,
)
from app.shared_state import (
    runtime_config as _runtime_config,
    get_project_integration_branch as _get_project_integration_branch,
    get_project_promotion_target as _get_project_promotion_target,
    get_project_pr_title_prefix as _get_project_pr_title_prefix,
    get_project_task_branch_namespace as _get_project_task_branch_namespace,
)
from app.task_locking import with_directions_locked
from app.ci_merge_helpers import effective_pr_number

logger = logging.getLogger(__name__)

# Duplicate-create guard: when a rollup parent is about to POST its rollup PR it
# stakes a ``rollup_pr_creating_at`` claim (epoch seconds) in parent.context
# UNDER the parent lock, BEFORE releasing it around the slow create_pull_request
# call. A concurrent _check_and_trigger_rollup invocation (sibling children
# completing together, reaper + completion event both firing) that observes a
# FRESH claim and no ``rollup_pr_number`` marker defers instead of POSTing a
# second PR — GitHub enforces one-open-PR-per-(head, base) only best-effort, so
# two near-simultaneous POSTs can both return 201 (incident #7350/#7351). A
# claim older than this TTL is treated as stale (peer crashed mid-create) and
# ignored so creation is never wedged. Paired with the idempotent pre-check in
# github_pulls.create_pull_request, which catches any duplicate that still slips
# through GitHub's non-atomic enforcement.
_ROLLUP_PR_CREATE_CLAIM_TTL_S = 300

# Bounded auto-reconcile attempts per rollup PR for a pure mechanical Alembic
# revision collision. Two attempts is enough to absorb a transient
# push/re-validate race; beyond that we fall back to the operator-alert / agent
# fix path rather than loop on a non-mechanical block.
_ALEMBIC_AUTORECONCILE_CAP = 2

# Bounded auto-bump attempts per rollup PR for assembled-tree module size
# breaches. Two bumps covers the rare case where CI re-runs before the bump
# commit propagates; beyond that we require manual approval rather than loop.
_SIZE_CAP_AUTOBUMP_CAP = 2


async def _attempt_alembic_autoreconcile(
    gh_token: str,
    owner: str,
    repo: str,
    rollup_pr_number: int,
    staging_branch: str,
    target_branch: str,
    *,
    promotion_target: str | None = None,
    project_id: str | None = None,
) -> dict | None:
    """Try to auto-renumber a rollup migration that collides with the target head.

    Returns the reconciliation ``plan`` dict when a reconciliation commit was
    PUSHED to *staging_branch* (the caller defers one sweep so re-validation can
    pass and the rollup can merge with no operator step). Returns ``None`` — with
    NO side effect on the repository — for every non-mechanical case, missing
    literal, or push failure, so the caller falls through to the existing
    operator-alert / agent-fix path. Conservative and fail-safe: only the
    unambiguous single-added-migration collision / stale-anchor is auto-remediated.

    K6 LOCK-WINDOW CONTRACT (2026-07-12 concurrency audit): this helper is PURE
    GitHub I/O — it takes NO ``session`` and NO ``Task``, touches no ORM state,
    and issues no DB query. That is load-bearing, not incidental. It performs a
    plan (PR file fetches), a file read, and a multi-request PUSH (GET ref → GET
    commit → POST blob xN → POST tree → POST commit → PATCH ref) that can run for
    minutes under GitHub rate-limit backoff. It MUST therefore be called from an
    UNLOCKED window: with the rollup parent's ``FOR UPDATE`` released AND no open
    transaction. It previously ran with the parent row locked and the transaction
    open, which (a) starved sibling-child heartbeats on the parent row and (b)
    left the connection idle-in-transaction, racing Postgres'
    ``idle_in_transaction_session_timeout`` (60s — database.py) with a call that
    has been observed to take ~285s.

    The caller owns ALL parent mutation, including STAKING the retry counter
    BEFORE this call (see ``_check_and_trigger_rollup``): the push is a real,
    irreversible side effect, so the attempt must be recorded durably before it
    is made. Any DB access added here would silently re-open a transaction across
    the push and reintroduce the defect — pass plain values instead.
    """
    from app.migration_chain import rewrite_expected_schema_revision
    from app.github import get_file_text_at_ref, commit_file_changes

    try:
        plan = await _plan_rollup_migration_reconciliation(
            gh_token,
            owner,
            repo,
            rollup_pr_number,
            target_branch,
            promotion_target=promotion_target,
            project_id=project_id,
        )
    except Exception as e:
        logger.warning(
            f"[ROLLUP] PR #{rollup_pr_number} alembic auto-reconcile planning "
            f"failed ({type(e).__name__}: {e}); falling back to fix path"
        )
        return None
    if not plan:
        return None

    # The migration becomes the new head, so EXPECTED_SCHEMA_REVISION must bump
    # to match. Read main.py at the staging-branch tip and rewrite the literal;
    # if it is absent we must not guess — fall back.
    try:
        main_text = await get_file_text_at_ref(
            "backend/app/main.py",
            token=gh_token,
            owner=owner,
            repo=repo,
            ref=staging_branch,
            project_id=project_id,
        )
    except Exception as e:
        logger.warning(
            f"[ROLLUP] PR #{rollup_pr_number} alembic auto-reconcile could not "
            f"fetch main.py ({type(e).__name__}: {e}); falling back to fix path"
        )
        return None
    if not main_text:
        return None
    new_main_text, replaced = rewrite_expected_schema_revision(
        main_text, plan["expected_schema_revision"]
    )
    if replaced == 0:
        logger.warning(
            f"[ROLLUP] PR #{rollup_pr_number} alembic auto-reconcile: no "
            f"EXPECTED_SCHEMA_REVISION literal in main.py; falling back to fix path"
        )
        return None

    # The plan renumbers the colliding migration AND its entire linear
    # descendant tail (one entry per renamed file in ``upserts``/``deletes``);
    # merge the rewritten main.py into the upserts. Renumber always changes the
    # NNNN prefix, so every old path is deleted so the rename is clean (one head,
    # no duplicate revision).
    upserts = {**plan["upserts"], "backend/app/main.py": new_main_text}
    deletes = list(plan["deletes"])
    try:
        await commit_file_changes(
            gh_token,
            owner,
            repo,
            staging_branch,
            upserts=upserts,
            deletes=deletes,
            message=(
                f"fix(alembic): auto-renumber rollup migration "
                f"{plan['old_revision']}->{plan['new_revision']} and rechain onto "
                f"{plan['new_down_revision']}"
            ),
            project_id=project_id,
        )
    except Exception as e:
        logger.warning(
            f"[ROLLUP] PR #{rollup_pr_number} alembic auto-reconcile push failed "
            f"({type(e).__name__}: {e}); falling back to fix path"
        )
        return None

    logger.info(
        f"[ROLLUP] PR #{rollup_pr_number} alembic collision auto-reconciled: "
        f"renumbered {plan['old_revision']}->{plan['new_revision']} on "
        f"{staging_branch}, bumped EXPECTED_SCHEMA_REVISION; deferring re-validation"
    )
    return plan


async def attempt_pr_migration_finalization(
    gh_token: str,
    owner: str,
    repo: str,
    *,
    pr_number: int,
    pr_branch: str,
    target_branch: str,
    promotion_target: str | None = None,
    project_id: str | None = None,
) -> dict | None:
    """Rewrite a PR's provisional migration chain onto the live target head.

    Returns a compact finalization record when a mechanical rewrite commit was
    pushed to ``pr_branch`` and ``None`` when the PR either needs no rewrite or
    cannot be reconciled safely. Fail-safe: any GitHub fetch/push failure yields
    ``None`` so callers keep the merge held rather than guessing.

    Lives here (not in ``rollup_migration_validation``) because it PUSHES: the
    planner module is held read-only by ``test_backmerge_migration_planner_is_
    read_only`` so the dual-head back-merge planner can never write a branch. The
    only legitimate inline migration pushes are the rollup staging-branch
    reconcile (``_attempt_alembic_autoreconcile`` above) and this PR-branch
    finalize — both intentionally co-located in this module.
    """
    from app.github import get_file_text_at_ref, commit_file_changes
    from app.migration_chain import rewrite_expected_schema_revision

    plan = await _plan_rollup_migration_reconciliation(
        gh_token,
        owner,
        repo,
        pr_number,
        target_branch,
        promotion_target=promotion_target,
        project_id=project_id,
    )
    if not plan:
        return None

    main_text = await get_file_text_at_ref(
        "backend/app/main.py",
        token=gh_token,
        owner=owner,
        repo=repo,
        ref=pr_branch,
        project_id=project_id,
    )
    if not main_text:
        return None
    new_main_text, replaced = rewrite_expected_schema_revision(
        main_text, plan["expected_schema_revision"]
    )
    if replaced == 0:
        return None

    upserts = dict(plan["upserts"])
    if new_main_text != main_text:
        upserts["backend/app/main.py"] = new_main_text
    if not upserts and not plan["deletes"]:
        return None

    commit_kind = (
        "rechain"
        if plan["old_revision"] == plan["new_revision"]
        else f"renumber {plan['old_revision']}->{plan['new_revision']}"
    )
    await commit_file_changes(
        gh_token,
        owner,
        repo,
        pr_branch,
        upserts=upserts,
        deletes=list(plan["deletes"]),
        message=(
            "fix(alembic): auto-finalize provisional migration chain "
            f"({commit_kind}, down_revision->{plan['new_down_revision']})"
        ),
        project_id=project_id,
    )
    return {
        "pr_number": pr_number,
        "branch": pr_branch,
        "old_revision": plan["old_revision"],
        "finalized_revision": plan["new_revision"],
        "finalized_down_revision": plan["new_down_revision"],
        "expected_schema_revision": plan["expected_schema_revision"],
    }


async def _classify_merged_rollup_landing(
    parent: Task,
    project: Project | None,
    session: AsyncSession,
    *,
    pr_number: int | None,
    target_branch: str,
    source: str,
) -> tuple[bool, bool]:
    """Return ``(integration_landing, terminal_merge)``.

    Terminal rollup merges are those that land on either the integration branch
    or the promotion target. Merges into intermediate staging branches must keep
    the parent EVALUATING until a later downstream landing completes the chain.
    """
    integration_branch = _get_project_integration_branch(project)
    promotion_target = _get_project_promotion_target(project)
    landed = target_branch == integration_branch
    promotion_landing = target_branch == promotion_target
    if not landed and promotion_landing:
        await merged_pr_counts_as_integration_landing(
            parent,
            project,
            session,
            pr_number=pr_number,
            merged_base_branch=target_branch,
            source=source,
        )
    if landed:
        # Branch-NAME equality above is necessary but NOT sufficient: confirm the
        # rollup PR's merge commit actually reached the integration branch before
        # the caller stamps the shipped badge. A DEFINITIVE negative strands the
        # task (marker + operator alert recorded by the gate); indeterminate /
        # missing-SHA / API-error preserves flag-trust so a transient outage
        # never blocks completion.
        verdict = await verify_rollup_merge_landed(
            parent,
            project,
            session,
            pr_number=pr_number,
            integration_branch=integration_branch,
            source=source,
        )
        if verdict == "stranded":
            landed = False
    return landed, landed or promotion_landing


def _promotion_target_rollup_allowed(parent: Task, target_branch: str) -> bool:
    """Allow the intentionally-gated hotfix lane to target promotion directly."""
    if not target_branch:
        return False
    payload = parent.first_payload or {}
    ctx = parent.context or {}
    return bool(payload.get("hotfix") or ctx.get("hotfix"))


def _task_owns_staging_branch(task: Task, staging_branch: str | None) -> bool:
    """Whether ``task`` may roll ``staging_branch`` up to the base.

    Research/plan staging branches are minted ``{prefix}{task.id[:8]}``, so the
    branch's trailing path segment is the OWNER's 8-hex id-prefix. Returns
    ``False`` only on positive proof of foreign ownership — the trailing segment
    is a valid id-prefix that is not this task's — i.e. the branch was inherited
    from an ancestor. Any other name (no id-suffix / legacy) is treated as owned,
    so we defer on evidence, never on ambiguity.
    """
    if not staging_branch:
        return False
    tail = staging_branch.rsplit("/", 1)[-1]
    looks_like_id_prefix = len(tail) == 8 and all(
        c in "0123456789abcdef" for c in tail
    )
    if not looks_like_id_prefix:
        return True
    return tail == str(task.id).replace("-", "")[:8]


_FORCE_DROP_REASONS = frozenset(
    {"rollup_no_pr_artifact", "malformed_pr_url", "review_respawn_cap"}
)


async def _propagate_landed_to_rollup_children(
    completed_children: list[Task],
    session: AsyncSession,
    *,
    merge_commit_sha: str | None = None,
) -> None:
    """Stamp landed_on_integration on children carried by a landed rollup.

    When a rollup PR merges onto the integration branch every COMPLETED child
    that merged into the rollup's staging branch (pr_merged=True but
    landed_on_integration still False or absent) must also receive the landed
    signal so cross-base dependents that wait on has_landed_signal(child) can
    unblock.  Children that already carry a positive landed signal are skipped
    (idempotent).

    Force-dropped children (rollup_no_pr_artifact, malformed_pr_url,
    review_respawn_cap) carry pr_merged=True but were never genuinely merged;
    they must retain landed_on_integration=False so they are not falsely
    credited as landed.

    *merge_commit_sha* is the landed parent's merge commit, forwarded to each
    newly-landed child so rollup children (which land via the parent's single
    PR with no PR fetch of their own) carry exact commit identity for the
    discovery-review receipt.

    Unlike the sync predecessor, this landing path corroborates each child's
    declared acceptance test at the merge commit BEFORE recording the verdict,
    so a child with its OWN (GitHub-verified) changed files whose declared test
    is absent from the diff mints machine-proven credit instead of soft.
    """
    from app.landed_signal import (
        NON_MERGE_UNBLOCK_REASON_KEY,
        corroborate_and_mark_landed_on_integration,
        has_landed_signal,
        has_merge_unblock_signal,
    )

    _project = (
        await session.get(Project, completed_children[0].project_id)
        if completed_children and completed_children[0].project_id is not None
        else None
    )
    for child in completed_children:
        ctx = child.context or {}
        if ctx.get(NON_MERGE_UNBLOCK_REASON_KEY) in _FORCE_DROP_REASONS:
            continue
        if has_merge_unblock_signal(child) and not has_landed_signal(child):
            await corroborate_and_mark_landed_on_integration(
                child,
                session,
                _project,
                merge_commit_sha=merge_commit_sha,
            )
            session.add(child)


async def _propagate_landed_to_staging_merged_descendants(
    parent_task_id, session: AsyncSession
) -> list[Task]:
    """Recursively stamp landed_on_integration on all staging-merged descendants.

    ``_propagate_landed_to_rollup_children`` only touches the direct children
    present in ``completed_children``.  When a lineage contains nested
    research/rollup sub-tasks, code tasks at deeper levels (grandchildren of
    the top-level rollup, etc.) were merged to an intermediate staging branch
    — not directly to integration — and would otherwise be left with
    ``landed_on_integration=False`` even though their code reached the
    integration trunk via the ancestor rollup merge.

    This function walks the full subtree rooted at *parent_task_id* via BFS
    and stamps ``landed_on_integration=True`` on every descendant that carries
    ``has_merge_unblock_signal=True`` (pr_merged=True) but not yet
    ``has_landed_signal``, skipping force-dropped children exactly as the sync
    helper does.  Idempotent: descendants that already carry the landed signal
    are skipped.

    Returns the descendant tasks newly stamped by THIS call (empty when every
    descendant was already landed / force-dropped / never merged). Callers that
    must also re-credit the affected directions — the churn / merge-rate
    counters are recomputed per-cycle from ``research_history`` and only pick a
    child up once ``is_merged_completion`` is True — use the returned tasks to
    drive one ``refresh_direction_outcomes_after_merge`` per distinct direction.
    Existing callers that ignore the return value are unaffected.
    """
    from app import acceptance_gate as _acceptance_gate
    from app.landed_signal import (
        NON_MERGE_UNBLOCK_REASON_KEY,
        corroborate_and_mark_landed_on_integration,
        has_landed_signal,
        has_merge_unblock_signal,
    )

    visited: set = {parent_task_id}
    queue: list = [parent_task_id]
    stamped: list[Task] = []

    # Read test_coverage_result from the root parent so it can be propagated
    # to descendants; coverage_delta acceptance evidence needs this key
    # present on each child task's context at verdict time.
    _parent = await session.get(Task, parent_task_id)
    _parent_ctx: dict = (_parent.context or {}) if _parent is not None else {}
    _parent_coverage: dict | None = _parent_ctx.get("test_coverage_result")
    # The rollup's own project, loaded once so the corroboration twin can
    # resolve GitHub credentials when it stamps a descendant's landed signal.
    _desc_project = (
        await session.get(Project, _parent.project_id)
        if _parent is not None and _parent.project_id is not None
        else None
    )
    # The landed parent's exact merge commit, forwarded to every descendant
    # stamped in this walk (rollup descendants land via the parent's single PR).
    _parent_merge_sha: str | None = _parent_ctx.get(LANDED_MERGE_SHA_KEY)

    # Read the landed PR-owning parent's GitHub-verified changed-file list so it
    # can be propagated DOWN to every landed descendant that lacks its own
    # ``_pr_changed_files`` context.  Rollup children merge via the PARENT's
    # single PR — they never own a GitHub PR, so ``_stamp_github_verified_
    # changed_files`` never runs on them and their declared ``named_test``
    # acceptance evidence cannot be located in the diff (declared_unsatisfied).
    # The parent's diff is the union of every sibling's changes, so it is stamped
    # as parent-derived (capped at the satisfied/unproven tier, never proven).
    _parent_changed: list[str] = [
        p
        for p in (_parent_ctx.get("_pr_changed_files") or [])
        if isinstance(p, str) and p.strip()
    ]

    while queue:
        rows = (
            (
                await session.execute(
                    select(Task).where(Task.parent_task_id.in_(queue))
                )
            )
            .scalars()
            .all()
        )

        next_queue: list = []
        for row in rows:
            if row.id in visited:
                continue
            visited.add(row.id)

            ctx = row.context or {}
            # Every non-force-dropped, staging-merged descendant participates in
            # the parent-derived coverage + changed-file propagation and verdict
            # re-record — whether or not it ALREADY carries the landed signal. A
            # child that landed BEFORE the parent's ``_pr_changed_files`` arrived
            # (the parent's single PR is the child's only changed-file source)
            # would otherwise be skipped here forever and stay frozen at
            # declared_unsatisfied, even once late-arriving provenance could flip
            # it. Only the landing stamp itself is gated on ``not
            # has_landed_signal`` so the ``stamped`` return contract (descendants
            # newly landed by THIS call) is preserved.
            if (
                ctx.get(NON_MERGE_UNBLOCK_REASON_KEY) not in _FORCE_DROP_REASONS
                and has_merge_unblock_signal(row)
            ):
                _propagated = False

                # Propagate test_coverage_result from the root parent to this
                # descendant so the coverage_delta acceptance gate
                # (_evaluate_coverage_delta) can find measured coverage data
                # instead of failing closed with "no result found".  Use
                # setdefault so any existing per-descendant coverage data is
                # never overwritten.
                if _parent_coverage is not None:
                    _desc_ctx = dict(row.context or {})
                    _desc_ctx.setdefault("test_coverage_result", _parent_coverage)
                    row.context = _desc_ctx
                    _propagated = True

                # Propagate the parent's changed-file context to descendants that
                # lack their own self-reported ``_pr_changed_files`` so a declared
                # named_test can be located in the diff and a stale
                # declared_unsatisfied verdict flips.  setdefault semantics: NEVER
                # clobber a child that already carries its own list.  After
                # stamping, re-evaluate the acceptance verdict so it re-stamps.
                if _parent_changed and not (row.context or {}).get(
                    "_pr_changed_files"
                ):
                    _cf_ctx = dict(row.context or {})
                    _cf_ctx.update(
                        _acceptance_gate.stamp_parent_derived_changed_files_signal(
                            _parent_changed
                        )
                    )
                    row.context = _cf_ctx
                    _acceptance_gate.record_code_acceptance_verdict(row)
                    _propagated = True

                if not has_landed_signal(row):
                    await corroborate_and_mark_landed_on_integration(
                        row,
                        session,
                        _desc_project,
                        merge_commit_sha=_parent_merge_sha,
                    )
                    session.add(row)
                    stamped.append(row)
                elif _propagated:
                    # Already-landed descendant that received fresh parent-derived
                    # provenance: persist the re-stamped context + verdict so the
                    # flip survives the caller's commit.
                    session.add(row)

            next_queue.append(row.id)

        queue = next_queue

    return stamped


async def _hold_rollup_for_branch_safety(
    parent: Task,
    project: Project | None,
    session: AsyncSession,
    detail: str,
    *,
    issue_code: str,
) -> None:
    """Persist a loud hold when rollup branch resolution would hit promotion."""
    if project is not None:
        _signal_blocking_project_branch_configuration(project, issue_code, detail)
    if _runtime_config.get("require_approval_gates", True):
        parent.requires_approval = True
    parent.latest_status_message = detail[:500]
    session.add(parent)
    await session.commit()
    logger.error("[ROLLUP] %s", detail)


# ── FIX A: non-rejection PR-close classification ──────────────────────────
# A Blueprint rollup/create PR can be closed without merging for two very
# different reasons:
#   • GENUINE REJECTION — a reviewer requested changes, the review-fix cap was
#     exhausted, or the agent's work was deemed unmergeable. These MUST stay
#     FAILED + alert so direction health and the operator see the real signal.
#   • NON-GENUINE CLEANUP / SUPERSESSION — the work already landed via a sibling
#     PR, a parent/sibling rollup de-scoped this PR, or the Blueprint bot closed
#     it as part of housekeeping. The desired outcome is (effectively) achieved
#     or the task is moot, so failing it false-fails a non-failure and can
#     cascade a rollup parent into a partial-failure finalize.
# This helper decides between the two. It is conservative: ANY genuine-rejection
# signal forces the FAILED path, and a non-genuine verdict additionally REQUIRES
# at least one positive cleanup/supersession signal — absence of signal is never
# treated as non-genuine.

_GENUINE_REJECTION_CTX_MARKERS = ("pr_rejected", "review_cap_exhausted")


def _ctx_has_genuine_rejection_marker(ctx: dict | None) -> bool:
    """True when task context carries an explicit genuine-rejection marker.

    ``pr_rejected`` is set by the review pipeline on a real CHANGES_REQUESTED /
    manual-close rejection; ``review_cap_exhausted`` (set as failure_detail by
    ``_handle_cap_exhaustion``) is a review-quality non-convergence. Either one
    forces the FAILED path regardless of any cleanup signal.
    """
    if not ctx:
        return False
    if ctx.get("pr_rejected") is True:
        return True
    return ctx.get("failure_detail") in _GENUINE_REJECTION_CTX_MARKERS


def _ctx_has_supersession_signal(task: "Task") -> bool:
    """True when context / status message marks this PR as superseded or
    de-scoped by a sibling/parent rollup (a recognized cleanup signal)."""
    ctx = task.context or {}
    if ctx.get("superseded") or ctx.get("pr_superseded") or ctx.get("descoped"):
        return True
    blob = " ".join(
        str(ctx.get(k) or "")
        for k in ("pr_close_reason", "close_reason", "rollup_descope_reason")
    )
    msg = (task.latest_status_message or "") + " " + blob
    return "superseded" in msg.lower() or "de-scoped" in msg.lower()


def _pr_author_is_blueprint_bot(pr_data: dict | None) -> bool:
    """True when the PR was authored by the Blueprint GitHub App (a ``[bot]``
    account). Blueprint creates all rollup/create PRs through its installation
    token, so a bot-authored PR closed-unmerged with no rejection marker is a
    housekeeping close, not a human rejection. GitHub does not expose the close
    *actor* on the PR object, so bot *authorship* is the available proxy."""
    if not pr_data:
        return False
    user = pr_data.get("user") or {}
    login = (user.get("login") or "").lower()
    return user.get("type") == "Bot" or login.endswith("[bot]")


async def _pr_closed_is_non_rejection(
    task: "Task",
    pr_data: dict,
    pr_number: int,
    session: AsyncSession,
) -> bool:
    """Decide whether a closed-unmerged PR was a NON-genuine close (→ CANCELLED)
    rather than a genuine rejection (→ FAILED).

    Returns True only when ALL of the following hold:
      1. No genuine-rejection DB marker (pr_rejected / review_cap_exhausted).
      2. No GitHub review with state == CHANGES_REQUESTED.
      3. At least one positive cleanup/supersession signal:
           • a supersession / de-scope marker, or
           • the PR is Blueprint-bot-authored (housekeeping close).

    Approval is deliberately not a cleanup signal. A human-authored PR can be
    approved and later closed because the work was abandoned or invalidated;
    only explicit supersession/de-scope evidence can downgrade that close.

    GUARDRAIL: any CHANGES_REQUESTED review, or any genuine-rejection marker, or
    the absence of every cleanup signal → returns False so the caller keeps the
    FAILED path. A FAILED reviews lookup (401, rate limit, 502, timeout — we saw
    NOTHING) is treated as "no recorded CHANGES_REQUESTED" and can NEVER on its
    own flip the verdict, because a positive cleanup signal is still required.

    A TRUNCATED reviews list is a DIFFERENT signal and IS decisive: we fetched real
    pages and know there are more we did not see, and because GitHub serves reviews
    oldest-first the ones we are missing are precisely the NEWEST — exactly where a
    late CHANGES_REQUESTED lives. That is evidence we cannot rule out a rejection,
    so it keeps FAILED. (Do not collapse the two: fail-closing on a mere fetch
    failure would let one rate-limited call turn a routine bot cleanup-close into a
    terminal FAILED + operator alert, and would make a 502 stricter than a 401.)
    """
    from app.github import REVIEWS_TRUNCATED, get_pull_request_reviews_checked
    from app.routers.agent import get_github_token_for_task

    ctx = task.context or {}

    # (1) Genuine-rejection markers always win.
    if _ctx_has_genuine_rejection_marker(ctx):
        return False

    # (2) Live CHANGES_REQUESTED check — a real reviewer rejection.
    try:
        gh_token, owner, repo = await get_github_token_for_task(task.id, session)
        reviews, reviews_status = await get_pull_request_reviews_checked(
            gh_token, owner, repo, pr_number, project_id=str(task.project_id)
        )
        if any(
            (r or {}).get("state") == "CHANGES_REQUESTED" for r in (reviews or [])
        ):
            logger.info(
                f"[PR_VERIFY] Task {task.id} PR #{pr_number} closed-unmerged has a "
                f"CHANGES_REQUESTED review — genuine rejection, keeping FAILED"
            )
            return False
        if reviews_status == REVIEWS_TRUNCATED:
            # FAIL CLOSED — on TRUNCATION only, never on a mere fetch failure.
            # We hold real pages and know more reviews exist that we did not see;
            # GitHub serves them oldest-first, so the missing ones are exactly the
            # NEWEST — where a late CHANGES_REQUESTED would be. That is positive
            # evidence we cannot rule out a rejection, and this is the last live
            # guard before a rejected PR is downgraded FAILED → CANCELLED.
            #
            # A REVIEWS_FAILED (page-1 non-200: rate limit, 502, …) deliberately
            # does NOT land here — it falls through to the best-effort path below,
            # same as the 401/raise case, so one transient hiccup cannot flip a
            # routine cleanup-close into a terminal FAILED + operator alert.
            logger.warning(
                f"[PR_VERIFY] Task {task.id} PR #{pr_number}: the reviews list is "
                f"TRUNCATED ({len(reviews or [])} fetched, more exist) — the newest "
                f"reviews are the ones missing, so a CHANGES_REQUESTED cannot be "
                f"ruled out; NOT treating the close as non-genuine"
            )
            return False
    except Exception as _rev_err:
        # Best-effort: a transient reviews lookup failure must never MASK a
        # rejection. We continue, but the positive-signal requirement below
        # still gates the non-genuine verdict.
        logger.warning(
            f"[PR_VERIFY] Task {task.id} PR #{pr_number} reviews lookup failed "
            f"({type(_rev_err).__name__}: {_rev_err}); requiring an explicit "
            f"cleanup signal before treating the close as non-rejection"
        )

    # (3) Require at least one positive cleanup/supersession signal.
    if _ctx_has_supersession_signal(task):
        return True
    return _pr_author_is_blueprint_bot(pr_data)


async def _dispatch_rollup_approval_alert(
    parent: Task, session: AsyncSession, reason: str, pr_number: int | None = None
) -> None:
    """Create a failure alert and fire an external APPROVAL_NEEDED notification
    when a rollup parent enters the approval-gated state so operators are
    notified instead of discovering the stall later.
    """
    import asyncio as _asyncio
    from app.alert_dispatch import _maybe_create_failure_alert
    from app.notification_dispatch import dispatch_external_notification

    try:
        await _maybe_create_failure_alert(parent, session, reason)
    except Exception as e:
        logger.warning(f"[NOTIFY] rollup approval alert creation failed: {e}")
    try:
        parent_ctx = parent.context if isinstance(parent.context, dict) else {}
        parent_payload = parent.first_payload or {}
        pr_url = parent_ctx.get("rollup_pr_url") or ""
        if not pr_url and pr_number:
            repo_full = parent_ctx.get("repo_full_name") or parent_ctx.get("repo") or ""
            if repo_full:
                pr_url = f"https://github.com/{repo_full}/pull/{pr_number}"
        raw_critical = (
            parent_ctx.get("critical_files")
            or parent_ctx.get("_pr_changed_files")
            or []
        )
        critical_files = (
            [f for f in raw_critical if isinstance(f, str)][:10]
            if isinstance(raw_critical, list)
            else []
        )
        direction_text = (
            parent_ctx.get("direction_text")
            or parent_payload.get("direction_text")
            or ""
        )
        scope = parent_ctx.get("scope") or parent_payload.get("scope") or ""
        # Fire-and-forget best-effort external notification; losing the task
        # on GC is acceptable and the call is already try/except-wrapped, so
        # RUF006 (store-a-reference) is intentionally suppressed below.
        _asyncio.ensure_future(  # noqa: RUF006
            dispatch_external_notification(
                parent.project_id,
                "APPROVAL_NEEDED",
                {
                    "task_id": str(parent.id),
                    "task_title": parent.title or str(parent.id)[:8],
                    "pr_number": pr_number,
                    "reason": reason,
                    "approval_reason": reason,
                    "pr_url": pr_url,
                    "critical_files": critical_files,
                    "direction_id": parent_ctx.get("direction_id") or "",
                    "direction_text": direction_text[:200],
                    "scope": scope,
                },
            )
        )
    except Exception as e:
        logger.warning(f"[NOTIFY] APPROVAL_NEEDED dispatch setup failed: {e}")


def _build_rollup_pr_body(
    task_count: int,
    staging_branch: str,
    rollup_note: str = "",
    *,
    direction_id: str | None = None,
    direction_title: str | None = None,
    children: list | None = None,
) -> str:
    """Build the Markdown body for a rollup PR.

    Includes direction, task-type breakdown, and aggregate cost when available.
    """
    lines: list[str] = [f"Rollup of {task_count} subtasks from `{staging_branch}`"]

    if direction_title or direction_id:
        dir_label = (direction_title or "")[:80]
        dir_suffix = f" `{direction_id[:8]}`" if direction_id else ""
        lines.append("")
        lines.append(f"**Direction:** {dir_label}{dir_suffix}")

    if children:
        from collections import Counter

        type_counts: Counter = Counter()
        total_cost: float = 0.0
        has_cost = False
        for c in children:
            payloads = getattr(c, "payloads", None) or []
            t = payloads[0].get("type", "create") if payloads else "create"
            type_counts[t] += 1
            prov_at = getattr(c, "vm_provisioned_at", None)
            stop_at = getattr(c, "vm_stopped_at", None)
            if prov_at and stop_at:
                from app.vm_cost_rates import get_vm_rate_per_second

                rate = get_vm_rate_per_second(
                    machine_type=getattr(c, "vm_machine_type", None),
                    spot=getattr(c, "vm_spot", False),
                )
                total_cost += (stop_at - prov_at).total_seconds() * rate
                has_cost = True
        if type_counts:
            breakdown = ", ".join(
                f"{cnt} {typ}" for typ, cnt in sorted(type_counts.items())
            )
            lines.append("")
            lines.append(f"**Tasks:** {breakdown}")
        if has_cost:
            lines.append(f"**Estimated cost:** ~${total_cost:.2f}")

    if rollup_note:
        lines.append(rollup_note)

    lines.append("")
    lines.append("---")
    lines.append("*Automated by Blueprint Pipeline*")
    return "\n".join(lines)


# Upper bound on sibling rollup PRs nudged per merge. Each nudge costs a PR
# fetch + (usually) an update-branch round-trip, all serial inside the rollup
# post-merge handler; observed queue depth is ~8, so 20 is generous headroom
# without letting a pathological backlog stall the handler.
_MERGE_QUEUE_MAX_SIBLINGS = 20


async def _nudge_sibling_rollup_prs(
    merged_parent: Task,
    session: AsyncSession,
    gh_token: str,
    owner: str,
    repo: str,
    target_branch: str,
    merged_pr_number: int | None = None,
) -> None:
    """Rollup merge train: proactively update-branch sibling rollup PRs.

    When N rollup PRs are queued against the same integration branch, each
    merge staleness-bumps the rest — GitHub flips them to ``behind`` and the
    LAZY recovery (CI polling / next rollup sweep) takes minutes to notice,
    while a stale ``dirty`` read burns a 20-50min agent conflict-fix cycle.
    This runs in the rollup post-merge handler: it finds the project's other
    rollup parents (EVALUATING/BLOCKED with a ``rollup_pr_number``), orders
    their PRs FIFO by PR ``created_at``, and calls the existing
    ``update_branch_with_sha_retry`` on each behind sibling. Merging base INTO
    the branch preserves review state (``review_approved`` was on the code;
    CI revalidates the combination on the new merge commit).

    Each successful update stamps ``_merge_queue_update_at`` on the sibling
    parent's context so the conflict-fix spawners defer a dirty observation
    while the update is fresh (<5 min) — the queue and the agent path never
    double-fire. Failures are logged and left to the lazy path unchanged.

    Best-effort by design: gated on runtime_config ``rollup_merge_queue_enabled``
    (default True); any per-sibling error skips that sibling, never the train.
    """
    if not _runtime_config.get("rollup_merge_queue_enabled", True):
        return
    from app.github import (
        get_branch_sha,
        get_pull_request,
        pr_is_not_behind_for,
        update_branch_with_sha_retry,
    )
    from app.time_utils import _utcnow

    project_id_str = str(merged_parent.project_id)

    # Sibling rollup parents: same project, not this parent, still in flight.
    # ``rollup_pr_number`` presence is checked dialect-aware (GIN ``?`` on
    # Postgres, json_extract on SQLite) — mirrors the stale-window heuristic.
    if resolve_dialect_name(session) == "postgresql":
        _rollup_pr_present = Task.context.has_key("rollup_pr_number")
    else:
        _rollup_pr_present = _func.json_extract(
            Task.context, "$.rollup_pr_number"
        ).isnot(None)
    sibling_tasks = (
        (
            await session.execute(
                select(Task).where(
                    Task.project_id == merged_parent.project_id,
                    Task.id != merged_parent.id,
                    Task.status.in_(
                        [TaskStatus.EVALUATING, TaskStatus.BLOCKED]
                    ),
                    _rollup_pr_present,
                )
            )
        )
        .scalars()
        .all()
    )

    # Collect open sibling rollup PRs against the same base, FIFO by created_at.
    # Snapshot (pr_number, task_id) scalars up front — the update loop below
    # commits/rolls back per sibling, and a rollback expires loaded ORM
    # instances (stale-batch trap); the locked re-fetch works from the id.
    candidates: list[tuple[str, int, object, dict]] = []
    seen_pr_numbers: set[int] = set()
    for sibling in sibling_tasks:
        ctx = sibling.context or {}
        # Child review/fix tasks spawned FOR a rollup PR also carry
        # rollup_pr_number — only true rollup parents awaiting merge qualify.
        if not _is_rollup_parent_awaiting_merge(ctx):
            continue
        try:
            pr_number = int(ctx.get("rollup_pr_number"))
        except (TypeError, ValueError):
            continue
        if pr_number in seen_pr_numbers:
            continue
        if merged_pr_number is not None and pr_number == merged_pr_number:
            continue
        seen_pr_numbers.add(pr_number)
        if len(candidates) >= _MERGE_QUEUE_MAX_SIBLINGS:
            logger.info(
                f"[MERGE-QUEUE] Sibling cap ({_MERGE_QUEUE_MAX_SIBLINGS}) reached for "
                f"project {project_id_str} — remaining rollups recover via the lazy path"
            )
            break
        try:
            pr_detail = await get_pull_request(
                gh_token, owner, repo, pr_number, project_id=project_id_str
            )
        except Exception as fetch_exc:
            # Best-effort train: a failed fetch falls back to lazy recovery
            # for that sibling.
            logger.warning(
                f"[MERGE-QUEUE] Could not fetch sibling rollup PR #{pr_number}: {fetch_exc}"
            )
            continue
        if (pr_detail.get("state") or "").lower() != "open" or pr_detail.get("merged"):
            continue
        base_ref = ((pr_detail.get("base") or {}).get("ref")) or ""
        if base_ref and target_branch and base_ref != target_branch:
            # Queued against a different base — our merge didn't bump it.
            continue
        candidates.append(
            (str(pr_detail.get("created_at") or ""), pr_number, sibling.id, pr_detail)
        )
    if not candidates:
        return
    # FIFO: oldest PR first (ISO-8601 sorts lexicographically; PR number
    # tiebreaks and covers a missing created_at).
    candidates.sort(key=lambda c: (c[0], c[1]))

    # Fetch the base tip once — pr_is_not_behind_for would otherwise round-trip
    # get_branch_sha per sibling. On failure each check falls back internally.
    base_tip_sha = None
    try:
        # Behind-detection base tip — staleness-tolerant, serve from the shared
        # ~60s branch-tip cache (same target_branch is read by every sweep).
        base_tip_sha = await get_branch_sha(
            gh_token,
            owner,
            repo,
            target_branch,
            project_id=project_id_str,
        )  # behind-detection base tip — cached-by-default (staleness-tolerant)
    except Exception as tip_exc:
        # Tip fetch is an optimization; per-sibling checks degrade gracefully
        # without it (pr_is_not_behind_for fetches per-call).
        logger.warning(
            f"[MERGE-QUEUE] get_branch_sha({target_branch}) failed: {tip_exc}"
        )

    for _created_at, pr_number, sibling_task_id, pr_detail in candidates:
        try:
            not_behind = await pr_is_not_behind_for(
                gh_token,
                owner,
                repo,
                pr_detail=pr_detail,
                base_branch=target_branch,
                base_tip_sha=base_tip_sha,
                project_id=project_id_str,
                call_site="rollup_merge_queue",
            )
            if not_behind is True:
                # Head already contains the base tip — nothing to update.
                continue
            upd = await update_branch_with_sha_retry(
                gh_token,
                owner,
                repo,
                pr_number,
                expected_head_sha=(pr_detail.get("head") or {}).get("sha"),
                project_id=project_id_str,
            )
            if not upd.get("updated"):
                # not_behind (stale flag) or transient failure — the lazy
                # behind-recovery path (CI polling / rollup sweep) owns it.
                logger.info(
                    f"[MERGE-QUEUE] update_branch skipped/failed for sibling rollup "
                    f"PR #{pr_number} ({upd.get('message') or 'not behind'}) — lazy path owns it"
                )
                continue
            # Stamp the in-flight marker UNDER a short row lock so the
            # conflict-fix spawners defer dirty observations inside the
            # freshness window. skip_locked: if another loop holds the row
            # (CI polling, heartbeat), skip the stamp — the update itself
            # already happened; worst case is today's behavior.
            locked_sibling = (
                (
                    await session.execute(
                        select(Task)
                        .where(
                            Task.id == sibling_task_id,
                            Task.status.in_(
                                [TaskStatus.EVALUATING, TaskStatus.BLOCKED]
                            ),
                        )
                        .with_for_update(skip_locked=True)
                        .execution_options(populate_existing=True)
                    )
                )
                .scalars()
                .first()
            )
            if locked_sibling is None:
                await session.rollback()
                logger.info(
                    f"[MERGE-QUEUE] Sibling rollup parent for PR #{pr_number} locked "
                    f"elsewhere — branch updated but marker not stamped"
                )
                continue
            new_ctx = {
                **(locked_sibling.context or {}),
                _MERGE_QUEUE_UPDATE_AT_KEY: _utcnow().isoformat(),
            }
            # Force the sibling's CI polling loop to refetch the PR promptly —
            # the update-branch merge commit changed the head SHA, mirroring
            # the lazy behind path's post-update refresh.
            review_loop = new_ctx.get("review_loop")
            if isinstance(review_loop, dict):
                new_ctx["review_loop"] = {
                    **review_loop,
                    "polls_since_pr_fetch": 999,
                    "head_sha": "",
                }
            locked_sibling.context = new_ctx
            locked_sibling.latest_status_message = (
                f"Rollup PR #{pr_number} was behind {target_branch} after sibling "
                f"rollup merge — merge queue updated branch, awaiting fresh CI"
            )
            session.add(locked_sibling)
            await session.commit()
            logger.info(
                f"[MERGE-QUEUE] Updated sibling rollup PR #{pr_number} after "
                f"PR #{merged_pr_number} merged into {target_branch}"
            )
        except Exception as sib_exc:
            # One sibling must never derail the train; its lazy recovery path
            # is unchanged. Rollback of an already-dead session is best-effort.
            try:
                await session.rollback()
            except Exception as rb_exc:
                logger.debug(
                    f"[MERGE-QUEUE] rollback after sibling PR #{pr_number} "
                    f"failure also failed: {rb_exc}"
                )
            logger.warning(
                f"[MERGE-QUEUE] Failed to nudge sibling rollup PR #{pr_number}: {sib_exc}"
            )


async def _do_rollup_post_merge_housekeeping(
    parent: Task,
    project: "Project | None",
    session: "AsyncSession",
    gh_token: str,
    owner: str,
    repo: str,
    rollup_pr_number: int,
    staging_branch: str,
    target_branch: str,
    completed_children: "list[Task]",
    completed_count: int,
    landed: bool,
    terminal: bool,
) -> None:
    """Post-merge housekeeping shared by the main, re-poll, and escalation merge paths.

    Must be called AFTER ``session.add(parent); await session.commit()`` so the
    FOR UPDATE lock is released before any GitHub/notification I/O. Performs:
    direction-outcomes refresh, staging-branch deletion, PR-merged notification,
    viewport-coverage snapshot, a second commit, optional terminal cleanup +
    broadcast, and the merge-train sibling nudge.
    """
    from app.broadcast import broadcast_to_project
    from app.github import delete_branch, get_pull_request_diff
    from app.viewport_coverage import update_viewport_coverage_snapshot_from_diff

    if landed:
        from app.direction_outcomes import refresh_direction_outcomes_after_merge

        _dir_representatives: dict[str, Task] = {}
        for c in completed_children:
            _ctx = c.context or {}
            _did = _ctx.get("direction_id")
            _rid = _ctx.get("research_task_id")
            if _did and _rid and _did not in _dir_representatives:
                _dir_representatives[_did] = c
        for _rep in _dir_representatives.values():
            await refresh_direction_outcomes_after_merge(_rep, session)
        await _record_completed_children_merge_evidence(completed_children, session)
        if project is not None:
            try:
                await _maybe_spawn_direction_merge_grade(
                    parent,
                    project,
                    session,
                    gh_token,
                    owner,
                    repo,
                    target_branch,
                )
            except Exception as _mg_exc:  # noqa: BLE001 — best-effort post-merge grade
                logger.warning(
                    f"[ROLLUP] Failed to spawn direction merge grade: {_mg_exc}"
                )
    try:
        await delete_branch(gh_token, owner, repo, staging_branch)
    except Exception as del_err:
        logger.warning(
            f"[ROLLUP] Failed to delete staging branch {staging_branch}: {del_err}"
        )
    logger.info(f"[ROLLUP] Auto-merged rollup into {target_branch}")
    try:
        from app.notification_dispatch import post_pr_merged_message

        _direction_id = (parent.context or {}).get("direction_id")
        _direction_text = "unknown direction"
        if _direction_id:
            _proj = await session.get(Project, parent.project_id)
            if _proj:
                for _d in _proj.directions or []:
                    if str(_d.get("id", "")) == str(_direction_id):
                        _direction_text = _d.get("text", "unknown direction")
                        break
        await post_pr_merged_message(
            session,
            parent.project_id,
            rollup_pr_number,
            _direction_text,
            completed_count,
            parent.id,
        )
    except Exception as _e:
        logger.warning(f"[NOTIFY] Failed to post PR merged message: {_e}")
    try:
        _snap_proj = await session.get(Project, parent.project_id)
        if _snap_proj is not None:
            _diff_text = await get_pull_request_diff(
                gh_token, owner, repo, rollup_pr_number
            )
            await update_viewport_coverage_snapshot_from_diff(
                session, _snap_proj, rollup_pr_number, _diff_text
            )
    except Exception as _snap_e:
        logger.warning(
            f"[ROLLUP] Failed to update viewport_coverage_snapshot: {_snap_e}"
        )
    await session.commit()
    if terminal:
        await _cleanup_branches(parent, session)
        await broadcast_to_project(
            str(parent.project_id),
            "task_updated",
            {
                "task_id": str(parent.id),
                "status": TaskStatus.COMPLETED,
            },
        )
    try:
        await _nudge_sibling_rollup_prs(
            parent,
            session,
            gh_token,
            owner,
            repo,
            target_branch,
            merged_pr_number=rollup_pr_number,
        )
    except Exception as _queue_exc:
        logger.warning(
            f"[MERGE-QUEUE] Sibling nudge after rollup PR "
            f"#{rollup_pr_number} failed: {_queue_exc}"
        )


async def _maybe_spawn_direction_merge_grade(
    parent: Task,
    project: "Project",
    session: "AsyncSession",
    gh_token: str,
    owner: str,
    repo: str,
    target_branch: str,
) -> None:
    """Spawn a direction-scoped blind objective grade after a landed rollup.

    The parent rollup's context carries the campaign's ``direction_id`` when the
    rollup is a multi-task campaign.  When that direction declares a gradeable
    objective, its integrated merged diff (the integration-branch tip the rollup
    just landed on) is graded against it via the direction-scoped spawner
    (``release_qualifier_audit._spawn_direction_merge_grade_task``).  The
    completed grade's verdicts are later consumed by the direction lifecycle
    (``strategic_completion.consume_direction_merge_grade_if_ready``) into the
    direction-scoped category-verdict ledger, and the direction's completion
    gate then reflects the objective verdict instead of child-merge counts.

    Duplicate-spawn guard: the spawned task id + candidate sha are stamped on
    the parent context; a re-poll of the same landed merge reuses the existing
    spawn instead of minting another grade.
    """
    ctx = dict(parent.context or {})
    direction_id = str(ctx.get("direction_id") or "").strip()
    if not direction_id:
        return
    direction = next(
        (
            d
            for d in (project.directions or [])
            if str(d.get("id") or "") == direction_id
        ),
        None,
    )
    if direction is None:
        return
    from app.release_qualifier_audit import (  # noqa: PLC0415
        _collect_direction_merged_pr_numbers,
        _resolve_direction_objective,
        _spawn_direction_merge_grade_task,
    )
    from app.release_scoring import is_contentless_goal  # noqa: PLC0415

    objective = _resolve_direction_objective(direction)
    if not objective or is_contentless_goal(objective):
        return
    try:
        from app.github_refs import get_branch_sha  # noqa: PLC0415

        candidate_sha = await get_branch_sha(gh_token, owner, repo, target_branch)
    except Exception as _sha_exc:  # noqa: BLE001 — best-effort post-merge grade
        logger.warning(
            f"[ROLLUP] Failed to resolve {target_branch} tip for direction "
            f"merge grade: {_sha_exc}"
        )
        return
    if not candidate_sha:
        return
    if ctx.get("_direction_merge_grade_candidate_sha") == candidate_sha:
        return  # already spawned for this merged diff
    import uuid  # noqa: PLC0415

    merged_prs = await _collect_direction_merged_pr_numbers(
        session, project.id, direction_id
    )
    from app.strategic_completion import (  # noqa: PLC0415
        _direction_active_categories,
        _direction_active_category_keys,
    )

    active_keys = await _direction_active_category_keys(
        session, project, direction, direction_id
    )
    if not active_keys:
        # No rubric to grade against — an empty active set is un-consumable:
        # direction_objective_completion_verdict returns None for it and
        # record_direction_merge_grade writes no ledger rows (no category
        # verdicts), so consume_direction_merge_grade_if_ready never records the
        # grade and the completion gate would never consult it. Skip spawning
        # (mirrors the is_contentless_goal early-return above).
        return
    active_categories = await _direction_active_categories(
        session, project, direction, active_keys
    )
    # The diff base is the STABLE promotion target, NOT the target_branch the
    # candidate was resolved from: on a terminal rollup target_branch IS the
    # integration branch, so an integration-branch base would yield an EMPTY
    # three-dot range and the objective could never be graded.
    from app.branch_resolution import get_project_promotion_target  # noqa: PLC0415

    grade_base = get_project_promotion_target(project)
    grade_task = await _spawn_direction_merge_grade_task(
        session,
        project.id,
        direction=direction,
        candidate_sha=candidate_sha,
        qualification_attempt_id=uuid.uuid4(),
        base_branch=grade_base,
        merged_pr_numbers=merged_prs,
        categories=active_categories or None,
    )
    if grade_task is None:
        return
    parent.context = {
        **ctx,
        "_direction_merge_grade_task_id": str(grade_task.id),
        "_direction_merge_grade_candidate_sha": candidate_sha,
    }
    from sqlalchemy.orm.attributes import flag_modified  # noqa: PLC0415

    flag_modified(parent, "context")
    session.add(parent)
    logger.info(
        f"[ROLLUP] Spawned direction merge grade {grade_task.id} for "
        f"{direction_id} ({grade_base}...{candidate_sha[:8]}) vs objective"
    )


async def _record_completed_children_merge_evidence(
    completed_children: "list[Task]", session: "AsyncSession"
) -> None:
    """Best-effort acceptance evidence recording for landed completed children.

    Force-dropped children (rollup_no_pr_artifact, malformed_pr_url,
    review_respawn_cap) are intentionally excluded: they carry pr_merged=True
    for orchestration bookkeeping but were never genuinely merged, so they must
    not receive merge/reachability evidence.
    """
    from app.acceptance_criteria import record_task_merge_evidence

    for _child in completed_children:
        ctx = _child.context or {}
        if ctx.get("_non_merge_unblock_reason") in _FORCE_DROP_REASONS:
            continue
        try:
            await record_task_merge_evidence(session, _child, _child.project_id)
        except Exception as exc:  # noqa: BLE001 — best-effort post-merge audit
            logger.warning(
                "[ROLLUP] criterion evidence recording failed for child %s: %r",
                getattr(_child, "id", None),
                exc,
            )


async def _mark_scan_report_posted(
    session: "AsyncSession", parent_id, pr_number: int
) -> None:
    """Durably record that the scan-report comment for ``pr_number`` was posted.

    NIT 8. K7 hoists ``create_pr_comment`` OUT of the parent's lock window, which
    is correct (a Postgres row lock is released by transaction end, so the old
    call site held the parent FOR UPDATE across a GitHub POST). The cost is that
    the post can succeed and the following ``_relock_parent`` can still be lost —
    leaving the comment on the PR with nothing recording it, so the next sweep
    re-scans and posts a DUPLICATE. A per-invocation local flag cannot fix that;
    only a durable, PR-keyed marker can.

    Runs in its OWN bounded transaction, taken AFTER the GitHub call and released
    immediately — no network I/O inside it, so this re-acquire can never become
    the very idle-in-transaction window K7 removed.

    Best-effort by design: on ``skip_locked`` contention or any error we log and
    move on. Failing to stamp the marker costs at most one duplicate comment on a
    later sweep (cosmetic); it can never block the rollup or lose the report.
    """
    from sqlalchemy.orm.attributes import flag_modified

    try:
        row = (
            await session.execute(
                select(Task)
                .where(Task.id == parent_id)
                .with_for_update(skip_locked=True)
                .execution_options(populate_existing=True)
            )
        ).scalars().first()
        if row is None:
            logger.info(
                f"[ROLLUP] Scan-report marker for PR #{pr_number} not stamped — parent "
                f"row held by a peer. Worst case: one duplicate comment next sweep."
            )
            await session.commit()
            return
        ctx = dict(row.context or {})
        ctx[_SCAN_REPORT_POSTED_PR_KEY] = pr_number
        row.context = ctx
        flag_modified(row, "context")
        session.add(row)
        await session.commit()
    except Exception as exc:
        # Best-effort de-dup marker: never let a marker failure break the rollup.
        #
        # ROLLBACK-EXPIRY CONTRACT (the campaign's own bug class — see
        # review_loop_convergence._handle_cap_exhaustion). This rollback is
        # MANDATORY (a failed commit leaves the session in pending-rollback) and it
        # expires EVERY loaded instance, including the caller's ``parent``. That is
        # safe HERE only because of what the caller does next: the sole ORM object
        # it touches after this returns is re-loaded by ``_relock_parent``, which
        # expires the cached instance and re-SELECTs it with populate_existing
        # anyway. Any future code added between this call and that re-lock MUST
        # re-fetch what it touches, or it will lazy-load outside greenlet context
        # and raise MissingGreenlet.
        logger.warning(
            f"[ROLLUP] Failed to persist scan-report marker for PR #{pr_number}: {exc}"
        )
        await session.rollback()


def _rollup_review_respawn_park_message(
    parent: Task, rollup_pr_number: int
) -> str | None:
    """Park-honoring gate for the rollup-PR review (re)spawn.

    Returns the truthful ``latest_status_message`` when the parent's review
    loop was already escalated to the operator (stuck/standoff breadcrumb from
    ``review_loop_parked_for_operator``), or ``None`` when the spawn should
    proceed — including the FIRST spawn for a fresh rollup PR, which has no
    terminal outcome recorded yet.

    Incident (lineage d3c12745 / PR #16759, 2026-08-06): after the stuck
    escalation parked the chain at 04:36, this sweep respawned 13 reviews —
    each prior review had COMPLETED, freeing its active-idempotency key, and
    nothing checked the park. Every respawn ran a model, re-hit the stuck gate
    on its fix spawn, and COUNTED toward the global pr_iterations recount,
    ratcheting the loop to the cap that then auto-closed the parked PR.
    """
    from app.review_loop_convergence import review_loop_parked_for_operator

    reason = review_loop_parked_for_operator(parent)
    if reason is None:
        return None
    return (
        f"Review loop parked for operator ({reason}) — not respawning review "
        f"for rollup PR #{rollup_pr_number}"
    )


async def _check_and_trigger_rollup(
    child_task: Task, session: AsyncSession, progress_cb=None
) -> bool:
    """Check if all siblings are done and trigger rollup PR for the parent.

    Returns:
        True if a silent internal session.rollback() was performed (e.g. the
        ``_sess_poisoned`` recovery path). The caller should re-fetch ORM objects
        before the next attribute access to avoid MissingGreenlet on expired
        instances in async contexts. False (or None) otherwise — no re-batch needed.

    Called after a child task completes, fails, or is cancelled.
    Also called from admin PATCH when status/context changes.

    ``progress_cb`` (C3): an optional zero-arg heartbeat invoked inside the
    per-child PR-verification loop. The reaper's stale-EVALUATING sweep passes
    ``_mark_reaper_progress`` so that verifying many unmerged children (each a
    serial GitHub round-trip via _verify_pr_merged) keeps the reaper liveness
    heartbeat fresh instead of silently consuming the whole cycle budget on one
    fan-heavy parent. Defaults to a no-op for the request-path callers.
    """
    from app.alert_dispatch import _maybe_create_failure_alert
    from app.time_utils import _utcnow
    from app.broadcast import broadcast_to_project, _sanitize_log
    from app.routers.agent import get_github_token_for_task
    from app.security import scan_pr_for_injection
    from app.github import (
        create_pr_comment,
        delete_branch,
        create_pull_request,
        merge_pull_request,
        get_pull_request,
        get_pull_request_diff,
        get_pr_files,
        get_branch_sha,
        close_pull_request,
        pr_is_not_behind_for,
        update_branch_with_sha_retry,
    )

    # Track whether a silent internal rollback was performed (via the
    # _sess_poisoned recovery path). The caller inspects this return value
    # to decide whether to re-fetch ORM objects before the next attribute
    # access, avoiding MissingGreenlet on expired instances in asyncpg.
    _did_silent_rollback = False

    if not child_task.parent_task_id:
        return

    # Lock parent row to serialize concurrent rollup checks from sibling completions.
    # skip_locked: if a sibling child is already running this check, defer — the
    # reaper's EVALUATING stale-parent sweep retries within 15 min as safety net.
    parent = (
        (
            await session.execute(
                select(Task)
                .where(Task.id == child_task.parent_task_id)
                .with_for_update(skip_locked=True)
            )
        )
        .scalars()
        .first()
    )
    if parent is None:
        logger.info(
            f"[ROLLUP] Skipped rollup check for task {child_task.id} — parent lock held, "
            "reaper sweep will retry"
        )
        return
    if parent.status != TaskStatus.EVALUATING:
        return

    # Heartbeat false-reap fix (Fix 1 — narrowed sibling lock):
    #
    # Previously this took ``FOR UPDATE skip_locked`` on ALL sibling children on
    # every child-terminal event. A live (IN_PROGRESS/QUEUED/PROVISIONING/
    # EVALUATING/BLOCKED) sibling's heartbeat then 409'd (its own
    # ``with_for_update(skip_locked=True)`` task-row write was skipped while this
    # lock was held), freezing its ``last_heartbeat`` → the reaper false-reaped
    # the live VM. But live siblings are ONLY READ here (for the active_count /
    # pipeline_active tally) — they are never mutated. Only TERMINAL
    # (COMPLETED / FAILED / CANCELLED) children get written (pr_merged force-set,
    # review respawn-count bump, requires_approval escalation). So we:
    #   1. read ALL children NON-locking — a consistent snapshot under the
    #      parent lock, sufficient for the read-only tally + list-building, and
    #   2. lock ONLY the terminal children FOR UPDATE (skip_locked) — the rows we
    #      actually mutate — re-populating the same identity-map objects so the
    #      downstream mutations land on row-locked rows.
    #
    # IDEMPOTENCY is preserved by the PARENT lock (acquired above): it serializes
    # concurrent rollup checks (lock-all-or-defer), so there is never a
    # double-rollup or double-respawn — only one invocation holds the parent and
    # proceeds; peers get None and defer. The all-children lock never provided
    # the idempotency guarantee (review respawn is idempotent via
    # _spawn_pipeline_task's own per-PR advisory lock + the parent-locked
    # _review_respawn_count; the rollup PR POST is idempotent via the parent-
    # locked rollup_pr_creating_at/rollup_pr_number markers). It only (a) avoided
    # acting on contended terminal data and (b) avoided deadlocks — both of which
    # the narrowed terminal-only lock still covers.
    all_children = (
        (
            await session.execute(
                select(Task).where(Task.parent_task_id == parent.id)
            )
        )
        .scalars()
        .all()
    )

    # Lock ONLY the terminal children (the rows we may mutate below). Live
    # siblings are deliberately left UNLOCKED so their heartbeats never 409.
    _TERMINAL_CHILD_STATUSES = (
        TaskStatus.COMPLETED,
        TaskStatus.FAILED,
        TaskStatus.CANCELLED,
    )
    terminal_child_ids = [
        c.id for c in all_children if c.status in _TERMINAL_CHILD_STATUSES
    ]
    if terminal_child_ids:
        locked_terminal = (
            (
                await session.execute(
                    select(Task)
                    .where(Task.id.in_(terminal_child_ids))
                    .with_for_update(skip_locked=True)
                    .execution_options(populate_existing=True)
                )
            )
            .scalars()
            .all()
        )
        # If a TERMINAL child we intend to mutate is held by a peer (another
        # rollup invocation racing on a sibling event, or a late agent_status
        # write), defer to avoid acting on / clobbering contended terminal data.
        # Preserves the original "don't act on incomplete data" intent, narrowed
        # to the rows that actually matter. Live siblings (never mutated) do NOT
        # gate this defer. The reaper's EVALUATING stale-parent sweep retries.
        if len(locked_terminal) < len(terminal_child_ids):
            logger.info(
                f"[ROLLUP] Deferred rollup for parent {parent.id} — "
                f"locked {len(locked_terminal)}/{len(terminal_child_ids)} "
                f"terminal children (others held)"
            )
            return
        # NB: ``populate_existing`` refreshed the terminal rows IN the session
        # identity map, so the terminal objects already inside ``all_children``
        # (and the ``children`` / ``completed_children`` / ``unmerged`` /
        # ``failed`` lists derived from it below) ARE the freshly-locked
        # instances — SQLAlchemy guarantees object identity per session. No
        # re-read of ``all_children`` is needed; downstream mutations on those
        # objects land on row-locked rows.

    # Consider create, refactor, plan, research, and specialized agent tasks for rollup decisions.
    # Review/fix tasks are pipeline helpers that operate on the same PR — excluded.
    ROLLUP_TYPES = {
        "create",
        "refactor",
        "migrate",
        "",
        "plan",
        "research",
        "security_audit",
        "qa",
        "deps",
        "docs",
        "smoketest",
        "perf",
    }
    children = [
        c
        for c in all_children
        if (c.first_payload).get("type", "create") in ROLLUP_TYPES
    ]

    # A parent that owns an unlanded PR is still the delivery owner even when
    # all of its children are review/fix helpers.  The old shortcut completed
    # it here, erasing ownership before the PR landed and allowing recovery
    # loops to commission the same logical repair again.
    if not children and all_children:
        parent_pr = effective_pr_number(parent)
        parent_ctx = parent.context or {}
        if parent_pr and not parent_ctx.get("pr_merged"):
            parent.status = TaskStatus.EVALUATING
            parent.latest_status_message = (
                f"PR #{parent_pr} remains delivery-owned; pipeline children are "
                "terminal but the PR has not landed"
            )[:500]
            session.add(parent)
            await session.commit()
            return
        parent.status = TaskStatus.COMPLETED
        parent.context = _clear_rollup_transient_state(parent.context)
        parent.latest_status_message = (
            "All subtasks were pipeline tasks (review/fix) — no rollup needed"
        )
        session.add(parent)
        await session.commit()
        await _cleanup_branches(parent, session)
        await broadcast_to_project(
            str(parent.project_id),
            "task_updated",
            {"task_id": str(parent.id), "status": TaskStatus.COMPLETED},
        )
        logger.info(
            f"[ROLLUP] Parent {parent.id} completed — all children were review/fix pipeline tasks"
        )
        return

    # Also check if any review/fix pipeline tasks are still active — don't rollup
    # while a review/fix cycle is in progress. Stale tasks (no heartbeat for
    # 15+ min) are excluded so a silently-dead pipeline task can't permanently
    # block rollup; the orchestrator's reaper will eventually cancel them.
    #
    # MAJOR 7: this is the 4th consumer of ``_is_pipeline_task_stale`` and it must
    # be fed the SAME liveness evidence as the three supersede sites in
    # review_pipeline. Left on the raw heartbeat signal, the two predicates
    # DISAGREE about the same task: the rollup would declare a lock-starved-but-
    # LIVE child "stale" and stop waiting on it, while ``_spawn_pipeline_task``
    # (which has the VmContact override) refuses to supersede it — one task, two
    # contradictory definitions of "stale". Thread both lookups through so
    # "stale" means exactly one thing across the codebase.
    _pipeline_children = [
        c for c in all_children if (c.first_payload).get("type") in ("review", "fix")
    ]
    _vm_contact_lookup, _runner_report_lookup = await _load_pipeline_liveness_lookups(
        session, _pipeline_children
    )
    pipeline_active = False
    pipeline_recovery_pending = False
    # The infra-recovery sweep can resurrect a recent FAILED leaf exactly once.
    # Treat an eligible review/fix failure as active until that bounded recovery
    # window closes. Otherwise rollup can observe the direct work child as
    # CANCELLED, mark the parent FAILED ("All N subtasks were cancelled"), and
    # then watch the review child auto-resurrect seconds later — leaving live
    # recovery work underneath a terminal parent.
    from app.failure_classification import INFRA_CLASSES

    _infra_resurrection_cutoff = _utcnow() - timedelta(hours=2)
    for c in all_children:
        if (c.first_payload).get("type") not in ("review", "fix"):
            continue
        if c.status == TaskStatus.FAILED:
            ctx = c.context or {}
            failure_category = ctx.get("failure_category")
            resurrection_pending = (
                failure_category in INFRA_CLASSES
                and int(ctx.get("_resurrection_count", 0)) < 1
                and c.updated_at is not None
                and c.updated_at >= _infra_resurrection_cutoff
                and ctx.get("timeout_subtype") != "oom"
                and ctx.get("pr_closed_unmerged") is not True
                and ctx.get("pr_closed_empty") is not True
                and ctx.get("pr_rejected") is not True
            )
            if resurrection_pending:
                pipeline_active = True
                pipeline_recovery_pending = True
                break
            continue
        if c.status not in (
            TaskStatus.QUEUED,
            TaskStatus.PROVISIONING,
            TaskStatus.IN_PROGRESS,
            TaskStatus.EVALUATING,
            # A BLOCKED review/fix child is non-terminal work parked on a
            # precondition (file_conflict, blocked-precondition halt, …); it must
            # keep the rollup waiting, mirroring the subtask tally below. Pre-fix
            # it was invisible here, so the completion/resume pass ran with a
            # still-active fix child and rebuilt review_loop from scratch — that
            # re-armed ci_waiting and discarded ci_fix_spawned, FAILing the parent
            # on the 1h CI-wait backstop (PR #8746, research 5b250ecd).
            TaskStatus.BLOCKED,
        ):
            continue
        # The staleness skip exists to stop a silently-dead VM (no heartbeat for
        # 15+ min) from permanently wedging rollup. A BLOCKED child never
        # heartbeats by design, so it would be falsely skipped as stale — bypass
        # the check for BLOCKED. Its lifetime is bounded instead by the
        # blocked-escalation machinery in reaper_sweeps, so it cannot wedge
        # rollup permanently.
        #
        # A CI-waiting child (review_loop.ci_waiting) is likewise expected-silent:
        # the orchestrator, not the VM, polls CI from here, so it goes stale while
        # legitimately mid-CI. Skipping it as stale lets the rollup proceed past a
        # still-active fix and spawn a review that then stale-supersedes that fix
        # in _spawn_pipeline_task — the exact churn that tripped the supersession
        # halt on PR #10389. Treat it as active (keep the rollup waiting) like a
        # BLOCKED park; ci_completion + the CI backstop bound a genuinely-stuck
        # one.
        if (
            c.status != TaskStatus.BLOCKED
            and not _task_is_actively_ci_waiting(c)
            and _is_pipeline_task_stale(
                c,
                vm_last_contact=_vm_contact_lookup.get(c.vm_instance_id or ""),
                runner_report_lookup=_runner_report_lookup,
            )
        ):
            logger.warning(
                f"[ROLLUP] Ignoring stale pipeline task {str(c.id)[:8]} "
                f"(status={c.status}, last_heartbeat={c.last_heartbeat}) — not blocking rollup"
            )
            continue
        pipeline_active = True
        break

    # Tally non-IO statuses first so we can decide whether the GitHub-backed
    # PR-merge verification loop is even necessary. ``all_done`` starts True
    # and gets cleared by FAILED/active children below; the verify loop also
    # clears it for any unmerged COMPLETED child (mirrors original semantics).
    all_done = True
    cancelled_count = 0
    active_count = 0
    completed_children: list[Task] = []
    for c in children:
        if c.status == TaskStatus.CANCELLED:
            cancelled_count += 1
            continue
        if c.status in (
            TaskStatus.QUEUED,
            TaskStatus.PROVISIONING,
            TaskStatus.IN_PROGRESS,
            TaskStatus.EVALUATING,
            # A BLOCKED child is non-terminal work parked on a precondition; the
            # auto-unblock sweep is expected to return it to QUEUED. Counting it
            # as active keeps the rollup waiting rather than rolling up an
            # incomplete subtree or miscounting it as a silent failure.
            TaskStatus.BLOCKED,
            # A PARKED child is non-terminal work waiting on an external signal;
            # the wake worker will return it to QUEUED when the source fires.
            # Treat it as active so a completed sibling does not prematurely
            # trigger a rollup that ignores the still-outstanding external job.
            TaskStatus.PARKED,
        ):
            active_count += 1
            all_done = False
            continue
        if c.status == TaskStatus.FAILED:
            all_done = False
            continue
        if c.status == TaskStatus.COMPLETED:
            completed_children.append(c)

    # Don't rollup if there are still active tasks or active review/fix pipeline
    if active_count > 0 or pipeline_active:
        if pipeline_recovery_pending:
            parent.latest_status_message = (
                "Recovering: infrastructure-failed review/fix task is eligible "
                "for auto-resurrection"
            )
            session.add(parent)
            await session.commit()
        elif pipeline_active and (parent.latest_status_message or "").startswith(
            "Blocked:"
        ):
            parent.latest_status_message = (
                "Fix/review pipeline running — waiting for PR to merge"
            )
            session.add(parent)
            await session.commit()
        return

    # ── Phase B3 lock-window break (Site 1: _verify_pr_merged) ──
    # _verify_pr_merged calls get_pull_request + get_github_token_for_task per
    # COMPLETED child. With 5-10 unmerged children x 30s GH the worst-case
    # held-lock window is 150-300s on the parent + N children. Release the
    # parent lock (and the children FOR UPDATE locks, which share this
    # session's transaction) around the verification loop and re-acquire the
    # parent afterwards so concurrent sibling heartbeats and admin writes
    # don't queue behind us. The verification only READS PR state from
    # GitHub and updates in-memory + DB via _verify_pr_merged's own
    # write_session, not the caller's session.
    parent_id_local = parent.id
    pre_verify_snapshot = await _release_parent_lock(parent, session)

    # Run the GitHub round-trips with no DB locks held. _verify_pr_merged
    # mutates the cached child's in-memory `task.status` (PR closed unmerged
    # → FAILED) and `task.context["pr_merged"]`, and commits the persisted
    # write via its own AsyncSessionLocal. The cached `children` list
    # objects retain those in-memory mutations across the lock-window break
    # (expire_on_commit=False), preserving original semantics: a child
    # whose verify-call returned True keeps the verify branch's
    # all_done contribution at True, even if the call also flipped its
    # status to FAILED. Downstream branches (the
    # `failed_count == len(children)` guard at line ~927 and the
    # rollup-prep loop) re-derive failed/completed_merged from `c.status`
    # on the same cached list and handle the post-verify FAILED case.
    for c in completed_children:
        # C3 heartbeat: each _verify_pr_merged is a serial GitHub round-trip;
        # a parent with many unmerged children would otherwise consume the
        # caller's whole liveness budget on this loop alone. No-op unless the
        # reaper sweep passed _mark_reaper_progress.
        if progress_cb is not None:
            progress_cb()
        if not await _verify_pr_merged(c, session):
            all_done = False

    # ── Re-acquire parent lock for downstream mutations ──
    # We deliberately do NOT re-fetch the children rows nor re-derive
    # all_done from the cached list: original behavior was that a verify
    # call returning True meant "child resolved" regardless of any
    # status side-effect (e.g. PR-closed-unmerged → FAILED), and the
    # downstream `completed_count == 0 and failed_count > 0` guard
    # finalizes the parent. Preserving this contract by carrying
    # `all_done` from the verify loop forward to the existing dispatch.
    parent = await _relock_parent(
        session,
        parent_id_local,
        pre_verify_snapshot,
        parent_in_session=parent,
    )
    if parent is None:
        return
    if parent.status != TaskStatus.EVALUATING:
        return

    # All children terminal but not all succeeded — finalize immediately instead of waiting 6 hours
    if not all_done and active_count == 0 and not pipeline_active:
        # Cap-exhausted children (requires_approval=True, PR auto-closed by
        # _handle_cap_exhaustion) are excluded from the unmerged respawn list:
        # respawning hits the cap again every cycle. Post-fix the cap handler
        # transitions them to FAILED with failure_detail=review_cap_exhausted;
        # this filter defensively covers any legacy COMPLETED+requires_approval
        # children that predate the fix.
        unmerged = [
            c
            for c in children
            if c.status == TaskStatus.COMPLETED
            and not (c.context or {}).get("pr_merged")
            and not c.requires_approval
        ]
        failed = [c for c in children if c.status == TaskStatus.FAILED]
        completed_merged = [
            c
            for c in children
            if c.status == TaskStatus.COMPLETED and (c.context or {}).get("pr_merged")
        ]

        if unmerged:
            # Some completed but PRs not yet merged.
            # Check if any are stale (>10min) with no active review — respawn pipeline.
            #
            # ── C2 lock-window break (review-respawn loop) ──
            # ``_spawn_pipeline_task`` commits internally (releasing the parent
            # FOR UPDATE lock acquired/re-acquired above) and performs GitHub
            # network I/O. The prior implementation called it INSIDE this loop
            # while the parent lock was nominally held, so each spawn silently
            # released the parent lock mid-loop and the next iteration mutated
            # ``parent`` / ``c`` against an unlocked row — exactly the
            # held-lock-across-IO / mutate-after-silent-release anti-pattern the
            # merge SUCCESS path already eliminated. Mirror that path here:
            #   Phase A — under the parent lock, decide per child and apply only
            #             DB-local mutations (force pr_merged, respawn-cap skip),
            #             collecting the children that need a review respawn.
            #   Phase B — release the parent lock around the spawns.
            #   Phase C — spawn lock-free (each spawn owns its advisory lock).
            #   Phase D — re-lock parent with snapshot validation, apply the
            #             cap-exhaustion escalations + status message, commit.
            from app.shared_state import resolve_task_branch

            now = _utcnow()
            stale_threshold = timedelta(minutes=10)
            respawned = 0
            respawned_fixes = 0
            # Phase A: decide under lock; collect respawn candidates.
            respawn_candidates: list[dict] = []
            # Batch-fetch PR artifacts for all unmerged children in one query.
            _unmerged_ids = [c.id for c in unmerged]
            _pr_arts_result = await session.execute(
                select(Artifact)
                .where(
                    Artifact.task_id.in_(_unmerged_ids),
                    Artifact.artifact_type == ArtifactType.PULL_REQUEST,
                )
            )
            _pr_art_by_task: dict = {}
            for _art in _pr_arts_result.scalars().all():
                if _art.task_id not in _pr_art_by_task:
                    _pr_art_by_task[_art.task_id] = _art
            # A completed plan with persisted subtasks but zero spawned children
            # is a stranded post-commit parent, not a no-op completion.
            _plan_ids = [
                c.id
                for c in unmerged
                if (c.first_payload or {}).get("type") == "plan"
                and isinstance(c.plan_output, dict)
                and bool(c.plan_output.get("subtasks"))
            ]
            _spawned_child_count: dict = {}
            if _plan_ids:
                _child_counts_result = await session.execute(
                    select(Task.parent_task_id, _func.count(Task.id))
                    .where(Task.parent_task_id.in_(_plan_ids))
                    .group_by(Task.parent_task_id)
                )
                _spawned_child_count = dict(_child_counts_result.all())
            # Batch-fetch all projects needed by unmerged children in one query.
            _unmerged_project_ids = {c.project_id for c in unmerged}
            _projects_result = await session.execute(
                select(Project).where(Project.id.in_(_unmerged_project_ids))
            )
            _project_by_id: dict = {p.id: p for p in _projects_result.scalars().all()}
            for c in unmerged:
                age = (now - c.updated_at).total_seconds() if c.updated_at else 0
                if age < stale_threshold.total_seconds():
                    continue  # Too recent — normal pipeline may still be working
                # Look up PR artifact to get pr_number and branch
                pr_art = _pr_art_by_task.get(c.id)
                if not pr_art or not pr_art.pr_url:
                    if c.id in _plan_ids and not _spawned_child_count.get(c.id, 0):
                        parent.latest_status_message = (
                            "Recovering: completed plan has persisted subtasks "
                            "but no spawned children"
                        )
                        session.add(parent)
                        logger.warning(
                            "[ROLLUP] Child %s is a childless plan with persisted "
                            "subtasks - preserving it for post-commit recovery",
                            str(c.id)[:8],
                        )
                        continue
                    # No PR artifact — _verify_pr_merged should have handled this.
                    # Force-set pr_merged so next cycle picks it up.
                    mark_unblocked_non_merge(c, reason="rollup_no_pr_artifact")
                    session.add(c)
                    logger.info(
                        f"[ROLLUP] Child {str(c.id)[:8]} has no PR artifact — marking as merged"
                    )
                    continue
                try:
                    pr_number = int(pr_art.pr_url.rstrip("/").split("/")[-1])
                except (ValueError, IndexError):
                    # Malformed pr_url (e.g. /commit/<sha>) — cannot parse a PR number.
                    # Force-set pr_merged so the next rollup cycle progresses instead of
                    # hanging forever. ROOT CAUSE 2/3 fixes upstream make this rare.
                    logger.warning(
                        f"[ROLLUP] malformed pr_url {pr_art.pr_url!r} for child "
                        f"{str(c.id)[:8]} — cannot parse PR number; "
                        f"force-setting pr_merged=True so rollup progresses"
                    )
                    mark_unblocked_non_merge(c, reason="malformed_pr_url")
                    session.add(c)
                    continue

                # Circuit-breaker: once a child's review has been respawned
                # MAX_REVIEW_RESPAWNS times without the PR merging, stop looping
                # and escalate to manual approval (mirrors the cap-exhaustion
                # escalation below). Guards against a review that keeps failing
                # fast — e.g. dispatch pre-emption — respawning every cycle.
                respawn_count = _review_respawn_count(c)
                if respawn_count >= MAX_REVIEW_RESPAWNS:
                    # Discretionary escalation: only approval-gate the parent when
                    # require_approval_gates is enabled. With gates off the operator
                    # opted out of manual gating, so escalating here would only let
                    # services.py's 2h approval-gate timeout FAIL the otherwise-healthy
                    # parent — discarding its already-merged siblings. Instead skip the
                    # wedged child (force pr_merged, the same drop-from-unmerged
                    # mechanism used for malformed pr_url above) so the parent converges
                    # via partial rollup, and alert so the dropped subtask is visible.
                    # (Sibling of the cap-exhaustion fix below at ~:1090; mirrors the
                    # review_pipeline repeat-fingerprint/stuck-cycle gating, whose
                    # un-skipped children fall through to this terminalization.)
                    gates_on = _runtime_config.get("require_approval_gates", True)
                    if gates_on and not parent.requires_approval:
                        parent.requires_approval = True
                        logger.warning(
                            f"[ROLLUP] Child {str(c.id)[:8]} PR #{pr_number} — review "
                            f"respawned {respawn_count}x without merging; escalating: "
                            f"marked parent {parent.id} requires_approval"
                        )
                        alert_msg = (
                            f"Rollup blocked: PR #{pr_number} review respawned "
                            f"{respawn_count}x without merging — manual intervention required"
                        )
                    elif not gates_on:
                        mark_unblocked_non_merge(c, reason="review_respawn_cap")
                        c.context = {
                            **(c.context or {}),
                            "_rollup_skipped_reason": "review_respawn_cap",
                        }
                        session.add(c)
                        logger.warning(
                            f"[ROLLUP] Child {str(c.id)[:8]} PR #{pr_number} — review "
                            f"respawned {respawn_count}x without merging and gates are "
                            f"off; skipping (force pr_merged) so parent {parent.id} "
                            f"converges via partial rollup"
                        )
                        alert_msg = (
                            f"Subtask PR #{pr_number} dropped from rollup: review "
                            f"respawned {respawn_count}x without merging "
                            f"(require_approval_gates off — converging via partial rollup)"
                        )
                    else:
                        alert_msg = None
                    if alert_msg:
                        try:
                            await _maybe_create_failure_alert(parent, session, alert_msg)
                        except Exception as _alert_err:
                            logger.warning(
                                f"[ROLLUP] Failed to create respawn-cap alert "
                                f"for parent {parent.id}: {_alert_err}"
                            )
                    continue

                c_payload = c.first_payload
                # Branch must respect the child's task type — agents use
                # `blueprint/refactor-*` for refactor tasks, etc. Hardcoding
                # `task-` here stalled review respawns on refactor children
                # (incident 2026-05-25, PRs #5779/#5812/#5815/#5819 all hit
                # the review cap because the clone targeted blueprint/task-*
                # while the agent had pushed blueprint/refactor-*).
                # For fix children, the real branch lives in
                # review_loop.branch or first_payload.branch — synthesizing
                # blueprint/task-{fix_id} yields a name no agent ever pushed,
                # so respawned reviews would fail at `git clone --branch`.
                project = _project_by_id.get(c.project_id)
                branch = resolve_task_branch(
                    c, _get_project_task_branch_namespace(project)
                )
                try:
                    base_branch = c_payload.get(
                        "base_branch"
                    ) or _get_project_integration_branch(project)
                # Defense-in-depth: the resolver currently fails SAFE and does not
                # raise. This guard exists so a future re-introduction of a raising
                # resolver path holds the rollup instead of aborting the sweep.
                except InvalidProjectBranchConfiguration as exc:
                    await _hold_rollup_for_branch_safety(
                        parent,
                        project,
                        session,
                        (
                            f"Rollup blocked: child {str(c.id)[:8]} resolved to "
                            f"an unsafe integration branch. {exc.detail}"
                        ),
                        issue_code="child_integration_branch_invalid",
                    )
                    return
                original_prompt = c_payload.get("prompt", c.description or "")
                respawn_candidates.append(
                    {
                        "child_id": c.id,
                        "pr_number": pr_number,
                        "branch": branch,
                        "base_branch": base_branch,
                        "original_prompt": original_prompt,
                        "respawn_count": respawn_count,
                    }
                )

            # Phase B: release the parent (+ children) FOR UPDATE lock around the
            # spawns. Capture a snapshot so the re-lock in Phase D detects peer
            # mutation of the rollup state machine during the unlocked window.
            # context_dirty=False: Phase A mutates CHILD contexts (full reassignment
            # via session.add(c) — SQLA tracks those) and at most the parent's
            # ``requires_approval`` column, never ``parent.context`` in place, so no
            # flag_modified on the parent JSONB is needed (avoids the TOAST-write
            # amplification the _release_parent_lock docstring warns about). The
            # requires_approval flip is a column mutation SQLA tracks on its own;
            # the snapshot below captures its post-mutation value, so the Phase D
            # re-lock won't false-defer on our own escalation.
            parent_id_local = parent.id
            unmerged_titles = [c.title[:40] for c in unmerged]
            pre_respawn_snapshot = await _release_parent_lock(parent, session)

            # Phase C: spawn reviews lock-free. Each _spawn_pipeline_task owns its
            # own per-PR advisory lock and commits its own work; no DB lock is held
            # here. Cap-exhaustion escalations that must touch the parent row are
            # deferred to Phase D (the parent is released here). Per-child mutations
            # (respawn-count bump) are persisted on this session and committed by
            # the Phase D relock commit.
            cap_exhausted_prs: list[int] = []
            # Batch-fetch all respawn-candidate child tasks into both the identity
            # map and a local lookup dict (N+1 guard: _spawn_pipeline_task commits
            # internally, expiring the session, and the dict gives us O(1) access
            # without re-fetching).
            child_ids = [cand["child_id"] for cand in respawn_candidates]
            children_rows = (
                (await session.execute(select(Task).where(Task.id.in_(child_ids))))
                .scalars()
                .all()
            )
            children_by_id = {child.id: child for child in children_rows}
            for cand in respawn_candidates:
                child = children_by_id.get(cand["child_id"])
                if child is None:
                    continue
                # A review can never land a PR whose merge blocker is a genuine
                # conflict with its base: the exact-target merge authority holds
                # every approval, and this loop would re-review until the respawn
                # cap escalates (incident 2026-08-06, PR #16741 — sibling child
                # PRs merged serially into the research base, leaving the last
                # one CONFLICTING; repeated approved reviews, zero fix tasks).
                # Classify the live mergeable state first and route dirty PRs to
                # a conflict-fix spawn. Any classification failure falls back to
                # the review respawn, so this path never blocks recovery.
                spawn_type = "review"
                fix_prompt = ""
                _live_pr: dict = {}
                try:
                    _cls_token, _cls_owner, _cls_repo = (
                        await get_github_token_for_task(child.id, session)
                    )
                    _live_pr = await get_pull_request(
                        _cls_token,
                        _cls_owner,
                        _cls_repo,
                        cand["pr_number"],
                        project_id=str(child.project_id),
                    )
                except Exception as _classify_exc:
                    logger.warning(
                        f"[ROLLUP] Could not classify PR #{cand['pr_number']} "
                        f"mergeable state before respawn ({_classify_exc}) — "
                        f"falling back to review respawn"
                    )
                if (_live_pr.get("mergeable_state") or "").lower() == "dirty":
                    spawn_type = "fix"
                    fix_prompt = (
                        f"PR #{cand['pr_number']} merging {cand['branch']} into "
                        f"{cand['base_branch']} has merge conflicts (GitHub "
                        f"mergeable_state=dirty), so review approvals cannot land it.\n\n"
                        f"Original task goal: {cand['original_prompt'][:500]}\n\n"
                        f"Steps to resolve:\n"
                        f"1. Run: git fetch origin {cand['base_branch']}\n"
                        f"2. Run: git rebase origin/{cand['base_branch']}\n"
                        f"3. Resolve conflicts — {cand['base_branch']} is the source of "
                        f"truth. Keep its current state and re-apply only the changes "
                        f"that serve the original task goal on top of it.\n"
                        f"4. Run: git rebase --continue after resolving each file, "
                        f"then git push --force-with-lease"
                    )
                review_task, was_spawned = await _spawn_pipeline_task(
                    child,
                    spawn_type,
                    session,
                    cand["pr_number"],
                    cand["branch"],
                    cand["base_branch"],
                    original_prompt=cand["original_prompt"],
                    fix_prompt=fix_prompt,
                    original_task_id=str(child.id),
                )
                if review_task and review_task.id != child.id:
                    respawned += 1
                    if spawn_type == "fix":
                        respawned_fixes += 1
                    if was_spawned:
                        child.context = {
                            **(child.context or {}),
                            "_review_respawn_count": cand["respawn_count"] + 1,
                        }
                        session.add(child)
                    logger.warning(
                        f"[ROLLUP] Respawned {spawn_type} for stale child {str(child.id)[:8]} "
                        f"PR #{cand['pr_number']} → {spawn_type} {str(review_task.id)[:8]} "
                        f"(respawn {_review_respawn_count(child)}/{MAX_REVIEW_RESPAWNS})"
                    )
                elif review_task is None and child.requires_approval:
                    # Cap exhaustion: _spawn_pipeline_task sets requires_approval=True on
                    # the child. But the child is COMPLETED, so the EVALUATING approval-gate
                    # timeout never fires on it — the parent would otherwise wait the full
                    # evaluating_fail_hours (default 48h) hard timeout. Escalate: set
                    # requires_approval on the parent so the 2-hour approval gate fires,
                    # and raise a failure alert. Gated on require_approval_gates: with gates
                    # off the operator opted out of manual gating, so the parent must converge
                    # via partial rollup rather than be approval-gated and timed out. (This
                    # also guards against the stuck-cycle / repeat-fingerprint paths, which set
                    # requires_approval on the child too, leaking into a parent escalation.)
                    # Deferred to Phase D — the parent row is released here.
                    if _runtime_config.get("require_approval_gates", True):
                        cap_exhausted_prs.append(cand["pr_number"])

            # Phase D: re-lock the parent for the terminal status write +
            # cap-exhaustion escalation. If a peer mutated the rollup state during
            # the unlocked spawn window, defer to the next cycle.
            await session.commit()  # flush Phase C per-child mutations
            parent = await _relock_parent(
                session,
                parent_id_local,
                pre_respawn_snapshot,
                parent_in_session=parent,
            )
            if parent is None:
                return
            if cap_exhausted_prs and not parent.requires_approval:
                parent.requires_approval = True
                for _pr in cap_exhausted_prs:
                    logger.warning(
                        f"[ROLLUP] Child PR #{_pr} hit review-fix cap — "
                        f"escalating: marked parent {parent.id} requires_approval"
                    )
                    try:
                        await _maybe_create_failure_alert(
                            parent,
                            session,
                            f"Rollup blocked: child PR #{_pr} review-fix cap exhausted — manual intervention required",
                        )
                    except Exception as _alert_err:
                        logger.warning(
                            f"[ROLLUP] Failed to create cap-exhaustion alert for parent {parent.id}: {_alert_err}"
                        )

            if respawned:
                _fix_note = (
                    f" ({respawned_fixes} conflict-fix)" if respawned_fixes else ""
                )
                parent.latest_status_message = f"Recovering: respawned {respawned} review(s){_fix_note} for unmerged PR(s) — {', '.join(unmerged_titles)}"
            else:
                parent.latest_status_message = f"Blocked: {len(unmerged)} subtask(s) completed but PR not merged — {', '.join(unmerged_titles)}"
            logger.info(
                f"[ROLLUP] Parent {parent.id} — {len(unmerged)} unmerged, {respawned} reviews respawned"
            )
            session.add(parent)
            await session.commit()
            return

        # No unmerged PRs — only failed/cancelled/merged children remain.
        # Before giving up on failed children, attempt retry via _requeue_parent_task.
        # Cap-exhausted children are terminal: their PR was auto-closed and a
        # fresh retry would hit the same merge conflict that triggered the cap.
        # Exclude them from requeue; count them as failed-for-rollup-decision only.
        retriable_failed = [
            c
            for c in failed
            if (c.context or {}).get("failure_detail") != "review_cap_exhausted"
        ]
        if retriable_failed:
            MAX_PARENT_REQUEUES = 3
            parent_ctx = parent.context or {}
            requeue_count = parent_ctx.get("_requeue_count", 0)
            if requeue_count < MAX_PARENT_REQUEUES:
                # Retry: clone and re-queue failed children; parent returns to EVALUATING.
                # Next rollup cycle will produce a full rollup if retries succeed, or fall
                # through to partial rollup once the requeue limit is exhausted.
                from app.task_lifecycle import _requeue_parent_task

                logger.info(
                    f"[ROLLUP] Parent {parent.id} — {len(retriable_failed)} retriable failed, "
                    f"{len(failed) - len(retriable_failed)} cap-exhausted, "
                    f"{len(completed_merged)} succeeded. Requeuing failed children "
                    f"(attempt {requeue_count + 1}/{MAX_PARENT_REQUEUES}) before partial rollup."
                )
                await _requeue_parent_task(parent, session)
                return
            # Requeue limit exhausted — fall through to partial rollup or full failure.
            logger.warning(
                f"[ROLLUP] Parent {parent.id} — requeue limit reached "
                f"({requeue_count}/{MAX_PARENT_REQUEUES}); proceeding with partial rollup or failure."
            )

        if completed_merged:
            # Some succeeded — proceed with partial rollup of what worked
            logger.warning(
                f"[ROLLUP] Parent {parent.id} — {len(failed)} failed, {len(completed_merged)} succeeded. Rolling up successes."
            )
            all_done = True  # Let rollup proceed with merged children
            # Fall through to rollup logic below
        else:
            # Nothing succeeded — fail the parent immediately
            parent.status = TaskStatus.FAILED
            parent.latest_status_message = (
                f"{len(failed)} of {len(children)} subtask(s) failed, none succeeded"
            )
            from app.failure_classification import (
                worst_child_failure_category,
                persist_failure_category,
            )

            _fc = worst_child_failure_category(failed)
            persist_failure_category(parent, _fc)
            _record_rollup_failure_signals(parent, _fc)
            parent.context = _clear_rollup_transient_state(parent.context)
            session.add(parent)
            await _maybe_create_failure_alert(
                parent, session, f"{len(failed)} subtasks failed, none succeeded"
            )
            await session.commit()
            await broadcast_to_project(
                str(parent.project_id),
                "task_updated",
                {"task_id": str(parent.id), "status": TaskStatus.FAILED},
            )
            logger.error(
                f"[ROLLUP] Parent {parent.id} — all {len(failed)} children failed, marking parent FAILED"
            )
            return

    child_statuses = [c.status for c in children]

    # Edge case: all rollup children CANCELLED — nothing to roll up. Route the
    # terminal verdict through the SAME shared helper the requeue/reaper path
    # uses (``_resolve_terminal_parent_verdict`` /
    # ``_terminate_all_non_genuine_cancelled``) so the two finalizers cannot
    # land the same child set in two different terminal states. The
    # EVALUATING-timeout recovery sweep
    # (recovery_sweeps._check_evaluating_timeouts) reaches this finalizer with
    # NO age gate and usually wins the race against the reaper's ~15-min requeue
    # rescue — so it MUST agree with the requeue verdict instead of diverging
    # (the cross-sweep drift the shared helper exists to prevent). This branch
    # historically hard-coded CANCELLED, which diverged from the requeue path's
    # verdict for an identical child set: FAILED + SCOPE_EMPTY when every child
    # is a non-genuine cancel (superseded / replaced-by-retry / cascade /
    # reassigned / scope-empty), or FAILED + AGENT_TIMEOUT when a genuine
    # cancellation is present. That divergence is exactly what
    # test_parent_terminal_parity locks.
    #
    # ``children`` is the rollup set (review/fix pipeline children already
    # filtered out above) — the same set the shared helper classifies. Every
    # child is CANCELLED in this branch, so no FAILED child can be
    # BLOCKED_PRECONDITION: ``blocked_count`` is 0.
    if children and cancelled_count == len(children):
        from app.task_lifecycle_requeue import (
            _record_parent_failure,
            _resolve_terminal_parent_verdict,
            _terminate_all_non_death_cancelled,
            _terminate_all_non_genuine_cancelled,
        )

        _, verdict_reason = await _resolve_terminal_parent_verdict(
            parent, children, 0
        )

        # Clear rollup transient deferral/retry counters before EITHER terminal
        # finalize commits so they don't leak into a future cycle. The shared
        # helpers commit internally and do NOT clear rollup state or clean branches
        # (the requeue path has no such state), so the rollup path owns both
        # around the helpers here.
        parent.context = _clear_rollup_transient_state(parent.context)

        if verdict_reason == "all_non_death_cancelled":
            # Every cancelled child is a non-death cancellation (co-deferred
            # shelving, requeue replacement, scope-empty no-op): the work is
            # recoverable or already-landed.  Finalize as CANCELLED with NO
            # direction-health penalty and NO failure alert — byte-for-byte the
            # requeue/reaper finalize.  Branch cleanup is still appropriate:
            # co-deferred findings re-surface from the ledger (not the branch),
            # replacement clones create their own branches, scope-empty tasks have
            # nothing to keep.
            await _terminate_all_non_death_cancelled(
                parent, children, session, broadcast_to_project
            )
            await _cleanup_branches(parent, session)
            logger.info(
                f"[ROLLUP] Parent {parent.id} CANCELLED — all "
                f"{len(children)} children non-death cancelled "
                f"(co-deferred / superseded / scope-empty)"
            )
            return

        if verdict_reason == "all_non_genuine_cancelled":
            # Every cancel is non-genuine (superseded / replaced-by-retry /
            # reassigned / scope-empty) but not all are non-death: the work may
            # have landed elsewhere, so this is FAILED + SCOPE_EMPTY with NO
            # direction-health penalty and NO failure alert — byte-for-byte the
            # requeue/reaper finalize. The shared helper persists the category,
            # commits, and broadcasts; we clean up the staging branches after.
            await _terminate_all_non_genuine_cancelled(
                parent, children, session, broadcast_to_project
            )
            await _cleanup_branches(parent, session)
            logger.info(
                f"[ROLLUP] Parent {parent.id} FAILED (SCOPE_EMPTY) — all "
                f"{len(children)} children non-genuine cancelled"
            )
            return

        # At least one GENUINE death (e.g. an operator manually cancelling every
        # child, or a child cancelled under its own review/fix divergence, with
        # no non-death recovery marker). DECISION: finalize FAILED — the shared
        # verdict counts a genuine death as a terminal failure, and both
        # requeue/reaper and rollup paths must agree (test_parent_terminal_parity).
        from app.failure_classification import (
            AGENT_TIMEOUT,
            persist_failure_category,
        )

        parent.status = TaskStatus.FAILED
        parent.latest_status_message = f"All {len(children)} subtasks were cancelled"
        persist_failure_category(parent, AGENT_TIMEOUT)
        _record_parent_failure(parent, AGENT_TIMEOUT)
        session.add(parent)
        await _maybe_create_failure_alert(
            parent, session, parent.latest_status_message
        )
        await session.commit()
        await _cleanup_branches(parent, session)
        await broadcast_to_project(
            str(parent.project_id),
            "task_updated",
            {"task_id": str(parent.id), "status": TaskStatus.FAILED},
        )
        logger.info(
            f"[ROLLUP] Parent {parent.id} FAILED — all {len(children)} children "
            f"cancelled, genuine death present (shared verdict)"
        )
        return

    if all_done and (len(children) - cancelled_count) > 0:
        # All non-cancelled children merged — create rollup PR
        parent_ctx = parent.context or {}
        staging_branch = parent_ctx.get("plan_branch") or parent_ctx.get(
            "research_branch"
        )
        target_branch = parent_ctx.get("parent_base") or parent_ctx.get("original_base")

        failed_children = [c for c in children if c.status == TaskStatus.FAILED]
        failed_count = len(failed_children)
        completed_count = len(children) - cancelled_count - failed_count

        # Guard: _verify_pr_merged may have flipped COMPLETED children to FAILED
        # (e.g. PR closed without merging) after the all_done loop evaluated their
        # status. If every non-cancelled child is now FAILED, there is nothing
        # to roll up — fail the parent instead of creating an empty rollup PR
        # that would trip the zero-diff guard and spuriously mark it COMPLETED.
        if completed_count == 0 and failed_count > 0:
            parent.status = TaskStatus.FAILED
            parent.context = _clear_rollup_transient_state(parent.context)
            parent.latest_status_message = (
                f"{failed_count} of {len(children)} subtask(s) failed, none succeeded"
            )
            from app.failure_classification import (
                worst_child_failure_category,
                persist_failure_category,
            )

            _fc = worst_child_failure_category(failed_children)
            persist_failure_category(parent, _fc)
            _record_rollup_failure_signals(parent, _fc)
            session.add(parent)
            await _maybe_create_failure_alert(
                parent, session, f"{failed_count} subtasks failed, none succeeded"
            )
            await session.commit()
            await broadcast_to_project(
                str(parent.project_id),
                "task_updated",
                {"task_id": str(parent.id), "status": TaskStatus.FAILED},
            )
            logger.error(
                f"[ROLLUP] Parent {parent.id} — all {failed_count} non-cancelled "
                f"children failed after PR verification, marking parent FAILED"
            )
            return

        # ── Structural invariant: only a staging branch's OWNER rolls it up ──
        # A descendant that merely INHERITED an ancestor's research/plan branch
        # (research_completion stamps it into every child; the derivation above
        # reads that inherited value) must not open a SECOND rollup for it — its
        # children's PRs already merged into that shared branch and the owner
        # carries them to base exactly once. The per-task rollup_pr_number guard
        # further down can't see the peer's PR. Ownership is proven off the
        # branch name (mints are {prefix}{id[:8]}); owns_rollup_branch is a
        # DURABLE flag for requeue-clones / mid-recreate tasks whose volatile
        # rollup_pr_number marker was cleared. (Incident: dup rollups
        # #13026/#13035 on research/bdb302f7 — the second wedged and FAILED.)
        if (
            staging_branch
            and not _task_owns_staging_branch(parent, staging_branch)
            and not parent_ctx.get("owns_rollup_branch")
            and not parent_ctx.get("rollup_pr_number")
        ):
            parent.status = TaskStatus.COMPLETED
            parent.context = _clear_rollup_transient_state(parent.context)
            parent.latest_status_message = (
                f"✓ {completed_count} subtask(s) merged into {staging_branch} "
                f"— rollup deferred to that branch's owner (not this task's branch)"
            )
            session.add(parent)
            await session.commit()
            await broadcast_to_project(
                str(parent.project_id),
                "task_updated",
                {"task_id": str(parent.id), "status": TaskStatus.COMPLETED},
            )
            logger.info(
                f"[ROLLUP] Parent {parent.id} completed WITHOUT rollup — staging "
                f"branch '{staging_branch}' is owned by an ancestor; its owner "
                f"carries the {completed_count} merged subtask(s) to base"
            )
            return

        cap_exhausted_children = [
            c
            for c in failed_children
            if (c.context or {}).get("failure_detail") == "review_cap_exhausted"
        ]
        plain_failed_count = failed_count - len(cap_exhausted_children)
        rollup_note = ""
        if cancelled_count > 0:
            rollup_note += f"\n\n*{cancelled_count} subtask(s) were cancelled and excluded from this rollup.*"
        if plain_failed_count:
            rollup_note += f"\n\n*{plain_failed_count} subtask(s) failed and were excluded from this rollup.*"
        if cap_exhausted_children:
            rollup_note += (
                f"\n\n*{len(cap_exhausted_children)} subtask(s) hit the review-fix cap "
                f"(PRs auto-closed, manual review required) and were excluded from this rollup.*"
            )

        if staging_branch and target_branch and staging_branch != target_branch:
            gh_token = owner = repo = None
            project = await session.get(Project, parent.project_id)
            promotion_target = (
                _get_project_promotion_target(project) if project is not None else None
            )
            integration_branch = (
                _get_project_integration_branch(project)
                if project is not None
                else None
            )
            # Block ONLY when the rollup would land fleet work on the promotion
            # target AND that promotion target is genuinely distinct from the
            # project's integration branch. Single-trunk projects (integration ==
            # promotion, e.g. a project whose only branch is 'main') legitimately
            # land every rollup on that shared trunk; holding those would stall
            # the project entirely. The hotfix lane is the explicit exemption for
            # the distinct-branch case.
            #
            # A custom staged rollup root (e.g. a project whose staging family is
            # 'release/' rolling up onto 'release/main') is ALSO exempt: landing
            # on such a staging branch is the intended staged flow even when that
            # branch happens to coincide with promotion_target. Those merges are
            # not held but proceed and are classified downstream by
            # merged_pr_counts_as_integration_landing, which raises the
            # "Merged PR landed on promotion target before integration" alert and
            # leaves pr_merged unset. Holding them here would shadow that path and
            # strand the staged rollup (PR #7064 vs #7105 merge-lane reconciliation).
            if (
                promotion_target
                and target_branch == promotion_target
                and target_branch != integration_branch
                and not _promotion_target_rollup_allowed(parent, target_branch)
                and not is_custom_staged_rollup_root(parent, project, target_branch)
            ):
                await _hold_rollup_for_branch_safety(
                    parent,
                    project,
                    session,
                    (
                        f"Rollup blocked: target branch '{target_branch}' is this "
                        f"project's promotion target. Configure a distinct "
                        "integration branch before landing fleet work."
                    ),
                    issue_code="rollup_target_matches_promotion_target",
                )
                return
            # Track the rollup PR number/url across the try block so the
            # exception handler can rescue a newly-created PR whose marker
            # commit failed (see orphan-PR rescue below).
            rollup_pr_number: int | None = None
            rollup_pr_url: str = ""
            # Initialize before the try so the rescue path in the except
            # handler can name the parent even if `get_github_token_for_task`
            # below raises before the canonical capture at line ~981. Without
            # these, the except handler's `parent_id_local` reference would
            # raise UnboundLocalError, masking the original exception.
            parent_id_local = parent.id
            project_id_local = parent.project_id
            try:
                gh_token, owner, repo = await get_github_token_for_task(
                    parent.id, session
                )

                # ── Phase B3 lock-window break ──
                # The next three GitHub calls (get_open_pull_requests,
                # get_branch_sha x2) are read-only verifications that can
                # collectively hang 5-30s during rate-limit. Release the
                # parent lock so concurrent heartbeats on sibling children
                # don't queue behind us.
                #
                # Audit Phase C4 (M-C1): with `expire_on_commit=False` (see
                # database.py), attributes on `parent` are NOT refreshed after
                # the commit below. Code between this release and the
                # `_relock_parent` call at the bottom of this block MUST treat
                # `parent` as snapshot-stale: only stable identifiers (id,
                # project_id) and the captured `pre_gh_snapshot` are safe to
                # read. Any mutable attribute (status, context, payloads,
                # latest_status_message) is potentially out-of-date and MUST
                # be re-read from the freshly-locked instance returned by
                # `_relock_parent`. The `parent_id_local` / `project_id_local`
                # captures below exist precisely so error logs and follow-up
                # GitHub calls don't accidentally trigger a stale read.
                parent_id_local = parent.id
                project_id_local = parent.project_id
                pre_gh_snapshot = await _release_parent_lock(parent, session)

                # Verify all PRs targeting the staging branch are resolved before rolling up
                from app.github import get_open_pull_requests

                try:
                    open_prs = await get_open_pull_requests(
                        gh_token, owner, repo, staging_branch
                    )
                except HTTPException as api_err:
                    if api_err.status_code == 401:
                        from app.routers.agent import invalidate_github_token_cache

                        invalidate_github_token_cache(str(project_id_local))
                        logger.warning(
                            f"[ROLLUP] Auth failure listing open PRs for {owner}/{repo} — retrying with fresh token"
                        )
                        gh_token, owner, repo = await get_github_token_for_task(
                            parent_id_local, session, force_refresh=True
                        )
                        open_prs = await get_open_pull_requests(
                            gh_token, owner, repo, staging_branch
                        )
                    elif api_err.status_code in (403, 500, 502, 503):
                        logger.warning(
                            f"[ROLLUP] GitHub error {api_err.status_code} listing open PRs — deferring to next cycle"
                        )
                        return
                    else:
                        raise

                # ── Drop orphan PRs left by CANCELLED children ──
                # Attribution uses the authoritative task↔PR link (pr_number recorded
                # in context) rather than branch-name regex, so staging-branch-headed
                # sub-rollup PRs (plan/8hex, research/8hex) are handled correctly.
                # A PR is orphaned only when EVERY recorded owner is terminal-and-dead-
                # ended (no live _replaced_by_task_id chain). PRs with no recorded owner
                # stay blocking — we never auto-close a PR we can't attribute to a task.
                # Race guard: we re-classify the initially-orphaned PRs with fresh DB
                # data immediately before closing — if any owner went live in the
                # interim, we abort the close for that PR and defer to next cycle.
                # The Phase B3 relock below provides additional protection: if the
                # parent was mutated (new task spawned), that cycle aborts entirely.
                if open_prs:
                    try:
                        _child_tasks = (
                            await session.execute(
                                select(Task).where(
                                    Task.parent_task_id == parent_id_local
                                )
                            )
                        ).scalars().all()
                        _live_prs, _orphan_prs = _classify_prs_authoritative(
                            open_prs, _child_tasks
                        )
                        if _orphan_prs:
                            from app.github import close_pull_request

                            # Re-check with fresh data immediately before closing —
                            # a retry/respawn between list-time and close-time could
                            # adopt one of the identified orphan PRs.
                            # populate_existing=True forces the ORM to re-read
                            # already-loaded rows from the DB rather than returning
                            # stale identity-mapped instances — detects concurrent
                            # owner-status changes committed since the first query.
                            _fresh_child_tasks = (
                                await session.execute(
                                    select(Task).where(
                                        Task.parent_task_id == parent_id_local
                                    ).execution_options(populate_existing=True)
                                )
                            ).scalars().all()
                            _still_live, _confirmed_orphans = _classify_prs_authoritative(
                                _orphan_prs, _fresh_child_tasks
                            )
                            _live_prs.extend(_still_live)

                            for _opr in _confirmed_orphans:
                                _onum = _opr.get("number")
                                _oref = (_opr.get("head") or {}).get("ref", "")
                                try:
                                    await close_pull_request(
                                        gh_token,
                                        owner,
                                        repo,
                                        _onum,
                                        project_id=str(project_id_local),
                                    )
                                    logger.info(
                                        f"[ROLLUP] Closed orphan PR #{_onum} (head "
                                        f"'{_oref}') — all owners terminal-and-dead-ended, "
                                        f"was blocking rollup of {parent_id_local}"
                                    )
                                except Exception as _oerr:
                                    logger.warning(
                                        f"[ROLLUP] Failed to close orphan PR #{_onum} "
                                        f"for {parent_id_local}: {_oerr} — excluding "
                                        f"from the blocking set anyway"
                                    )
                            open_prs = _live_prs
                    except Exception as _orphan_exc:
                        # Never let orphan-cleanup crash the rollup cycle; fall
                        # back to the raw open-PR set (original behavior).
                        logger.warning(
                            f"[ROLLUP] Orphan-PR filtering failed for "
                            f"{parent_id_local}: {_orphan_exc} — using raw open-PR set"
                        )

                # Verify target branch exists before creating PR.
                # Distinguish 404 (actually deleted) from transient GitHub errors
                # (5xx, rate-limit) — only fail on confirmed 404.
                try:
                    await get_branch_sha(gh_token, owner, repo, target_branch)
                except HTTPException as branch_err:
                    if branch_err.status_code == 404:
                        raise Exception(
                            f"Target branch '{target_branch}' does not exist — "
                            f"cannot create rollup PR. This indicates a branch was "
                            f"deleted out of order."
                        ) from branch_err
                    # Transient GitHub error — defer to next evaluation cycle
                    logger.warning(
                        f"[ROLLUP] Transient GitHub error checking target branch "
                        f"'{target_branch}': {branch_err.detail} — will retry"
                    )
                    return

                # Verify staging (head) branch exists — if it was deleted
                # (e.g., by a child rollup that found an empty diff and cleaned
                # up, or by GitHub auto-delete after a prior PR merge), check
                # whether all children completed successfully. If so, the work
                # already landed on the target branch and we can complete.
                staging_branch_missing = False
                try:
                    await get_branch_sha(gh_token, owner, repo, staging_branch, live=True)
                except HTTPException as http_err:
                    if http_err.status_code == 404:
                        # Use `parent_id_local` (captured pre-release) instead
                        # of `parent.id` to make the snapshot-stale contract
                        # explicit: see audit-C4 (M-C1) note above.
                        logger.warning(
                            f"[ROLLUP] Staging branch '{staging_branch}' not found (404) for task {parent_id_local}"
                        )
                        staging_branch_missing = True
                    elif http_err.status_code in (401, 403):
                        logger.exception(
                            f"[ROLLUP] Auth error {http_err.status_code} checking staging branch — deferring"
                        )
                        return
                    else:
                        logger.warning(
                            f"[ROLLUP] GitHub {http_err.status_code} checking staging branch — deferring"
                        )
                        return
                except Exception as e:
                    logger.warning(
                        f"[ROLLUP] Error checking staging branch '{staging_branch}': {e} — deferring"
                    )
                    return

                # ── Phase B3: re-acquire the lock before applying mutations ──
                # The three GitHub reads above were performed without the lock.
                # Validate updated_at hasn't changed; if it has, another writer
                # mutated the parent during our window — defer to next cycle.
                parent = await _relock_parent(
                    session,
                    parent_id_local,
                    pre_gh_snapshot,
                    parent_in_session=parent,
                )
                if parent is None:
                    return
                parent_ctx = parent.context or {}
                # Re-validate parent status — a peer may have moved it past EVALUATING.
                if parent.status != TaskStatus.EVALUATING:
                    return

                if open_prs:
                    pr_nums = [f"#{p['number']}" for p in open_prs]
                    parent.latest_status_message = f"Rollup waiting: {len(open_prs)} open PR(s) targeting {staging_branch}: {', '.join(pr_nums)}"
                    session.add(parent)
                    await session.commit()
                    logger.info(
                        f"[ROLLUP] Parent {parent.id} has {len(open_prs)} open PR(s) targeting {staging_branch}: {', '.join(pr_nums)} — waiting"
                    )
                    return

                if staging_branch_missing:
                    # Check if all children completed — if so, work already landed
                    all_children_ok = completed_count > 0 and failed_count == 0
                    if all_children_ok:
                        logger.info(
                            f"[ROLLUP] Staging branch '{staging_branch}' gone but all "
                            f"{completed_count} children completed — work already on {target_branch}"
                        )
                        parent.status = TaskStatus.COMPLETED
                        parent.context = _clear_rollup_transient_state(parent.context)
                        parent.latest_status_message = (
                            f"All {completed_count} subtasks completed with no actionable "
                            f"changes — research produced no new code to merge"
                        )
                        # Note on the direction that this cycle found nothing actionable
                        research_payload = parent.first_payload
                        dir_id = research_payload.get("current_direction_id")
                        if dir_id:
                            def _append_empty_diff_note(directions: list) -> None:
                                for d in directions:
                                    if d.get("id") == dir_id:
                                        notes = d.get("notes") or []
                                        notes.append(
                                            {
                                                "text": "Research cycle completed with no actionable changes — all findings already implemented or not applicable.",
                                                "added_at": _utcnow().isoformat(),
                                                "source": "rollup_empty_diff",
                                            }
                                        )
                                        d["notes"] = notes[-10:]  # Keep last 10
                                        break
                            await with_directions_locked(session, parent.project_id, _append_empty_diff_note, skip_locked=False)
                        session.add(parent)
                        await session.commit()
                        await _cleanup_branches(parent, session)
                        await broadcast_to_project(
                            str(parent.project_id),
                            "task_updated",
                            {"task_id": str(parent.id), "status": TaskStatus.COMPLETED},
                        )
                        return
                    raise Exception(
                        f"Staging branch '{staging_branch}' does not exist and "
                        f"{failed_count} child(ren) failed — cannot create rollup PR"
                    )

                is_target_intermediate = (
                    project is not None and project.is_staging_branch(target_branch)
                )

                # Resume from prior crash — skip PR creation if already done
                existing_pr = parent_ctx.get("rollup_pr_number")
                # ── Duplicate-create guard ──
                # No PR marker yet, but a peer invocation may be mid-create: it
                # stakes ``rollup_pr_creating_at`` under the lock just before the
                # release-window around create_pull_request below. Observing a
                # FRESH claim here means a concurrent worker is already POSTing the
                # rollup PR, so defer rather than POST a duplicate (GitHub's
                # one-open-PR rule is best-effort — incident #7350/#7351). A stale
                # claim (peer crashed mid-create) past the TTL falls through and we
                # re-create.
                if not existing_pr:
                    _claim_ts = parent_ctx.get("rollup_pr_creating_at")
                    if isinstance(_claim_ts, (int, float)) and (
                        time.time() - _claim_ts
                    ) < _ROLLUP_PR_CREATE_CLAIM_TTL_S:
                        logger.info(
                            f"[ROLLUP] Parent {parent_id_local} rollup PR creation "
                            f"already in progress (claim {time.time() - _claim_ts:.0f}s "
                            f"old) — deferring to avoid a duplicate PR"
                        )
                        return
                if existing_pr:
                    # ── Phase B3: release lock around get_pull_request ──
                    pre_existing_pr_snapshot = await _release_parent_lock(
                        parent, session
                    )
                    pr_state: dict | None = None
                    pr_state_err: Exception | None = None
                    try:
                        pr_state = await get_pull_request(
                            gh_token, owner, repo, existing_pr
                        )
                    except Exception as e:
                        pr_state_err = e
                    # Re-acquire and validate
                    parent = await _relock_parent(
                        session,
                        parent_id_local,
                        pre_existing_pr_snapshot,
                        parent_in_session=parent,
                    )
                    if parent is None:
                        return
                    parent_ctx = parent.context or {}
                    if parent.status != TaskStatus.EVALUATING:
                        return
                    # Re-read existing_pr from fresh context (a peer may have cleared it)
                    existing_pr = parent_ctx.get("rollup_pr_number")
                    if pr_state_err is not None:
                        logger.warning(
                            f"[ROLLUP] Could not verify PR #{existing_pr} state: {pr_state_err} — proceeding with existing marker"
                        )
                    elif existing_pr is not None and pr_state is not None:
                        if pr_state.get("merged"):
                            logger.info(
                                f"[ROLLUP] PR #{existing_pr} already merged — classifying landing"
                            )
                            (_landed, _terminal_rollup_merge) = (
                                await _classify_merged_rollup_landing(
                                    parent,
                                    project,
                                    session,
                                    pr_number=existing_pr,
                                    target_branch=target_branch,
                                    source="rollup_management.existing_pr_merged",
                                )
                            )
                            parent.status = (
                                TaskStatus.COMPLETED
                                if _terminal_rollup_merge
                                else TaskStatus.EVALUATING
                            )
                            # Read the POST-classify context so the durable stranded
                            # marker written by _classify_merged_rollup_landing (via
                            # mark_stranded_non_landing) survives this reset; resetting
                            # from the pre-classify parent_ctx snapshot would clobber it.
                            parent.context = {
                                **_clear_rollup_transient_state(parent.context),
                            }
                            if _landed:
                                await corroborate_and_apply_merged_landed_signal(
                                    parent,
                                    project,
                                    session,
                                    base_branch=target_branch,
                                    hotfix_lane=_promotion_target_rollup_allowed(
                                        parent, target_branch
                                    ),
                                    non_merge_reason="merged_pr_non_integration_branch",
                                    # The already-merged PR payload carries its
                                    # merge commit — pin corroboration to that
                                    # immutable tree, not the 60s-TTL tip cache.
                                    merge_commit_sha=pr_state.get(
                                        "merge_commit_sha"
                                    ),
                                )
                                await _propagate_landed_to_rollup_children(
                                    completed_children,
                                    session,
                                    merge_commit_sha=(
                                        parent.context or {}
                                    ).get(LANDED_MERGE_SHA_KEY),
                                )
                                await _propagate_landed_to_staging_merged_descendants(
                                    parent.id, session
                                )
                            if _terminal_rollup_merge:
                                parent.latest_status_message = (
                                    f"Rollup PR #{existing_pr} already merged"
                                )
                            else:
                                parent.latest_status_message = (
                                    f"Rollup PR #{existing_pr} already merged into "
                                    f"{target_branch}; awaiting downstream landing"
                                )
                            session.add(parent)
                            await session.commit()
                            if _landed:
                                from app.direction_outcomes import (
                                    refresh_direction_outcomes_after_merge,
                                )

                                _dir_representatives: dict[str, Task] = {}
                                for _c in completed_children:
                                    _ctx = _c.context or {}
                                    _did = _ctx.get("direction_id")
                                    _rid = _ctx.get("research_task_id")
                                    if (
                                        _did
                                        and _rid
                                        and _did not in _dir_representatives
                                    ):
                                        _dir_representatives[_did] = _c
                                for _rep in _dir_representatives.values():
                                    await refresh_direction_outcomes_after_merge(
                                        _rep, session
                                    )
                                await _record_completed_children_merge_evidence(
                                    completed_children, session
                                )
                            await _cleanup_branches(parent, session)
                            await broadcast_to_project(
                                str(parent.project_id),
                                "task_updated",
                                {
                                    "task_id": str(parent.id),
                                    "status": parent.status,
                                },
                            )
                            return
                        if pr_state.get("state") == "closed":
                            # C4 (4th same-class site): a closed-not-merged
                            # rollup PR may be a TRANSIENT self-close from the
                            # orchestrator's mergeability-reset loop (close+reopen
                            # to force GitHub to recompute mergeable=None — see
                            # orchestrator.py's CI poll). The orchestrator records
                            # its own reset window via review_loop.pending_reopen
                            # on THIS parent (the rollup PR's review_loop, seeded
                            # at the large-rollup → review handoff). Clearing the
                            # rollup_pr_number/rollup_pr_url markers during that
                            # window strands the parent into re-creating a
                            # DUPLICATE rollup PR next cycle. Mirror the other
                            # three handlers: only treat closed as abandoned when
                            # there is NO pending_reopen reset marker; during the
                            # reset window DEFER and leave the markers intact so
                            # the reopen completes and the resume path retries.
                            _parent_loop = parent_ctx.get("review_loop", {}) or {}
                            if _parent_loop.get("pending_reopen"):
                                logger.info(
                                    f"[ROLLUP] PR #{existing_pr} closed-not-merged with "
                                    f"pending_reopen set — transient mergeability-reset, "
                                    f"deferring (markers preserved, will retry next cycle)"
                                )
                                # Leave markers intact; do NOT clear, do NOT create
                                # a duplicate. Return so we don't re-process a
                                # momentarily-closed PR (re-scan / re-spawn review)
                                # this cycle — the orchestrator's reopen lands and
                                # the next rollup sweep resumes with the same PR.
                                parent.latest_status_message = (
                                    f"Rollup PR #{existing_pr} mergeability-reset in "
                                    f"progress (closed/reopen) — deferring"
                                )
                                session.add(parent)
                                await session.commit()
                                return
                            else:
                                logger.warning(
                                    f"[ROLLUP] PR #{existing_pr} was closed without merge — clearing marker, will retry"
                                )
                                parent.context = {
                                    k: v
                                    for k, v in parent_ctx.items()
                                    if k not in ("rollup_pr_number", "rollup_pr_url")
                                }
                                # Durable ownership survives the volatile marker
                                # clear: a requeue-clone on a foreign-named branch
                                # whose rollup_pr_number is being dropped here to
                                # retry with a fresh PR must still be recognized as
                                # the branch owner next cycle — otherwise the
                                # OWNER-gate above would see (foreign branch + no
                                # marker) and silently strand reviewed work
                                # (invariant #2).
                                parent.context["owns_rollup_branch"] = True
                                parent_ctx = parent.context
                                existing_pr = None
                                session.add(parent)
                                await session.commit()

                if existing_pr:
                    rollup_pr_number = existing_pr
                    rollup_pr_url = parent_ctx.get("rollup_pr_url", "")
                    logger.info(
                        f"[ROLLUP] Resuming with existing PR #{rollup_pr_number}"
                    )
                else:
                    _prefix = _get_project_pr_title_prefix(project)
                    pr_title = f"{_prefix}Rollup: {parent.title}"
                    _dir_id = parent.first_payload.get(
                        "current_direction_id"
                    ) or parent_ctx.get("direction_id")
                    _dir_title = parent_ctx.get("direction_title") or parent.title
                    _completed_children = [
                        c for c in children if c.status == TaskStatus.COMPLETED
                    ]
                    pr_body = _build_rollup_pr_body(
                        completed_count,
                        staging_branch,
                        rollup_note,
                        direction_id=str(_dir_id) if _dir_id else None,
                        direction_title=_dir_title,
                        children=_completed_children,
                    )

                    # ── Phase B3: release lock around create_pull_request ──
                    # PR creation is the longest-running call in this function:
                    # 5-15s on the happy path, minutes during rate-limit. The
                    # rollup_pr_number/rollup_pr_url written below are the
                    # marker for crash-recovery, so the post-create re-acquire
                    # MUST validate and persist before any other writer can
                    # observe an empty marker.
                    project_id_str = str(parent.project_id)
                    # Stake a creation claim UNDER the lock before releasing it
                    # around the slow create_pull_request call below. A concurrent
                    # invocation that grabs the lock during the create window will
                    # observe this claim (the guard near ``existing_pr`` above) and
                    # defer instead of POSTing a duplicate rollup PR (#7350/#7351).
                    # context_dirty=True forces the JSONB write so the claim is
                    # actually persisted before the lock is released.
                    parent.context = {
                        **parent_ctx,
                        "rollup_pr_creating_at": time.time(),
                    }
                    parent_ctx = parent.context
                    pre_create_snapshot = await _release_parent_lock(
                        parent, session, context_dirty=True
                    )

                    pr_result = await create_pull_request(
                        gh_token,
                        owner,
                        repo,
                        title=pr_title,
                        head=staging_branch,
                        base=target_branch,
                        body=pr_body,
                        project_id=project_id_str,
                    )

                    parent = await _relock_parent(
                        session,
                        parent_id_local,
                        pre_create_snapshot,
                        parent_in_session=parent,
                    )
                    if parent is None:
                        # Concurrent writer mutated the parent during PR creation.
                        # The orphan-PR rescue path in the except handler will
                        # persist the marker via a fresh session if we raise.
                        # Re-fetch without lock and bail safely.
                        logger.info(
                            f"[ROLLUP] Parent {parent_id_local} mutated during create_pull_request — "
                            f"orphan-PR rescue will persist marker"
                        )
                        rollup_pr_number = pr_result.get("number")
                        rollup_pr_url = pr_result.get("html_url", "")
                        # Trigger orphan-PR rescue path. The sentinel class lets
                        # the outer handler short-circuit past the post-rescue
                        # parent.context manipulation that would NPE here.
                        raise _ParentMutatedDuringPRCreation(
                            f"Parent {parent_id_local} mutated during create_pull_request — deferring"
                        )
                    parent_ctx = parent.context or {}

                    # Empty diff — branches are identical, nothing to merge
                    if pr_result.get("empty_diff"):
                        logger.info(
                            f"[ROLLUP] Empty diff for {staging_branch} → {target_branch} — marking COMPLETED"
                        )
                        parent.status = TaskStatus.COMPLETED
                        parent.context = _clear_rollup_transient_state(parent.context)
                        parent.latest_status_message = f"✓ All {completed_count} subtasks completed — no new changes to merge (fixes already on {target_branch})"
                        try:
                            await delete_branch(gh_token, owner, repo, staging_branch)
                        except Exception as e:
                            logger.warning(
                                f"[ROLLUP] Failed to delete staging branch '{staging_branch}' for task {parent.id}: {e}"
                            )
                        session.add(parent)
                        await session.commit()
                        await _cleanup_branches(parent, session)
                        await broadcast_to_project(
                            str(parent.project_id),
                            "task_updated",
                            {"task_id": str(parent.id), "status": TaskStatus.COMPLETED},
                        )
                        return

                    rollup_pr_url = pr_result.get("html_url", "")
                    rollup_pr_number = pr_result.get("number")
                    logger.info(
                        f"[ROLLUP] Created PR #{rollup_pr_number}: {staging_branch} → {target_branch}"
                    )

                    # Commit marker immediately — if we crash after this point,
                    # restart will find the marker and skip PR creation. Drop the
                    # creation claim now that the durable marker supersedes it.
                    parent.context = {
                        **{
                            k: v
                            for k, v in parent_ctx.items()
                            if k != "rollup_pr_creating_at"
                        },
                        "rollup_pr_number": rollup_pr_number,
                        "rollup_pr_url": rollup_pr_url,
                    }
                    parent_ctx = parent.context  # Update local ref
                    session.add(parent)
                    await session.commit()

                # Re-acquire lock on parent for merge phase (marker commit released it).
                # skip_locked: rollup PR marker is already persisted, so if a concurrent
                # sibling holds the lock we defer safely — reaper sweep will retry within 15 min.
                parent_id = parent.id
                parent = (
                    (
                        await session.execute(
                            select(Task)
                            .where(Task.id == parent_id)
                            .with_for_update(skip_locked=True)
                        )
                    )
                    .scalars()
                    .first()
                )
                if parent is None:
                    logger.info(
                        f"[ROLLUP] Skipped merge phase for parent {parent_id} — lock held, "
                        "reaper sweep will retry"
                    )
                    return
                parent_ctx = parent.context or {}

                # Guard: concurrent sibling may have created a different rollup PR in the gap
                if parent_ctx.get("rollup_pr_number") != rollup_pr_number:
                    logger.info(
                        f"[ROLLUP] Concurrent sibling created rollup PR for parent {parent.id} — yielding"
                    )
                    return

                # Check if any child task was high-risk — if so, require approval on the final merge.
                # Refactor tasks are structural (move code, no behavior change) and should not
                # trigger the high-risk gate even when classified as risk='high' by plan agents.
                # Gate is controlled by runtime setting (default True) — disable for repos
                # that don't need human-in-the-loop approval.
                has_high_risk_child = any(
                    (c.context or {}).get("risk") == "high"
                    for c in children
                    if c.status == TaskStatus.COMPLETED
                    and (c.first_payload or {}).get("type")
                    != "refactor"  # refactors are structural, not behavioral risk
                )
                if (
                    has_high_risk_child
                    and not is_target_intermediate
                    and _runtime_config.get("require_approval_gates", True)
                ):
                    parent.requires_approval = True
                    parent.latest_status_message = (
                        f"All {completed_count} subtasks merged, rollup PR #{rollup_pr_number} awaiting approval "
                        f"(contains high-risk changes)"
                    )
                    parent.context = {
                        **parent_ctx,
                        "rollup_pr_url": rollup_pr_url,
                        "rollup_pr_number": rollup_pr_number,
                    }
                    session.add(parent)
                    # Snapshot status before the alert dispatch: _dispatch_rollup_
                    # approval_alert can poison/roll back the session and expire
                    # `parent` (rollback expires even with expire_on_commit=False),
                    # so a post-commit parent.status read would sync lazy-load in
                    # async ctx → MissingGreenlet.
                    _bcast_status = parent.status
                    await _dispatch_rollup_approval_alert(
                        parent,
                        session,
                        f"Rollup PR #{rollup_pr_number} awaiting approval — contains high-risk changes",
                        pr_number=rollup_pr_number,
                    )
                    await session.commit()
                    await broadcast_to_project(
                        str(parent.project_id),
                        "task_updated",
                        {"task_id": str(parent.id), "status": _bcast_status},
                    )
                    logger.info(
                        f"[ROLLUP] PR #{rollup_pr_number} has high-risk children — requires approval before merge to {target_branch}"
                    )
                    return

                # Auto-merge small rollups on intermediate branches; review large ones
                # Measure by total lines changed (additions + deletions)
                # ── Phase B3: release lock around diff-size check + close on zero-diff ──
                # get_pull_request can hang 5-30s; close_pull_request + delete_branch
                # are also synchronous I/O. Releasing the lock around this cluster
                # lets concurrent heartbeats on sibling children proceed.
                project_id_str = str(parent.project_id)
                pre_diff_snapshot = await _release_parent_lock(parent, session)
                try:
                    pr_detail = await get_pull_request(
                        gh_token,
                        owner,
                        repo,
                        rollup_pr_number,
                        project_id=project_id_str,
                    )
                    lines_changed = pr_detail.get("additions", 0) + pr_detail.get(
                        "deletions", 0
                    )
                except Exception as e:
                    logger.warning(
                        f"[ROLLUP] Failed to fetch PR #{rollup_pr_number} diff size for task {parent_id_local}: {e}"
                    )
                    lines_changed = 999  # assume large if we can't check
                    pr_detail = None

                # Zero-diff guard: squash-merged commits can produce a PR with commits
                # but no file changes. Close before spawning review/fix VMs.
                if pr_detail is not None and lines_changed == 0:
                    logger.info(
                        f"[ROLLUP] PR #{rollup_pr_number} has zero lines changed — closing and classifying landing"
                    )
                    try:
                        await close_pull_request(
                            gh_token,
                            owner,
                            repo,
                            rollup_pr_number,
                            project_id=project_id_str,
                        )
                    except Exception as e:
                        logger.warning(
                            f"[ROLLUP] Failed to close zero-diff PR #{rollup_pr_number}: {e}"
                        )
                    try:
                        await delete_branch(gh_token, owner, repo, staging_branch)
                    except Exception as e:
                        logger.warning(
                            f"[ROLLUP] Failed to delete staging branch '{staging_branch}' for task {parent_id_local}: {e}"
                        )
                    # Re-acquire to apply mutation
                    parent = await _relock_parent(
                        session,
                        parent_id_local,
                        pre_diff_snapshot,
                        parent_in_session=parent,
                    )
                    if parent is None:
                        return
                    (_landed, _terminal_rollup_merge) = (
                        await _classify_merged_rollup_landing(
                            parent,
                            project,
                            session,
                            pr_number=rollup_pr_number,
                            target_branch=target_branch,
                            source="rollup_management.zero_diff_close",
                        )
                    )
                    parent.status = (
                        TaskStatus.COMPLETED
                        if _terminal_rollup_merge
                        else TaskStatus.EVALUATING
                    )
                    if _terminal_rollup_merge:
                        parent.latest_status_message = (
                            f"✓ All {completed_count} subtasks completed — rollup PR "
                            f"#{rollup_pr_number} had no file changes (already on "
                            f"{target_branch})"
                        )
                    else:
                        parent.latest_status_message = (
                            f"✓ All {completed_count} subtasks completed — rollup PR "
                            f"#{rollup_pr_number} had no file changes (already on "
                            f"{target_branch}); awaiting downstream landing"
                        )
                    _cleared = _clear_rollup_transient_state(parent.context)
                    parent.context = {
                        **{
                            k: v
                            for k, v in _cleared.items()
                            if k not in ("rollup_pr_number", "rollup_pr_url")
                        },
                    }
                    if _landed:
                        # No merge commit exists on this path: the zero-diff PR
                        # was CLOSED (its branch was already equivalent to the
                        # target) and credited as a merge-equivalent landing.
                        # Corroboration runs unpinned and, per its own contract,
                        # will not stamp a durable absence from a cached tip.
                        await corroborate_and_apply_merged_landed_signal(
                            parent,
                            project,
                            session,
                            base_branch=target_branch,
                            hotfix_lane=_promotion_target_rollup_allowed(
                                parent, target_branch
                            ),
                            non_merge_reason="merged_pr_non_integration_branch",
                        )
                        await _propagate_landed_to_rollup_children(
                            completed_children,
                            session,
                            merge_commit_sha=(
                                parent.context or {}
                            ).get(LANDED_MERGE_SHA_KEY),
                        )
                        await _propagate_landed_to_staging_merged_descendants(
                            parent.id, session
                        )
                    session.add(parent)
                    await session.commit()
                    if _landed:
                        from app.direction_outcomes import (
                            refresh_direction_outcomes_after_merge,
                        )

                        _dir_representatives: dict[str, Task] = {}
                        for _c in completed_children:
                            _ctx = _c.context or {}
                            _did = _ctx.get("direction_id")
                            _rid = _ctx.get("research_task_id")
                            if _did and _rid and _did not in _dir_representatives:
                                _dir_representatives[_did] = _c
                        for _rep in _dir_representatives.values():
                            await refresh_direction_outcomes_after_merge(
                                _rep, session
                            )
                        await _record_completed_children_merge_evidence(
                            completed_children, session
                        )
                    await _cleanup_branches(parent, session)
                    await broadcast_to_project(
                        str(parent.project_id),
                        "task_updated",
                        {"task_id": str(parent.id), "status": parent.status},
                    )
                    return

                # Non-zero-diff path: re-acquire for downstream mutations.
                parent = await _relock_parent(
                    session,
                    parent_id_local,
                    pre_diff_snapshot,
                    parent_in_session=parent,
                )
                if parent is None:
                    return
                parent_ctx = parent.context or {}
                if parent.status != TaskStatus.EVALUATING:
                    return

                # If diff size is unverifiable (metadata fetch failed), defer the
                # review-spawn decision to the next rollup cycle rather than
                # assuming large and burning VM time on a PR whose size is
                # unknown. Previously unverifiable → lines_changed=999 → review
                # spawned on potentially-empty PRs (2026-04-17 audit: ~15
                # VM-hours/day wasted on the empty-rollup class).
                if pr_detail is None:
                    logger.info(
                        f"[ROLLUP] Deferring review for PR #{rollup_pr_number} — could not verify "
                        f"diff size; will retry next cycle"
                    )
                    parent.latest_status_message = f"Rollup PR #{rollup_pr_number} — diff size unverified, retrying next cycle"
                    session.add(parent)
                    await session.commit()
                    return

                is_small_rollup = lines_changed <= 100
                logger.info(
                    f"[ROLLUP] PR #{rollup_pr_number}: {lines_changed} lines changed, {'auto-merge' if is_small_rollup and is_target_intermediate else 'review'}"
                )
                if is_target_intermediate and is_small_rollup:
                    # ── Phase B3: release lock around security scan + comment + merge ──
                    # The scan inspects PR diffs (multiple GH API calls), the comment
                    # is a write, and the merge is the slowest call of all. Holding
                    # the lock across this cluster previously starved sibling-child
                    # heartbeats during minutes-long rate-limit windows.
                    pre_merge_snapshot = await _release_parent_lock(parent, session)
                    rollup_scan: dict | None = None
                    rollup_scan_err: Exception | None = None
                    try:
                        rollup_scan = await scan_pr_for_injection(
                            gh_token, owner, repo, rollup_pr_number
                        )
                        logger.info(f"[SECURITY] Rollup scan: {rollup_scan['summary']}")
                    except Exception as e:
                        rollup_scan_err = e
                        logger.error(
                            f"[SECURITY] Rollup scan failed for PR #{rollup_pr_number}, blocking: {e}",
                            exc_info=True,
                        )

                    # Validate the combined Alembic migration chain on the rollup
                    # branch before auto-merge. Two child PRs can each add a
                    # migration with the same revision off the same head; each
                    # passes its own CI in isolation but the merged rollup then
                    # has duplicate revisions / multiple heads, breaking
                    # `alembic upgrade head` on the integration branch for every
                    # subsequent task. Fail-safe: fetch/parse failure does not
                    # auto-merge. Runs in this unlocked window (GitHub I/O).
                    alembic_status, alembic_reason = await _validate_rollup_migration_chain(
                        gh_token,
                        owner,
                        repo,
                        rollup_pr_number,
                        target_branch,
                        promotion_target=promotion_target,
                    )

                    # Check assembled-tree module size caps before auto-merge.
                    # Two child PRs can each add lines to a capped file and pass
                    # CI in isolation; the combined rollup may breach the cap.
                    # Auto-bumps and commits the updated JSON so the next CI run
                    # passes. Runs in this unlocked window (GitHub I/O).
                    #
                    # Pre-check the auto-bump budget BEFORE calling preflight so
                    # that we never commit a cap bump when the budget is already
                    # exhausted. preflight_rollup_size_caps returns "hold" when
                    # commit_bump=False and a breach is detected, avoiding the
                    # spurious commit that would otherwise happen before the
                    # budget check in the "bumped" branch below.
                    from app.rollup_size_preflight import preflight_rollup_size_caps
                    _sb_pr_pre = parent_ctx.get("_size_cap_autobump_pr")
                    _sb_count_pre = (
                        int(parent_ctx.get("_size_cap_autobump_count", 0))
                        if _sb_pr_pre == rollup_pr_number
                        else 0
                    )
                    size_cap_status, size_cap_reason = await preflight_rollup_size_caps(
                        gh_token,
                        owner,
                        repo,
                        rollup_pr_number,
                        staging_branch,
                        project_id=str(project_id_local) if project_id_local else None,
                        commit_bump=_sb_count_pre < _SIZE_CAP_AUTOBUMP_CAP,
                    )

                    # ── K7: post the security-scan report HERE, while the parent
                    # lock is still released (2026-07-12 concurrency audit).
                    #
                    # This used to live below, AFTER the re-acquire, behind the
                    # comment "post comment outside lock window is fine; we don't
                    # mutate the parent for this case". That comment was FALSE on
                    # both paths that reach it: a Postgres row lock is released by
                    # TRANSACTION END, not by the absence of a mutation. On the
                    # findings-but-not-blocked path (findings is set independently
                    # of blocked, so medium/low findings give findings != [] with
                    # blocked=False) the last lock op was the `_relock_parent`
                    # below and nothing had committed, so `create_pr_comment` ran
                    # with the parent held FOR UPDATE and the connection
                    # idle-in-transaction. On the blocked+gates-off path the
                    # report was ALSO posted a SECOND time, under the re-taken
                    # lock, duplicating the comment on the PR.
                    #
                    # Posting here touches no ORM state, so nothing needs the
                    # lock. `_report_posted` makes the post exactly-once across
                    # every downstream branch (the blocked paths below only
                    # re-attempt if this attempt FAILED).
                    #
                    # NIT 8: a local flag is only exactly-once WITHIN one
                    # invocation. Because the post now happens BEFORE the
                    # `_relock_parent` below, a LOST re-lock returns early with the
                    # comment already on the PR and nothing recording that — so the
                    # next sweep re-scans and posts the SAME report again, trading
                    # one double-post for a rarer cross-sweep double-post. The fact
                    # of the post is therefore persisted, keyed by PR, in its own
                    # short transaction (below) — exactly-once ACROSS sweeps.
                    _report_posted = (
                        parent_ctx.get(_SCAN_REPORT_POSTED_PR_KEY) == rollup_pr_number
                    )
                    if (
                        rollup_scan is not None
                        and rollup_scan.get("findings")
                        and not _report_posted
                    ):
                        try:
                            await create_pr_comment(
                                gh_token,
                                owner,
                                repo,
                                rollup_pr_number,
                                rollup_scan["report"],
                            )
                            _report_posted = True
                        except Exception as e:
                            logger.warning(
                                f"[ROLLUP] Failed to post scan comment on PR #{rollup_pr_number}: {e}"
                            )
                        if _report_posted:
                            # Durably record it BEFORE the re-lock that may be lost.
                            # Its own bounded transaction — no GitHub I/O inside, so
                            # this re-acquire cannot be held across the network.
                            await _mark_scan_report_posted(
                                session, parent_id_local, rollup_pr_number
                            )

                    # Re-acquire to apply mutations / dispatch alerts.
                    parent = await _relock_parent(
                        session,
                        parent_id_local,
                        pre_merge_snapshot,
                        parent_in_session=parent,
                    )
                    if parent is None:
                        return
                    parent_ctx = parent.context or {}
                    if parent.status != TaskStatus.EVALUATING:
                        return

                    # C5 invariant: every requires_approval-set site below is
                    # gated on require_approval_gates. With gates OFF the operator
                    # has opted out of manual approval, so setting the flag would
                    # only let services.py's 2h approval-gate timeout FAIL the
                    # otherwise-healthy parent and discard already-merged siblings.
                    # The two block classes have different gates-off behaviour:
                    #   - scan-error (transient/non-deterministic): DEFER and retry
                    #     next cycle — the scan may succeed on a re-run; never merge
                    #     unscanned and never permanently block.
                    #   - scan-blocked (deterministic injection finding): fall
                    #     through to merge with a LOUD operator alert — re-blocking
                    #     every cycle would be a permanent strand, and gates-off
                    #     means the operator accepts auto-merge with post-hoc review.
                    _gates_on = _runtime_config.get("require_approval_gates", True)
                    if rollup_scan_err is not None:
                        if _gates_on:
                            parent.requires_approval = True
                            parent.latest_status_message = _sanitize_log(f"Rollup security scan failed — requires manual approval: {type(rollup_scan_err).__name__}")[:500]
                            parent.context = {
                                **parent_ctx,
                                "rollup_pr_url": rollup_pr_url,
                                "rollup_pr_number": rollup_pr_number,
                            }
                            session.add(parent)
                            await _dispatch_rollup_approval_alert(
                                parent,
                                session,
                                f"Rollup PR #{rollup_pr_number} security scan failed — requires manual approval: {type(rollup_scan_err).__name__}",
                                pr_number=rollup_pr_number,
                            )
                            await session.commit()
                            return
                        # Gates off: defer to the next rollup cycle (the scan error
                        # is transient; retrying is correct, blocking is not).
                        logger.warning(
                            f"[ROLLUP] PR #{rollup_pr_number} security scan failed "
                            f"({type(rollup_scan_err).__name__}) and gates are off — "
                            f"deferring to next cycle for re-scan (not blocking)"
                        )
                        parent.latest_status_message = (
                            f"Rollup PR #{rollup_pr_number} — security scan failed "
                            f"({type(rollup_scan_err).__name__}); retrying next cycle"
                        )[:500]
                        parent.context = {
                            **parent_ctx,
                            "rollup_pr_url": rollup_pr_url,
                            "rollup_pr_number": rollup_pr_number,
                        }
                        session.add(parent)
                        await session.commit()
                        return

                    if rollup_scan["blocked"]:
                        if _gates_on:
                            parent.requires_approval = True
                            parent.latest_status_message = (
                                f"Rollup security scan blocked: {rollup_scan['summary']}"
                            )
                            parent.context = {
                                **parent_ctx,
                                "rollup_pr_url": rollup_pr_url,
                                "rollup_pr_number": rollup_pr_number,
                            }
                            # Persist + commit BEFORE the comment (releases lock for I/O).
                            session.add(parent)
                            await _dispatch_rollup_approval_alert(
                                parent,
                                session,
                                f"Rollup PR #{rollup_pr_number} blocked by security scan: {rollup_scan['summary']}",
                                pr_number=rollup_pr_number,
                            )
                            await session.commit()
                            # The report was already posted in the unlocked window
                            # above (K7). Only retry here if that attempt FAILED —
                            # posting unconditionally is what double-posted the
                            # report on the gates-off path.
                            if rollup_scan["findings"] and not _report_posted:
                                try:
                                    await create_pr_comment(
                                        gh_token,
                                        owner,
                                        repo,
                                        rollup_pr_number,
                                        rollup_scan["report"],
                                    )
                                    _report_posted = True
                                except Exception as e:
                                    logger.warning(
                                        f"[ROLLUP] Failed to post scan-block comment on PR #{rollup_pr_number}: {e}"
                                    )
                                if _report_posted:
                                    # Persist it here too, not just the local flag:
                                    # a failed first post + successful RETRY would
                                    # otherwise leave no durable marker, and the next
                                    # sweep would re-scan and post a duplicate.
                                    await _mark_scan_report_posted(
                                        session, parent_id_local, rollup_pr_number
                                    )
                            return
                        # Gates off: do NOT re-block every cycle. Fire a loud
                        # operator alert (so the deterministic finding is visible)
                        # and fall through to the auto-merge below. The scan report
                        # is still posted to the PR for post-hoc review.
                        logger.warning(
                            f"[ROLLUP] PR #{rollup_pr_number} security scan BLOCKED "
                            f"({rollup_scan['summary']}) but gates are off — alerting "
                            f"and proceeding to merge (post-hoc review via PR comment)"
                        )
                        await _dispatch_rollup_approval_alert(
                            parent,
                            session,
                            f"Rollup PR #{rollup_pr_number} security scan flagged "
                            f"({rollup_scan['summary']}) — auto-merging anyway "
                            f"(require_approval_gates off); review post-hoc",
                            pr_number=rollup_pr_number,
                        )
                        await session.commit()
                        # Already posted in the unlocked window above (K7) — only
                        # retry on a failed first attempt. The unconditional post
                        # that used to be here, PLUS the one below, meant this
                        # path posted the SAME report TWICE on every gates-off
                        # blocked rollup (the second time under the re-taken lock).
                        if rollup_scan["findings"] and not _report_posted:
                            try:
                                await create_pr_comment(
                                    gh_token,
                                    owner,
                                    repo,
                                    rollup_pr_number,
                                    rollup_scan["report"],
                                )
                                _report_posted = True
                            except Exception as e:
                                logger.warning(
                                    f"[ROLLUP] Failed to post scan-block comment on PR #{rollup_pr_number}: {e}"
                                )
                            if _report_posted:
                                # Durable marker on the RETRY path too (see above):
                                # a failed first post + successful retry must not
                                # leave the next sweep free to post a duplicate.
                                await _mark_scan_report_posted(
                                    session, parent_id_local, rollup_pr_number
                                )
                        # Re-acquire the parent lock dropped by the alert dispatch
                        # path before falling through to the alembic guard + merge.
                        parent = await _relock_parent(
                            session,
                            parent_id_local,
                            pre_merge_snapshot,
                            parent_in_session=parent,
                        )
                        if parent is None:
                            return
                        parent_ctx = parent.context or {}
                        if parent.status != TaskStatus.EVALUATING:
                            return

                    # (The findings-but-not-blocked scan comment that used to sit
                    # here — under the parent's FOR UPDATE — is now posted in the
                    # unlocked window above. See the K7 note there.)

                    # Alembic combined-chain guard (parent is locked here).
                    if alembic_status == "defer":
                        logger.info(
                            f"[ROLLUP] Deferring auto-merge for PR #{rollup_pr_number} — "
                            f"could not validate Alembic chain: {alembic_reason}"
                        )
                        parent.latest_status_message = (
                            f"Rollup PR #{rollup_pr_number} — Alembic chain unverified, "
                            f"retrying next cycle"
                        )[:500]
                        session.add(parent)
                        await session.commit()
                        return
                    if alembic_status == "block":
                        # Before the operator-alert / agent-fix fallback, try a
                        # bounded automatic reconciliation of a PURE mechanical
                        # collision: a single added migration that simply needs a
                        # new revision number + a rechain onto the current head.
                        # Anything ambiguous (multi-migration, body conflict,
                        # parse failure, non-resolving renumber) yields no plan
                        # and falls through unchanged. The per-PR counter caps
                        # retries so a non-mechanical block never loops.
                        #
                        # K6 (2026-07-12 concurrency audit): the reconcile does a
                        # PR-file plan, a file read and a MULTI-REQUEST GitHub push
                        # (GET ref → GET commit → POST blob xN → POST tree → POST
                        # commit → PATCH ref — none of it coalescable). It used to
                        # run with the parent still held FOR UPDATE from the
                        # re-acquire above — the code even said so ("parent is
                        # locked here") — one continuous idle-in-transaction window
                        # that starts at the re-lock and never resets, racing the
                        # 60s idle_in_transaction_session_timeout (database.py)
                        # against a call that has been measured at ~285s. Use the
                        # SAME release/relock sandwich the (strictly lighter,
                        # read-only) security scan above already uses.
                        _ar_pr = parent_ctx.get("_alembic_autoreconcile_pr")
                        _ar_count = (
                            int(parent_ctx.get("_alembic_autoreconcile_count", 0))
                            if _ar_pr == rollup_pr_number
                            else 0
                        )
                        # MAJOR 4: a peer sweep is mid-push on this PR.
                        # _check_and_trigger_rollup is invoked from at least six
                        # places (github webhook, agent_status, task_lifecycle,
                        # reaper_evaluating_sweeps, reaper_post_orphan_sweep,
                        # recovery_evaluating), so two sweeps CAN both enter this
                        # block. Releasing the parent lock across the push (the
                        # whole point of K6) means the second one is no longer
                        # deferred by ``skip_locked``, and both would push to
                        # ``staging_branch``. ``commit_file_changes`` uses
                        # ``force: False`` (github.py:1194) so the loser gets a 422
                        # rather than clobbering — but it burns GitHub calls and
                        # muddies the counter.
                        #
                        # The stake below doubles as a durable in-flight marker: a
                        # peer that sees a FRESH one defers. It is TTL-bounded so a
                        # sweep that dies mid-push (or loses its re-lock) cannot
                        # wedge the reconcile forever — after the TTL the next sweep
                        # proceeds, and the cap still bounds total attempts.
                        # Deliberately NOT a pg advisory lock: the release-commit
                        # returns the connection to the pool (rollup_locking.py H6),
                        # so a session-scoped advisory lock would ride a pooled
                        # connection into another caller and leak on crash.
                        if _alembic_autoreconcile_in_flight(
                            parent_ctx, rollup_pr_number
                        ):
                            logger.info(
                                f"[ROLLUP] Alembic auto-reconcile already in flight for "
                                f"PR #{rollup_pr_number} — deferring to next cycle "
                                f"rather than issuing a concurrent second push to "
                                f"{staging_branch}"
                            )
                            parent.latest_status_message = (
                                f"Rollup PR #{rollup_pr_number} — Alembic auto-reconcile "
                                f"in flight on a peer sweep, retrying next cycle"
                            )[:500]
                            session.add(parent)
                            await session.commit()
                            return
                        if _ar_count < _ALEMBIC_AUTORECONCILE_CAP:
                            from sqlalchemy.orm.attributes import flag_modified

                            # STAKE THE CLAIM BEFORE RELEASING. The push is a REAL,
                            # irreversible side effect, so the attempt must be
                            # durably recorded before it is attempted. If we
                            # incremented after the push (as the old code did), a
                            # peer-mutation defer on re-lock would discard the
                            # increment while the push had already landed — and the
                            # retry cap could be bypassed indefinitely. Failing the
                            # other way (counting an attempt that never pushed) is
                            # safe and is corrected below.
                            #
                            # context_dirty=True is MANDATORY: _release_parent_lock
                            # only calls flag_modified when told to, so without it
                            # this JSONB write is silently DROPPED.
                            _ar_staked = _ar_count + 1
                            parent.context = {
                                **parent_ctx,
                                "rollup_pr_url": rollup_pr_url,
                                "rollup_pr_number": rollup_pr_number,
                                "_alembic_autoreconcile_pr": rollup_pr_number,
                                "_alembic_autoreconcile_count": _ar_staked,
                                # Doubles as the in-flight marker a peer sweep
                                # defers on (MAJOR 4) — same write, same lock,
                                # same commit as the stake, so it is durable
                                # before the push is attempted.
                                _ALEMBIC_AUTORECONCILE_INFLIGHT_AT_KEY: _utcnow().isoformat(),
                            }
                            _ar_snapshot = await _release_parent_lock(
                                parent, session, context_dirty=True
                            )

                            # ── UNLOCKED: plan + read + multi-request push ──
                            _ar_plan = await _attempt_alembic_autoreconcile(
                                gh_token,
                                owner,
                                repo,
                                rollup_pr_number,
                                staging_branch,
                                target_branch,
                                promotion_target=promotion_target,
                                project_id=(
                                    str(project_id_local) if project_id_local else None
                                ),
                            )

                            parent = await _relock_parent(
                                session,
                                parent_id_local,
                                _ar_snapshot,
                                parent_in_session=parent,
                            )
                            if parent is None:
                                # Peer holds/mutated the parent. The staked counter
                                # is already committed, so a push that DID land is
                                # correctly accounted for; a push that did NOT land
                                # costs at most one of the two attempts. Never a cap
                                # bypass — that asymmetry is the point.
                                return
                            parent_ctx = parent.context or {}
                            if parent.status != TaskStatus.EVALUATING:
                                return

                            if _ar_plan is not None:
                                # The push LANDED. Keep the stake (a real side
                                # effect must stay counted) but clear OUR in-flight
                                # marker so the next cycle's re-validation is not
                                # blocked by it. CAS: only clear it if it is still
                                # ours — a peer that staked in our unlocked window
                                # owns a fresher marker and must keep it.
                                if (
                                    int(
                                        parent_ctx.get(
                                            "_alembic_autoreconcile_count", 0
                                        )
                                    )
                                    == _ar_staked
                                    and parent_ctx.get("_alembic_autoreconcile_pr")
                                    == rollup_pr_number
                                ):
                                    _ar_done_ctx = dict(parent_ctx)
                                    _ar_done_ctx.pop(
                                        _ALEMBIC_AUTORECONCILE_INFLIGHT_AT_KEY, None
                                    )
                                    parent.context = _ar_done_ctx
                                    flag_modified(parent, "context")
                                    parent_ctx = parent.context
                                parent.latest_status_message = (
                                    f"Rollup PR #{rollup_pr_number} — auto-renumbered "
                                    f"Alembic migration {_ar_plan['old_revision']}->"
                                    f"{_ar_plan['new_revision']}; re-validating next cycle"
                                )[:500]
                                session.add(parent)
                                await session.commit()
                                return

                            # No plan / fetch failure / push failure. We now KNOW,
                            # under the lock, that no commit was pushed, so hand the
                            # staked attempt back and fall through to the
                            # operator-alert / re-chain-fix path unchanged. (Only
                            # ever un-staked on this confirmed-no-side-effect path;
                            # a lost re-lock keeps the stake.)
                            #
                            # MAJOR 3 — COMPARE-AND-SWAP, not an absolute write.
                            # This used to write ``_ar_count`` (our PRE-stake
                            # baseline) unconditionally, which is a stale absolute
                            # write that can clobber a PEER's stake and RESET the
                            # cap. The interleaving that breaks it:
                            #   S1 reads 0 → stakes 1 → its push FAILS
                            #   S2 reads 1 → stakes 2 → its push SUCCEEDS
                            #   S1 re-locks (the counter is NOT in
                            #     _capture_rollup_parent_snapshot, so S2's write does
                            #     NOT trip the validator) → _ar_plan is None →
                            #     writes count = 0
                            # A LANDED push is now uncounted and the cap is reset —
                            # so the headline invariant ("the cap can only get
                            # stricter, never bypassed") was FALSE under concurrent
                            # reconcilers.
                            #
                            # Hand the stake back ONLY if our own stake is still on
                            # top: re-read the FRESH count under the re-taken lock
                            # (parent_ctx was rebuilt from the fresh row above) and
                            # decrement only when it is exactly what we left. If a
                            # peer has staked since, its attempt — and any push it
                            # landed — stays counted, and we simply forfeit our own
                            # refund. Forfeiting a refund is always safe; erasing a
                            # peer's landed attempt is not.
                            _ar_fresh = (
                                int(
                                    parent_ctx.get("_alembic_autoreconcile_count", 0)
                                )
                                if parent_ctx.get("_alembic_autoreconcile_pr")
                                == rollup_pr_number
                                else 0
                            )
                            _ar_new_ctx = {
                                **parent_ctx,
                                "_alembic_autoreconcile_pr": rollup_pr_number,
                            }
                            if _ar_fresh == _ar_staked:
                                # Our stake is still on top — no peer intervened, so
                                # the refund is ours to take, and the in-flight
                                # marker we wrote is ours to clear.
                                _ar_new_ctx["_alembic_autoreconcile_count"] = _ar_count
                                _ar_new_ctx.pop(
                                    _ALEMBIC_AUTORECONCILE_INFLIGHT_AT_KEY, None
                                )
                            else:
                                # A peer staked in our unlocked window. Keep ITS
                                # count (a push it landed must stay accounted) and
                                # leave ITS fresher in-flight marker alone — clearing
                                # it would green-light a third concurrent push.
                                logger.info(
                                    f"[ROLLUP] Alembic auto-reconcile for PR "
                                    f"#{rollup_pr_number}: not un-staking (staked "
                                    f"{_ar_staked}, found {_ar_fresh}) — a peer sweep "
                                    f"staked in our unlocked window; keeping its count "
                                    f"so a push it landed stays accounted"
                                )
                            parent.context = _ar_new_ctx
                            flag_modified(parent, "context")
                            parent_ctx = parent.context
                            session.add(parent)
                        # C5: gate the approval flag on require_approval_gates.
                        # A broken combined migration chain is deterministic and
                        # would break `alembic upgrade head` for the whole project,
                        # so we must NOT auto-merge it regardless of the gate — but
                        # with gates OFF we also must NOT set requires_approval
                        # (services.py would 2h-timeout-FAIL the parent). Instead we
                        # always spawn the re-chain fix below; gates-off relies on
                        # that fix landing rather than on manual approval. The merge
                        # is held either way (this branch returns before the merge).
                        _gates_on = _runtime_config.get("require_approval_gates", True)
                        if _gates_on:
                            parent.requires_approval = True
                        parent.latest_status_message = (
                            f"Rollup PR #{rollup_pr_number} blocked — invalid Alembic "
                            f"migration chain: {alembic_reason}"
                        )[:500]
                        parent.context = {
                            **parent_ctx,
                            "rollup_pr_url": rollup_pr_url,
                            "rollup_pr_number": rollup_pr_number,
                        }
                        session.add(parent)
                        await _dispatch_rollup_approval_alert(
                            parent,
                            session,
                            f"Rollup PR #{rollup_pr_number} blocked — invalid Alembic "
                            f"migration chain: {alembic_reason}"
                            + ("" if _gates_on else " (gates off — re-chain fix spawned)"),
                            pr_number=rollup_pr_number,
                        )
                        await session.commit()
                        _alembic_fix_prompt = (
                            f"Rollup PR #{rollup_pr_number} merging {staging_branch} into "
                            f"{target_branch} has an invalid combined Alembic migration "
                            f"chain: {alembic_reason}. Two sibling PRs added migrations "
                            f"that collide. Do NOT modify any already-landed migration "
                            f"(applied history is immutable); instead rename and re-chain "
                            f"your migration to the next free NNNN revision with "
                            f"down_revision pointing at the current head, then bump "
                            f"EXPECTED_SCHEMA_REVISION in backend/app/main.py to match."
                        )
                        await _spawn_pipeline_task(
                            parent,
                            "fix",
                            session,
                            rollup_pr_number,
                            staging_branch,
                            target_branch,
                            fix_prompt=_alembic_fix_prompt,
                            original_task_id=str(parent.id),
                        )
                        logger.warning(
                            f"[ROLLUP] PR #{rollup_pr_number} blocked — invalid Alembic "
                            f"chain ({alembic_reason}) — held for approval, spawned re-chain fix"
                        )
                        return

                    # Module size-cap preflight guard (parent is locked here).
                    if size_cap_status == "defer":
                        logger.info(
                            f"[ROLLUP] Deferring auto-merge for PR #{rollup_pr_number} — "
                            f"could not validate module size caps: {size_cap_reason}"
                        )
                        parent.latest_status_message = (
                            f"Rollup PR #{rollup_pr_number} — size-cap check failed, "
                            f"retrying next cycle"
                        )[:500]
                        session.add(parent)
                        await session.commit()
                        return
                    if size_cap_status == "hold":
                        _gates_on = _runtime_config.get("require_approval_gates", True)
                        if _gates_on:
                            parent.requires_approval = True
                        parent.latest_status_message = (
                            f"Rollup PR #{rollup_pr_number} held — size-cap: "
                            f"{size_cap_reason}"
                        )[:500]
                        parent.context = {
                            **parent_ctx,
                            "rollup_pr_url": rollup_pr_url,
                            "rollup_pr_number": rollup_pr_number,
                        }
                        session.add(parent)
                        await _dispatch_rollup_approval_alert(
                            parent,
                            session,
                            f"Rollup PR #{rollup_pr_number} held — module size-cap: "
                            f"{size_cap_reason}",
                            pr_number=rollup_pr_number,
                        )
                        await session.commit()
                        logger.warning(
                            f"[ROLLUP] PR #{rollup_pr_number} held — size-cap "
                            f"({size_cap_reason})"
                        )
                        return
                    if size_cap_status == "bumped":
                        # Cap was auto-bumped and committed to the staging branch.
                        # Defer one sweep so CI re-runs with the updated JSON.
                        # Budget was already pre-checked before calling preflight
                        # (commit_bump=False when exhausted → "hold" instead of
                        # "bumped"), so _sb_count_pre < _SIZE_CAP_AUTOBUMP_CAP here.
                        parent.context = {
                            **parent_ctx,
                            "rollup_pr_url": rollup_pr_url,
                            "rollup_pr_number": rollup_pr_number,
                            "_size_cap_autobump_pr": rollup_pr_number,
                            "_size_cap_autobump_count": _sb_count_pre + 1,
                        }
                        parent.latest_status_message = (
                            f"Rollup PR #{rollup_pr_number} — auto-bumped module size "
                            f"caps: {size_cap_reason}; re-validating next cycle"
                        )[:500]
                        session.add(parent)
                        await session.commit()
                        logger.info(
                            f"[ROLLUP] PR #{rollup_pr_number} size-cap auto-bump "
                            f"committed (attempt {_sb_count_pre + 1}/{_SIZE_CAP_AUTOBUMP_CAP}): "
                            f"{size_cap_reason}"
                        )
                        return

                    _rollup_checked_head = str(
                        (pr_detail.get("head") or {}).get("sha") or ""
                    )
                    _rollup_checked_base = str(
                        (pr_detail.get("base") or {}).get("sha") or ""
                    )
                    if not _rollup_checked_head or not _rollup_checked_base:
                        parent.latest_status_message = (
                            f"Rollup PR #{rollup_pr_number} merge held — could not "
                            "bind the attempt to an immutable base/head"
                        )[:500]
                        session.add(parent)
                        await session.commit()
                        return

                    # Release lock around the actual merge call (slowest of all).
                    pre_actual_merge_snapshot = await _release_parent_lock(parent, session)
                    # NOTE: A pre-merge "is the PR behind base?" check used to live
                    # here, gated on (target_tip_sha != head_sha). That gate was
                    # always true for any PR with commits (head is by definition
                    # ≥1 commit ahead of base), so it triggered update_branch on
                    # every sweep → GitHub 422 "no new commits on base" → counted
                    # as failure → spawned fix tasks with the false premise that
                    # the PR was behind. The authoritative "behind" check lives
                    # post-merge at the `_ms == "behind"` branch below — GitHub's
                    # mergeable_state distinguishes real behind from
                    # head-ahead-of-base. Removed 2026-05-26 (PR #6096 incident).
                    async def _submit_small_rollup_merge(
                        expected_head_sha: str,
                    ) -> dict:
                        return await merge_pull_request(
                            gh_token,
                            owner,
                            repo,
                            rollup_pr_number,
                            project_id=str(parent.project_id),
                            expected_head_sha=expected_head_sha,
                        )

                    _rollup_authority, merge_result = await attempt_policy_bound_merge(
                        parent,
                        session,
                        merge_call=_submit_small_rollup_merge,
                        gh_token=gh_token,
                        owner=owner,
                        repo=repo,
                        pr_number=rollup_pr_number,
                        branch=staging_branch,
                        base_branch=target_branch,
                        checked_head_sha=_rollup_checked_head,
                        checked_base_sha=_rollup_checked_base,
                        review_not_required_reason=(
                            "small intermediate rollup (<=100 changed lines): "
                            "downstream rollup receives full review"
                        ),
                    )
                    _rollup_merge_held = merge_result is None
                    if merge_result is None:
                        merge_result = {
                            "merged": False,
                            "message": "held by authoritative review gate",
                        }
                    parent = await _relock_parent(
                        session,
                        parent_id_local,
                        pre_actual_merge_snapshot,
                        parent_in_session=parent,
                    )
                    if parent is None:
                        # Concurrent mutation; merge call already completed externally,
                        # next sweep will pick up the merged state.
                        return
                    parent_ctx = parent.context or {}
                    if _rollup_merge_held:
                        session.add(parent)
                        await session.commit()
                        return
                    if merge_result.get("merged"):
                        (_landed, _terminal_rollup_merge) = (
                            await _classify_merged_rollup_landing(
                                parent,
                                project,
                                session,
                                pr_number=rollup_pr_number,
                                target_branch=target_branch,
                                source="rollup_management.auto_merge_success",
                            )
                        )
                        parent.status = (
                            TaskStatus.COMPLETED
                            if _terminal_rollup_merge
                            else TaskStatus.EVALUATING
                        )
                        # Read the POST-classify context: _classify_merged_rollup_landing
                        # may have written the durable stranded marker
                        # (landed_on_integration=False + STRANDED_LANDING_REASON_KEY) into
                        # parent.context via mark_stranded_non_landing. Resetting from the
                        # pre-classify parent_ctx snapshot would clobber that marker before
                        # the commit below. _clear_rollup_transient_state preserves the
                        # landing/stranded keys (they are not transient).
                        parent.context = {
                            **_clear_rollup_transient_state(parent.context),
                            "rollup_pr_url": rollup_pr_url,
                        }
                        if _landed:
                            await corroborate_and_apply_merged_landed_signal(
                                parent,
                                project,
                                session,
                                base_branch=target_branch,
                                hotfix_lane=_promotion_target_rollup_allowed(
                                    parent, target_branch
                                ),
                                non_merge_reason="merged_pr_non_integration_branch",
                                # merge_pull_request returns the exact merge
                                # commit; record it even if the ancestry gate
                                # degraded and could not fetch the PR.
                                merge_commit_sha=merge_result.get("sha"),
                            )
                            await _propagate_landed_to_rollup_children(
                                completed_children,
                                session,
                                merge_commit_sha=(
                                    parent.context or {}
                                ).get(LANDED_MERGE_SHA_KEY),
                            )
                            await _propagate_landed_to_staging_merged_descendants(
                                parent.id, session
                            )
                        if _terminal_rollup_merge:
                            parent.latest_status_message = (
                                f"All {completed_count} subtasks merged, rollup PR auto-merged"
                            )
                        else:
                            parent.latest_status_message = (
                                f"All {completed_count} subtasks merged into "
                                f"{target_branch}; awaiting downstream landing"
                            )
                        session.add(parent)
                        await session.commit()
                        if _landed:
                            from app.direction_outcomes import (
                                refresh_direction_outcomes_after_merge,
                            )

                            _dir_representatives: dict[str, Task] = {}
                            for _c in completed_children:
                                _ctx = _c.context or {}
                                _did = _ctx.get("direction_id")
                                _rid = _ctx.get("research_task_id")
                                if (
                                    _did
                                    and _rid
                                    and _did not in _dir_representatives
                                ):
                                    _dir_representatives[_did] = _c
                            for _rep in _dir_representatives.values():
                                await refresh_direction_outcomes_after_merge(
                                    _rep, session
                                )
                            await _record_completed_children_merge_evidence(
                                completed_children, session
                            )
                        try:
                            await delete_branch(gh_token, owner, repo, staging_branch)
                        except Exception as del_err:
                            logger.warning(
                                f"[ROLLUP] Failed to delete staging branch {staging_branch}: {del_err}"
                            )
                        logger.info(f"[ROLLUP] Auto-merged rollup into {target_branch}")
                        try:
                            from app.notification_dispatch import post_pr_merged_message

                            _direction_id = (parent.context or {}).get("direction_id")
                            _direction_text = "unknown direction"
                            if _direction_id:
                                _proj = await session.get(Project, parent.project_id)
                                if _proj:
                                    for _d in _proj.directions or []:
                                        if str(_d.get("id", "")) == str(_direction_id):
                                            _direction_text = _d.get(
                                                "text", "unknown direction"
                                            )
                                            break
                            await post_pr_merged_message(
                                session,
                                parent.project_id,
                                rollup_pr_number,
                                _direction_text,
                                completed_count,
                                parent.id,
                            )
                        except Exception as _e:
                            logger.warning(
                                f"[NOTIFY] Failed to post PR merged message: {_e}"
                            )
                        try:
                            from app.github import get_pull_request_diff
                            from app.viewport_coverage import (
                                update_viewport_coverage_snapshot_from_diff,
                            )

                            _snap_proj = await session.get(Project, parent.project_id)
                            if _snap_proj is not None:
                                # MAJOR 6: ``session.get`` above AUTOBEGINS a
                                # transaction (the merge-success commit ended the
                                # previous one), so without this commit the
                                # ``get_pull_request_diff`` below — an unbounded
                                # GitHub call — would run idle-in-transaction and
                                # race the 60s idle_in_transaction_session_timeout.
                                # Committing ends that read-only txn; it does NOT
                                # expire ``parent``/``_snap_proj``
                                # (expire_on_commit=False), and the snapshot helper
                                # below opens its own txn as needed.
                                await session.commit()
                                _diff_text = await get_pull_request_diff(
                                    gh_token, owner, repo, rollup_pr_number
                                )
                                await update_viewport_coverage_snapshot_from_diff(
                                    session, _snap_proj, rollup_pr_number, _diff_text
                                )
                        except Exception as _snap_e:
                            logger.warning(
                                f"[ROLLUP] Failed to update viewport_coverage_snapshot: {_snap_e}"
                            )
                        await session.commit()
                        if _terminal_rollup_merge:
                            await _cleanup_branches(parent, session)
                            await broadcast_to_project(
                                str(parent.project_id),
                                "task_updated",
                                {
                                    "task_id": str(parent.id),
                                    "status": TaskStatus.COMPLETED,
                                },
                            )
                        # Rollup merge train: this merge just staleness-bumped
                        # every sibling rollup PR queued against target_branch.
                        # Proactively update-branch them (FIFO by PR created_at)
                        # instead of waiting for lazy CI-poll recovery. Best-
                        # effort — a queue failure never un-merges this rollup.
                        try:
                            await _nudge_sibling_rollup_prs(
                                parent,
                                session,
                                gh_token,
                                owner,
                                repo,
                                target_branch,
                                merged_pr_number=rollup_pr_number,
                            )
                        except Exception as _queue_exc:
                            # Merge train is strictly best-effort post-merge work.
                            logger.warning(
                                f"[MERGE-QUEUE] Sibling nudge after rollup PR "
                                f"#{rollup_pr_number} failed: {_queue_exc}"
                            )
                        return
                    else:
                        # Merge failed (conflicts) — spawn review/fix cycle instead of giving up
                        merge_msg = merge_result.get("message", "unknown")
                        # Before spawning a fix task, check if the PR is merely
                        # behind its base (common when concurrent rollups land on
                        # the same target). If so, update-branch via GitHub API
                        # and let the next rollup sweep re-attempt the merge.
                        _pr_refetch_failed = False
                        try:
                            _pr_detail = await get_pull_request(
                                gh_token,
                                owner,
                                repo,
                                rollup_pr_number,
                                project_id=str(parent.project_id),
                            )
                        except Exception as _pr_exc:
                            _pr_detail = {}
                            _pr_refetch_failed = True
                            logger.debug(
                                f"[ROLLUP] Could not refetch PR #{rollup_pr_number} for behind-check: {_pr_exc}"
                            )
                        # PR refetch retry budget: transient GitHub API errors (500,
                        # 503, network timeouts) shouldn't burn a fix VM. Defer the
                        # sweep on early failures; only fall through to fix-spawn
                        # once consecutive failures exhaust the budget.
                        if _pr_refetch_failed:
                            _refetch_failures = (
                                parent_ctx.get("_refetch_failure_count", 0) + 1
                            )
                            _refetch_budget = _runtime_config.get(
                                "rollup_refetch_failure_retries", 3
                            )
                            if _refetch_failures < _refetch_budget:
                                parent.context = {
                                    **parent_ctx,
                                    "rollup_pr_url": rollup_pr_url,
                                    "rollup_pr_number": rollup_pr_number,
                                    "_refetch_failure_count": _refetch_failures,
                                }
                                parent.latest_status_message = (
                                    f"Rollup PR #{rollup_pr_number} refetch failed after merge attempt "
                                    f"— deferring to next sweep ({_refetch_failures}/{_refetch_budget})"
                                )
                                session.add(parent)
                                await session.commit()
                                logger.info(
                                    f"[ROLLUP] PR #{rollup_pr_number} refetch failed after failed merge "
                                    f"(attempt {_refetch_failures}/{_refetch_budget}) — deferring fix spawn"
                                )
                                return
                            parent_ctx = {
                                k: v
                                for k, v in parent_ctx.items()
                                if k != "_refetch_failure_count"
                            }
                            logger.warning(
                                f"[ROLLUP] PR #{rollup_pr_number} refetch failed {_refetch_failures} consecutive "
                                f"sweeps — falling through to fix spawn"
                            )
                        elif "_refetch_failure_count" in parent_ctx:
                            parent_ctx = {
                                k: v
                                for k, v in parent_ctx.items()
                                if k != "_refetch_failure_count"
                            }
                        # Classify mergeable_state before assuming this is a real
                        # conflict. Only 'dirty' (and 'behind' as fall-through if
                        # update_branch fails) warrant a fix VM. 'blocked' and
                        # 'unstable' resolve when checks/reviews complete; 'unknown'
                        # means GitHub is still computing — all should be retried by
                        # the next rollup sweep instead of burning a fix cycle.
                        _ms = (_pr_detail.get("mergeable_state") or "").lower()
                        # Budget-exhausted refetch-failed: _pr_detail is {} so _ms="".
                        # We already exhausted the retry budget above; treat it as an
                        # un-classifiable failed merge and fall through to fix-spawn
                        # instead of entering the deferral path (which would loop
                        # forever — next sweep would also have an empty _pr_detail
                        # if the API is persistently failing).
                        if _pr_refetch_failed:
                            logger.warning(
                                f"[ROLLUP] PR #{rollup_pr_number} refetch budget exhausted "
                                f"— skipping state-classification, falling through to fix-spawn"
                            )
                        elif _ms == "behind":
                            # Before escalating to update_branch, check if the
                            # ancestor test already considers this PR clean.
                            # mergeable_state="behind" doesn't short-circuit
                            # pr_is_not_behind so is_ancestor_of is consulted.
                            _behind_head_sha = (
                                _pr_detail.get("head", {}).get("sha") or None
                            )
                            _post_not_behind = await pr_is_not_behind_for(
                                gh_token,
                                owner,
                                repo,
                                pr_detail=_pr_detail,
                                base_branch=target_branch,
                                project_id=str(parent.project_id),
                                call_site="rollup_post_merge",
                            )
                            if _post_not_behind is True:
                                logger.info(
                                    f"[ROLLUP] PR #{rollup_pr_number} "
                                    f"mergeable_state=behind but "
                                    f"pr_is_not_behind=True — skipping "
                                    f"update_branch, deferring to next sweep"
                                )
                                return
                            _upd = await update_branch_with_sha_retry(
                                gh_token,
                                owner,
                                repo,
                                rollup_pr_number,
                                expected_head_sha=_behind_head_sha,
                                project_id=str(parent.project_id),
                            )
                            if _upd.get("updated"):
                                # Reset the behind-retry counter — update succeeded, so any
                                # prior transient failures were just that, transient.
                                _new_ctx = {
                                    **parent_ctx,
                                    "rollup_pr_url": rollup_pr_url,
                                    "rollup_pr_number": rollup_pr_number,
                                }
                                _new_ctx.pop("_behind_update_retries", None)
                                parent.context = _new_ctx
                                parent.latest_status_message = (
                                    f"Rollup PR #{rollup_pr_number} was behind {target_branch} — updated branch, "
                                    f"awaiting fresh CI; next rollup sweep will re-attempt merge"
                                )
                                session.add(parent)
                                await session.commit()
                                logger.info(
                                    f"[ROLLUP] PR #{rollup_pr_number} behind {target_branch} — update_branch succeeded, skipped fix spawn"
                                )
                                return
                            if _upd.get("not_behind"):
                                # GitHub's cached mergeable_state was stale — the PR
                                # is not actually behind (no new commits on base).
                                # Treat as a successful no-op: clear any prior retry
                                # counter and defer to the next sweep.
                                _new_ctx = {
                                    **parent_ctx,
                                    "rollup_pr_url": rollup_pr_url,
                                    "rollup_pr_number": rollup_pr_number,
                                }
                                _new_ctx.pop("_behind_update_retries", None)
                                parent.context = _new_ctx
                                parent.latest_status_message = (
                                    f"Rollup PR #{rollup_pr_number} not behind {target_branch} "
                                    f"(no new commits on base) — cached mergeable_state was stale, "
                                    f"deferring to next sweep"
                                )
                                session.add(parent)
                                await session.commit()
                                logger.info(
                                    f"[ROLLUP] PR #{rollup_pr_number} behind {target_branch} — "
                                    f"update_branch reports no new commits on base, deferring to next sweep"
                                )
                                return
                            # update_branch failed — this is a transient GitHub API error,
                            # not a real merge conflict. Defer like the non-dirty path
                            # (counter + status message), retry on the next sweep, and
                            # only fall through to fix spawn once the retry budget is
                            # exhausted. Prevents premature fix VMs from wasting a
                            # review/fix cycle on a PR that just needs another API call.
                            _behind_retries = (
                                parent_ctx.get("_behind_update_retries", 0) + 1
                            )
                            _behind_budget = _runtime_config.get(
                                "rollup_behind_update_retries", 3
                            )
                            _upd_msg = _upd.get("message") or "unknown"
                            if _behind_retries < _behind_budget:
                                parent.context = {
                                    **parent_ctx,
                                    "rollup_pr_url": rollup_pr_url,
                                    "rollup_pr_number": rollup_pr_number,
                                    "_behind_update_retries": _behind_retries,
                                }
                                parent.latest_status_message = (
                                    f"Rollup PR #{rollup_pr_number} behind {target_branch} — update_branch failed "
                                    f"({_upd_msg}); retrying next sweep ({_behind_retries}/{_behind_budget})"
                                )
                                session.add(parent)
                                await session.commit()
                                logger.info(
                                    f"[ROLLUP] PR #{rollup_pr_number} behind {target_branch} — update_branch failed "
                                    f"(attempt {_behind_retries}/{_behind_budget}): {_upd_msg} — deferring to next sweep"
                                )
                                return
                            await _maybe_create_failure_alert(
                                parent,
                                session,
                                f"Rollup PR #{rollup_pr_number} behind {target_branch} — update_branch failed "
                                f"{_behind_retries} consecutive sweeps ({_upd_msg}); falling through to fix spawn",
                            )
                            # Clear the counter so a future behind state starts fresh.
                            parent_ctx = {
                                k: v
                                for k, v in parent_ctx.items()
                                if k != "_behind_update_retries"
                            }
                            logger.warning(
                                f"[ROLLUP] PR #{rollup_pr_number} behind {target_branch} — update_branch failed "
                                f"{_behind_retries} times ({_upd_msg}); falling through to fix spawn"
                            )
                        elif _ms != "dirty":
                            # Non-conflict states: known stalls (blocked/unstable/unknown)
                            # or any unrecognized value GitHub may introduce. All defer
                            # to the next rollup sweep instead of spawning a fix. Track
                            # consecutive deferrals so silent stalls surface with an
                            # alert (and escalate to approval) instead of waiting out
                            # the 6h hard timeout. Refetch-failed+budget-exhausted cases
                            # are handled above and never reach this branch.
                            _ms_key = _ms or "empty"
                            _ms_display = _ms or "missing (field absent)"
                            _is_unrecognized = _ms_key not in (
                                "blocked",
                                "unstable",
                                "unknown",
                                "empty",
                            )
                            # empty/unknown = GitHub hasn't finished computing
                            # mergeability yet (async after approval; typically
                            # resolves in seconds). Re-poll 2-3 times with a
                            # short backoff before entering the deferral path —
                            # in the common case this resolves quickly and we
                            # can retry the merge in the same sweep, avoiding
                            # a sweep-interval delay that can compound into a
                            # CI-backstop timeout if the state stays empty
                            # across many sweeps.
                            if _ms_key in ("empty", "unknown"):
                                import asyncio as _asyncio

                                _repoll_n = int(
                                    _runtime_config.get(
                                        "rollup_mergeable_repoll_attempts", 3
                                    )
                                )
                                _repoll_s = float(
                                    _runtime_config.get(
                                        "rollup_mergeable_repoll_delay_s", 3
                                    )
                                )
                                _repoll_merged = False
                                _repoll_merge_sha: str | None = None
                                _repoll_success_ri: int | None = None
                                _repoll_merge_fail_msg: str | None = None
                                _repoll_authority_held = False

                                # Release the FOR UPDATE lock before I/O to honour
                                # the release-lock-around-GitHub-I/O invariant
                                # (mirrors the pre_actual_merge_snapshot pattern at
                                # ~2972 in the main merge path). parent has no
                                # pending uncommitted mutations at this point.
                                pre_repoll_snapshot = await _release_parent_lock(
                                    parent, session
                                )

                                for _ri in range(1, _repoll_n + 1):
                                    await _asyncio.sleep(_repoll_s)
                                    try:
                                        _rp_detail = await get_pull_request(
                                            gh_token,
                                            owner,
                                            repo,
                                            rollup_pr_number,
                                            project_id=str(parent.project_id),
                                        )
                                        _ms_rp = (
                                            _rp_detail.get("mergeable_state") or ""
                                        ).lower()
                                    except Exception as _rp_exc:
                                        logger.debug(
                                            f"[ROLLUP] PR #{rollup_pr_number} "
                                            f"mergeability re-poll {_ri}/{_repoll_n} "
                                            f"failed: {_rp_exc}"
                                        )
                                        break
                                    logger.info(
                                        f"[ROLLUP] PR #{rollup_pr_number} "
                                        f"mergeability re-poll {_ri}/{_repoll_n}: "
                                        f"mergeable_state="
                                        f"{_ms_rp or 'empty'}"
                                    )
                                    if _ms_rp == "clean":
                                        # Resolved — retry merge inline so the
                                        # task doesn't wait for the next sweep.
                                        _rp_head = str(
                                            (_rp_detail.get("head") or {}).get("sha")
                                            or ""
                                        )
                                        _rp_base = str(
                                            (_rp_detail.get("base") or {}).get("sha")
                                            or ""
                                        )
                                        _validated_base = str(
                                            (pr_detail.get("base") or {}).get("sha")
                                            or ""
                                        )
                                        if (
                                            not _rp_head
                                            or not _rp_base
                                            or _rp_base != _validated_base
                                        ):
                                            _repoll_merge_fail_msg = (
                                                "live base/head no longer matches the "
                                                "validated rollup target"
                                            )
                                            break
                                        async def _submit_repoll_merge(
                                            expected_head_sha: str,
                                        ) -> dict:
                                            return await merge_pull_request(
                                                gh_token,
                                                owner,
                                                repo,
                                                rollup_pr_number,
                                                project_id=project_id_str,
                                                expected_head_sha=expected_head_sha,
                                            )

                                        _rp_authority, _rp_merge = (
                                            await attempt_policy_bound_merge(
                                                parent,
                                                session,
                                                merge_call=_submit_repoll_merge,
                                                gh_token=gh_token,
                                                owner=owner,
                                                repo=repo,
                                                pr_number=rollup_pr_number,
                                                branch=staging_branch,
                                                base_branch=target_branch,
                                                checked_head_sha=_rp_head,
                                                checked_base_sha=_rp_base,
                                                review_not_required_reason=(
                                                    "small intermediate rollup "
                                                    "(<=100 changed lines): downstream "
                                                    "rollup receives full review"
                                                ),
                                            )
                                        )
                                        if _rp_merge is None:
                                            _repoll_authority_held = True
                                            _repoll_merge_fail_msg = (
                                                "held by authoritative review gate"
                                            )
                                        elif _rp_merge.get("merged"):
                                            _repoll_merged = True
                                            _repoll_merge_sha = _rp_merge.get("sha")
                                            _repoll_success_ri = _ri
                                        else:
                                            _repoll_merge_fail_msg = _rp_merge.get(
                                                "message"
                                            )
                                        break
                                    elif _ms_rp and _ms_rp not in (
                                        "empty",
                                        "unknown",
                                    ):
                                        # State changed but not to clean — update
                                        # _ms vars and fall through to the
                                        # appropriate deferral/fix path.
                                        _ms = _ms_rp
                                        _ms_key = _ms_rp
                                        _ms_display = _ms_rp
                                        _is_unrecognized = _ms_key not in (
                                            "blocked",
                                            "unstable",
                                            "unknown",
                                            "empty",
                                        )
                                        break

                                # Relock after the I/O window.
                                parent = await _relock_parent(
                                    session,
                                    parent_id_local,
                                    pre_repoll_snapshot,
                                    parent_in_session=parent,
                                )
                                if parent is None:
                                    return
                                parent_ctx = parent.context or {}

                                if _repoll_authority_held:
                                    session.add(parent)
                                    await session.commit()
                                    return

                                if _repoll_merged:
                                    (
                                        _rp_landed,
                                        _rp_terminal,
                                    ) = await _classify_merged_rollup_landing(
                                        parent,
                                        project,
                                        session,
                                        pr_number=rollup_pr_number,
                                        target_branch=target_branch,
                                        source="rollup_management.repoll_merge",
                                    )
                                    parent.status = (
                                        TaskStatus.COMPLETED
                                        if _rp_terminal
                                        else TaskStatus.EVALUATING
                                    )
                                    parent.context = {
                                        **_clear_rollup_transient_state(
                                            parent.context
                                        ),
                                        "rollup_pr_url": rollup_pr_url,
                                    }
                                    if _rp_landed:
                                        await corroborate_and_apply_merged_landed_signal(
                                            parent,
                                            project,
                                            session,
                                            base_branch=target_branch,
                                            hotfix_lane=_promotion_target_rollup_allowed(
                                                parent, target_branch
                                            ),
                                            # Merge sha from the repoll merge —
                                            # pins corroboration to the merge
                                            # tree, not the cached branch tip.
                                            merge_commit_sha=_repoll_merge_sha,
                                        )
                                        await _propagate_landed_to_rollup_children(
                                            completed_children,
                                            session,
                                            merge_commit_sha=(
                                                parent.context or {}
                                            ).get(LANDED_MERGE_SHA_KEY),
                                        )
                                        await _propagate_landed_to_staging_merged_descendants(
                                            parent.id, session
                                        )
                                    if _rp_terminal:
                                        parent.latest_status_message = (
                                            f"All {completed_count} subtasks merged, "
                                            f"rollup PR auto-merged"
                                        )
                                    else:
                                        parent.latest_status_message = (
                                            f"All {completed_count} subtasks merged "
                                            f"into {target_branch}; awaiting "
                                            f"downstream landing"
                                        )
                                    session.add(parent)
                                    await session.commit()
                                    logger.info(
                                        f"[ROLLUP] PR #{rollup_pr_number} "
                                        f"mergeability resolved to clean on "
                                        f"re-poll {_repoll_success_ri} — merge succeeded"
                                    )
                                    await _do_rollup_post_merge_housekeeping(
                                        parent,
                                        project,
                                        session,
                                        gh_token,
                                        owner,
                                        repo,
                                        rollup_pr_number,
                                        staging_branch,
                                        target_branch,
                                        completed_children,
                                        completed_count,
                                        _rp_landed,
                                        _rp_terminal,
                                    )
                                    return
                                if _repoll_merge_fail_msg is not None:
                                    logger.warning(
                                        f"[ROLLUP] PR #{rollup_pr_number} "
                                        f"re-poll: resolved to clean "
                                        f"but merge failed: {_repoll_merge_fail_msg}"
                                    )
                                # Fall through to the deferral path below with
                                # updated _ms_key / fresh parent_ctx after relock.
                            # Re-poll resolved to dirty; skip deferral and fall through
                            # to the conflict-fix spawn path below.
                            if _ms != "dirty":
                                prev_state = parent_ctx.get("_merge_deferred_state")
                                deferred_count = (
                                    (parent_ctx.get("_merge_deferred_count", 0) + 1)
                                    if prev_state == _ms_key
                                    else 1
                                )
                                # Total-deferral counter: increments on every deferral regardless
                                # of state change. Prevents a flapping PR (blocked↔unstable↔…) from
                                # evading the per-state threshold forever.
                                total_deferrals = (
                                    parent_ctx.get("_merge_total_deferrals", 0) + 1
                                )
                                now = _utcnow()
                                first_deferred_at = (
                                    parent_ctx.get("_merge_first_deferred_at")
                                    or now.isoformat()
                                )
                                try:
                                    from datetime import datetime as _dt

                                    _first_dt = _dt.fromisoformat(first_deferred_at)
                                    elapsed_minutes = (
                                        now - _first_dt
                                    ).total_seconds() / 60.0
                                except (TypeError, ValueError):
                                    elapsed_minutes = 0.0
                                # 'unknown' / empty = GitHub stuck computing mergeability or refetch
                                # failing; unrecognized = GitHub returned a new state we don't know
                                # how to interpret. All escalate faster than 'blocked'/'unstable'
                                # which legitimately wait for checks/reviews.
                                _fast_escalate = _ms_key not in ("blocked", "unstable")
                                alert_threshold = _runtime_config.get(
                                    "rollup_merge_deferred_alert_unknown"
                                    if _fast_escalate
                                    else "rollup_merge_deferred_alert",
                                    5 if _fast_escalate else 10,
                                )
                                approval_threshold = _runtime_config.get(
                                    "rollup_merge_deferred_approval", 30
                                )
                                total_alert_threshold = alert_threshold * 2
                                total_approval_threshold = approval_threshold * 2
                                time_alert_minutes = _runtime_config.get(
                                    "rollup_merge_deferred_time_alert_minutes", 30
                                )
                                time_approval_minutes = _runtime_config.get(
                                    "rollup_merge_deferred_time_approval_minutes", 60
                                )
                                # Per-state alert latch: tracks which states have already
                                # fired an alert so a flapping PR (blocked→unstable→blocked)
                                # doesn't re-fire the per-state alert each time a state
                                # re-crosses its threshold. A new state triggers a fresh
                                # alert; a state that's already alerted stays quiet.
                                alerted_states = set(
                                    parent_ctx.get("_merge_alerted_states") or []
                                )
                                parent.context = {
                                    **parent_ctx,
                                    "rollup_pr_url": rollup_pr_url,
                                    "rollup_pr_number": rollup_pr_number,
                                    "_merge_deferred_count": deferred_count,
                                    "_merge_deferred_state": _ms_key,
                                    "_merge_total_deferrals": total_deferrals,
                                    "_merge_first_deferred_at": first_deferred_at,
                                    "_merge_alerted_states": sorted(alerted_states),
                                }
                                parent.latest_status_message = (
                                    f"Rollup PR #{rollup_pr_number} merge deferred (mergeable_state={_ms_display}, "
                                    f"deferral {deferred_count}/total {total_deferrals}) — next rollup sweep will re-attempt"
                                )
                                # Per-state alert: fires once per state when its counter
                                # crosses the alert threshold. Use `>= threshold AND not
                                # already alerted` so a state that crosses the threshold
                                # in a previous sweep doesn't re-fire this sweep. Alert/
                                # approval dispatch is wrapped in try/except so dispatch
                                # failures don't prevent the counter-persisting commit —
                                # otherwise counters reset each sweep and thresholds never fire.
                                if (
                                    deferred_count >= alert_threshold
                                    and _ms_key not in alerted_states
                                ):
                                    try:
                                        await _maybe_create_failure_alert(
                                            parent,
                                            session,
                                            f"Rollup PR #{rollup_pr_number} stuck in mergeable_state={_ms_display} "
                                            f"for {deferred_count} consecutive sweeps",
                                        )
                                    except Exception as _alert_err:
                                        logger.warning(
                                            f"[ROLLUP] Failed to create per-state deferral alert for PR #{rollup_pr_number}: {_alert_err}"
                                        )
                                    alerted_states.add(_ms_key)
                                    parent.context = {
                                        **parent.context,
                                        "_merge_alerted_states": sorted(alerted_states),
                                    }
                                # Escalation for persistent empty/unknown: after the alert
                                # threshold fires, attempt one forced merge re-try so the PR
                                # doesn't silently wait past the CI-backstop window (~1h).
                                # GitHub returns 405/409 for genuinely unmergeable PRs (safe),
                                # and the rollup_management deferral path resumes on failure.
                                # Only fires once per alerting cycle (latched on
                                # _merge_escalation_attempted) to avoid a retry storm.
                                if (
                                    _fast_escalate
                                    and deferred_count >= alert_threshold
                                    and not parent_ctx.get("_merge_escalation_attempted")
                                ):
                                    parent.context = {
                                        **parent.context,
                                        "_merge_escalation_attempted": True,
                                    }
                                    # Release lock BEFORE merge I/O, committing all
                                    # deferral counters + escalation latch so the next
                                    # sweep sees them even if the merge call crashes.
                                    pre_esc_snapshot = await _release_parent_lock(
                                        parent, session, context_dirty=True
                                    )
                                    try:
                                        _esc_detail = await get_pull_request(
                                            gh_token,
                                            owner,
                                            repo,
                                            rollup_pr_number,
                                            project_id=project_id_str,
                                        )
                                        _esc_head = str(
                                            (_esc_detail.get("head") or {}).get("sha")
                                            or ""
                                        )
                                        _esc_base = str(
                                            (_esc_detail.get("base") or {}).get("sha")
                                            or ""
                                        )
                                        _validated_base = str(
                                            (pr_detail.get("base") or {}).get("sha")
                                            or ""
                                        )
                                        if (
                                            _esc_head
                                            and _esc_base
                                            and _esc_base == _validated_base
                                        ):
                                            async def _submit_escalation_merge(
                                                expected_head_sha: str,
                                            ) -> dict:
                                                return await merge_pull_request(
                                                    gh_token,
                                                    owner,
                                                    repo,
                                                    rollup_pr_number,
                                                    project_id=project_id_str,
                                                    expected_head_sha=expected_head_sha,
                                                )

                                            _esc_authority, _esc_result = (
                                                await attempt_policy_bound_merge(
                                                    parent,
                                                    session,
                                                    merge_call=_submit_escalation_merge,
                                                    gh_token=gh_token,
                                                    owner=owner,
                                                    repo=repo,
                                                    pr_number=rollup_pr_number,
                                                    branch=staging_branch,
                                                    base_branch=target_branch,
                                                    checked_head_sha=_esc_head,
                                                    checked_base_sha=_esc_base,
                                                    review_not_required_reason=(
                                                        "small intermediate rollup "
                                                        "(<=100 changed lines): "
                                                        "downstream rollup receives "
                                                        "full review"
                                                    ),
                                                )
                                            )
                                            if _esc_result is None:
                                                _esc_result = {
                                                    "merged": False,
                                                    "authority_held": True,
                                                    "message": (
                                                        "held by authoritative review gate"
                                                    ),
                                                }
                                        else:
                                            _esc_result = {
                                                "merged": False,
                                                "message": "live base/head changed since validation",
                                            }
                                    except Exception as _esc_exc:
                                        logger.warning(
                                            f"[ROLLUP] PR #{rollup_pr_number} "
                                            f"escalation merge attempt failed: {_esc_exc}"
                                        )
                                        _esc_result = {}
                                    # Relock after I/O window.
                                    parent = await _relock_parent(
                                        session,
                                        parent_id_local,
                                        pre_esc_snapshot,
                                        parent_in_session=parent,
                                    )
                                    if parent is None:
                                        return
                                    parent_ctx = parent.context or {}
                                    if _esc_result.get("authority_held"):
                                        session.add(parent)
                                        await session.commit()
                                        return
                                    if _esc_result.get("merged"):
                                        (
                                            _esc_landed,
                                            _esc_terminal,
                                        ) = await _classify_merged_rollup_landing(
                                            parent,
                                            project,
                                            session,
                                            pr_number=rollup_pr_number,
                                            target_branch=target_branch,
                                            source="rollup_management.escalation_merge",
                                        )
                                        parent.status = (
                                            TaskStatus.COMPLETED
                                            if _esc_terminal
                                            else TaskStatus.EVALUATING
                                        )
                                        parent.context = {
                                            **_clear_rollup_transient_state(
                                                parent.context
                                            ),
                                            "rollup_pr_url": rollup_pr_url,
                                        }
                                        if _esc_landed:
                                            await corroborate_and_apply_merged_landed_signal(
                                                parent,
                                                project,
                                                session,
                                                base_branch=target_branch,
                                                hotfix_lane=_promotion_target_rollup_allowed(
                                                    parent, target_branch
                                                ),
                                                # Merge sha from the escalation
                                                # merge — pins corroboration to
                                                # the immutable merge tree.
                                                merge_commit_sha=_esc_result.get(
                                                    "sha"
                                                ),
                                            )
                                            await _propagate_landed_to_rollup_children(
                                                completed_children,
                                                session,
                                                merge_commit_sha=(
                                                    parent.context or {}
                                                ).get(LANDED_MERGE_SHA_KEY),
                                            )
                                            await _propagate_landed_to_staging_merged_descendants(
                                                parent.id, session
                                            )
                                        if _esc_terminal:
                                            parent.latest_status_message = (
                                                f"All {completed_count} subtasks merged, "
                                                f"rollup PR auto-merged"
                                            )
                                        else:
                                            parent.latest_status_message = (
                                                f"All {completed_count} subtasks merged "
                                                f"into {target_branch}; awaiting "
                                                f"downstream landing"
                                            )
                                        session.add(parent)
                                        await session.commit()
                                        logger.info(
                                            f"[ROLLUP] PR #{rollup_pr_number} "
                                            f"escalation merge succeeded after "
                                            f"{deferred_count} deferred sweeps"
                                        )
                                        await _do_rollup_post_merge_housekeeping(
                                            parent,
                                            project,
                                            session,
                                            gh_token,
                                            owner,
                                            repo,
                                            rollup_pr_number,
                                            staging_branch,
                                            target_branch,
                                            completed_children,
                                            completed_count,
                                            _esc_landed,
                                            _esc_terminal,
                                        )
                                        return
                                    logger.warning(
                                        f"[ROLLUP] PR #{rollup_pr_number} "
                                        f"escalation merge attempt after "
                                        f"{deferred_count} deferred sweeps: "
                                        f"not merged ({_esc_result.get('message')})"
                                    )
                                    # Fall through to total-count alert using fresh
                                    # parent_ctx after the relock above.
                                # Total-count alert: fires once when total crosses threshold,
                                # catching state-flapping PRs that evade the per-state counter.
                                if (
                                    total_deferrals >= total_alert_threshold
                                    and not parent_ctx.get("_merge_total_alerted")
                                ):
                                    try:
                                        await _maybe_create_failure_alert(
                                            parent,
                                            session,
                                            f"Rollup PR #{rollup_pr_number} merge deferred {total_deferrals} times across "
                                            f"states (flapping) — manual investigation required",
                                        )
                                    except Exception as _alert_err:
                                        logger.warning(
                                            f"[ROLLUP] Failed to create total-deferral alert for PR #{rollup_pr_number}: {_alert_err}"
                                        )
                                    parent.context = {
                                        **parent.context,
                                        "_merge_total_alerted": True,
                                    }
                                # Time-based alert: fires once after total deferral time crosses threshold
                                if (
                                    elapsed_minutes >= time_alert_minutes
                                    and not parent_ctx.get("_merge_time_alerted")
                                ):
                                    try:
                                        await _maybe_create_failure_alert(
                                            parent,
                                            session,
                                            f"Rollup PR #{rollup_pr_number} merge has been deferred for "
                                            f"{elapsed_minutes:.0f} minutes — manual investigation required",
                                        )
                                    except Exception as _alert_err:
                                        logger.warning(
                                            f"[ROLLUP] Failed to create time-based deferral alert for PR #{rollup_pr_number}: {_alert_err}"
                                        )
                                    parent.context = {
                                        **parent.context,
                                        "_merge_time_alerted": True,
                                    }
                                # Approval gate: any of per-state, total, or time thresholds can fire it.
                                # Only one approval dispatch — the first condition wins. Discretionary:
                                # only gate when require_approval_gates is on. With gates off the
                                # per-state/total/time *alerts* above still fire (visibility preserved),
                                # but we do NOT approval-gate the parent — otherwise services.py's 2h
                                # approval-gate timeout would FAIL a healthy EVALUATING parent whose
                                # rollup PR is merely deferred. The evaluating_fail_hours backstop
                                # handles a genuinely stuck rollup. (Mirrors the review-cap/respawn-cap
                                # gating elsewhere in this file.)
                                if (
                                    _runtime_config.get("require_approval_gates", True)
                                    and not parent.requires_approval
                                ):
                                    if deferred_count >= approval_threshold:
                                        parent.requires_approval = True
                                        try:
                                            await _dispatch_rollup_approval_alert(
                                                parent,
                                                session,
                                                f"Rollup PR #{rollup_pr_number} stuck in mergeable_state={_ms_display} "
                                                f"for {deferred_count} sweeps — manual investigation required",
                                                pr_number=rollup_pr_number,
                                            )
                                        except Exception as _alert_err:
                                            logger.warning(
                                                f"[ROLLUP] Failed to dispatch per-state approval alert for PR #{rollup_pr_number}: {_alert_err}"
                                            )
                                    elif total_deferrals >= total_approval_threshold:
                                        parent.requires_approval = True
                                        try:
                                            await _dispatch_rollup_approval_alert(
                                                parent,
                                                session,
                                                f"Rollup PR #{rollup_pr_number} deferred {total_deferrals} times across "
                                                f"states — manual investigation required",
                                                pr_number=rollup_pr_number,
                                            )
                                        except Exception as _alert_err:
                                            logger.warning(
                                                f"[ROLLUP] Failed to dispatch total-deferral approval alert for PR #{rollup_pr_number}: {_alert_err}"
                                            )
                                    elif elapsed_minutes >= time_approval_minutes:
                                        parent.requires_approval = True
                                        try:
                                            await _dispatch_rollup_approval_alert(
                                                parent,
                                                session,
                                                f"Rollup PR #{rollup_pr_number} has been deferred for "
                                                f"{elapsed_minutes:.0f} minutes — manual investigation required",
                                                pr_number=rollup_pr_number,
                                            )
                                        except Exception as _alert_err:
                                            logger.warning(
                                                f"[ROLLUP] Failed to dispatch time-based approval alert for PR #{rollup_pr_number}: {_alert_err}"
                                            )
                                session.add(parent)
                                await session.commit()
                                _log = logger.warning if _is_unrecognized else logger.info
                                _log(
                                    f"[ROLLUP] PR #{rollup_pr_number} merge skipped: "
                                    f"{'unrecognized ' if _is_unrecognized else ''}mergeable_state={_ms_display} "
                                    f"(deferral {deferred_count}/total {total_deferrals}/{elapsed_minutes:.0f}min; "
                                    f"alert@{alert_threshold}/{total_alert_threshold}/{time_alert_minutes}min, "
                                    f"approval@{approval_threshold}/{total_approval_threshold}/{time_approval_minutes}min) "
                                    f"— deferring to next rollup sweep, no fix spawn"
                                )
                                return
                        # Merge-queue double-fire guard: a proactive update-
                        # branch from the rollup merge train landed on this PR
                        # moments ago. GitHub's mergeable computation lags the
                        # new merge commit, so a 'dirty' read inside the
                        # freshness window is likely stale — defer the fix
                        # spawn to the next sweep instead of double-firing an
                        # agent against the queue's in-flight update. A
                        # GENUINE conflict outlives the window and falls
                        # through to the unchanged fix path below.
                        if _ms == "dirty" and _merge_queue_update_in_flight(parent_ctx):
                            parent.context = {
                                **parent_ctx,
                                "rollup_pr_url": rollup_pr_url,
                                "rollup_pr_number": rollup_pr_number,
                            }
                            parent.latest_status_message = (
                                f"Rollup PR #{rollup_pr_number} reported dirty shortly "
                                f"after a merge-queue branch update — deferring "
                                f"conflict-fix spawn to next sweep"
                            )
                            session.add(parent)
                            await session.commit()
                            logger.info(
                                f"[ROLLUP] PR #{rollup_pr_number} dirty within the "
                                f"merge-queue update window — deferring fix spawn"
                            )
                            return
                        # Observability: check if concurrent rollups may explain this conflict
                        try:
                            # R4-W2 (M3): ``has_key`` uses the GIN ``?``
                            # operator and matches ``ix_task_context_gin``;
                            # the prior ``as_string().is_not(None)`` form
                            # bypassed the index. ``?`` is Postgres-only, so
                            # branch on the resolved dialect and fall back to
                            # the portable ``json_extract`` form on SQLite.
                            if resolve_dialect_name(session) == "postgresql":
                                _rollup_pr_present = Task.context.has_key(
                                    "rollup_pr_url"
                                )
                            else:
                                _rollup_pr_present = _func.json_extract(
                                    Task.context, "$.rollup_pr_url"
                                ).isnot(None)
                            recent_rollups = (
                                await session.execute(
                                    select(_func.count())
                                    .select_from(Task)
                                    .where(
                                        Task.status == TaskStatus.COMPLETED,
                                        Task.id != parent.id,
                                        Task.updated_at
                                        >= _utcnow() - timedelta(hours=1),
                                        _rollup_pr_present,
                                    )
                                )
                            ).scalar() or 0
                            if recent_rollups > 0:
                                logger.warning(
                                    f"[ROLLUP] Conflict on PR #{rollup_pr_number} may be stale-window: "
                                    f"{recent_rollups} other rollup(s) merged to {target_branch} in the last hour"
                                )
                        except Exception as e:
                            logger.warning(
                                f"[ROLLUP] Failed to query concurrent rollup count for PR #{rollup_pr_number} (task {parent.id}): {e}"
                            )
                        parent.context = {
                            **parent_ctx,
                            "rollup_pr_url": rollup_pr_url,
                            "rollup_pr_number": rollup_pr_number,
                            "_is_rollup_review": True,
                        }
                        parent.latest_status_message = f"Rollup PR #{rollup_pr_number} merge blocked: {merge_msg} — spawning fix task"
                        try:
                            pr_files = await get_pr_files(
                                gh_token, owner, repo, rollup_pr_number
                            )
                            files_list = "\n".join(
                                f"  - {f['filename']} ({f['status']})" for f in pr_files
                            )
                        except Exception as e:
                            logger.warning(
                                f"[ROLLUP] Failed to fetch PR #{rollup_pr_number} file list for task {parent.id}: {e}"
                            )
                            files_list = "  (could not fetch file list)"
                            pr_files = []
                        has_migrations = any(
                            (f.get("filename") or "").startswith(
                                "backend/alembic/versions/"
                            )
                            for f in pr_files
                        )
                        migration_guidance = ""
                        if has_migrations:
                            migration_guidance = (
                                "\n\nMigration chain guidance:\n"
                                "This rollup contains database migrations from multiple subtasks. After resolving conflicts:\n"
                                "- The migration chain in backend/alembic/versions/ must be linear: each migration's `down_revision` "
                                "must point to the revision of the previous migration in the chain.\n"
                                "- All revision numbers (e.g., 0039, 0040, ...) must be unique and sequential — no gaps, no duplicates.\n"
                                "- Renumber/rewrite migration files as needed so the chain is contiguous from the latest pre-rollup revision.\n"
                                "- Update `EXPECTED_SCHEMA_REVISION` in backend/app/main.py to match the final (highest) migration's revision."
                            )
                        parent_goal = (parent.description or parent.title or "")[:500]
                        if _ms == "behind":
                            # Budget-exhausted behind path: no real conflicts, just a stale
                            # GitHub flag. The no-op guidance is critical — if the PR head is
                            # already ancestor of base, git rebase will be a no-op and the
                            # fix agent must not treat that as failure (PR #6153 incident).
                            fix_prompt = (
                                f"Rollup PR #{rollup_pr_number} merging {staging_branch} into {target_branch} "
                                f"is behind the target branch and the GitHub update-branch API failed across "
                                f"the retry budget ({_upd_msg}). Rebase manually onto origin/{target_branch}:\n\n"
                                f"1. git fetch origin {target_branch}\n"
                                f"2. git rebase origin/{target_branch}\n"
                                f"3. If no conflicts and no commits were replayed, the PR is already "
                                f"up-to-date — GitHub's mergeability flag is just stale. Report no "
                                f"changes needed (don't force-push); retries will clear it.\n"
                                f"4. Otherwise resolve conflicts and git push --force-with-lease."
                            )
                        else:
                            fix_prompt = (
                                f"This rollup PR (#{rollup_pr_number}) merging {staging_branch} into {target_branch} has conflicts.\n"
                                f"Error: {merge_msg}\n\n"
                                f"Rollup goal: {parent_goal}\n\n"
                                f"Files in this PR:\n{files_list}\n\n"
                                f"Steps to resolve:\n"
                                f"1. Run: git fetch origin {target_branch}\n"
                                f"2. Run: git rebase origin/{target_branch}\n"
                                f"3. Resolve any conflicts — {target_branch} is the source of truth. Keep its refactors and restructuring.\n"
                                f"   Re-apply only the changes that serve the rollup goal (described above) on top of {target_branch}'s current state.\n"
                                f"   If files were heavily rewritten on {target_branch}, adapt the rollup changes to the new structure.\n"
                                f"4. Run: git rebase --continue after resolving each file"
                                f"{migration_guidance}"
                            )
                        fix_task, _ = await _spawn_pipeline_task(
                            parent,
                            "fix",
                            session,
                            rollup_pr_number,
                            staging_branch,
                            target_branch,
                            fix_prompt=fix_prompt,
                            original_task_id=str(parent.id),
                        )
                        if fix_task:
                            logger.error(
                                f"[ROLLUP] Merge blocked for rollup PR #{rollup_pr_number}, spawned fix → task {fix_task.id}"
                            )
                        else:
                            from app.review_pipeline import (
                                SPAWN_REASON_KEY,
                                SPAWN_REASON_DEFERRED,
                                SPAWN_REASON_DEDUP,
                                SPAWN_REASON_ALREADY_MERGED,
                                SPAWN_REASON_PR_CLOSED_UNMERGED,
                            )

                            spawn_reason = (parent.context or {}).get(SPAWN_REASON_KEY)
                            if spawn_reason in (
                                SPAWN_REASON_DEFERRED,
                                SPAWN_REASON_DEDUP,
                            ):
                                # Another review/fix is in flight on this PR — leave
                                # parent EVALUATING; next rollup sweep retries once the
                                # in-flight task finishes. latest_status_message was
                                # already set by _spawn_pipeline_task.
                                logger.info(
                                    f"[ROLLUP] Fix spawn deferred for rollup PR #{rollup_pr_number} "
                                    f"({spawn_reason}) — leaving parent EVALUATING, next sweep retries"
                                )
                            elif spawn_reason == SPAWN_REASON_ALREADY_MERGED:
                                # PR merged while we were preparing the fix — no-op.
                                logger.info(
                                    f"[ROLLUP] PR #{rollup_pr_number} merged mid-flight — no fix needed"
                                )
                            elif spawn_reason == SPAWN_REASON_PR_CLOSED_UNMERGED:
                                # PR closed without merging — no fix needed.
                                logger.info(
                                    f"[ROLLUP] PR #{rollup_pr_number} closed without merge — no fix needed"
                                )
                            else:
                                # cap / stuck / unknown — terminal.
                                parent.status = TaskStatus.FAILED
                                parent.context = _clear_rollup_transient_state(
                                    parent.context
                                )
                                parent.latest_status_message = (
                                    f"Rollup PR #{rollup_pr_number} merge blocked, fix not spawned "
                                    f"({spawn_reason or 'unknown'})"
                                )
                                from app.failure_classification import (
                                    classify_failure,
                                    persist_failure_category,
                                )

                                _fc = classify_failure(parent)
                                persist_failure_category(parent, _fc)
                                _record_rollup_failure_signals(parent, _fc)
                                await _maybe_create_failure_alert(
                                    parent,
                                    session,
                                    f"Rollup PR #{rollup_pr_number} merge blocked — fix iteration cap exhausted, manual intervention required",
                                )
                                logger.warning(
                                    f"[ROLLUP] WARNING: Rollup fix spawn returned None for PR #{rollup_pr_number} (reason={spawn_reason})"
                                )
                            # NOTE: Do NOT delete staging_branch here — it contains
                            # all merged child work and requeueing should be possible.
                else:
                    # Large rollup → review path. Two flavours:
                    #   • integration-branch target (automain/main): ci.yml runs
                    #     on the PR, so we set ci_waiting=True. Both ci_passed
                    #     and review_approved must land on review_loop before
                    #     the merge gate fires (see app.ci_completion._review_approved
                    #     + the rollup gate handoff in app.task_lifecycle's
                    #     verdict==APPROVE branch).
                    #   • intermediate target (research/*, plan/*): ci.yml does
                    #     NOT trigger (its branches list is [main, automain]),
                    #     so a CI wait is unsatisfiable and times out at the
                    #     1h backstop. Skip ci_waiting; the review task's
                    #     verdict==APPROVE branch falls through to the direct
                    #     merge path, and the next rollup sweep marks this parent
                    #     COMPLETED when it sees the PR merged.
                    # State-regression guard (PR #8746, research 5b250ecd): when
                    # RESUMING with an existing rollup PR, the prior review_loop may
                    # already record real CI/fix progress — ci_passed=False (a fix
                    # is needed), ci_fix_spawned (a fix is in flight), or a
                    # non-terminal (incl. BLOCKED) fix child on this PR. Rebuilding
                    # the seed unconditionally re-arms ci_waiting=True / a fresh
                    # ci_wait_started and discards that progress, so the 1h CI-wait
                    # backstop later FAILs the parent and cascade-cancels the fix.
                    # Detect that regression and PRESERVE the existing review_loop
                    # instead of overwriting it. (Changes A+B keep a BLOCKED fix
                    # visible so this resume path is rarely reached in the incident
                    # scenario; this is the direct backstop.)
                    #
                    # Park-honoring gate (lineage d3c12745 / PR #16759): when the
                    # stuck/standoff escalation already parked this chain for the
                    # operator, do NOT respawn a review — the previous review
                    # COMPLETED (freeing its active-idempotency key), so without
                    # this check every sweep spawns another review that re-hits
                    # the stuck gate and ratchets pr_iterations toward the cap.
                    # Leave the parent EVALUATING for operator action with a
                    # truthful message; a fresh rollup PR (no breadcrumb yet)
                    # falls through to the normal first spawn.
                    _park_msg = _rollup_review_respawn_park_message(
                        parent, rollup_pr_number
                    )
                    if _park_msg is not None:
                        parent.latest_status_message = _park_msg
                        session.add(parent)
                        await session.commit()
                        logger.warning(
                            f"[ROLLUP] {_park_msg} (parent {str(parent.id)[:8]} left "
                            f"for operator action)"
                        )
                        return
                    existing_loop = parent_ctx.get("review_loop") or {}
                    # Only trust prior loop state that belongs to THIS PR — a
                    # recreated rollup PR must not inherit a stale loop (and its
                    # ci_fix_spawned/ci_passed) from its predecessor's number.
                    _loop_pr = existing_loop.get("pr_number")
                    same_pr_loop = _loop_pr is not None and str(_loop_pr) == str(
                        rollup_pr_number
                    )
                    has_ci_progress = same_pr_loop and (
                        existing_loop.get("ci_passed") is False
                        or bool(existing_loop.get("ci_fix_spawned"))
                    )
                    active_fix_exists = False
                    if not has_ci_progress:
                        from app.ci_completion import _pr_number_match

                        _active_pr_tasks = (
                            (
                                await session.execute(
                                    select(Task).where(
                                        Task.project_id == parent.project_id,
                                        # Includes BLOCKED: a fix parked on a
                                        # precondition is still in flight and must
                                        # not have its loop state wiped on resume.
                                        Task.status.in_(
                                            [
                                                TaskStatus.QUEUED,
                                                TaskStatus.PROVISIONING,
                                                TaskStatus.IN_PROGRESS,
                                                TaskStatus.EVALUATING,
                                                TaskStatus.BLOCKED,
                                            ]
                                        ),
                                        Task.archived.is_(False),
                                        _pr_number_match(session, rollup_pr_number),
                                    )
                                )
                            )
                            .scalars()
                            .all()
                        )
                        active_fix_exists = any(
                            t.id != parent.id and t.first_payload.get("type") == "fix"
                            for t in _active_pr_tasks
                        )
                    if has_ci_progress or (same_pr_loop and active_fix_exists):
                        logger.info(
                            f"[ROLLUP] Resuming rollup PR #{rollup_pr_number} with "
                            f"in-flight CI/fix state (ci_passed={existing_loop.get('ci_passed')}, "
                            f"ci_fix_spawned={bool(existing_loop.get('ci_fix_spawned'))}, "
                            f"active_fix={active_fix_exists}) — preserving review_loop, "
                            f"not re-arming ci_waiting"
                        )
                        review_loop_seed = existing_loop
                        parent.latest_status_message = (
                            f"Rollup PR #{rollup_pr_number} resuming with in-flight "
                            f"fix/CI state — review_loop preserved"
                        )
                        parent.context = {
                            **parent_ctx,
                            "rollup_pr_url": rollup_pr_url,
                            "rollup_pr_number": rollup_pr_number,
                            "_is_rollup_review": True,
                            "review_loop": review_loop_seed,
                        }
                    else:
                        # Do NOT pre-seed review_approved here — the spawned review
                        # task writes it back to this parent's review_loop on APPROVE.
                        review_loop_seed = {
                            "pr_number": rollup_pr_number,
                            "branch": staging_branch,
                            "base_branch": target_branch,
                            "original_task_id": str(parent.id),
                        }
                        if active_fix_exists:
                            # A non-terminal fix exists for this PR but the prior
                            # loop belongs to a different (recreated) PR number:
                            # rebuild the seed for the current PR, record that a
                            # fix is needed, and do NOT arm the CI wait — the
                            # in-flight fix child drives the next transition.
                            review_loop_seed["ci_passed"] = False
                        elif not is_target_intermediate:
                            review_loop_seed["ci_waiting"] = True
                            review_loop_seed["ci_wait_started"] = _utcnow().isoformat()
                        parent.latest_status_message = f"All {completed_count} subtasks merged, rollup PR #{rollup_pr_number} awaiting review"
                        parent.context = {
                            **parent_ctx,
                            "rollup_pr_url": rollup_pr_url,
                            "rollup_pr_number": rollup_pr_number,
                            "_is_rollup_review": True,
                            "review_loop": review_loop_seed,
                        }
                    review_task, _ = await _spawn_pipeline_task(
                        parent,
                        "review",
                        session,
                        rollup_pr_number,
                        staging_branch,
                        target_branch,
                        original_prompt=parent.description or parent.title,
                        original_task_id=str(parent.id),
                    )
                    if review_task:
                        logger.info(
                            f"[ROLLUP] Spawned review for rollup PR #{rollup_pr_number} → task {review_task.id}"
                        )
                    else:
                        from app.review_pipeline import (
                            SPAWN_REASON_KEY,
                            SPAWN_REASON_DEFERRED,
                            SPAWN_REASON_DEDUP,
                            SPAWN_REASON_ALREADY_MERGED,
                            SPAWN_REASON_PR_CLOSED_UNMERGED,
                        )

                        spawn_reason = (parent.context or {}).get(SPAWN_REASON_KEY)
                        if spawn_reason in (SPAWN_REASON_DEFERRED, SPAWN_REASON_DEDUP):
                            # An active review/fix already owns this PR — leave parent
                            # EVALUATING; next rollup sweep retries. latest_status_message
                            # was already set by _spawn_pipeline_task.
                            logger.info(
                                f"[ROLLUP] Review spawn deferred for rollup PR #{rollup_pr_number} "
                                f"({spawn_reason}) — leaving parent EVALUATING, next sweep retries"
                            )
                        elif spawn_reason == SPAWN_REASON_ALREADY_MERGED:
                            logger.info(
                                f"[ROLLUP] PR #{rollup_pr_number} merged mid-flight — no review needed"
                            )
                        elif spawn_reason == SPAWN_REASON_PR_CLOSED_UNMERGED:
                            logger.info(
                                f"[ROLLUP] PR #{rollup_pr_number} closed without merge — no review needed"
                            )
                        else:
                            parent.status = TaskStatus.FAILED
                            parent.context = _clear_rollup_transient_state(
                                parent.context
                            )
                            parent.latest_status_message = (
                                f"Rollup PR #{rollup_pr_number} created but review not spawned "
                                f"({spawn_reason or 'unknown'})"
                            )
                            from app.failure_classification import (
                                classify_failure,
                                persist_failure_category,
                            )

                            _fc = classify_failure(parent)
                            persist_failure_category(parent, _fc)
                            _record_rollup_failure_signals(parent, _fc)
                            await _maybe_create_failure_alert(
                                parent,
                                session,
                                f"Rollup PR #{rollup_pr_number} created but review task could not be spawned — iteration cap exhausted",
                            )
                            logger.warning(
                                f"[ROLLUP] WARNING: Rollup review spawn returned None for PR #{rollup_pr_number} (reason={spawn_reason})"
                            )
                        # NOTE: Do NOT delete staging_branch here — it contains
                        # all merged child work and requeueing should be possible.
            except Exception as e:
                # Session-poison recovery (prod burst 2026-06-07 01:05): a
                # DB-level failure inside the try — e.g. _spawn_pipeline_task's
                # spawn commit cancelled by lock_timeout — aborts the
                # transaction, and every session use below (including the
                # lazy-refreshing ``parent.context`` read in the retry-counter
                # branch) then raises PendingRollbackError, escaping this
                # handler entirely and cascading through the rest of
                # post-commit. When (and only when) the transaction is already
                # dead, roll back FIRST so the recovery below runs on a clean
                # transaction, then re-fetch ``parent`` by its pre-captured id
                # — rollback() expires every loaded instance even with
                # expire_on_commit=False, and an attribute read on the expired
                # instance would sync-lazy-load and raise MissingGreenlet.
                # Non-DB failures (GitHub HTTPException, etc.) keep their
                # pending session state exactly as before.
                _sess_poisoned = False
                with contextlib.suppress(Exception):
                    _sess_poisoned = not session.is_active
                if _sess_poisoned:
                    _did_silent_rollback = True
                    with contextlib.suppress(Exception):
                        await session.rollback()
                    if parent_id_local is not None:
                        with contextlib.suppress(Exception):
                            _refetched_parent = await session.get(
                                Task, parent_id_local
                            )
                            if _refetched_parent is not None:
                                parent = _refetched_parent
                # Orphan PR rescue: if we created a rollup PR during this try
                # but the marker commit failed (connection loss, session invalid),
                # the local variable still holds the PR number while the DB has
                # no record.  Persist it via a separate session so the retry
                # cycle's crash-recovery check resumes the existing PR instead
                # of creating a duplicate.
                #
                # Production 2026-05-04: when the "Parent state changed during
                # create_pull_request" raise at line ~1211 fired, ``parent``
                # had been reassigned to None by ``_relock_parent`` at the
                # call above. Subsequent ``parent.id`` access in this except
                # block crashed with AttributeError, swallowing the rescue.
                # Use ``parent_id_local`` / ``project_id_local`` (captured
                # pre-release in this same scope at lines 981-982) which
                # always hold the original parent identifiers regardless of
                # whether ``parent`` got reassigned to None mid-flow.
                _err_parent_id = (
                    parent_id_local
                    if parent_id_local is not None
                    else (parent.id if parent is not None else None)
                )
                _err_project_id = (
                    project_id_local
                    if project_id_local is not None
                    else (parent.project_id if parent is not None else None)
                )
                if rollup_pr_number is not None and _err_parent_id is not None:
                    try:
                        async with AsyncSessionLocal() as rescue_session:
                            db_parent = await rescue_session.get(Task, _err_parent_id)
                            if db_parent is not None:
                                db_ctx = db_parent.context or {}
                                if not db_ctx.get("rollup_pr_number"):
                                    db_parent.context = {
                                        **db_ctx,
                                        "rollup_pr_number": rollup_pr_number,
                                        "rollup_pr_url": rollup_pr_url,
                                    }
                                    rescue_session.add(db_parent)
                                    await rescue_session.commit()
                                    logger.warning(
                                        f"[ROLLUP] Rescued rollup PR #{rollup_pr_number} marker "
                                        f"for parent {_err_parent_id} via separate session — "
                                        f"preventing orphan on retry"
                                    )
                    except Exception as rescue_err:
                        logger.error(
                            f"[ROLLUP] Failed to rescue rollup PR #{rollup_pr_number} marker "
                            f"for parent {_err_parent_id}: {rescue_err}",
                            exc_info=True,
                        )
                # Sentinel deferral: the rest of this handler (retry counter,
                # status flip, failure classification, alert, post-handler
                # finalization at line ~2049) all dereference ``parent``, which
                # is None on this path. The rescue above already persisted the
                # PR marker; the next sweep cycle re-enters with fresh parent
                # state and resumes from the existing-PR branch.
                if isinstance(e, _ParentMutatedDuringPRCreation):
                    return _did_silent_rollback
                err_str = str(e).lower()
                # Auth failures: invalidate the cached token so the next retry uses a fresh one
                if (
                    isinstance(e, HTTPException)
                    and e.status_code == 401
                    and _err_project_id is not None
                ):
                    from app.routers.agent import invalidate_github_token_cache

                    invalidate_github_token_cache(str(_err_project_id))
                    logger.warning(
                        f"[ROLLUP] Auth failure (401) for parent {_err_parent_id} — invalidated token cache"
                    )
                is_transient = (
                    isinstance(e, HTTPException)
                    and e.status_code in (401, 500, 502, 503, 504)
                ) or any(
                    kw in err_str
                    for kw in [
                        "api.github.com",
                        "500 internal server error",
                        "502 bad gateway",
                        "503 service unavailable",
                        "504 gateway timeout",
                        "internal server error",
                        "connection",
                        "timeout",
                        "rate limit",
                        "ssl",
                        "name resolution",
                        # Staging branch may have been deleted by a race — retry
                        # to allow recovery or manual intervention
                        "staging branch",
                        "validation failed",
                        # Auth failures — token expired/revoked; cache is invalidated above
                        "unauthorized",
                        "bad credentials",
                        "token expired",
                    ]
                )
                rollup_retries = (parent.context or {}).get("_rollup_retries", 0) + 1
                if is_transient and rollup_retries <= (
                    max_rollup := _runtime_config.get("max_rollup_retries", 5)
                ):
                    # Transient GitHub error — leave parent EVALUATING, retry next
                    # cycle. Drop our own creation claim so the retry isn't blocked
                    # by it; create_pull_request's idempotent pre-check catches any
                    # PR that a lost-response POST actually created.
                    parent.context = {
                        **{
                            k: v
                            for k, v in (parent.context or {}).items()
                            if k != "rollup_pr_creating_at"
                        },
                        "_rollup_retries": rollup_retries,
                    }
                    parent.latest_status_message = f"Rollup PR creation failed (attempt {rollup_retries}/{max_rollup}), will retry: {type(e).__name__}"
                    logger.warning(
                        f"[ROLLUP] Transient error creating rollup PR (attempt {rollup_retries}): {e}"
                    )
                else:
                    logger.warning(
                        f"[ROLLUP] Failed to create rollup PR: {e}", exc_info=True
                    )
                    parent.status = TaskStatus.FAILED
                    parent.context = _clear_rollup_transient_state(parent.context)
                    parent.latest_status_message = f"All {completed_count} subtasks done but rollup PR creation failed: {type(e).__name__}"[:500]
                    from app.failure_classification import (
                        classify_failure,
                        persist_failure_category,
                    )

                    _fc = classify_failure(parent)
                    persist_failure_category(parent, _fc)
                    _record_rollup_failure_signals(parent, _fc)
                    await _maybe_create_failure_alert(
                        parent,
                        session,
                        f"All {completed_count} subtasks completed but rollup PR creation failed after {rollup_retries} retries — manual recovery required",
                    )
                    # NOTE: Do NOT delete staging_branch here — it contains all
                    # merged child work. Preserving it allows manual recovery or
                    # requeue. Branches are cleaned up by _cleanup_branches when
                    # the parent eventually reaches a terminal state, or by the
                    # stale branch cleanup service after 7 days.
        else:
            parent.status = TaskStatus.COMPLETED
            parent.latest_status_message = (
                f"All {completed_count} subtasks completed and merged"
            )

        # Final guard: a parent with an unmerged rollup PR is not done yet —
        # the review/fix/CI pipeline is still working on landing the code.
        _pctx = parent.context or {}
        if (
            parent.status == TaskStatus.COMPLETED
            and _pctx.get("rollup_pr_number")
            and not _pctx.get("pr_merged")
        ):
            parent.status = TaskStatus.EVALUATING
            _rpr = _pctx["rollup_pr_number"]
            parent.latest_status_message = f"All {completed_count} subtasks done — rollup PR #{_rpr} awaiting merge"
            logger.info(
                f"[ROLLUP] Parent {parent.id} — blocked COMPLETED: rollup PR #{_rpr} not yet merged"
            )

        # Terminal transition — scrub transient deferral/retry counters so a
        # future re-entry (admin requeue, reused parent) starts from zero.
        if parent.status in (TaskStatus.COMPLETED, TaskStatus.FAILED):
            parent.context = _clear_rollup_transient_state(parent.context)

        session.add(parent)
        await session.commit()
        await broadcast_to_project(
            str(parent.project_id),
            "task_updated",
            {"task_id": str(parent.id), "status": parent.status},
        )
        logger.info(
            f"[ROLLUP] Parent {parent.id} — all children done (completed={completed_count}, cancelled={cancelled_count})"
        )

    elif any(s == TaskStatus.FAILED for s in child_statuses) and active_count == 0:
        # All done but some failed — mark parent as failed but keep staging branch
        # (it may contain merged work from successful children that can be salvaged)
        parent.status = TaskStatus.FAILED
        failed_count = sum(1 for s in child_statuses if s == TaskStatus.FAILED)
        parent.latest_status_message = (
            f"{failed_count} of {len(children)} subtasks failed"
        )
        from app.failure_classification import (
            worst_child_failure_category,
            persist_failure_category,
        )

        _failed_children = [c for c in children if c.status == TaskStatus.FAILED]
        _fc = worst_child_failure_category(_failed_children)
        persist_failure_category(parent, _fc)
        _record_rollup_failure_signals(parent, _fc)
        parent.context = _clear_rollup_transient_state(parent.context)
        session.add(parent)
        await session.commit()
        await broadcast_to_project(str(parent.project_id), "task_updated",
                                   {"task_id": str(parent.id), "status": TaskStatus.FAILED})
        logger.error(f"[ROLLUP] Parent {parent.id} failed — {failed_count} children failed")

    # Every path above either returns explicitly or falls through to here.
    # If a silent internal rollback occurred, signal it to the caller so it
    # can re-fetch ORM objects before the next attribute access.
    return _did_silent_rollback
# fmt: on
