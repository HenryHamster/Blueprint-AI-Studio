"""``main → automain`` back-merge — keep the integration trunk in sync with ``main``.

Phase 5 of the automain LTS design (docs/plans/automain-lts-plan-v1.md), since
generalised so the back-merge runs for ANY advance of ``main``, not only
hotfixes.

``main`` is the human-grade LTS branch; ``automain`` is the pipeline's
integration trunk.  ``main`` advances two ways the integration trunk does not
automatically pick up:

  * a **hotfix** — an expedited fix branched off ``main`` that merges straight
    to ``main`` so ``deploy.yml`` fires (the post-rollup-merge handler in
    task_lifecycle.py calls :func:`open_backmerge_pr` once per hotfix);
  * a **direct push** to ``main`` — a human landing a fix straight on the LTS
    branch, which carries no task and no ``hotfix:true`` marker at all.

Either way ``main`` diverges past ``automain``.  A missed back-merge makes the
next ``automain → main`` promotion conflict — so every advance of ``main`` MUST
be followed by a ``main → automain`` back-merge.

Two entry points:

  * :func:`open_backmerge_pr` — opens + merges one ``main → automain`` PR.  The
    exact inverse of ``release_qualifier.promote()``.  Called once by the
    hotfix-merge handler (``periodic=False``).
  * :func:`reconcile_main_into_automain` — the periodic reconciler.  Runs every
    scheduler cycle and catches whatever the hotfix trigger missed — most
    importantly direct pushes, which have no marker to trigger on.  Delegates
    to :func:`open_backmerge_pr` with ``periodic=True``.

It deliberately reuses ``promote``'s structure and the same GitHub helpers
(``create_pull_request`` / ``merge_pull_request``) and credential resolver
(``release_qualifier._resolve_github_with_session``).

Failure philosophy (design doc §6 gotcha 6 — "the back-merge conflict-alert is
mandatory, not optional"): a back-merge that does not complete must never pass
silently.  The behaviour differs by caller:

  * ``periodic=False`` (a one-shot hotfix back-merge) — every non-merge raises
    an Alert; the operator must see a stuck one-shot back-merge.
  * ``periodic=True`` (the reconciler, which retries every cycle) — alerting is
    deduped, never suppressed: a blocked back-merge raises exactly one
    unresolved Alert per project (the first stuck cycle escalates; later cycles
    stay quiet until it is dismissed), and a genuine conflict's Alert +
    resolution plan are likewise deduped project-wide.  So a persistent failure
    surfaces once — not never, and not every cycle.

``open_backmerge_pr`` is idempotent: ``github.create_pull_request`` returns the
existing open PR on a 422 "already exists", so calling it repeatedly (the
periodic case) reuses the in-flight PR and re-attempts its merge rather than
opening duplicates.

The caller commits the session.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app import github
from app.broadcast import broadcast_to_project

# release_qualifier does NOT import this module, so importing from it here is
# safe — no circular import.  _resolve_github_with_session is the shared
# credential resolver used by the whole LTS machinery.
from app.release_qualifier import _resolve_github_with_session
from app.models import (
    Alert,
    AlertSeverity,
    Project,
    Task,
    TaskHistory,
    TaskStatus,
)
from app.payload_schemas import PayloadTooLargeError, validate_internal_payload
from app.shared_state import runtime_config as _runtime_config

logger = logging.getLogger(__name__)

# Title of the conflict-resolution Alert.  Used both when creating the Alert
# and when checking whether an unresolved one already exists, so a retried /
# periodic back-merge does not stack duplicate alerts.
_CONFLICT_ALERT_TITLE = "Back-merge conflict: main -> automain"

# Title of the generic (non-conflict) failure Alert — a 405, a PR-creation
# failure, or any unexpected exception.  Only raised for one-shot callers
# (``periodic=False``); the periodic reconciler logs + retries instead.
_BLOCKED_ALERT_TITLE = "Back-merge failed: main -> automain"

# The conflict-resolution plan task always targets automain — it is a normal
# automain-bound fix, NOT a hotfix.  It must NOT carry hotfix:true.
_RESOLUTION_BASE_BRANCH = "automain"

# Priority for the conflict-resolution plan task — expedited (design doc:
# back-merge conflict resolution gates the next promotion; >= 90).
_RESOLUTION_PRIORITY = 95

# Terminal task statuses — a resolution plan in one of these no longer counts
# as "open" for the duplicate-spawn guard.
_TERMINAL_STATUSES: frozenset[TaskStatus] = frozenset(
    {TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.CANCELLED}
)


async def reconcile_main_into_automain(
    session: AsyncSession,
    project_id: uuid.UUID,
) -> dict[str, Any]:
    """Periodically back-merge ``main`` into ``automain`` — the catch-all sync.

    The hotfix-triggered back-merge (:func:`open_backmerge_pr` from the
    post-rollup-merge handler) only fires for tasks marked ``hotfix:true``.  A
    *direct push* to ``main`` — a human landing a fix straight on the LTS
    branch — carries no such marker, so nothing back-merges it and the branches
    silently diverge until the next ``automain → main`` promotion conflicts.
    This reconciler closes that gap: it runs every scheduler cycle regardless
    of HOW ``main`` advanced.

    No-op unless ``integration_branch == "automain"`` — when the pipeline still
    builds on ``main`` there is no separate trunk to reconcile (and a
    ``main → main`` PR would be nonsense).

    Delegates to :func:`open_backmerge_pr` with ``periodic=True``: a back-merge
    merely waiting on CI is logged and retried next cycle rather than raising
    an Alert every cycle; a genuine conflict still raises one Alert and spawns
    one resolution plan (deduped project-wide).

    The caller commits the session.  Returns a status dict — ``open_backmerge_pr``'s
    result, or ``{"status": "skipped", "reason": "lts_inactive"}`` when the LTS
    branch model is not active.
    """
    integration_branch = str(_runtime_config.get("integration_branch", "main"))
    if integration_branch != "automain":
        return {"status": "skipped", "reason": "lts_inactive"}
    return await open_backmerge_pr(session, project_id, periodic=True)


async def open_backmerge_pr(
    session: AsyncSession,
    project_id: uuid.UUID,
    *,
    merged_pr_url: str = "",
    hotfix_task_id: uuid.UUID | str | None = None,
    periodic: bool = False,
) -> dict[str, Any]:
    """Open and merge a ``main → automain`` back-merge PR.

    The inverse of ``release_qualifier.promote()``.  Steps:

      1. Load the project; resolve ``(token, owner, repo)``.
      2. ``create_pull_request`` ``main → automain``.  Idempotent — on a 422
         "already exists" ``create_pull_request`` returns the existing open PR,
         so a repeated (periodic) call reuses the in-flight PR.
      3. If the PR result has ``empty_diff`` truthy (``main == automain`` —
         nothing to back-merge), return ``{"status": "noop_empty_diff"}``.
      4. ``merge_pull_request`` with ``merge_method="merge"`` (preserve the
         ``main`` commit history on ``automain``).
      5. On ``merged`` truthy, return ``{"status": "merged", "pr_url": ...}``.
      6. On ``conflict`` (a 409), raise an ``Alert`` AND spawn a high-priority
         ``plan`` task instructing a ``git merge origin/main`` conflict
         resolution against ``automain``; return ``{"status": "conflict"}``.
         The plan task does NOT carry ``hotfix:true``.  Alert + plan are
         deduped project-wide — only one resolution is ever needed at a time.
      7. On any other non-merge (a 405) or any exception, raise an ``Alert``
         (``periodic=False``) — a one-shot back-merge that fails must never
         pass silently — and return ``{"status": "blocked"}``.

    ``periodic`` — set ``True`` by :func:`reconcile_main_into_automain`, which
    calls this every scheduler cycle.  A periodic caller dedups its alerting so
    it neither storms (an Alert every cycle) nor goes silent: a blocked
    non-merge (step 7) raises one unresolved Alert per project until dismissed,
    and the conflict path (step 6) raises one Alert + one plan, both deduped.

    ``merged_pr_url`` / ``hotfix_task_id`` describe the hotfix that triggered
    this back-merge, when there was one; the periodic reconciler passes
    neither.  They are recorded in the PR body, the Alert message, and the
    resolution-plan context for traceability.

    The caller commits the session.  Returns a status dict.
    """
    hotfix_task_id_str = str(hotfix_task_id) if hotfix_task_id is not None else None
    trigger_desc = _describe_trigger(merged_pr_url, hotfix_task_id_str)

    # populate_existing=True forces a fresh SELECT instead of returning a
    # possibly-expired identity-map hit. The branch-reconciliation service
    # (lifecycle._branch_reconciliation_service) reuses one long-lived session
    # across all projects, committing/rolling back per iteration — both expire
    # identity-map instances. Without the refresh, a later `project.github_owner`
    # read in _resolve_github_with_session lazy-loads under async and raises
    # MissingGreenlet, crash-looping the reconciler (so main→automain never
    # back-merges and the branches silently diverge).
    project = await session.get(Project, project_id, populate_existing=True)
    if project is None:
        logger.warning(
            "[BACKMERGE] open_backmerge_pr: project %s not found",
            project_id,
        )
        return {"status": "blocked", "error": "project_not_found"}

    # 1. Resolve GitHub credentials (App token → installation → PAT).
    resolved = await _resolve_github_with_session(session, project)
    if resolved is None:
        logger.warning(
            "[BACKMERGE] no GitHub credentials for project %s — "
            "cannot back-merge",
            project_id,
        )
        await _raise_blocked_alert(
            session,
            project_id,
            trigger_desc,
            reason="no GitHub credentials available",
            periodic=periodic,
        )
        return {"status": "blocked", "error": "no_credentials"}
    token, owner, repo = resolved

    # 2. Open the main → automain back-merge PR.  create_pull_request returns
    #    the existing open PR on a 422 "already exists", so this is idempotent
    #    when the periodic reconciler calls it repeatedly.
    trigger_line = f"Triggered by: {trigger_desc}\n\n" if trigger_desc else ""
    try:
        pr = await github.create_pull_request(
            token,
            owner,
            repo,
            title="Back-merge: main -> automain",
            head="main",
            base="automain",
            body=(
                "Automated back-merge of the LTS branch `main` into the "
                "integration trunk `automain`.\n\n"
                f"{trigger_line}"
                "Without this back-merge `main` and `automain` diverge and the "
                "next `automain -> main` promotion conflicts (see "
                "docs/plans/automain-lts-plan-v1.md Phase 5).\n\n"
                "Opened by the back-merge machinery "
                "(backend/app/hotfix_backmerge.py)."
            ),
            project_id=str(project_id),
        )
    except Exception as exc:  # noqa: BLE001 — a PR-creation failure blocks, never raises
        logger.warning(
            "[BACKMERGE] failed to open back-merge PR for project %s: %s",
            project_id,
            exc,
        )
        await _raise_blocked_alert(
            session,
            project_id,
            trigger_desc,
            reason=f"could not open the back-merge PR: {str(exc)[:200]}",
            periodic=periodic,
        )
        return {"status": "blocked", "error": "pr_creation_failed"}

    # 3. Empty diff — main == automain, nothing to back-merge.  A degenerate
    #    but valid outcome: create_pull_request returns
    #    {"empty_diff": True, "merged": True}.
    if pr.get("empty_diff"):
        logger.info(
            "[BACKMERGE] project %s: main == automain (empty diff) — "
            "nothing to back-merge",
            project_id,
        )
        return {"status": "noop_empty_diff"}

    pr_number = pr.get("number")
    pr_url = pr.get("html_url", "")
    if not pr_number:
        logger.warning(
            "[BACKMERGE] back-merge PR for project %s has no number",
            project_id,
        )
        await _raise_blocked_alert(
            session,
            project_id,
            trigger_desc,
            reason="the back-merge PR was created without a PR number",
            periodic=periodic,
        )
        return {"status": "blocked", "error": "pr_missing_number"}

    # 4. Merge the PR — merge_method="merge" preserves the main commit history.
    #    merge_pull_request does NOT raise on 405/409; it returns merged=False.
    try:
        merge_result = await github.merge_pull_request(
            token,
            owner,
            repo,
            pr_number,
            merge_method="merge",
            project_id=str(project_id),
        )
    except Exception as exc:  # noqa: BLE001 — a 5xx raises HTTPException; treat as blocked
        logger.warning(
            "[BACKMERGE] merge raised for back-merge PR #%s "
            "(project %s): %s",
            pr_number,
            project_id,
            exc,
        )
        await _raise_blocked_alert(
            session,
            project_id,
            trigger_desc,
            reason=f"merging back-merge PR #{pr_number} raised: {str(exc)[:200]}",
            pr_url=pr_url,
            periodic=periodic,
        )
        return {"status": "blocked", "error": "merge_error"}

    # 5. Success.
    if merge_result.get("merged"):
        logger.info(
            "[BACKMERGE] project %s: main -> automain back-merge "
            "merged via PR #%s",
            project_id,
            pr_number,
        )
        return {"status": "merged", "pr_url": pr_url}

    msg = merge_result.get("message", "merge not completed")

    # 6. Conflict (409) — raise an Alert AND spawn a conflict-resolution plan.
    if merge_result.get("conflict"):
        logger.warning(
            "[BACKMERGE] back-merge PR #%s for project %s hit a merge "
            "conflict: %s",
            pr_number,
            project_id,
            msg,
        )
        await _handle_conflict(
            session,
            project_id,
            pr_number,
            pr_url,
            hotfix_task_id_str,
            trigger_desc,
            message=str(msg),
            periodic=periodic,
        )
        return {"status": "conflict", "pr_url": pr_url}

    # 7. Any other non-merge (405 — checks/reviews block).  _raise_blocked_alert
    #    escalates: always for a one-shot caller, once-deduped for the periodic
    #    reconciler — so a persistently stuck back-merge surfaces without a storm.
    logger.warning(
        "[BACKMERGE] back-merge PR #%s for project %s did not merge: %s",
        pr_number,
        project_id,
        msg,
    )
    await _raise_blocked_alert(
        session,
        project_id,
        trigger_desc,
        reason=(
            f"back-merge PR #{pr_number} did not merge "
            f"(branch protection / checks): {str(msg)[:200]}"
        ),
        pr_url=pr_url,
        periodic=periodic,
    )
    return {"status": "blocked", "error": "merge_blocked", "pr_url": pr_url}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _describe_trigger(merged_pr_url: str, hotfix_task_id: str | None) -> str:
    """Human-readable reference to what advanced ``main``, or ``""`` if unknown.

    A hotfix-triggered back-merge passes the merged hotfix PR URL and/or the
    hotfix task id.  The periodic reconciler passes neither — a direct push to
    ``main`` has no task — in which case this returns ``""`` and callers omit
    the trigger clause entirely rather than printing a misleading "hotfix".
    """
    parts: list[str] = []
    if merged_pr_url:
        parts.append(merged_pr_url)
    if hotfix_task_id:
        parts.append(f"task {hotfix_task_id}")
    if not parts:
        return ""
    return "hotfix " + " / ".join(parts)


async def _resolution_plan_already_open(
    session: AsyncSession,
    project_id: uuid.UUID,
) -> bool:
    """Return True if a back-merge conflict-resolution plan is already open.

    Only ONE such plan is ever needed at a time: it runs ``git merge
    origin/main`` into ``automain``, which reconciles ALL divergence regardless
    of how many hotfixes or direct pushes caused it.  The guard is therefore
    project-wide (not per-hotfix) — it dedupes the spawn for every trigger: the
    hotfix path retried for one hotfix, a second hotfix, and the periodic
    reconciler called every cycle.

    A resolution plan is identified by ``context["backmerge_conflict"] is
    True``.  Only non-terminal tasks count as "open" — a CANCELLED/FAILED prior
    attempt does not block a fresh spawn.
    """
    rows = (
        (
            await session.execute(
                select(Task).where(
                    Task.project_id == project_id,
                    Task.status.notin_(_TERMINAL_STATUSES),
                )
            )
        )
        .scalars()
        .all()
    )
    return any(
        (task.context or {}).get("backmerge_conflict") is True for task in rows
    )


async def _handle_conflict(
    session: AsyncSession,
    project_id: uuid.UUID,
    pr_number: int,
    pr_url: str,
    hotfix_task_id: str | None,
    trigger_desc: str,
    *,
    message: str,
    periodic: bool,
) -> None:
    """Raise a conflict Alert and (if not already open) spawn a resolution plan.

    A back-merge conflict needs human/agent resolution — it is never transient
    — so it always escalates.  Alert + resolution ``plan`` task are spawned
    together, both gated by :func:`_resolution_plan_already_open`:

      * ``periodic=False`` (one-shot hotfix back-merge) — when a resolution
        plan is already open the plan is not duplicated, but an Alert is still
        raised so the operator sees a recurrence of a one-shot back-merge.
      * ``periodic=True`` (the reconciler retries every cycle) — when a
        resolution plan is already open NEITHER a duplicate Alert NOR a
        duplicate plan is created, so a persistent conflict yields one of each
        instead of one per cycle.

    Concurrency note: the dedup reads committed state, so two genuinely
    concurrent callers — the promotion-conflict hook and the periodic
    reconciler ticking in the same instant — can each pass the check before
    either commits and both spawn.  Accepted as benign: the window is tiny and
    both resolution plans run the same ``git merge origin/main``, so a
    duplicate is redundant, never destructive.
    """
    already_open = await _resolution_plan_already_open(session, project_id)

    if periodic and already_open:
        # The reconciler runs every cycle — a resolution plan is already open
        # and being worked; staying quiet keeps a persistent conflict from
        # stacking an Alert + plan each cycle.
        logger.info(
            "[BACKMERGE] back-merge conflict for project %s — a resolution "
            "plan is already open; periodic reconciler stays quiet",
            project_id,
        )
        return

    trigger_clause = f" (triggered by {trigger_desc})" if trigger_desc else ""
    alert = Alert(
        project_id=project_id,
        severity=AlertSeverity.ERROR,
        title=_CONFLICT_ALERT_TITLE,
        message=(
            f"The automated main -> automain back-merge "
            f"(PR #{pr_number}{trigger_clause}) hit a merge conflict and "
            f"could not be merged automatically. A resolution plan task has "
            f"been spawned. Until automain absorbs main, the next "
            f"automain -> main promotion will conflict. ({message[:160]})"
        ),
    )
    session.add(alert)
    await session.flush()

    spawned_plan: Task | None = None
    if already_open:
        logger.info(
            "[BACKMERGE] a back-merge conflict-resolution plan is already "
            "open for project %s — not spawning a duplicate",
            project_id,
        )
    else:
        spawned_plan = await _spawn_resolution_plan(
            session,
            project_id,
            pr_number,
            pr_url,
            hotfix_task_id,
            trigger_desc,
        )

    await _broadcast_alert(project_id, alert)
    if spawned_plan is not None:
        await broadcast_to_project(
            str(project_id),
            "task_updated",
            {"task_id": str(spawned_plan.id), "status": TaskStatus.QUEUED},
        )


async def _spawn_resolution_plan(
    session: AsyncSession,
    project_id: uuid.UUID,
    pr_number: int,
    pr_url: str,
    hotfix_task_id: str | None,
    trigger_desc: str,
) -> Task:
    """Create + queue a high-priority ``plan`` task to resolve the conflict.

    The plan targets ``automain`` (``base_branch="automain"``) and instructs a
    ``git merge origin/main`` conflict resolution.  It is a NORMAL
    automain-bound fix — it deliberately does NOT carry ``hotfix:true``.
    """
    trigger_clause = f" ({trigger_desc})" if trigger_desc else ""
    prompt = (
        f"The automated `main -> automain` back-merge PR #{pr_number} "
        f"({pr_url or 'no URL'}) hit a merge conflict and could not be merged.\n\n"
        f"A change has landed on `main` that `automain` does not have"
        f"{trigger_clause}, and `automain` must absorb it or the next "
        f"`automain -> main` promotion will conflict.\n\n"
        f"Resolve it: from a checkout of `automain`, run `git merge origin/main`, "
        f"resolve every conflict so that `automain` contains both the `main` "
        f"changes and the in-flight integration work, and commit the merge. "
        f"Do NOT discard the `main` changes and do NOT discard automain's "
        f"integration work.\n\n"
        f"IMPORTANT GUARDRAILS:\n"
        f"- This is a normal automain-targeted fix, NOT a hotfix.\n"
        f"- Do NOT delete or skip tests just to make the merge build pass.\n"
        f"- Keep both sides of every conflict reconciled — a `main` line and an "
        f"integration line that touch the same code must be merged, not "
        f"clobbered.\n"
        f"- Produce subtasks that land the resolved merge on `automain`."
    )

    payload: dict[str, Any] = {
        "type": "plan",
        "prompt": prompt,
        "base_branch": _RESOLUTION_BASE_BRANCH,
    }
    type_model = (_runtime_config.get("task_type_models", {}) or {}).get("plan")
    if type_model:
        payload["model"] = type_model
    type_effort = (_runtime_config.get("task_type_efforts", {}) or {}).get("plan")
    if type_effort:
        payload["effort"] = type_effort
    try:
        payload = validate_internal_payload(
            payload, "hotfix_backmerge:conflict_resolution_plan"
        )
    except PayloadTooLargeError:
        # The resolution-plan payload is tiny; a too-large error is a real bug.
        raise

    task = Task(
        project_id=project_id,
        title="Resolve back-merge conflict: main -> automain",
        description=(
            f"The automated main -> automain back-merge PR #{pr_number} hit a "
            f"merge conflict; resolve it on automain so the next promotion "
            f"does not conflict."
        ),
        priority=_RESOLUTION_PRIORITY,
        payloads=[payload],
        context={
            # Marker field — _resolution_plan_already_open dedupes on this.
            "backmerge_conflict": True,
            # Traceability only (the dedup is project-wide, not keyed on this).
            "hotfix_task_id": hotfix_task_id,
            "backmerge_pr_number": pr_number,
            "backmerge_pr_url": pr_url,
        },
        vm_instance_id=f"agent-vm-{uuid.uuid4().hex}",
        status=TaskStatus.QUEUED,
        created_by="ORCHESTRATOR",
    )
    session.add(task)
    await session.flush()
    session.add(
        TaskHistory(
            task_id=task.id,
            action="SPAWNED",
            details={
                "spawned_by": "BACKMERGE_CONFLICT",
                "priority": _RESOLUTION_PRIORITY,
                "backmerge_pr_number": pr_number,
                "hotfix_task_id": hotfix_task_id,
            },
        )
    )
    logger.warning(
        "[BACKMERGE] spawned conflict-resolution plan task %s "
        "(priority=%d) for back-merge PR #%s",
        task.id,
        _RESOLUTION_PRIORITY,
        pr_number,
    )
    return task


async def _raise_blocked_alert(
    session: AsyncSession,
    project_id: uuid.UUID,
    trigger_desc: str,
    *,
    reason: str,
    pr_url: str | None = None,
    periodic: bool = False,
) -> None:
    """Surface a non-conflict back-merge failure (missing creds, PR-creation
    failure, merge exception, 405 non-merge).

    ``periodic=False`` (a one-shot hotfix back-merge) — always create an ERROR
    Alert: a failed one-shot back-merge must never pass silently (design doc §6
    gotcha 6).

    ``periodic=True`` (the reconciler, which retries every cycle) — create an
    ERROR Alert too, but DEDUPED: only when no unresolved back-merge-blocked
    Alert is already open for the project.  A periodic caller must not go
    silent on a persistently stuck back-merge (CI red forever, revoked
    credentials) — that would leave main and automain diverged with no signal
    — but it must not stack an Alert every 10-minute cycle either.  So the
    first stuck cycle escalates with exactly one Alert; later cycles log at
    WARNING and stay quiet until that Alert is dismissed.

    No fix task is spawned either way — a 405 is a branch-protection / CI
    problem the operator resolves, not a code conflict.
    """
    if periodic:
        existing = (
            (
                await session.execute(
                    select(Alert).where(
                        Alert.project_id == project_id,
                        Alert.title == _BLOCKED_ALERT_TITLE,
                        Alert.dismissed_at.is_(None),
                    )
                )
            )
            .scalars()
            .first()
        )
        if existing is not None:
            logger.warning(
                "[BACKMERGE] periodic back-merge for project %s still "
                "blocked: %s — an unresolved Alert is already open; staying "
                "quiet until it is dismissed",
                project_id,
                reason,
            )
            return
        logger.warning(
            "[BACKMERGE] periodic back-merge for project %s blocked: %s — "
            "raising one Alert (will not repeat until dismissed)",
            project_id,
            reason,
        )

    trigger_clause = f" (triggered by {trigger_desc})" if trigger_desc else ""
    message = (
        f"The automated main -> automain back-merge{trigger_clause} did not "
        f"complete: {reason}. main and automain remain diverged — the next "
        f"automain -> main promotion will conflict until this is resolved "
        f"manually."
    )
    if pr_url:
        message += f" Back-merge PR: {pr_url}"
    alert = Alert(
        project_id=project_id,
        severity=AlertSeverity.ERROR,
        title=_BLOCKED_ALERT_TITLE,
        message=message,
    )
    session.add(alert)
    await session.flush()
    await _broadcast_alert(project_id, alert)


async def _broadcast_alert(project_id: uuid.UUID, alert: Alert) -> None:
    """Best-effort websocket broadcast of a created Alert to the project."""
    try:
        await broadcast_to_project(
            str(project_id),
            "alert_created",
            {
                "alert_id": str(alert.id),
                "severity": alert.severity,
                "title": alert.title,
            },
        )
    except Exception as exc:  # noqa: BLE001 — a broadcast failure must not block
        logger.debug(
            "[BACKMERGE] alert broadcast failed for project %s: %s",
            project_id,
            exc,
        )
