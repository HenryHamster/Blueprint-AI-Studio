"""Orphan-recovery sweep extracted from ``app.recovery_post_commit``.

Holds the two functions that form the self-contained orphan-recovery cluster:

* ``_spawn_review_for_recovered_task`` — shared helper that spawns the review
  pipeline, creates an Alert, and broadcasts for a recovered task.
* ``_recover_orphaned_tasks`` — the sweep entry point: detects COMPLETED tasks
  with no PR artifact / no review child and reaped tasks with a PR artifact
  but no review child, looks up PRs on GitHub, and spawns reviews.

These are extracted following the ``reaper.py`` decomposition pattern
(``reaper_halt.py``, ``reaper_orphan_classify.py``).  Back-compat re-imports
live at the bottom of ``app.recovery_post_commit`` so every existing call site
(``app.recovery_sweeps``, ``app.services``, tests) resolves unchanged.
"""

import logging
from datetime import timedelta

from sqlalchemy import or_
from sqlalchemy.orm import load_only
from sqlmodel import select

import app.shared_state as _shared_state
from app import services
from app.models import (
    Task,
    TaskStatus,
    Artifact,
    ArtifactType,
    Alert,
    AlertSeverity,
)
from app.time_utils import _utcnow

logger = logging.getLogger(__name__)


async def _spawn_review_for_recovered_task(
    source_task,
    session,
    pr_number: int,
    branch: str,
    base_branch: str,
    original_prompt: str,
    original_task_id: str,
    project_id,
    short_id: str,
    alert_title: str,
    alert_message: str,
    log_message: str,
) -> None:
    """Spawn review pipeline + alert + broadcast for a recovered task.

    Shared by Phase 1 (COMPLETED orphan) and Phase 2 (reaped-with-artifact)
    of _recover_orphaned_tasks; the caller commits before invoking this for
    Phase 2 (task status transition), then this helper does the spawn-commit
    and broadcast in one place.
    """
    from app.review_pipeline import _spawn_pipeline_task

    review_task, _ = await _spawn_pipeline_task(
        source_task,
        "review",
        session,
        pr_number,
        branch,
        base_branch,
        original_prompt=original_prompt,
        original_task_id=original_task_id,
    )

    alert = Alert(
        project_id=project_id,
        severity=AlertSeverity.WARNING,
        title=alert_title,
        message=alert_message,
        task_id=source_task.id,
        event_type="task.failed",
    )
    session.add(alert)
    await session.commit()

    await services.broadcast_to_project(
        str(project_id),
        "alert_created",
        {
            "alert_id": str(alert.id),
            "severity": "WARNING",
            "title": alert.title,
        },
    )
    logger.warning(
        log_message + f", review={str(review_task.id)[:8] if review_task else 'NONE'}"
    )


async def _recover_orphaned_tasks():
    """Detect COMPLETED tasks with no PR artifact and no review child. Look up PR on GitHub and spawn review."""
    from app.routers.agent import get_github_token_for_task
    from app.orchestrator import _mark_orchestrator_progress
    from app.models import Project

    try:
        five_min_ago = _utcnow() - timedelta(minutes=5)
        one_hour_ago = _utcnow() - timedelta(hours=1)

        # ── Phase A: cycle reads on a short-lived session, snapshot to plain
        # data, then CLOSE it before any per-row session opens (audit P1). A
        # single shared session held across the per-row loop meant a _bounded
        # token-fetch cancellation could poison it (PendingRollback), silently
        # dropping every later orphan's artifact/alert/review-spawn commit. We
        # snapshot just the skip-decision data here; each row that needs GitHub
        # I/O opens its OWN session below and re-fetches the task fresh.
        async with services.AsyncSessionLocal() as read_session:
            # Phase B4 (H-P6): drop status_log/plan_output JSONB. The recovery
            # loop only reads first_payload / context / description / project_id /
            # id and writes context. status_log is not consumed in this branch.
            candidates = (
                (
                    await read_session.execute(
                        select(Task)
                        .options(
                            load_only(
                                Task.id,
                                Task.status,
                                Task.project_id,
                                Task.created_by,
                                Task.created_at,
                                Task.description,
                                Task.payloads,
                                Task.context,
                                raiseload=True,
                            )
                        )
                        .where(
                            Task.status == TaskStatus.COMPLETED,
                            Task.created_by == "ORCHESTRATOR",
                            Task.created_at <= five_min_ago,
                            # Normal window: tasks created in the last hour.
                            # Extended: also include post_commit_failed=True tasks
                            # regardless of age — the post-commit sweep may abandon
                            # a task hours after creation (agent runtime +
                            # lock-starvation + 90-min backstop ≈ 3.5h observed),
                            # which puts it permanently outside the 1-hour window.
                            # post_commit_failed is write-only without this OR arm;
                            # coupling it here makes the abandon→recovery path
                            # event-driven on the flag rather than on creation age.
                            or_(
                                Task.created_at >= one_hour_ago,
                                Task.post_commit_failed.is_(True),
                            ),
                        )
                        # C3: per-cycle row cap. Each candidate costs a serial
                        # get_github_token_for_task + list-pulls GitHub round-trip;
                        # an unbounded fan-out is the leader-cycle saturation this
                        # cluster targets. Oldest-first so the most-stale orphans
                        # are serviced before the cap truncates; deferred rows
                        # re-qualify next cycle (runs every 120s).
                        .order_by(Task.created_at.asc())
                        .limit(services._ORPHAN_RECOVERY_MAX_PER_CYCLE)
                    )
                )
                .scalars()
                .all()
            )
            if len(candidates) >= services._ORPHAN_RECOVERY_MAX_PER_CYCLE:
                logger.warning(
                    "[RECOVERY] Hit per-cycle cap of %d orphan candidates — "
                    "remainder deferred to next sweep",
                    services._ORPHAN_RECOVERY_MAX_PER_CYCLE,
                )

            # Snapshot the per-row skip-decision data as plain tuples so it
            # outlives the read_session (id, task_type, first_payload, context).
            cand_snaps = [
                (t.id, (t.first_payload or {}).get("type", "")) for t in candidates
            ]
            cand_ids = [t.id for t in candidates]
            if cand_ids:
                artifact_rows = (
                    await read_session.execute(
                        select(Artifact.task_id).where(
                            Artifact.task_id.in_(cand_ids),
                            Artifact.artifact_type == ArtifactType.PULL_REQUEST,
                        )
                    )
                ).all()
                tasks_with_pr = {row[0] for row in artifact_rows}

                child_rows = (
                    await read_session.execute(
                        select(Task.parent_task_id, Task.payloads).where(
                            Task.parent_task_id.in_(cand_ids)
                        )
                    )
                ).all()
                children_by_parent: dict = {}
                for parent_id, payloads in child_rows:
                    children_by_parent.setdefault(parent_id, []).append(payloads)
            else:
                tasks_with_pr = set()
                children_by_parent = {}
        # read_session closed — no session held across the per-row loop.

        for task_id, task_type in cand_snaps:
            # C3 heartbeat: serial GitHub I/O per candidate — pump the
            # orchestrator progress heartbeat each row so a long-but-
            # progressing recovery pass is not watchdog-killed.
            _mark_orchestrator_progress()
            # Canonical write-task-type allowlist: a task type that opens a PR
            # is recovery-eligible. Sourced from the registry so a newly-added
            # write type is honored here automatically (no string-literal tuple
            # to keep in sync). See app/task_capabilities.py:WRITE_TASK_TYPES.
            if task_type not in services.WRITE_TASK_TYPES:
                continue

            # Check if PR artifact exists
            if task_id in tasks_with_pr:
                continue

            # Check if review child exists
            child_payloads_list = children_by_parent.get(task_id, [])
            if any(
                (payloads or [{}])[0].get("type") == "review"
                for payloads in child_payloads_list
            ):
                continue

            # Audit P1: open a FRESH session per orphan. A _bounded() token /
            # list-pulls cancellation that poisons THIS session is confined to
            # this iteration — the `async with` closes it on the way out and the
            # next orphan gets a clean session, so one slow / cancelled lookup
            # no longer drops the rest of the sweep's artifact/alert/spawn work.
            async with services.AsyncSessionLocal() as session:
                # Re-fetch the orphan fresh on this session (the spawn path
                # below needs a live ORM instance bound to it) and re-validate
                # it is still a COMPLETED orphan.
                task = await session.get(Task, task_id)
                if task is None or task.status != TaskStatus.COMPLETED:
                    continue

                # Orphan has no PR yet. Cap retries — if we've looked 3 times
                # and found nothing, stop burning API quota on this row until
                # new evidence (an artifact showing up via webhook) arrives.
                # Recovery service runs every 120s; 3 attempts = ~6 min window
                # which is enough to catch the usual "PR created a few seconds
                # after agent reported done" race but won't keep polling for
                # the full 55-minute scan window.
                ctx = task.context or {}
                attempts_so_far = int(ctx.get("_recovery_attempts", 0))
                if attempts_so_far >= 3:
                    continue

                try:
                    gh_token, owner, repo = await services._bounded(
                        get_github_token_for_task(task.id, session),
                        services._GH_CALL_TIMEOUT_S,
                        what=f"recovery token {str(task.id)[:8]}",
                    )
                except Exception:
                    logger.warning(
                        f"[RECOVERY] Skipping orphan {str(task.id)[:8]} — GitHub token fetch failed",
                        exc_info=True,
                    )
                    continue

                _proj = await session.get(Project, task.project_id)
                branch = services._derive_task_branch(
                    task.id,
                    task_type,
                    _shared_state.get_project_task_branch_namespace(_proj),
                )

                try:
                    from app.github import _github_request

                    headers = {
                        "Authorization": f"Bearer {gh_token}",
                        "Accept": "application/vnd.github.v3+json",
                        "X-GitHub-Api-Version": "2022-11-28",
                    }
                    resp = await services._bounded(
                        _github_request(
                            "GET",
                            f"https://api.github.com/repos/{owner}/{repo}/pulls?head={owner}:{branch}&state=open",
                            headers,
                        ),
                        services._GH_CALL_TIMEOUT_S,
                        what=f"recovery list-pulls {str(task.id)[:8]}",
                    )
                    prs = resp.json() if resp.status_code == 200 else []
                except Exception:
                    logger.warning(
                        f"[RECOVERY] Skipping orphan {str(task.id)[:8]} — GitHub PR lookup failed",
                        exc_info=True,
                    )
                    continue

                if not prs:
                    # Record the miss so we back off after N failed lookups.
                    task.context = {**ctx, "_recovery_attempts": attempts_so_far + 1}
                    session.add(task)
                    await session.commit()
                    logger.info(
                        f"[RECOVERY] Skipping orphan {str(task.id)[:8]} — no open PR found for branch {branch} (attempt {attempts_so_far + 1}/3)"
                    )
                    continue

                pr = prs[0]
                pr_url = pr["html_url"]
                pr_number = pr["number"]
                base_branch = task.first_payload.get(
                    "base_branch"
                ) or _shared_state.get_project_integration_branch(_proj)

                # Create the missing artifact
                artifact = Artifact(
                    task_id=task.id,
                    artifact_type=ArtifactType.PULL_REQUEST,
                    pr_url=pr_url,
                )
                session.add(artifact)

                original_prompt = task.first_payload.get(
                    "prompt", task.description or ""
                )
                await _spawn_review_for_recovered_task(
                    source_task=task,
                    session=session,
                    pr_number=pr_number,
                    branch=branch,
                    base_branch=base_branch,
                    original_prompt=original_prompt,
                    original_task_id=str(task.id),
                    project_id=task.project_id,
                    short_id=str(task.id)[:8],
                    alert_title=f"Auto-recovered orphaned PR #{pr_number}",
                    alert_message=f"Task {str(task.id)[:8]} completed with PR #{pr_number} but review was never spawned. Auto-recovered.",
                    log_message=f"[RECOVERY] Auto-recovered orphaned task {str(task.id)[:8]} → PR #{pr_number}",
                )

        # Phase 2: Reaped-with-artifact recovery.
        # Tasks that were reaped (set back to QUEUED/PROVISIONING) after the PR
        # was already created. The PR endpoint saves the artifact, so these
        # tasks have an artifact but never reached COMPLETED and thus never
        # triggered pipeline routing.
        #
        # Audit P1: same Phase-A (short-lived read session, snapshot, close) /
        # Phase-B (per-row session) split as Phase 1 above.
        async with services.AsyncSessionLocal() as read_session:
            reaped_candidates = (
                (
                    await read_session.execute(
                        select(Task).where(
                            Task.status.in_(
                                [TaskStatus.QUEUED, TaskStatus.PROVISIONING]
                            ),
                            Task.created_by == "ORCHESTRATOR",
                            Task.attempt_count > 0,
                            Task.created_at >= one_hour_ago,
                        )
                    )
                )
                .scalars()
                .all()
            )

            reaped_snaps = [
                (t.id, (t.first_payload or {}).get("type", ""))
                for t in reaped_candidates
            ]
            reaped_ids = [t.id for t in reaped_candidates]
            if reaped_ids:
                pr_artifact_rows = (
                    await read_session.execute(
                        select(Artifact.task_id, Artifact.pr_url).where(
                            Artifact.task_id.in_(reaped_ids),
                            Artifact.artifact_type == ArtifactType.PULL_REQUEST,
                        )
                    )
                ).all()
                pr_url_by_task: dict = {}
                for _t_id, _pr_url in pr_artifact_rows:
                    if _pr_url and not pr_url_by_task.get(_t_id):
                        pr_url_by_task[_t_id] = _pr_url

                reaped_child_rows = (
                    await read_session.execute(
                        select(Task.parent_task_id, Task.payloads).where(
                            Task.parent_task_id.in_(reaped_ids)
                        )
                    )
                ).all()
                reaped_children_by_parent: dict = {}
                for parent_id, payloads in reaped_child_rows:
                    reaped_children_by_parent.setdefault(parent_id, []).append(payloads)
            else:
                pr_url_by_task = {}
                reaped_children_by_parent = {}
        # read_session closed — no session held across the per-row loop.

        for task_id, task_type in reaped_snaps:
            # Same canonical write-task-type allowlist as _recover_orphaned_tasks
            # — a reaped task that opened a PR is eligible for review handoff.
            # See app/task_capabilities.py:WRITE_TASK_TYPES.
            if task_type not in services.WRITE_TASK_TYPES:
                continue

            # Only recover tasks that already have a PR artifact
            pr_url = pr_url_by_task.get(task_id)
            if not pr_url:
                continue

            # Skip if review child already exists
            child_payloads_list = reaped_children_by_parent.get(task_id, [])
            if any(
                (payloads or [{}])[0].get("type") == "review"
                for payloads in child_payloads_list
            ):
                continue

            # Audit P1: per-row session. The lock+release-commit+spawn unit
            # runs in its own session so a cancellation/poison on one reaped
            # row can't drop the others.
            async with services.AsyncSessionLocal() as session:
                # ── Site 3 lock-during-IO fix ──
                # Previously this loop held FOR UPDATE on `task` across
                # `_spawn_pipeline_task`, which acquires
                # `pg_advisory_xact_lock(pr_number)` via
                # `acquire_pr_spawn_lock` (task_locking.py:223). If a
                # peer worker was mid-spawn for the same PR, this
                # iteration would block on the advisory lock with the
                # task FOR UPDATE held — starving any other writer
                # touching this row. Fix: hold the task FOR UPDATE only
                # for the COMPLETED transition, commit to release it,
                # then call `_spawn_pipeline_task` (the advisory lock is
                # per-PR and self-coordinating). The spawn-and-alert
                # commit follows on a fresh transaction.
                locked = (
                    (
                        await session.execute(
                            select(Task)
                            .where(Task.id == task_id)
                            .with_for_update(skip_locked=True)
                        )
                    )
                    .scalars()
                    .first()
                )
                if not locked or locked.status not in (
                    TaskStatus.QUEUED,
                    TaskStatus.PROVISIONING,
                ):
                    continue

                pr_number = int(pr_url.rstrip("/").split("/")[-1])
                short_id = str(locked.id).replace("-", "")[:8]
                _proj = await session.get(Project, locked.project_id)
                base_branch = locked.first_payload.get(
                    "base_branch"
                ) or _shared_state.get_project_integration_branch(_proj)
                branch = services._derive_task_branch(
                    locked.id,
                    task_type,
                    _shared_state.get_project_task_branch_namespace(_proj),
                )
                original_prompt = locked.first_payload.get(
                    "prompt", locked.description or ""
                )
                project_id_local = locked.project_id
                task_id_local = locked.id

                locked.status = TaskStatus.COMPLETED
                locked.latest_status_message = (
                    f"Auto-completed: PR #{pr_number} found after reap recovery"
                )
                session.add(locked)
                # Commit-release the task FOR UPDATE before invoking the
                # spawn (which acquires the per-PR advisory lock). The
                # task is now COMPLETED and persisted; the spawn can
                # proceed on a fresh transaction without holding the
                # task row lock across the advisory-lock acquire.
                await session.commit()

                # Re-fetch a fresh, unlocked instance for the spawn. The
                # spawn path reads source_task fields (context, payloads,
                # title) and writes back ``latest_status_message`` /
                # ``context`` markers; using the same `locked` object
                # without re-attach risks DetachedInstanceError after the
                # commit released the SQLA identity for some attributes.
                refreshed = await session.get(Task, task_id_local)
                if refreshed is None:
                    logger.warning(
                        f"[RECOVERY] Task {short_id} disappeared after release-commit — skipping spawn"
                    )
                    continue
                await _spawn_review_for_recovered_task(
                    source_task=refreshed,
                    session=session,
                    pr_number=pr_number,
                    branch=branch,
                    base_branch=base_branch,
                    original_prompt=original_prompt,
                    original_task_id=str(task_id_local),
                    project_id=project_id_local,
                    short_id=short_id,
                    alert_title=f"Auto-recovered reaped task with PR #{pr_number}",
                    alert_message=f"Task {short_id} was reaped after creating PR #{pr_number}. Auto-completed and review spawned.",
                    log_message=f"[RECOVERY] Auto-completed reaped task {short_id} → PR #{pr_number}",
                )
    except Exception as e:
        logger.error(f"[RECOVERY] Orphaned task sweep failed: {e}", exc_info=True)
