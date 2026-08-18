"""Shared repo_bootstrap spawn helper.

Provides an idempotent ``spawn_bootstrap_task`` function used by both the
onboarding handler (first-cycle bootstrap) and the operator retry endpoint /
background recovery sweep (recovery bootstraps).

Eligibility checks mirror the strategic spawn gate in strategic_spawning.py so
any recovery-spawned task re-engages the strategic deferral automatically.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Literal

from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.attributes import flag_modified
from sqlmodel import select

from app.deploy_identity import project_is_deploy_repo
from app.models import Project, Task, TaskHistory, TaskStatus
from app.time_utils import _coerce_naive_utc, _utcnow, exponential_backoff

logger = logging.getLogger(__name__)

# Statuses that constitute an "active" bootstrap task (matches strategic gate).
_ACTIVE_STATUSES = [
    TaskStatus.QUEUED,
    TaskStatus.PROVISIONING,
    TaskStatus.IN_PROGRESS,
]
_TERMINAL_RETRY_STATUSES = [
    TaskStatus.COMPLETED,
    TaskStatus.FAILED,
    TaskStatus.CANCELLED,
]
BOOTSTRAP_RECOVERY_MAX_ATTEMPTS = 3
BOOTSTRAP_RECOVERY_BASE_BACKOFF_HOURS = 1


def _parse_recovery_at(value: object) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        return _coerce_naive_utc(
            datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        )
    except ValueError:
        return None


def coerce_recovery_attempts(value: object) -> int:
    try:
        return max(int(value or 0), 0)
    except (TypeError, ValueError):
        return 0


def next_recovery_backoff(attempts: int) -> timedelta:
    """Return the automatic bootstrap recovery delay for a degraded attempt."""
    delay_seconds = exponential_backoff(
        max(attempts, 1),
        base=BOOTSTRAP_RECOVERY_BASE_BACKOFF_HOURS * 3600,
        cap=(2 ** (BOOTSTRAP_RECOVERY_MAX_ATTEMPTS - 1)) * BOOTSTRAP_RECOVERY_BASE_BACKOFF_HOURS * 3600,
        jitter=0,
    )
    return timedelta(seconds=delay_seconds)


# A refresh is a one-shot operator action against an already-COMPLETED project.
# It bypasses the bootstrap_completed_at not-eligible gate but, unlike automatic
# recovery, does NOT arm the backoff/auto-retry loop.
REFRESH_SPAWN_REASON = "operator_refresh"


def _empty_repo_build_config_fields() -> dict:
    """Durable fields stamped when the live repo is confirmed commitless.

    Mirrors ``admin_onboard._empty_repo_build_config`` field-for-field (kept in
    sync deliberately, not imported, to avoid a router→helper import cycle) so a
    self-healed legacy repo carries the same shape a fresh empty-repo onboarding
    would: ``detection_completed`` is implied, the ``repo_is_empty`` guard is
    reachable, and ``maybe_refresh_empty_build_config`` can later re-detect once
    real source lands.
    """
    return {
        "language": None,
        "directories": [],
        "env_vars": {},
        "detected_at": datetime.now(timezone.utc).isoformat(),
        "repo_is_empty": True,
        "source_file_count": 0,
        "has_ci": False,
    }


async def _self_heal_undetermined_emptiness(
    project: Project,
    build_cfg: dict,
    _session: AsyncSession,
) -> Literal["empty", "non_empty", "undetermined"]:
    """Resolve an undetermined durable ``repo_is_empty`` against the live repo.

    Legacy projects predating empty-repo detection carry no ``repo_is_empty`` at
    all. Both the spawn gate and the recovery sweep coalesce that absent signal
    to "not empty", so a genuinely commitless legacy repo gets bootstrap clones
    that die exit-128 (``Remote branch not found``) and retry blindly to
    exhaustion — never seeded, never re-detected, permanently stranded.

    Resolve the signal authoritatively while we still hold a write-capable
    installation token: seed a truly commitless repo (giving the trunk a real
    commit) and stamp ``repo_is_empty=True`` with the full empty-repo config
    shape. For a repository with commits, use the canonical source-file
    classifier rather than treating a commit as proof of real source; a
    README-only or scaffold-only repository is still greenfield. Both cases
    persist to ``project.build_config`` (caller commits) so the durable signal
    stops being undetermined and future sweeps classify it correctly.

    Best-effort: no installation token or ANY GitHub error leaves the signal
    undetermined and the caller falls through to its prior behavior — a transient
    failure must never block a spawn or mis-stamp a healthy repo.
    """
    owner = project.github_owner or ""
    repo = project.github_repo or ""
    if not owner or not repo:
        return "undetermined"

    from app.github import (
        ensure_repo_initialized,
        get_repo_default_branch,
        get_repo_installation_token,
        repo_has_commits,
    )

    try:
        token = await get_repo_installation_token(owner, repo)
        if not token:
            return "undetermined"

        if await repo_has_commits(token, owner, repo):
            # Commit existence only distinguishes a cloneable repository from a
            # commitless one. The onboarding detector is the authoritative
            # classifier for greenfield status because it excludes README and
            # scaffold files from its bounded source-file count.
            from app.build_config_detection import apply_operator_build_config_overrides
            from app.routers.admin_onboard import _detect_build_config

            detected = await _detect_build_config(
                token,
                owner,
                repo,
                ref=project.integration_branch,
            )
            if (
                not detected
                or detected.get("detection_incomplete")
                or not isinstance(detected.get("repo_is_empty"), bool)
                or not isinstance(detected.get("source_file_count"), int)
            ):
                return "undetermined"

            detected = apply_operator_build_config_overrides(dict(detected), build_cfg)
            # Keep the gate below on the same updated mapping; it intentionally
            # reads ``build_cfg`` so this legacy self-heal does not need a
            # second eligibility path.  Detection owns its fields, but this is
            # also a lifecycle record: retain durable bootstrap/recovery and
            # command-writeback evidence the detector cannot derive.
            build_cfg.update(detected)
            return "empty" if build_cfg["repo_is_empty"] else "non_empty"

        # Commitless: seed the trunk so `--branch <trunk>` clones stop dying,
        # then stamp the durable empty-repo signal so the guard suppresses the
        # doomed bootstrap here instead of retrying it to exhaustion.
        default_branch = project.integration_branch or await get_repo_default_branch(
            token, owner, repo
        )
        await ensure_repo_initialized(token, owner, repo, default_branch)
        build_cfg.update(_empty_repo_build_config_fields())
        return "empty"
    except Exception:  # noqa: BLE001 — best-effort: any failure leaves the signal undetermined
        logger.warning(
            "[BOOTSTRAP] Undetermined-emptiness self-heal failed for project %s "
            "(%s/%s) — leaving signal undetermined, falling through",
            project.id,
            owner,
            repo,
            exc_info=True,
        )
        return "undetermined"


@dataclass
class BootstrapSpawnResult:
    """Result of a spawn_bootstrap_task call."""

    outcome: Literal["spawned", "already_active", "already_completed", "not_eligible"]
    task_id: str | None = None
    reason: str = ""
    # A legacy emptiness self-heal can update the durable config even when the
    # request correctly decides not to enqueue a task. HTTP callers use this
    # signal to commit that narrow, successful repair without committing an
    # unrelated no-op outcome.
    build_config_changed: bool = False


async def spawn_bootstrap_task(
    project: Project,
    session: AsyncSession,
    *,
    is_first_bootstrap: bool = False,
    spawn_reason: str = "recovery",
    automatic_recovery: bool = False,
) -> BootstrapSpawnResult:
    """Idempotently enqueue a repo_bootstrap task for an eligible project.

    Eligibility:
    - Project must not be the deploy's own repo (``project_is_deploy_repo`` is
      False — deploy-identity match, or the ``is_self_repo`` fallback).
    - Project must not be an empty repo (``build_config.repo_is_empty`` is False/absent).
    - Project must not have ``bootstrap_completed_at`` set in ``build_config``
      UNLESS this is an operator refresh (``spawn_reason == "operator_refresh"``),
      which deliberately re-runs bootstrap for an already-completed project to
      replace the prior repo_overview with a fresher one.

    Idempotency:
    - Returns ``already_active`` if any QUEUED/PROVISIONING/IN_PROGRESS
      repo_bootstrap task already exists for this project.
    - Returns ``already_completed`` when ``bootstrap_completed_at`` is set
      (except under an operator refresh, which bypasses this gate).
    - Returns ``not_eligible`` for self-repo or empty-repo projects.

    On success creates the Task row with the given ``is_first_bootstrap`` flag
    and a SPAWNED TaskHistory row, then returns ``spawned`` with the task id.
    The caller is responsible for committing the session.

    A refresh (``spawn_reason == "operator_refresh"``) is a one-shot operator
    action: it does NOT clear ``bootstrap_completed_at`` (preserving the
    first-bootstrap strategic gate semantics for brand-new projects) and does
    NOT arm the automatic-recovery backoff loop.
    """
    automatic_creation = is_first_bootstrap or automatic_recovery
    if automatic_creation:
        from app.task_locking import (
            AUTOMATIC_TASK_CREATION_ALLOWED,
            acquire_automatic_task_creation_authority,
        )

        # Pause/archive is authoritative before any repository-side self-heal,
        # and this transaction lock is retained through the Task/config write.
        creation_admission = await acquire_automatic_task_creation_authority(
            session, project.id
        )
        if creation_admission != AUTOMATIC_TASK_CREATION_ALLOWED:
            return BootstrapSpawnResult(
                outcome="not_eligible",
                reason=f"automatic_task_creation:{creation_admission}",
            )
        await session.refresh(project)
    else:
        # Explicit retry/refresh remains allowed while paused, but it still
        # serializes its whole-JSON write against concurrent operator patches.
        from app.task_locking import lock_project_or_429

        project = await lock_project_or_429(session, project.id)

    build_cfg = dict(project.build_config or {})
    is_refresh = spawn_reason == REFRESH_SPAWN_REASON

    if project_is_deploy_repo(project):
        return BootstrapSpawnResult(
            outcome="not_eligible",
            reason="self_repo",
        )

    # Legacy projects predating empty-repo detection carry an undetermined
    # (absent/None) durable repo_is_empty. Both this gate and the recovery sweep
    # otherwise coalesce that to "not empty", so a genuinely commitless legacy
    # repo loops clone_failed forever. Resolve the signal against the live repo
    # first: a real empty repo is seeded + stamped here (and suppressed by the
    # guard below); a repo with commits is classified by its real source-file
    # count and proceeds only when that count exceeds the greenfield threshold.
    # A no-token / GitHub-error/incomplete detection leaves the signal
    # undetermined and falls through to the prior behavior, so nothing regresses.
    config_before_self_heal = dict(build_cfg)
    if build_cfg.get("repo_is_empty") is None:
        await _self_heal_undetermined_emptiness(project, build_cfg, session)
    build_config_changed = build_cfg != config_before_self_heal
    if build_config_changed:
        project.build_config = build_cfg
        flag_modified(project, "build_config")
        session.add(project)

    if project_is_deploy_repo(project):
        return BootstrapSpawnResult(
            outcome="not_eligible",
            reason="self_repo",
            build_config_changed=build_config_changed,
        )

    if build_cfg.get("repo_is_empty"):
        return BootstrapSpawnResult(
            outcome="not_eligible",
            reason="repo_is_empty",
            build_config_changed=build_config_changed,
        )

    if build_cfg.get("bootstrap_completed_at") and not is_refresh:
        return BootstrapSpawnResult(
            outcome="already_completed",
            reason="bootstrap_completed_at already set",
            build_config_changed=build_config_changed,
        )

    # Check for existing active bootstrap task.
    existing = (
        await session.execute(
            select(Task.id)
            .where(
                Task.project_id == project.id,
                Task.payloads[0]["type"].as_string() == "repo_bootstrap",
                Task.status.in_(_ACTIVE_STATUSES),
            )
            .limit(1)
        )
    ).first()

    if existing is not None:
        return BootstrapSpawnResult(
            outcome="already_active",
            task_id=str(existing[0]),
            reason="existing active repo_bootstrap task",
            build_config_changed=build_config_changed,
        )

    if is_refresh:
        # One-shot operator refresh of a completed project. Do NOT clear
        # bootstrap_completed_at (keeps the first-bootstrap strategic gate
        # intact for brand-new projects) and do NOT arm the auto-recovery
        # backoff loop — drop any stale backoff markers and record a clear
        # non-automatic recovery_state so sweeps never pick this up.
        build_cfg.pop("bootstrap_recovery_next_at", None)
        build_cfg.pop("bootstrap_recovery_exhausted_at", None)
        build_cfg["bootstrap_recovery_attempts"] = 0
        build_cfg["bootstrap_recovery_state"] = "operator_refresh"
        project.build_config = build_cfg
        flag_modified(project, "build_config")
        session.add(project)
    elif automatic_recovery:
        terminal_attempts = (
            await session.execute(
                select(Task.id).where(
                    Task.project_id == project.id,
                    Task.payloads[0]["type"].as_string() == "repo_bootstrap",
                    Task.status.in_(_TERMINAL_RETRY_STATUSES),
                )
            )
        ).all()
        recorded_attempts = coerce_recovery_attempts(
            build_cfg.get("bootstrap_recovery_attempts")
        )
        attempts = max(recorded_attempts, len(terminal_attempts))
        if attempts >= BOOTSTRAP_RECOVERY_MAX_ATTEMPTS:
            if not build_cfg.get("bootstrap_recovery_exhausted_at"):
                build_cfg["bootstrap_recovery_attempts"] = attempts
                build_cfg["bootstrap_recovery_state"] = "exhausted"
                build_cfg["bootstrap_recovery_exhausted_at"] = _utcnow().isoformat()
                project.build_config = build_cfg
                flag_modified(project, "build_config")
                session.add(project)
            return BootstrapSpawnResult(
                outcome="not_eligible",
                reason="bootstrap_recovery_exhausted",
            )
        next_recovery_at = _parse_recovery_at(
            build_cfg.get("bootstrap_recovery_next_at")
        )
        if next_recovery_at and next_recovery_at > _utcnow():
            return BootstrapSpawnResult(
                outcome="not_eligible",
                reason="bootstrap_recovery_backoff",
            )
    elif spawn_reason == "operator_retry":
        build_cfg.pop("bootstrap_recovery_next_at", None)
        build_cfg.pop("bootstrap_recovery_exhausted_at", None)
        build_cfg["bootstrap_recovery_attempts"] = 0
        build_cfg["bootstrap_recovery_state"] = "operator_retry"
        project.build_config = build_cfg
        flag_modified(project, "build_config")
        session.add(project)

    bootstrap_payload = {
        "type": "repo_bootstrap",
        "github_owner": project.github_owner or "",
        "github_repo": project.github_repo or "",
        "integration_branch": project.integration_branch or "",
        "is_first_bootstrap": is_first_bootstrap,
    }
    task = Task(
        project_id=project.id,
        title=f"Bootstrap: {project.name}",
        description=(
            "First-cycle repo bootstrap: read project structure, confirm "
            "build/test commands, seed design context"
            if is_first_bootstrap
            else "Recovery repo bootstrap: re-read project structure and seed design context"
        ),
        payloads=[bootstrap_payload],
        vm_instance_id=f"agent-vm-{uuid.uuid4().hex}",
        status=TaskStatus.QUEUED,
        created_by="ORCHESTRATOR",
        context={"bootstrap_reason": spawn_reason},
    )
    session.add(task)
    await session.flush()

    history = TaskHistory(
        task_id=task.id,
        action="SPAWNED",
        details={"spawned_by": "ORCHESTRATOR", "reason": spawn_reason},
    )
    session.add(history)

    logger.info(
        "[BOOTSTRAP] Spawned repo_bootstrap task %s for project %s (reason=%s)",
        task.id,
        project.id,
        spawn_reason,
    )
    return BootstrapSpawnResult(
        outcome="spawned",
        task_id=str(task.id),
        reason=spawn_reason,
    )
