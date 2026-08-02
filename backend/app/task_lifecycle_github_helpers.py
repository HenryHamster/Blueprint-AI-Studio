"""GitHub-integration retry/util helpers for the task lifecycle.

Extracted from ``task_lifecycle.py`` to isolate a cohesive cluster of
GitHub-facing helpers that are independent of the ``_process_post_commit``
state machine:

- :func:`_comment_with_retry` — exponential-backoff wrapper around PR comments.
- :func:`_pick_merge_method` — merge-vs-squash picker for a task's auto-merge.
- :func:`_get_approval_gate_file_lists` — approval-gate infra/critical file
  extractor.

These are pure helper functions with no shared state with the state machine,
so they live in their own module and are re-imported back into
``task_lifecycle.py`` to keep call sites unchanged.
"""

import asyncio
import logging
import random as _random

from app.models import Task, Project

logger = logging.getLogger(__name__)


def _get_approval_gate_file_lists(project: Project) -> tuple[set, set]:
    """Get infra_files and critical_files for approval gate.

    Returns (infra_files_set, critical_files_set).
    Uses project.approval_gate_config when present; otherwise returns empty sets.
    """
    config = (project.approval_gate_config or {}) if project else {}

    # Infer the missing half when only one of infra/critical is configured.
    # If only infra_files is set, any infra change must require approval (critical = infra).
    # If only critical_files is set, infra must include all critical files (infra = critical).
    infra = set(config.get("infra_files", []))
    critical = set(config.get("critical_files", []))
    if infra and not critical:
        critical = set(infra)
    if critical and not infra:
        infra = set(critical)
    return infra, critical


def _pick_merge_method(task: Task) -> str:
    """Choose ``"merge"`` vs ``"squash"`` for ``task``'s PR auto-merge.

    Default is ``"squash"`` — the orchestrator collapses each task's PR into a
    single commit on the base branch.  Back-merge conflict-resolution tasks
    (``context["backmerge_conflict"] is True``, spawned by
    ``hotfix_backmerge._spawn_resolution_task``) override to ``"merge"`` so the
    PR lands as a real two-parent merge commit on ``automain``.  Without that,
    ``main`` never becomes an ancestor of ``automain``, the merge-base never
    advances, and every future back-merge / promotion has to content-replay
    the same commits.
    """
    if (task.context or {}).get("backmerge_conflict"):
        return "merge"
    return "squash"


async def _comment_with_retry(
    create_pr_comment_fn,
    gh_token: str,
    owner: str,
    repo: str,
    pr_number: int,
    body: str,
    max_attempts: int = 3,
) -> None:
    """Call create_pr_comment with exponential backoff for transient GitHub failures.

    create_pr_comment raises HTTPException on any non-201 response, so a single
    upstream 5xx would otherwise abort the post-commit flow. Swallows failure
    after exhausting retries — a missing PR comment is not worth aborting the
    merge pipeline.
    """
    last_exc = None
    for attempt in range(max_attempts):
        try:
            await create_pr_comment_fn(gh_token, owner, repo, pr_number, body)
            return
        except Exception as exc:  # noqa: PERF203 — per-attempt try is required for the retry loop
            last_exc = exc
            if attempt < max_attempts - 1:
                backoff = (2**attempt) * (1 + _random.random())  # ~1-2s, ~2-4s
                logger.warning(
                    f"[PIPELINE] PR #{pr_number} comment attempt {attempt + 1} raised "
                    f"{type(exc).__name__}, retrying in {backoff:.1f}s: {exc!r}"
                )
                await asyncio.sleep(backoff)
    logger.error(
        f"[PIPELINE] PR #{pr_number} comment failed after {max_attempts} attempts: {last_exc!r}"
    )
