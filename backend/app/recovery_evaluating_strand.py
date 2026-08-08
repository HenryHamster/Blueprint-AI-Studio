"""EVALUATING out-of-band-merge recovery handlers, extracted from
``app.recovery_evaluating``.

Two thematically-paired per-row recovery branches lifted out of the
``_check_evaluating_timeouts`` Phase-B loop. Both detect an EVALUATING parent
whose PR merged out-of-band via a GitHub lookup and converge it, and both guard
'only act on a GitHub-confirmed merged PR':

  * ``_handle_evaluating_strand_backstop`` — a non-rollup review/CI task that
    spawned a conflict-fix / ci-fix child (``review_loop.conflict_fix_spawned``
    / ``ci_fix_spawned`` set) and parked in EVALUATING when the latch was never
    cleared.
  * ``_handle_rollup_lost_marker`` — a rollup parent whose ``rollup_pr_number``
    marker was never persisted (crash/reap window), leaving it stranded in
    EVALUATING with only a staging branch in context.

Each handler takes the per-row locals it needs (``task``, ``ctx``, ``session``)
and returns ``True`` when it handled the row (the caller ``continue``s) or
``False`` to fall through to the remaining recovery branches unchanged. The
move is purely structural — every commit / notification / GitHub-call semantic
is preserved byte-for-byte. Shared bounding infrastructure (``_bounded``,
``_GH_CALL_TIMEOUT_S``, ``broadcast_to_project``) is referenced through the
``services`` module object so test patches against ``app.services.<name>`` and
the function-local ``app.routers.agent`` / ``app.github`` import sites still
take effect.
"""

import contextlib
import logging
from datetime import datetime
from app.time_utils import _utcnow

from app import services
from app.landed_signal import corroborate_and_mark_landed_on_integration
from app.models import Project, TaskStatus
from app.recovery_evaluating_helpers import _apply_rollup_merge_classification

logger = logging.getLogger(__name__)


async def _handle_evaluating_strand_backstop(
    task, ctx, session, *, maybe_create_failure_alert
):
    """EVALUATING-STRAND BACKSTOP. A review/CI task that spawned a
    conflict-fix or ci-fix child (review_loop.conflict_fix_spawned /
    ci_fix_spawned set) parks here until the chain resolves. The
    historical strand: an APPROVED review hit a merge conflict,
    spawned a conflict-fix, the fix's PR later MERGED, but the
    original review was never terminalized — the conflict_fix_spawned
    latch was never cleared (orchestrator:3086 skips it, ci_completion
    only updates the chain root) so it sits in EVALUATING forever.
    The root cause (children inheriting _is_rollup_review and looking
    like rollup parents) is fixed in review_pipeline; this is a
    belt-and-suspenders recovery for already-stranded rows.

    GUARDRAIL: only acts on NON-rollup-parent tasks (real rollup
    parents are handled by their own merge gate), and NEVER completes
    a task whose PR is still OPEN — open PRs are left EVALUATING.

    Returns ``True`` when the row was terminalized (caller ``continue``s);
    ``False`` to leave the row EVALUATING and fall through.
    """
    _strand_rl = ctx.get("review_loop") or {}
    _strand_spawned = _strand_rl.get("conflict_fix_spawned") or (
        _strand_rl.get("ci_fix_spawned")
    )
    if _strand_spawned and not ctx.get("pr_merged"):
        from app.rollup_state import _is_rollup_parent_awaiting_merge

        if not _is_rollup_parent_awaiting_merge(ctx):
            _strand_pr = _strand_rl.get("pr_number") or ctx.get("rollup_pr_number")
            if _strand_pr:
                _s_state = "open"
                _s_merged = False
                _s_lookup_ok = False
                _s_merge_sha: str | None = None
                try:
                    from app.routers.agent import (
                        get_github_token_for_task,
                    )
                    from app.github import get_pull_request

                    _gh = await services._bounded(
                        get_github_token_for_task(task.id, session),
                        services._GH_CALL_TIMEOUT_S,
                        what=f"strand token {str(task.id)[:8]}",
                    )
                    _gh_token, _owner, _repo = _gh
                    _pr = await services._bounded(
                        get_pull_request(_gh_token, _owner, _repo, _strand_pr),
                        services._GH_CALL_TIMEOUT_S,
                        what=f"strand get_pull_request #{_strand_pr}",
                    )
                    _s_state = _pr.get("state") or "open"
                    _s_merged = bool(_pr.get("merged"))
                    _s_merge_sha = _pr.get("merge_commit_sha")
                    _s_lookup_ok = True
                except Exception as _strand_exc:
                    logger.warning(
                        f"[EVAL TIMEOUT strand] Task {str(task.id)[:8]} "
                        f"PR #{_strand_pr} lookup failed: "
                        f"{type(_strand_exc).__name__}: {_strand_exc} — "
                        f"leaving EVALUATING for next sweep"
                    )
                if _s_lookup_ok and _s_state == "closed" and _s_merged:
                    from sqlalchemy.orm.attributes import flag_modified

                    task.status = TaskStatus.COMPLETED
                    task.latest_status_message = (
                        f"Strand recovery: conflict/CI-fix PR "
                        f"#{_strand_pr} merged out-of-band"
                    )
                    # The APPROVED original's work landed on the
                    # integration branch via its conflict/CI-fix
                    # child's merged PR, so stamp the canonical
                    # integration-landing signal (preserves the
                    # prior bare ``pr_merged=True`` analytics
                    # meaning while routing through the helper).
                    task.context = dict(ctx)
                    # The async twin corroborates the declared acceptance test at
                    # the merge tree before stamping the landed signal, so this
                    # out-of-band strand-recovery landing also mints machine-proven
                    # credit for a not-in-diff declared test (previously the marker
                    # was left absent on every non-CI-monitor landing path).
                    # The project is REQUIRED for corroboration — the tree read
                    # resolves the repo's GitHub credentials from it, so passing
                    # None would degrade every corroboration to indeterminate and
                    # leave the marker absent exactly as before. It also mints the
                    # capability proof on this landing path (same guarantee as
                    # ``rollup_pr_verification``).
                    _strand_project = (
                        await session.get(Project, task.project_id)
                        if task.project_id
                        else None
                    )
                    await corroborate_and_mark_landed_on_integration(
                        task,
                        session,
                        _strand_project,
                        # The child PR's own merge commit, from the lookup
                        # above: corroboration must read the immutable merge
                        # tree, not the 60s-TTL branch-tip cache that can still
                        # serve the pre-merge tip.
                        merge_commit_sha=_s_merge_sha,
                    )
                    flag_modified(task, "context")
                    session.add(task)
                    await session.commit()
                    logger.info(
                        f"[EVAL TIMEOUT strand] Task {str(task.id)[:8]} "
                        f"terminalized COMPLETED — PR #{_strand_pr} merged"
                    )
                    await services.broadcast_to_project(
                        str(task.project_id),
                        "task_updated",
                        {
                            "task_id": str(task.id),
                            "status": TaskStatus.COMPLETED,
                        },
                    )
                    return True
                if _s_lookup_ok and _s_state == "closed" and not _s_merged:
                    # PR closed without merging — terminal, but route
                    # through FIX A's non-rejection classifier so a
                    # cleanup/supersession close lands CANCELLED and a
                    # genuine rejection lands FAILED.
                    from app.rollup_management import (
                        _pr_closed_is_non_rejection,
                    )
                    from sqlalchemy.orm.attributes import flag_modified
                    from app.failure_classification import (
                        SCOPE_EMPTY,
                        classify_failure,
                        persist_failure_category,
                    )

                    _non_rej = False
                    with contextlib.suppress(Exception):
                        _non_rej = await _pr_closed_is_non_rejection(
                            task, _pr, _strand_pr, session
                        )
                    if _non_rej:
                        task.status = TaskStatus.CANCELLED
                        task.latest_status_message = (
                            f"Strand recovery: PR #{_strand_pr} closed "
                            f"without merging (no rejection — cleanup)"
                        )
                        task.context = {
                            **ctx,
                            "pr_closed_unmerged": True,
                            "failure_detail": "pr_closed_non_rejection",
                            "failure_category": SCOPE_EMPTY,
                        }
                        flag_modified(task, "context")
                        session.add(task)
                        await session.commit()
                        await services.broadcast_to_project(
                            str(task.project_id),
                            "task_updated",
                            {
                                "task_id": str(task.id),
                                "status": TaskStatus.CANCELLED,
                            },
                        )
                        return True
                    task.status = TaskStatus.FAILED
                    task.latest_status_message = (
                        f"Strand recovery: PR #{_strand_pr} closed without merging"
                    )
                    task.context = {**ctx, "pr_closed_unmerged": True}
                    _sfc = classify_failure(task)
                    persist_failure_category(task, _sfc)
                    flag_modified(task, "context")
                    session.add(task)
                    await maybe_create_failure_alert(
                        task,
                        session,
                        f"Strand recovery: PR #{_strand_pr} closed without merging",
                    )
                    await session.commit()
                    await services.broadcast_to_project(
                        str(task.project_id),
                        "task_updated",
                        {
                            "task_id": str(task.id),
                            "status": TaskStatus.FAILED,
                        },
                    )
                    return True
                # STILL OPEN (or lookup failed) → leave EVALUATING.
                # GUARDRAIL: never complete a task whose PR is open.
    return False


async def _handle_rollup_lost_marker(task, ctx, session):
    """ROLLUP-PARENT LOST-MARKER RECOVERY. When a rollup parent's
    rollup_pr_number marker was not persisted (crash/reap window
    between GitHub PR creation and the DB write), the parent stays
    EVALUATING forever — every convergence path is keyed on the
    marker.  Worse, the rollup machinery keeps re-commissioning
    child tasks against the now-deleted staging branch, burning VMs
    in a BRANCH_GONE loop.

    Detection: EVALUATING task has a staging branch in context
    (plan_branch / research_branch) but no rollup_pr_number and no
    pr_merged.  We query GitHub for closed PRs from that head
    branch; if one is merged we converge the parent.

    GUARDRAIL: only acts when GitHub confirms a merged PR.  If the
    lookup fails or the PR is still open (state=closed returns []),
    fall through unchanged and re-check after _LM_RECHECK_INTERVAL_S
    so the sweep catches the PR merging later.

    Returns ``True`` when the parent converged (caller ``continue``s);
    ``False`` to fall through (including the no-merged back-off path,
    which stamps ``_lost_marker_checked_at`` on ``task.context`` — the
    caller re-syncs its loop-local ``ctx`` afterward).
    """
    _LM_RECHECK_INTERVAL_S = 1800  # 30 minutes between re-checks
    _stg_branch = ctx.get("plan_branch") or ctx.get("research_branch")
    _lm_last_checked_at = ctx.get("_lost_marker_checked_at")
    _lm_recheck_due = not _lm_last_checked_at or (
        (
            _utcnow() - datetime.fromisoformat(_lm_last_checked_at)
        ).total_seconds()
        > _LM_RECHECK_INTERVAL_S
    )
    if (
        _stg_branch
        and not ctx.get("rollup_pr_number")
        and not ctx.get("pr_merged")
        and _lm_recheck_due
    ):
        _lm_merged = False
        _lm_pr_data: dict | None = None
        _lm_lookup_ok = False
        try:
            from app.routers.agent import get_github_token_for_task
            from app.github import get_pull_requests_for_head_branch

            _lm_token, _lm_owner, _lm_repo = await services._bounded(
                get_github_token_for_task(task.id, session),
                services._GH_CALL_TIMEOUT_S,
                what=f"lost-marker token {str(task.id)[:8]}",
            )
            _target_base = ctx.get("parent_base") or ctx.get("original_base")
            _lm_prs = await services._bounded(
                get_pull_requests_for_head_branch(
                    _lm_token,
                    _lm_owner,
                    _lm_repo,
                    _stg_branch,
                    base=_target_base,
                ),
                services._GH_CALL_TIMEOUT_S,
                what=f"lost-marker list-prs {_stg_branch!r}",
            )
            # Fallback: if the base-filtered query returned nothing and
            # we had a base filter, retry without it — the actual base
            # ref may differ from what's in context.
            if not _lm_prs and _target_base:
                _lm_prs = await services._bounded(
                    get_pull_requests_for_head_branch(
                        _lm_token,
                        _lm_owner,
                        _lm_repo,
                        _stg_branch,
                        base=None,
                    ),
                    services._GH_CALL_TIMEOUT_S,
                    what=f"lost-marker list-prs unfiltered {_stg_branch!r}",
                )
            _lm_lookup_ok = True
            for _pr in _lm_prs or []:
                if _pr.get("merged_at") or bool(_pr.get("merged")):
                    _lm_merged = True
                    _lm_pr_data = _pr
                    break
        except Exception as _lm_exc:
            logger.warning(
                f"[EVAL TIMEOUT lost-marker] Task {str(task.id)[:8]} "
                f"branch {_stg_branch!r} lookup failed: "
                f"{type(_lm_exc).__name__}: {_lm_exc} — leaving EVALUATING"
            )

        if _lm_lookup_ok and not _lm_merged:
            # No merged PR found (PR still open, not yet created, or
            # closed-unmerged).  Stamp a timestamp so we back off and
            # re-check after _LM_RECHECK_INTERVAL_S rather than
            # querying GitHub on every sweep cycle.
            from sqlalchemy.orm.attributes import flag_modified

            task.context = {
                **ctx,
                "_lost_marker_checked_at": _utcnow().isoformat(),
            }
            flag_modified(task, "context")
            session.add(task)
            await session.commit()
        elif _lm_lookup_ok and _lm_merged and _lm_pr_data is not None:
            # Confirmed: rollup PR was merged.  Converge the parent.
            from sqlalchemy.orm.attributes import flag_modified
            from app.rollup_state import _clear_rollup_transient_state
            from app.models import Project

            _lm_pr_num = int(_lm_pr_data.get("number") or 0) or None
            _lm_pr_url = _lm_pr_data.get("html_url") or None
            _lm_base_ref = (_lm_pr_data.get("base") or {}).get("ref") or None

            _lm_proj = await session.get(Project, task.project_id)
            _lm_new_ctx = _clear_rollup_transient_state({**ctx})
            if _lm_pr_num:
                _lm_new_ctx["rollup_pr_number"] = _lm_pr_num
            if _lm_pr_url:
                _lm_new_ctx["rollup_pr_url"] = _lm_pr_url
            task.context = _lm_new_ctx
            (
                _lm_record_merged,
                _lm_terminal,
                _lm_landed,
            ) = await _apply_rollup_merge_classification(
                task,
                _lm_proj,
                session,
                pr_number=_lm_pr_num,
                merged_base_branch=_lm_base_ref,
                source="recovery_sweeps.lost_marker",
                msg_terminal=(
                    f"✓ rollup PR #{_lm_pr_num} already merged to "
                    f"{{landed}}; converged from stranded EVALUATING"
                ),
                msg_non_terminal=(
                    f"Lost-marker recovery: rollup PR #{_lm_pr_num} "
                    f"merged into {{landed}}; awaiting downstream landing"
                ),
                hotfix_lane=None,
                merge_commit_sha=_lm_pr_data.get("merge_commit_sha"),
            )
            flag_modified(task, "context")
            session.add(task)
            await session.commit()
            logger.info(
                f"[EVAL TIMEOUT lost-marker] Task {str(task.id)[:8]} "
                f"converged → {task.status} (rollup PR #{_lm_pr_num} "
                f"merged into {_lm_landed})"
            )
            await services.broadcast_to_project(
                str(task.project_id),
                "task_updated",
                {
                    "task_id": str(task.id),
                    "status": task.status,
                },
            )
            return True
    return False
