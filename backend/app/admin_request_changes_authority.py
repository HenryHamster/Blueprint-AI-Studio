"""Attempt-scoped authority for the admin request-changes spawn boundary."""

from __future__ import annotations

import copy
import uuid
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from app.admin_approval_authority import (
    APPROVAL_FOLLOWUP_CLAIM_KEY,
    approval_hold_provenance,
    approval_merge_in_flight,
    consume_approval_hold_metadata,
    revoke_operator_approval_followup,
)
from app.closed_pr_reconciliation import revoke_review_wait_approval_hold
from app.models import Artifact, ArtifactType, Task, TaskHistory, TaskStatus


@dataclass(frozen=True)
class RequestChangesSnapshot:
    """The exact approval and PR attempt observed by the HTTP request."""

    task_id: uuid.UUID
    project_id: uuid.UUID
    status: TaskStatus | str
    archived: bool
    artifact_id: uuid.UUID
    artifact_pr_url: str
    pr_number: int
    review_attempt: dict[str, Any]
    approval_disposition: dict[str, Any]
    action: str
    modified_by: str


def _approval_disposition(task: Task) -> dict[str, Any]:
    context = dict(task.context or {})
    return {
        "requires_approval": task.requires_approval,
        "approval_set_at": copy.deepcopy(context.get("_approval_set_at")),
        "approval_hold_reason": copy.deepcopy(
            context.get("approval_hold_reason")
        ),
        "touched_critical": copy.deepcopy(context.get("touched_critical")),
        "operator_followup_claim": copy.deepcopy(
            context.get(APPROVAL_FOLLOWUP_CLAIM_KEY)
        ),
        "spend_approval_cleared_at": task.spend_approval_cleared_at,
    }


def capture_request_changes_snapshot(
    task: Task,
    artifact: Artifact,
    *,
    pr_number: int,
    action: str,
    modified_by: str,
) -> RequestChangesSnapshot:
    """Freeze the route's read-only observation before canonical locks."""

    return RequestChangesSnapshot(
        task_id=task.id,
        project_id=task.project_id,
        status=task.status,
        archived=bool(task.archived),
        artifact_id=artifact.id,
        artifact_pr_url=str(artifact.pr_url),
        pr_number=int(pr_number),
        review_attempt=copy.deepcopy(
            dict((task.context or {}).get("review_loop") or {})
        ),
        approval_disposition=_approval_disposition(task),
        action=action,
        modified_by=modified_by,
    )


def build_live_request_changes_review_target(
    snapshot: RequestChangesSnapshot,
    live_pr: dict[str, Any],
) -> dict[str, str | int] | None:
    """Bind a re-review to one complete, open GitHub base/head observation.

    Project and PR identity come from the frozen, access-checked request
    snapshot; both SHAs come from the same live GitHub response.  Refuse a
    partial/closed response instead of mixing persisted or webhook-era SHAs
    into a guessed target.
    """

    if snapshot.action != "review" or not isinstance(live_pr, dict):
        return None
    if (
        live_pr.get("merged") is True
        or str(live_pr.get("state") or "").lower() != "open"
    ):
        return None
    base = live_pr.get("base")
    head = live_pr.get("head")
    if not isinstance(base, dict) or not isinstance(head, dict):
        return None
    base_sha = base.get("sha")
    head_sha = head.get("sha")
    if (
        not isinstance(base_sha, str)
        or not base_sha
        or not isinstance(head_sha, str)
        or not head_sha
    ):
        return None
    return {
        "project_id": str(snapshot.project_id),
        "pr_number": snapshot.pr_number,
        "base_sha": base_sha,
        "head_sha": head_sha,
    }


def build_request_changes_spawn_boundary(
    snapshot: RequestChangesSnapshot,
) -> tuple[
    Callable[[AsyncSession], Awaitable[Task | None]],
    Callable[[Task, AsyncSession], Awaitable[None]],
]:
    """Build exact authority and atomic-publication callbacks for one request."""

    authority_granted = False

    async def _acquire(authority_session: AsyncSession) -> Task | None:
        nonlocal authority_granted
        authority_granted = False
        authoritative_task = (
            (
                await authority_session.execute(
                    select(Task)
                    .where(Task.id == snapshot.task_id)
                    .with_for_update()
                    .execution_options(populate_existing=True)
                )
            )
            .scalars()
            .first()
        )
        if authoritative_task is None:
            return None
        if await approval_merge_in_flight(authoritative_task, authority_session):
            # A durable SUBMISSION_STARTED receipt means GitHub may already have
            # accepted the merge.  Request-changes must wait for exact-target
            # convergence instead of publishing a fix/review against an
            # ambiguously merged attempt.
            return None

        authoritative_artifact = (
            (
                await authority_session.execute(
                    select(Artifact)
                    .where(
                        Artifact.id == snapshot.artifact_id,
                        Artifact.task_id == snapshot.task_id,
                        Artifact.artifact_type == ArtifactType.PULL_REQUEST,
                    )
                    .with_for_update()
                    .execution_options(populate_existing=True)
                )
            )
            .scalars()
            .first()
        )
        current_review_attempt = copy.deepcopy(
            dict((authoritative_task.context or {}).get("review_loop") or {})
        )
        if not (
            authoritative_task.project_id == snapshot.project_id
            and authoritative_task.status == snapshot.status
            and bool(authoritative_task.archived) == snapshot.archived
            and authoritative_task.archived is not True
            and authoritative_task.requires_approval is True
            and current_review_attempt == snapshot.review_attempt
            and _approval_disposition(authoritative_task)
            == snapshot.approval_disposition
            and authoritative_artifact is not None
            and authoritative_artifact.pr_url == snapshot.artifact_pr_url
        ):
            return None
        authority_granted = True
        return authoritative_task

    async def _publish(
        authoritative_task: Task, publication_session: AsyncSession
    ) -> None:
        nonlocal authority_granted
        if not authority_granted or authoritative_task.id != snapshot.task_id:
            raise RuntimeError(
                "request-changes publication attempted without exact source authority"
            )
        if await approval_merge_in_flight(
            authoritative_task, publication_session
        ):
            raise RuntimeError(
                "request-changes publication raced an in-flight approval merge"
            )
        authority_granted = False

        review_wait_revoked = revoke_review_wait_approval_hold(
            authoritative_task
        )
        operator_claim_revoked = revoke_operator_approval_followup(
            authoritative_task
        )
        consume_approval_hold_metadata(
            authoritative_task,
            expected_episode=approval_hold_provenance(authoritative_task),
        )
        authoritative_task.requires_approval = False
        authoritative_task.latest_status_message = (
            f"Changes requested — spawned {snapshot.action} task"
        )
        publication_session.add(authoritative_task)
        publication_session.add(
            TaskHistory(
                task_id=authoritative_task.id,
                action="MODIFIED",
                details={
                    "changes": {
                        "requires_approval": False,
                        "latest_status_message": (
                            authoritative_task.latest_status_message
                        ),
                    },
                    "modified_by": snapshot.modified_by,
                    "request_changes": {
                        "action": snapshot.action,
                        "artifact_id": str(snapshot.artifact_id),
                        "artifact_pr_url": snapshot.artifact_pr_url,
                        "review_wait_provenance_revoked": review_wait_revoked,
                        "operator_followup_claim_revoked": (
                            operator_claim_revoked
                        ),
                    },
                },
            )
        )

    return _acquire, _publish
