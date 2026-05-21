"""Hotfix back-merge — keep ``automain`` in sync with ``main`` after a hotfix.

Phase 5 of the automain LTS design (docs/plans/automain-lts-plan-v1.md).

A hotfix is an expedited fix branched off ``main`` that merges straight to
``main`` (so ``deploy.yml`` fires).  That merge advances ``main`` past
``automain``: the two branches diverge.  A missed back-merge makes the *next*
``automain → main`` promotion conflict — so every hotfix MUST be followed by a
``main → automain`` back-merge.

This module is the exact inverse of ``release_qualifier.promote()``:
``promote`` opens + merges ``automain → main``; ``open_backmerge_pr`` opens +
merges ``main → automain``.  It deliberately reuses ``promote``'s structure and
the same GitHub helpers (``create_pull_request`` / ``merge_pull_request``) and
credential resolver (``release_qualifier._resolve_github_with_session``).

Public API: ``open_backmerge_pr``.

Failure philosophy (design doc §6 gotcha 6 — "the back-merge conflict-alert is
mandatory, not optional"): a back-merge that does NOT complete must never pass
silently.  Every non-merge outcome raises an :class:`~app.models.Alert`; a
conflict additionally spawns a high-priority ``plan`` task to resolve the merge
by hand.  The caller commits the session.
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
# and (substring-matched) when checking whether an unresolved one already
# exists, so a retried back-merge does not stack duplicate alerts.
_CONFLICT_ALERT_TITLE = "Hotfix back-merge conflict: main -> automain"

# Title of the generic (non-conflict) failure Alert — a 405, a PR-creation
# failure, or any unexpected exception.
_BLOCKED_ALERT_TITLE = "Hotfix back-merge failed: main -> automain"

# The conflict-resolution plan task always targets automain — it is a normal
# automain-bound fix, NOT a hotfix.  It must NOT carry hotfix:true.
_RESOLUTION_BASE_BRANCH = "automain"

# Priority for the conflict-resolution plan task — expedited (design doc:
# hotfix work gets high priority; >= 90).
_RESOLUTION_PRIORITY = 95

# Terminal task statuses — a resolution plan in one of these no longer counts
# as "open" for the duplicate-spawn guard.
_TERMINAL_STATUSES: frozenset[TaskStatus] = frozenset(
    {TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.CANCELLED}
)


async def open_backmerge_pr(
    session: AsyncSession,
    project_id: uuid.UUID,
    *,
    merged_pr_url: str = "",
    hotfix_task_id: uuid.UUID | str | None = None,
) -> dict[str, Any]:
    """Open and merge a ``main → automain`` back-merge PR after a hotfix.

    The inverse of ``release_qualifier.promote()``.  Steps:

      1. Load the project; resolve ``(token, owner, repo)``.
      2. ``create_pull_request`` ``main → automain``.
      3. If the PR result has ``empty_diff`` truthy (``main == automain`` —
         nothing to back-merge), return ``{"status": "noop_empty_diff"}``.
      4. ``merge_pull_request`` with ``merge_method="merge"`` (preserve the
         hotfix commit history on ``automain``).
      5. On ``merged`` truthy, return ``{"status": "merged", "pr_url": ...}``.
      6. On ``conflict`` (a 409), raise an ``Alert`` AND spawn a high-priority
         ``plan`` task instructing a ``git merge origin/main`` conflict
         resolution against ``automain``; return ``{"status": "conflict"}``.
         The plan task does NOT carry ``hotfix:true`` — it is a normal
         automain-targeted fix.  A second resolution plan is NOT spawned if one
         is already open for the same hotfix.
      7. On any other non-merge (a 405) or any exception, raise an ``Alert`` —
         a failed back-merge must never pass silently — and return
         ``{"status": "blocked"}``.

    ``merged_pr_url`` / ``hotfix_task_id`` describe the hotfix that triggered
    this back-merge; they are recorded in the PR body, the Alert message, and
    the resolution-plan context for traceability.

    The caller commits the session.  Returns a status dict.
    """
    hotfix_task_id_str = str(hotfix_task_id) if hotfix_task_id is not None else None

    project = await session.get(Project, project_id)
    if project is None:
        logger.warning(
            "[HOTFIX_BACKMERGE] open_backmerge_pr: project %s not found",
            project_id,
        )
        return {"status": "blocked", "error": "project_not_found"}

    # 1. Resolve GitHub credentials (App token → installation → PAT).
    resolved = await _resolve_github_with_session(session, project)
    if resolved is None:
        logger.warning(
            "[HOTFIX_BACKMERGE] no GitHub credentials for project %s — "
            "cannot back-merge",
            project_id,
        )
        await _raise_blocked_alert(
            session,
            project_id,
            hotfix_task_id_str,
            merged_pr_url,
            reason="no GitHub credentials available",
        )
        return {"status": "blocked", "error": "no_credentials"}
    token, owner, repo = resolved

    hotfix_ref = _describe_hotfix(merged_pr_url, hotfix_task_id_str)

    # 2. Open the main → automain back-merge PR.
    try:
        pr = await github.create_pull_request(
            token,
            owner,
            repo,
            title="Back-merge: main -> automain after hotfix",
            head="main",
            base="automain",
            body=(
                "Automated back-merge of the LTS branch into the integration "
                "trunk after a hotfix merged to `main`.\n\n"
                f"Triggering hotfix: {hotfix_ref}\n\n"
                "Without this back-merge `main` and `automain` diverge and the "
                "next `automain -> main` promotion conflicts (see "
                "docs/plans/automain-lts-plan-v1.md Phase 5).\n\n"
                "Opened by the hotfix back-merge "
                "(backend/app/hotfix_backmerge.py)."
            ),
            project_id=str(project_id),
        )
    except Exception as exc:  # noqa: BLE001 — a PR-creation failure blocks, never raises
        logger.warning(
            "[HOTFIX_BACKMERGE] failed to open back-merge PR for project %s: %s",
            project_id,
            exc,
        )
        await _raise_blocked_alert(
            session,
            project_id,
            hotfix_task_id_str,
            merged_pr_url,
            reason=f"could not open the back-merge PR: {str(exc)[:200]}",
        )
        return {"status": "blocked", "error": "pr_creation_failed"}

    # 3. Empty diff — main == automain, nothing to back-merge.  A degenerate
    #    but valid outcome: create_pull_request returns
    #    {"empty_diff": True, "merged": True}.
    if pr.get("empty_diff"):
        logger.info(
            "[HOTFIX_BACKMERGE] project %s: main == automain (empty diff) — "
            "nothing to back-merge",
            project_id,
        )
        return {"status": "noop_empty_diff"}

    pr_number = pr.get("number")
    pr_url = pr.get("html_url", "")
    if not pr_number:
        logger.warning(
            "[HOTFIX_BACKMERGE] back-merge PR for project %s has no number",
            project_id,
        )
        await _raise_blocked_alert(
            session,
            project_id,
            hotfix_task_id_str,
            merged_pr_url,
            reason="the back-merge PR was created without a PR number",
        )
        return {"status": "blocked", "error": "pr_missing_number"}

    # 4. Merge the PR — merge_method="merge" preserves the hotfix history.
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
            "[HOTFIX_BACKMERGE] merge raised for back-merge PR #%s "
            "(project %s): %s",
            pr_number,
            project_id,
            exc,
        )
        await _raise_blocked_alert(
            session,
            project_id,
            hotfix_task_id_str,
            merged_pr_url,
            reason=f"merging back-merge PR #{pr_number} raised: {str(exc)[:200]}",
            pr_url=pr_url,
        )
        return {"status": "blocked", "error": "merge_error"}

    # 5. Success.
    if merge_result.get("merged"):
        logger.info(
            "[HOTFIX_BACKMERGE] project %s: main -> automain back-merge "
            "merged via PR #%s",
            project_id,
            pr_number,
        )
        return {"status": "merged", "pr_url": pr_url}

    msg = merge_result.get("message", "merge not completed")

    # 6. Conflict (409) — raise an Alert AND spawn a conflict-resolution plan.
    if merge_result.get("conflict"):
        logger.warning(
            "[HOTFIX_BACKMERGE] back-merge PR #%s for project %s hit a merge "
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
            merged_pr_url,
            message=str(msg),
        )
        return {"status": "conflict", "pr_url": pr_url}

    # 7. Any other non-merge (405 — checks/reviews block).  A failed back-merge
    #    must never pass silently.
    logger.warning(
        "[HOTFIX_BACKMERGE] back-merge PR #%s for project %s did not merge: %s",
        pr_number,
        project_id,
        msg,
    )
    await _raise_blocked_alert(
        session,
        project_id,
        hotfix_task_id_str,
        merged_pr_url,
        reason=(
            f"back-merge PR #{pr_number} did not merge "
            f"(branch protection / checks): {str(msg)[:200]}"
        ),
        pr_url=pr_url,
    )
    return {"status": "blocked", "error": "merge_blocked", "pr_url": pr_url}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _describe_hotfix(merged_pr_url: str, hotfix_task_id: str | None) -> str:
    """Human-readable reference to the hotfix that triggered this back-merge."""
    parts: list[str] = []
    if merged_pr_url:
        parts.append(merged_pr_url)
    if hotfix_task_id:
        parts.append(f"task {hotfix_task_id}")
    return " / ".join(parts) if parts else "unknown hotfix"


async def _resolution_plan_already_open(
    session: AsyncSession,
    project_id: uuid.UUID,
    hotfix_task_id: str | None,
) -> bool:
    """Return True if a conflict-resolution plan is already open for this hotfix.

    Guards the conflict-resolution spawn against duplicates: if
    ``open_backmerge_pr`` is invoked twice for the same hotfix (a retry, a
    re-delivered webhook), a second resolution plan must NOT be spawned.

    A resolution plan is identified by ``context["backmerge_conflict"] is True``
    and a matching ``context["hotfix_task_id"]``.  Only non-terminal tasks
    count as "open" — a CANCELLED/FAILED prior attempt does not block a fresh
    spawn.

    When ``hotfix_task_id`` is None the hotfix cannot be correlated, so the
    guard cannot dedupe — it returns False (spawn proceeds; the Alert still
    fires, so the situation is never silent).
    """
    if hotfix_task_id is None:
        return False

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
    for task in rows:
        ctx = task.context or {}
        if ctx.get("backmerge_conflict") is True and (
            str(ctx.get("hotfix_task_id")) == hotfix_task_id
        ):
            return True
    return False


async def _handle_conflict(
    session: AsyncSession,
    project_id: uuid.UUID,
    pr_number: int,
    pr_url: str,
    hotfix_task_id: str | None,
    merged_pr_url: str,
    *,
    message: str,
) -> None:
    """Raise a conflict Alert and (if not already open) spawn a resolution plan.

    The Alert always fires — the operator must see a stuck back-merge.  The
    resolution ``plan`` task is spawned only when one is not already open for
    the same hotfix (dedupe via :func:`_resolution_plan_already_open`).
    """
    hotfix_ref = _describe_hotfix(merged_pr_url, hotfix_task_id)

    alert = Alert(
        project_id=project_id,
        severity=AlertSeverity.ERROR,
        title=_CONFLICT_ALERT_TITLE,
        message=(
            f"The automated main -> automain back-merge (PR #{pr_number}) "
            f"after hotfix {hotfix_ref} hit a merge conflict and could not be "
            f"merged automatically. A resolution plan task has been spawned. "
            f"Until automain absorbs main, the next automain -> main promotion "
            f"will conflict. ({message[:160]})"
        ),
    )
    session.add(alert)
    await session.flush()

    spawned_plan: Task | None = None
    if await _resolution_plan_already_open(session, project_id, hotfix_task_id):
        logger.info(
            "[HOTFIX_BACKMERGE] a back-merge conflict-resolution plan is "
            "already open for hotfix %s — not spawning a duplicate",
            hotfix_task_id,
        )
    else:
        spawned_plan = await _spawn_resolution_plan(
            session,
            project_id,
            pr_number,
            pr_url,
            hotfix_task_id,
            hotfix_ref,
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
    hotfix_ref: str,
) -> Task:
    """Create + queue a high-priority ``plan`` task to resolve the conflict.

    The plan targets ``automain`` (``base_branch="automain"``) and instructs a
    ``git merge origin/main`` conflict resolution.  It is a NORMAL
    automain-bound fix — it deliberately does NOT carry ``hotfix:true``.
    """
    prompt = (
        f"The automated `main -> automain` back-merge PR #{pr_number} "
        f"({pr_url or 'no URL'}) hit a merge conflict and could not be merged.\n\n"
        f"This back-merge follows hotfix {hotfix_ref}: a fix landed on `main` "
        f"and `automain` must absorb it, or the next `automain -> main` "
        f"promotion will conflict.\n\n"
        f"Resolve it: from a checkout of `automain`, run `git merge origin/main`, "
        f"resolve every conflict so that `automain` contains both the hotfix "
        f"changes and the in-flight integration work, and commit the merge. "
        f"Do NOT discard the hotfix changes and do NOT discard automain's "
        f"integration work.\n\n"
        f"IMPORTANT GUARDRAILS:\n"
        f"- This is a normal automain-targeted fix, NOT a hotfix.\n"
        f"- Do NOT delete or skip tests just to make the merge build pass.\n"
        f"- Keep both sides of every conflict reconciled — a hotfix line and an "
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
        title="Resolve hotfix back-merge conflict: main -> automain",
        description=(
            f"The automated main -> automain back-merge PR #{pr_number} hit a "
            f"merge conflict; resolve it on automain so the next promotion "
            f"does not conflict."
        ),
        priority=_RESOLUTION_PRIORITY,
        payloads=[payload],
        context={
            # Marker fields — _resolution_plan_already_open dedupes on these.
            "backmerge_conflict": True,
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
                "spawned_by": "HOTFIX_BACKMERGE_CONFLICT",
                "priority": _RESOLUTION_PRIORITY,
                "backmerge_pr_number": pr_number,
                "hotfix_task_id": hotfix_task_id,
            },
        )
    )
    logger.warning(
        "[HOTFIX_BACKMERGE] spawned conflict-resolution plan task %s "
        "(priority=%d) for back-merge PR #%s",
        task.id,
        _RESOLUTION_PRIORITY,
        pr_number,
    )
    return task


async def _raise_blocked_alert(
    session: AsyncSession,
    project_id: uuid.UUID,
    hotfix_task_id: str | None,
    merged_pr_url: str,
    *,
    reason: str,
    pr_url: str | None = None,
) -> None:
    """Create an ERROR Alert for a non-conflict back-merge failure.

    Covers a missing-credentials abort, a PR-creation failure, a merge
    exception, and a 405 non-merge.  A failed back-merge must never pass
    silently (design doc §6 gotcha 6).  No fix task is spawned — a 405 is a
    branch-protection / CI problem the operator resolves, not a code conflict.
    """
    hotfix_ref = _describe_hotfix(merged_pr_url, hotfix_task_id)
    message = (
        f"The automated main -> automain back-merge after hotfix {hotfix_ref} "
        f"did not complete: {reason}. main and automain remain diverged — the "
        f"next automain -> main promotion will conflict until this is resolved "
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
            "[HOTFIX_BACKMERGE] alert broadcast failed for project %s: %s",
            project_id,
            exc,
        )
