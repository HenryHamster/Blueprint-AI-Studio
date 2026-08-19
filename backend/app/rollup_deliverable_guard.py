"""Rollup child-deliverable survival guard (ghost-success detection).

Defect class (PR #17480, 2026-08-19): a conflict-fix task resolving a rollup
PR's merge conflicts resolved them WHOLESALE toward the base branch, silently
erasing a child PR's deliverable from the rollup branch. The rollup PR then
merged green and the rollup parent completed with "Rollup PR merged" — a ghost
success: the pipeline believed the work landed while part of it was destroyed
during conflict resolution.

This module is a DETECTION gate, not a resolution engine. Right before a
rollup parent is stamped COMPLETED off its merged rollup PR, it verifies that
each constituent child PR's deliverable survived into the merged rollup diff:
a staging-merged child whose GitHub-verified changed files are ENTIRELY absent
from the rollup PR's own changed-file set had its deliverable dropped.

Failure semantics (deliberately conservative — a guard that false-blocks
healthy rollups is itself a platform defect):

* ``"dropped"``       — at least one child's verified deliverable is entirely
  absent from the verified rollup diff AND a conflict-fix intervention on this
  PR is on record (the incident signature). The guard stamps a durable context
  receipt naming each dropped child and raises the operator failure alert; the
  caller must NOT stamp ghost success (it fails the parent with the WHY).
* ``"warned"``        — same verified zero-overlap but with NO conflict-fix
  evidence. Absence from the PR diff is then ambiguous (the base branch may
  have legitimately caught up with identical content, making the child's files
  disappear from the diff while the deliverable IS in the merged result), so
  the guard stamps a loud warning receipt + error log and lets completion
  proceed instead of hard-blocking.
* ``"intact"``        — every verifiable child overlaps the rollup diff (or
  there is nothing to verify).
* ``"indeterminate"`` — the rollup diff or credentials could not be resolved.
  Flag-trust is preserved: a transient GitHub outage never blocks completion
  (mirrors :func:`app.rollup_pr_verification.verify_rollup_merge_landed`).

Bounding: child changed files are read from the metadata already stamped at
merge time (``_pr_changed_files`` + provenance sentinels from ci_completion /
rollup propagation, PR #17159); at most ``_MAX_LIVE_CHILD_COMPARES`` live
commit-range compares fill gaps, and at most ONE live ``get_pr_files`` call
resolves the rollup diff when the stamped copy is unusable (truncated).
"""

import logging

from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from app.acceptance_provenance import PARENT_DERIVED_CHANGED_FILES_KEY
from app.landed_signal import (
    FORCE_DROP_UNBLOCK_REASONS,
    NON_MERGE_UNBLOCK_REASON_KEY,
    has_merge_unblock_signal,
)
from app.models import Project, Task, TaskStatus

logger = logging.getLogger(__name__)

# Durable context receipts on the rollup parent. DROPPED blocks ghost success;
# WARNING is the ambiguous-case loud receipt (completion still proceeds).
DROPPED_DELIVERABLES_KEY = "rollup_dropped_deliverables"
DROPPED_DELIVERABLES_REASON_KEY = "rollup_dropped_deliverables_reason"
DELIVERABLE_WARNING_KEY = "rollup_deliverable_warning"

# Bounds: rollups have modest child counts; these caps only guard pathology.
_MAX_DESCENDANTS = 300
_MAX_LIVE_CHILD_COMPARES = 8
_MISSING_FILES_SAMPLE = 10


async def finalize_rollup_parent_after_merge(
    orig: Task,
    project: "Project | None",
    session,
    *,
    terminal: bool,
    base_branch,
    pr_number,
    github_credentials: tuple | None = None,
    ci_loop: dict | None = None,
    merge_task: "Task | None" = None,
    source: str,
) -> str:
    """Stamp the rollup parent's terminal state after its PR merged.

    Extracted from the (size-capped) ci_completion / task_lifecycle merge
    handlers. ``terminal=False`` (custom-staged rollup awaiting downstream
    landing) keeps today's deferred message. ``terminal=True`` runs the
    ghost-success deliverable guard first: verdict ``"dropped"`` fails the
    parent with the reason naming the dropped child PR(s); every other verdict
    (including a guard crash — the guard must never break completion) stamps
    the historical ``COMPLETED`` / "Rollup PR merged". Returns ``"deferred"``,
    ``"failed"``, or ``"completed"``.
    """
    if not terminal:
        orig.latest_status_message = (
            f"Rollup PR merged into {base_branch}; awaiting downstream landing"
        )
        return "deferred"
    verdict = "indeterminate"
    try:
        verdict = await verify_rollup_child_deliverables(
            orig,
            project,
            session,
            pr_number=pr_number,
            github_credentials=github_credentials,
            ci_loop=ci_loop,
            merge_task=merge_task,
            source=source,
        )
    except Exception:
        logger.warning(
            "[ROLLUP_DELIVERABLE] guard errored for task %s — not blocking "
            "completion (%s)",
            getattr(orig, "id", None),
            source,
            exc_info=True,
        )
    if verdict == "dropped":
        from app.failure_classification import (
            CODE_ERROR,
            persist_failure_category,
        )

        orig.status = TaskStatus.FAILED
        orig.latest_status_message = (orig.context or {}).get(
            DROPPED_DELIVERABLES_REASON_KEY
        ) or (
            "Rollup PR merged but a child deliverable was dropped during "
            "conflict resolution"
        )
        persist_failure_category(orig, CODE_ERROR)
        return "failed"
    orig.status = TaskStatus.COMPLETED
    orig.latest_status_message = "Rollup PR merged"
    return "completed"


def _verified_own_changed_files(ctx: dict) -> list[str] | None:
    """The task's GitHub-verified OWN-PR changed files, or None if unusable.

    Parent-derived (rollup-union) provenance is rejected — comparing the
    parent's own diff against itself would be circular and always "survive".
    Truncated child lists remain usable for SURVIVAL testing: any listed file
    present in the rollup diff proves survival, and a wholesale drop erases
    every file, including the ≤50 stored ones.
    """
    if ctx.get("_pr_changed_files_github_verified") is not True:
        return None
    if ctx.get(PARENT_DERIVED_CHANGED_FILES_KEY) is True:
        return None
    raw = ctx.get("_pr_changed_files")
    if not isinstance(raw, list):
        return None
    return [p for p in raw if isinstance(p, str) and p.strip()]


def _child_is_deliverable_candidate(ctx: dict) -> bool:
    """Whether a descendant genuinely contributed a PR to the rollup branch.

    Force-dropped children (no PR artifact / malformed URL / respawn cap) and
    deliberate success-closes or supersessions never delivered code THROUGH
    this rollup, so their absence from the rollup diff is expected, not a drop.
    """
    if ctx.get(NON_MERGE_UNBLOCK_REASON_KEY) in FORCE_DROP_UNBLOCK_REASONS:
        return False
    if ctx.get("pr_closed_success") or ctx.get("pr_closed_empty"):
        return False
    return not (
        ctx.get("superseded") or ctx.get("pr_superseded") or ctx.get("descoped")
    )


def _pr_numbers_match(candidate, pr_number) -> bool:
    try:
        return candidate is not None and int(candidate) == int(pr_number)
    except (TypeError, ValueError):
        return False


def _child_pr_number(child: Task):
    """Best-effort PR number for naming the dropped child (never raises)."""
    from app.ci_merge_helpers import effective_pr_number

    number = effective_pr_number(child)
    if number is None:
        number = (child.first_payload or {}).get("pr_number")
    try:
        return int(number) if number is not None else None
    except (TypeError, ValueError):
        return None


async def _resolve_rollup_changed_files(
    orig: Task,
    session: AsyncSession,
    *,
    pr_number,
    credentials: tuple | None,
) -> tuple[set[str] | None, tuple | None]:
    """The merged rollup PR's authoritative changed-file set.

    Prefers the GitHub-verified, non-truncated list already stamped at landing
    (PR #17159) over a live call. Returns ``(None, credentials)`` when no
    authoritative set can be resolved (caller degrades to indeterminate).
    """
    ctx = orig.context or {}
    stamped = _verified_own_changed_files(ctx)
    if stamped is not None and not ctx.get("_pr_changed_files_truncated"):
        return set(stamped), credentials

    # Stamped copy unusable (absent or truncated at 50 — absence testing on a
    # truncated list is unsound). One bounded live fetch, paginated internally.
    if credentials is None:
        try:
            from app.routers.agent import get_github_token_for_task

            credentials = await get_github_token_for_task(orig.id, session)
        except Exception:
            logger.debug(
                "[ROLLUP_DELIVERABLE] credentials unavailable for task %s",
                getattr(orig, "id", None),
            )
            return None, None
    if (
        not isinstance(credentials, tuple)
        or len(credentials) != 3
        or not all(isinstance(v, str) and v.strip() for v in credentials)
    ):
        return None, None
    try:
        from app.github import get_pr_files

        raw_files = await get_pr_files(
            credentials[0],
            credentials[1],
            credentials[2],
            int(pr_number),
            str(orig.project_id) if orig.project_id is not None else None,
        )
    except Exception:
        logger.warning(
            "[ROLLUP_DELIVERABLE] rollup PR #%s file list fetch failed for "
            "task %s — degrading to indeterminate",
            pr_number,
            getattr(orig, "id", None),
            exc_info=True,
        )
        return None, credentials
    return (
        {
            item["filename"]
            for item in (raw_files or [])
            if isinstance(item, dict)
            and isinstance(item.get("filename"), str)
            and item["filename"].strip()
        },
        credentials,
    )


async def verify_rollup_child_deliverables(
    orig: Task,
    project: "Project | None",
    session: AsyncSession,
    *,
    pr_number,
    github_credentials: tuple | None = None,
    ci_loop: dict | None = None,
    merge_task: "Task | None" = None,
    source: str,
) -> str:
    """Verify each constituent child deliverable survived the merged rollup.

    Run by the rollup completion sites (ci_completion / task_lifecycle) AFTER
    the rollup PR merged and BEFORE the parent is stamped COMPLETED. Returns
    ``"intact"``, ``"indeterminate"``, ``"warned"``, or ``"dropped"`` — see the
    module docstring for exact semantics. Never raises for GitHub / credential
    errors (degrades to ``"indeterminate"``); on ``"dropped"`` the durable
    receipt + operator alert have already been recorded and the caller must
    not stamp ghost success.
    """
    if pr_number is None or project is None:
        return "indeterminate"

    # ── Walk the rollup subtree (BFS, bounded) ─────────────────────────────
    candidates: list[Task] = []
    conflict_fix_evidence = bool((ci_loop or {}).get("conflict_fix_spawned")) or bool(
        ((orig.context or {}).get("review_loop") or {}).get("conflict_fix_spawned")
    )
    if merge_task is not None and (merge_task.first_payload or {}).get(
        "fix_reason"
    ) == "merge_conflict":
        conflict_fix_evidence = True

    visited: set = {orig.id}
    queue: list = [orig.id]
    while queue and len(visited) < _MAX_DESCENDANTS:
        rows = (
            (
                await session.execute(
                    select(Task).where(
                        Task.parent_task_id.in_(queue),
                        Task.project_id == orig.project_id,
                    )
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
            next_queue.append(row.id)
            payload = row.first_payload or {}
            if payload.get("fix_reason") == "merge_conflict" and _pr_numbers_match(
                payload.get("pr_number"), pr_number
            ):
                # A conflict-fix child ran against THIS rollup PR — the
                # incident signature that authorizes the hard verdict.
                conflict_fix_evidence = True
                continue
            ctx = row.context or {}
            if has_merge_unblock_signal(row) and _child_is_deliverable_candidate(ctx):
                candidates.append(row)
        queue = next_queue

    if not candidates:
        return "intact"

    # ── Resolve the authoritative rollup diff (stamped first, 1 live call) ─
    rollup_files, credentials = await _resolve_rollup_changed_files(
        orig, session, pr_number=pr_number, credentials=github_credentials
    )
    if rollup_files is None:
        return "indeterminate"

    # ── Per-child survival check off stamped metadata ──────────────────────
    dropped: list[dict] = []
    live_compare_budget = _MAX_LIVE_CHILD_COMPARES
    for child in candidates:
        files = _verified_own_changed_files(child.context or {})
        if files is None and live_compare_budget > 0 and credentials is not None:
            # Gap-fill with the child's exact PR commit range (the same helper
            # rollup propagation uses); it stamps the child context for reuse.
            live_compare_budget -= 1
            try:
                from app.acceptance_criteria import stamp_rollup_child_commit_range

                if await stamp_rollup_child_commit_range(
                    child,
                    gh_token=credentials[0],
                    owner=credentials[1],
                    repo=credentials[2],
                ):
                    session.add(child)
                    files = _verified_own_changed_files(child.context or {})
            except Exception:
                logger.debug(
                    "[ROLLUP_DELIVERABLE] child %s commit-range gap-fill failed",
                    getattr(child, "id", None),
                    exc_info=True,
                )
        if not files:
            continue  # unverifiable or empty diff — never a block signal
        if rollup_files.isdisjoint(files):
            dropped.append(
                {
                    "task_id": str(child.id),
                    "pr_number": _child_pr_number(child),
                    "title": (child.title or "")[:120],
                    "missing_files": files[:_MISSING_FILES_SAMPLE],
                    "file_count": len(files),
                }
            )

    if not dropped:
        return "intact"

    def _name(entry: dict) -> str:
        pr_part = f"PR #{entry['pr_number']}" if entry["pr_number"] else "PR unknown"
        return (
            f"{pr_part} (task {entry['task_id'][:8]}, {entry['title']!r}: "
            f"0/{entry['file_count']} changed files present)"
        )

    named = "; ".join(_name(e) for e in dropped)
    if conflict_fix_evidence:
        reason = (
            f"Rollup PR #{pr_number} merged but dropped child deliverable(s): "
            f"{named}. A conflict-fix intervened on this PR and none of the "
            f"child's changed files survived into the merged rollup diff — "
            f"likely wholesale conflict resolution toward the base branch. "
            f"The dropped work must be re-landed ({source})."
        )
        orig.context = {
            **(orig.context or {}),
            DROPPED_DELIVERABLES_KEY: dropped,
            DROPPED_DELIVERABLES_REASON_KEY: reason,
        }
        logger.error(
            "[ROLLUP_DELIVERABLE] %s task=%s", reason, getattr(orig, "id", None)
        )
        try:
            from app.alert_dispatch import _maybe_create_failure_alert

            await _maybe_create_failure_alert(orig, session, reason)
        except Exception:
            logger.warning(
                "[ROLLUP_DELIVERABLE] dropped-deliverable alert failed for %s",
                getattr(orig, "id", None),
                exc_info=True,
            )
        return "dropped"

    # No conflict-fix on record: zero overlap is ambiguous (the base branch may
    # have legitimately absorbed identical content, hiding the files from the
    # PR diff while the deliverable IS in the merged result). Loud receipt, no
    # block — false-blocking healthy rollups is also a platform defect.
    warning = (
        f"Rollup PR #{pr_number} merged with child deliverable(s) absent from "
        f"its diff: {named}. No conflict-fix intervened on this PR, so this "
        f"may be legitimate supersession — completing, but verify the child "
        f"work is present on the target branch ({source})."
    )
    orig.context = {**(orig.context or {}), DELIVERABLE_WARNING_KEY: warning}
    logger.error(
        "[ROLLUP_DELIVERABLE] %s task=%s", warning, getattr(orig, "id", None)
    )
    return "warned"
