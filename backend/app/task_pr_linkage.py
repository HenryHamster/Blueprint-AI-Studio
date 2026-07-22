"""Persist PR linkage (pr_number/branch) onto the task row's leading payload.

Historically the PR-creation paths saved a ``PULL_REQUEST`` :class:`Artifact`
only — the task row itself never learned its ``pr_number`` / ``branch``. For a
parentless (admin-created standalone) task that flips ``COMPLETED`` at PR-open,
that left ``GET /api/admin/tasks/{id}`` returning ``pr_number=null`` /
``branch=null`` even though a PR existed: the linkage lived solely in the
artifact and was never queryable on the task.

The leading payload (``payloads[0]``) is the canonical home for this linkage —
auto-review tasks already store ``payloads[0].pr_number`` / ``.branch``, the
admin UI reads ``task.payloads[0].pr_number``, and ``branch_resolution`` reads
``task.first_payload["branch"]``. This module writes the same keys so every
PR-opening path keeps the task row truthful, not just the artifact.
"""

import re
import uuid

from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.attributes import flag_modified
from sqlmodel import select

from app.models import Task

_PR_NUMBER_FROM_URL = re.compile(r"/pull/(\d+)/?$")

# Task types whose ``pr_number`` is an INPUT (an existing PR they review/fix),
# NOT a PR they opened themselves. The standalone "PR opened — review owns
# merge" terminal message must never overwrite these tasks' messages, since
# their pr_number points at a PR owned by another task.
PR_INPUT_TASK_TYPES = frozenset({"review", "fix", "verify_tests"})


def parse_pr_number_from_url(pr_url: str | None) -> int | None:
    """Parse the integer PR number from a GitHub PR URL, or ``None``.

    Mirrors the inline ``int(pr_url.rstrip("/").split("/")[-1])`` parse used at
    several call sites but is null-safe and rejects non-PR URLs.
    """
    if not pr_url:
        return None
    match = _PR_NUMBER_FROM_URL.search(pr_url.rstrip("/"))
    return int(match.group(1)) if match else None


def persist_task_pr_linkage(
    task: Task,
    *,
    pr_number: int | None = None,
    branch: str | None = None,
) -> bool:
    """Write ``pr_number`` / ``branch`` onto ``task.payloads[0]`` in place.

    Returns ``True`` when the payload changed (so callers can decide whether a
    commit/flag is needed). A no-op when the values are already present or when
    nothing was supplied. Caller is responsible for committing the session.
    """
    payloads = list(task.payloads or [])
    head = dict(payloads[0]) if payloads else {}
    changed = False
    if pr_number is not None and head.get("pr_number") != pr_number:
        head["pr_number"] = pr_number
        changed = True
    if branch and head.get("branch") != branch:
        head["branch"] = branch
        changed = True
    if not changed:
        return False
    if payloads:
        payloads[0] = head
    else:
        payloads = [head]
    task.payloads = payloads
    flag_modified(task, "payloads")
    return True


async def find_review_task_id_for_pr(
    session: AsyncSession,
    project_id: uuid.UUID,
    pr_number: int,
) -> uuid.UUID | None:
    """Return the id of the review task that owns ``pr_number`` in this project.

    A standalone deliverable task completes at PR-open while a separate review
    task (spawned by the auto-review webhook) owns the merge. This resolves that
    review task so the terminal status message can cite it. Returns ``None`` if
    no review task exists yet (the webhook may not have fired).
    """
    # Imported lazily to keep this module's import graph minimal and avoid a
    # cycle through the ci_completion stack at module load.
    from app.ci_merge_helpers import _pr_number_match

    rows = (
        (
            await session.execute(
                select(Task)
                .where(
                    Task.project_id == project_id,
                    _pr_number_match(session, pr_number),
                )
                .order_by(Task.created_at.desc())
                .limit(10)
            )
        )
        .scalars()
        .all()
    )
    for candidate in rows:
        if (candidate.first_payload or {}).get("type") == "review":
            return candidate.id
    return None


def standalone_pr_completion_message(
    pr_number: int,
    review_task_id: uuid.UUID | None,
) -> str:
    """Compose the terminal status message for a standalone PR-open completion.

    Cites the PR number and (when known) the review task that owns the merge,
    e.g. ``"PR #8618 opened — review c7bf6da8 owns merge"`` — replacing the
    generic heartbeat remnant that otherwise reads as "COMPLETED, no PR".
    """
    if review_task_id is not None:
        return f"PR #{pr_number} opened — review {str(review_task_id)[:8]} owns merge"
    return f"PR #{pr_number} opened — review pending"
