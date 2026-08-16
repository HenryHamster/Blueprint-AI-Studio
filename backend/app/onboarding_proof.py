"""Durable onboarding-proof milestone timeline.

Idempotent stamp helpers for each milestone in the external-repo onboarding
journey, plus a read-side assembler that returns the full timeline for the
project detail API.

All stamps use the build_config JSONB sub-key pattern (same flag_modified path
as bootstrap_completed_at) so no migration is needed and a bad write cannot
corrupt sibling keys.  Each helper is best-effort and never raises — a failure
must never block the merge or spawn hot paths.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


async def lock_project_for_onboarding_proof(
    project: Any,
    session: Any,
) -> Any | None:
    """Fresh-read ``Project`` authority before a build-config proof stamp.

    Onboarding milestones are written from automatic completion/landing paths,
    several of which spend seconds in GitHub I/O before reaching the stamp.  A
    whole-JSONB write from the Project snapshot loaded before that I/O can
    silently overwrite an operator edit made in the meantime -- including the
    automatic Strategic lane switch.  Re-enter the canonical Project row
    boundary immediately before stamping and retain its transaction lock until
    the caller commits.

    The helper is deliberately non-blocking.  Some landing callers already own
    a Task row; waiting for Project there would invert the canonical
    Project-before-Task order.  Contention therefore skips this best-effort
    observability stamp, while the next qualifying event can retry it.  Plain
    object/session doubles keep the historical unit-test seam.
    """

    if project is None or session is None:
        return None

    from sqlalchemy.ext.asyncio import AsyncSession

    if not isinstance(session, AsyncSession):
        return project

    from app.models import Project
    from sqlalchemy import inspect as sa_inspect
    from sqlmodel import select

    project_id = getattr(project, "id", None)
    if project_id is None:
        return None
    if sa_inspect(project).attrs.build_config.history.has_changes():
        # populate_existing would otherwise discard an intentional pending
        # build_config mutation. No current caller reaches this branch; keep it
        # fail-closed for future call sites and require their own authoritative
        # key-wise merge before they opt into this best-effort stamp.
        logger.warning(
            "[ONBOARDING_PROOF] Deferred proof stamp for project %s -- "
            "caller already has a dirty build_config",
            project_id,
        )
        return None
    # The landing transaction can have a dirty Task or an unrelated dirty
    # Project scalar. Never let SQLAlchemy autoflush either one *before* the
    # Project lock: that would turn this non-blocking Project-after-Task seam
    # into an implicit blocking UPDATE. Also do not select the Project ORM
    # entity with populate_existing here: SQLAlchemy can discard unrelated
    # dirty Project fields even when load_only projects id/build_config. A
    # column-only read takes the same canonical FOR NO KEY UPDATE row authority
    # without refreshing any ORM state; we then replace only the clean, stale
    # build_config attribute with the locked current snapshot.
    with session.no_autoflush:
        locked_row = (
            await session.execute(
                select(Project.id, Project.build_config)
                .where(Project.id == project_id)
                .with_for_update(skip_locked=True, key_share=True)
            )
        ).first()
    if locked_row is None:
        logger.info(
            "[ONBOARDING_PROOF] Deferred proof stamp for project %s -- "
            "Project row is missing or contended",
            project_id,
        )
        return None
    project.build_config = dict(locked_row[1] or {})
    return project


def _stamp_build_config_key_once(
    project: Any,
    session: Any,
    key: str,
    value: Any,
) -> None:
    """Idempotent, best-effort write of a single build_config sub-key.

    Writes ``key = value`` into ``project.build_config`` ONLY when the key is
    not already set.  Uses the established ``flag_modified`` pattern so
    SQLAlchemy tracks the nested-dict mutation without a full column replace.
    """
    try:
        if project is None:
            return
        bc: dict = dict(getattr(project, "build_config", None) or {})
        if bc.get(key) is not None:
            return  # already stamped — idempotent
        bc[key] = value
        project.build_config = bc
        try:
            from sqlalchemy.orm.attributes import flag_modified

            flag_modified(project, "build_config")
        except Exception:  # noqa: BLE001
            # Detached / plain-object stubs (unit tests) skip flag_modified.
            pass
        if session is not None:
            session.add(project)
    except Exception:  # noqa: BLE001
        logger.warning(
            "[ONBOARDING_PROOF] failed to stamp build_config.%s", key, exc_info=True
        )


def stamp_first_task_spawned(project: Any, session: Any, *, when: Any) -> None:
    """Stamp the first non-bootstrap autonomous task-spawn timestamp."""
    when_iso = when.isoformat() if hasattr(when, "isoformat") else str(when)
    _stamp_build_config_key_once(project, session, "first_task_spawned_at", when_iso)


def stamp_first_pr_opened(project: Any, session: Any, *, when: Any) -> None:
    """Stamp the first PR-opened timestamp."""
    when_iso = when.isoformat() if hasattr(when, "isoformat") else str(when)
    _stamp_build_config_key_once(project, session, "first_pr_opened_at", when_iso)


def stamp_first_merge_gate_verdict(
    project: Any,
    session: Any,
    *,
    executed_real_tests: bool,
    unverified_reason: str | None,
    when: Any,
) -> None:
    """Stamp the first merge-gate terminal verdict idempotently.

    Records whether the gate ACTUALLY executed the repo's own tests
    (``executed_real_tests``) and, when it fell open, the reason.  Truthfulness
    invariant: ``first_merge_gate_executed_tests`` is NEVER written as ``True``
    when ``executed_real_tests`` is ``False``.
    """
    try:
        if project is None:
            return
        bc: dict = dict(getattr(project, "build_config", None) or {})
        if bc.get("first_merge_gate_verdict_at") is not None:
            return  # already stamped — idempotent
        when_iso = when.isoformat() if hasattr(when, "isoformat") else str(when)
        bc["first_merge_gate_verdict_at"] = when_iso
        # Truthfulness guard: only write True when tests genuinely ran.
        bc["first_merge_gate_executed_tests"] = bool(executed_real_tests)
        if not executed_real_tests and unverified_reason:
            bc["first_merge_gate_unverified_reason"] = str(unverified_reason)
        project.build_config = bc
        try:
            from sqlalchemy.orm.attributes import flag_modified

            flag_modified(project, "build_config")
        except Exception:  # noqa: BLE001
            pass
        if session is not None:
            session.add(project)
    except Exception:  # noqa: BLE001
        logger.warning(
            "[ONBOARDING_PROOF] failed to stamp first merge-gate verdict", exc_info=True
        )


def stamp_first_pr_merged(project: Any) -> None:
    """Stamp the first-PR-merged timestamp.

    No session parameter: the caller's SQLAlchemy session already tracks
    ``project`` (mirrors the ``record_capability_proof`` pattern).
    """
    try:
        if project is None:
            return
        bc: dict = dict(getattr(project, "build_config", None) or {})
        if bc.get("first_pr_merged_at") is not None:
            return  # already stamped — idempotent
        from app.time_utils import _utcnow

        bc["first_pr_merged_at"] = _utcnow().isoformat()
        project.build_config = bc
        try:
            from sqlalchemy.orm.attributes import flag_modified

            flag_modified(project, "build_config")
        except Exception:  # noqa: BLE001
            pass
    except Exception:  # noqa: BLE001
        logger.warning(
            "[ONBOARDING_PROOF] failed to stamp first PR merged", exc_info=True
        )


# Task types that represent autonomous code improvements delivered to a repo.
# Matches the "code-change" SR-metrics filter — the first time one of these
# merges on an external project, the g-generality retirement condition is met.
_AUTONOMOUS_IMPROVEMENT_TASK_TYPES: frozenset[str] = frozenset(
    ("create", "fix", "refactor")
)


def stamp_first_autonomous_improvement(task: Any, project: Any) -> None:
    """Stamp the first autonomous-improvement timestamp on a non-self-repo project.

    Called at every integration-branch merge via ``apply_merged_landed_signal``.
    Guards: skips the deploy's own repo (via ``project_is_deploy_repo`` — deploy
    identity when stamped, else the ``is_self_repo`` fallback) and tasks whose
    type is not one of {create, fix, refactor}.  Idempotent — a second call on
    the same project
    is a no-op.  No session parameter: the caller's SQLAlchemy session already
    tracks ``project`` (mirrors ``stamp_first_pr_merged``).
    """
    try:
        if project is None:
            return
        from app.deploy_identity import project_is_deploy_repo

        if project_is_deploy_repo(project):
            return
        task_type = (getattr(task, "first_payload", None) or {}).get("type")
        if task_type not in _AUTONOMOUS_IMPROVEMENT_TASK_TYPES:
            return
        bc: dict = dict(getattr(project, "build_config", None) or {})
        if bc.get("first_autonomous_improvement_at") is not None:
            return  # already stamped — idempotent
        from app.time_utils import _utcnow

        bc["first_autonomous_improvement_at"] = _utcnow().isoformat()
        project.build_config = bc
        try:
            from sqlalchemy.orm.attributes import flag_modified

            flag_modified(project, "build_config")
        except Exception:  # noqa: BLE001, S110
            pass
    except Exception:  # noqa: BLE001
        logger.warning(
            "[ONBOARDING_PROOF] failed to stamp first autonomous improvement",
            exc_info=True,
        )


def build_onboarding_proof_block(project: Any) -> dict[str, Any] | None:
    """Assemble the full onboarding milestone timeline for the project API.

    Returns a stable, JSON-serializable dict with eight milestone keys.
    ``repo_added_at`` comes from ``project.created_at``; the remaining seven
    come from ``build_config`` sub-keys.  Keys whose milestone has not yet
    occurred are ``null`` (present but null) so consumers can show an
    incomplete timeline without special-casing absent keys.

    Returns ``None`` only when ``project`` is ``None``.
    """
    if project is None:
        return None
    created_at = getattr(project, "created_at", None)
    bc: dict = getattr(project, "build_config", None) or {}
    if hasattr(created_at, "isoformat"):
        repo_added_at = created_at.isoformat()
    elif created_at is not None:
        repo_added_at = str(created_at)
    else:
        repo_added_at = None
    return {
        "repo_added_at": repo_added_at,
        "bootstrap_completed_at": bc.get("bootstrap_completed_at"),
        "first_task_spawned_at": bc.get("first_task_spawned_at"),
        "first_pr_opened_at": bc.get("first_pr_opened_at"),
        "first_merge_gate_verdict_at": bc.get("first_merge_gate_verdict_at"),
        "first_merge_gate_executed_tests": bc.get("first_merge_gate_executed_tests"),
        "first_merge_gate_unverified_reason": bc.get(
            "first_merge_gate_unverified_reason"
        ),
        "first_pr_merged_at": bc.get("first_pr_merged_at"),
        "first_autonomous_improvement_at": bc.get("first_autonomous_improvement_at"),
    }
