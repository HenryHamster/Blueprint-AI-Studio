"""Admin project read/write endpoints for post-onboarding project config.

This router exposes the operator-facing project surface after onboarding:
list/view endpoints, owner-gated PATCH for project config, and a re-detect
endpoint that refreshes repo-derived defaults while preserving explicit
operator overrides for branch naming, cleanup prefixes, Git identity, and
self-improvement flags.
"""

import logging
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select, delete
from sqlalchemy import func, update
from typing import Dict, Any, List
import uuid
from datetime import timedelta
from sqlalchemy.orm import load_only
from sqlalchemy.orm.attributes import flag_modified

from app.database import get_session, resolve_dialect_name
from app.shared_state import limiter
from app.models import (
    Task,
    TaskStatus,
    Project,
    ProjectStatus,
    ProjectMember,
    _utcnow,
    notify_tasks_terminated,
)
from app.auth import get_current_user
from app.admin import (
    _get_user_project,
    _get_user_project_with_role,
    _member_project_ids,
    _user_project_ids,
)
from app.task_locking import lock_project_or_429
from app.build_config_lint import build_config_lint_warnings
from app.ci_resolution import (
    compute_merge_verification_verdict,
    merge_gate_runtime_block,
)
from app.capability_proof import enrich_problem_model_capability_proofs
from app.problem_model import compute_model_size_signal, ProblemModel
from app.capability_trial_verdict import get_trial_verdicts
from app.task_direction_refs import task_primary_direction_ids_shallow
from app.onboarding_proof import build_onboarding_proof_block
from app.project_outcome_projection import build_project_outcome_projection
from app.release_models import (
    CriterionEvidence,
    ReleaseDeployArtifact,
    ReleaseVersion,
    VersionAcceptanceCriterion,
)

router = APIRouter(
    prefix="/api/admin",
    tags=["admin-projects"],
    dependencies=[Depends(get_current_user)],
)


logger = logging.getLogger(__name__)


def _redact_build_config(build_config: dict, role: str) -> dict:
    """Return build_config with env_vars omitted for non-owner callers.

    Owners see the full config because they are responsible for Git identity
    and environment overrides; members get a redacted copy.
    """
    if role == "owner":
        return build_config
    result = dict(build_config)
    result.pop("env_vars", None)
    return result


def _build_capability_proof_summary(proof_records: Dict[str, Any]) -> Dict[str, Any]:
    """Convert capability_proof_records into a summary per capability.

    For each capability, returns: proof_count, latest proof_kind, latest proved_at,
    latest commit_sha (from the most recent proof entry in the list).
    """
    summary = {}
    if not proof_records:
        return summary
    for cap_name, entries in proof_records.items():
        if not isinstance(entries, list) or not entries:
            continue
        latest = entries[-1]  # Most recent entry is last
        summary[cap_name] = {
            "proof_count": len(entries),
            "latest_proof_kind": latest.get("proof_kind"),
            "latest_proved_at": latest.get("proved_at"),
            "latest_commit_sha": latest.get("commit_sha"),
        }
    return summary


@router.get("/projects/{project_id}/outcome-projection")
@limiter.limit("30/minute")
async def get_project_outcome_projection(
    request: Request,
    project_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(get_current_user),
):
    """Return separate delivery, integration, deployment, and evidence facts."""
    project = await _get_user_project(project_id, current_user["user_id"], session)

    current_version = (
        await session.execute(
            select(ReleaseVersion)
            .where(ReleaseVersion.project_id == project_id)
            .order_by(ReleaseVersion.version_number.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    promoted_version = (
        await session.execute(
            select(ReleaseVersion)
            .where(
                ReleaseVersion.project_id == project_id,
                ReleaseVersion.status == "promoted",
            )
            .order_by(ReleaseVersion.version_number.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    task_facts = (
        await session.execute(
            select(Task.status, func.count(Task.id), func.max(Task.updated_at))
            .where(Task.project_id == project_id, Task.archived.is_(False))
            .group_by(Task.status)
        )
    ).all()
    criteria = []
    evidence = []
    artifacts = []
    if current_version is not None:
        criteria = list(
            (
                await session.execute(
                    select(VersionAcceptanceCriterion).where(
                        VersionAcceptanceCriterion.release_version_id
                        == current_version.id
                    )
                )
            ).scalars()
        )
        evidence = list(
            (
                await session.execute(
                    select(CriterionEvidence).where(
                        CriterionEvidence.project_id == project_id,
                        CriterionEvidence.criterion_id.in_(
                            [criterion.id for criterion in criteria]
                        ),
                    )
                )
            ).scalars()
        ) if criteria else []
    if promoted_version is not None:
        artifacts = list(
            (
                await session.execute(
                    select(ReleaseDeployArtifact).where(
                        ReleaseDeployArtifact.version_id == promoted_version.id
                    )
                )
            ).scalars()
        )

    return build_project_outcome_projection(
        project_id=str(project_id),
        task_facts=task_facts,
        current_version=current_version,
        promoted_version=promoted_version,
        project=project,
        criteria=criteria,
        evidence=evidence,
        deploy_artifacts=artifacts,
        now=_utcnow(),
    )



@router.get("/projects")
@limiter.limit("30/minute")
async def get_all_projects(
    request: Request,
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
    archived: bool = Query(False),
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(get_current_user),
):
    """List the caller's projects with lightweight columns and redacted config.

    By default archived projects are EXCLUDED so they disappear from the active
    dashboard. Pass ``archived=true`` to fetch only the archived set (the UI's
    restore view). The ``archived`` flag is returned per item so the frontend
    can render archive state.
    """
    # Select only lightweight columns — large JSONB (directions, vision, design_doc,
    # problem_model, self_reflection_log, running_update) are excluded here and
    # available via GET /api/admin/projects/{project_id}.

    # Build dialect-safe directions_count expression
    dialect_name = resolve_dialect_name(session)
    if dialect_name == "sqlite":
        directions_count_expr = func.coalesce(
            func.json_array_length(Project.directions), 0
        )
    else:
        directions_count_expr = func.coalesce(
            func.jsonb_array_length(Project.directions), 0
        )

    stmt = (
        select(
            Project.id,
            Project.name,
            Project.created_at,
            Project.status,
            Project.user_id,
            Project.github_owner,
            Project.github_repo,
            Project.is_self_repo,
            Project.autonomous_enabled,
            Project.paused,
            Project.archived,
            Project.build_config,
            Project.merge_gate_runtime,
            Project.sr_catch_rate_targets,
            Project.running_update_acknowledged_at,
            directions_count_expr.label("directions_count"),
            ProjectMember.role.label("my_role"),
        )
        .join(ProjectMember, ProjectMember.project_id == Project.id)
        .where(
            ProjectMember.user_id == current_user["user_id"],
            Project.archived.is_(archived),
        )
        .order_by(Project.created_at.desc())
        .limit(limit + 1)
        .offset(offset)
    )
    rows = (await session.execute(stmt)).all()
    has_more = len(rows) > limit
    page_rows = rows[:limit]

    # Single grouped query for task-status counts across all user-accessible projects.
    # Scoped to caller membership via _user_project_ids — no cross-project leakage.
    project_ids_for_page = [row.id for row in page_rows]
    task_status_counts: Dict[str, Any] = {}
    if project_ids_for_page:
        counts_result = await session.execute(
            select(Task.project_id, Task.status, func.count())
            .where(Task.project_id.in_(project_ids_for_page))
            .where(Task.archived.is_(False))
            .group_by(Task.project_id, Task.status)
        )
        for proj_id, status, count in counts_result.all():
            proj_key = str(proj_id)
            if proj_key not in task_status_counts:
                task_status_counts[proj_key] = {
                    "queued": 0,
                    "running": 0,
                    "evaluating": 0,
                    "blocked": 0,
                    "parked": 0,
                    "failed": 0,
                    "completed": 0,
                    "cancelled": 0,
                    "total": 0,
                }
            bucket = task_status_counts[proj_key]
            n = int(count or 0)
            bucket["total"] += n
            if status == TaskStatus.QUEUED:
                bucket["queued"] += n
            elif status in (TaskStatus.IN_PROGRESS, TaskStatus.PROVISIONING):
                bucket["running"] += n
            elif status == TaskStatus.EVALUATING:
                bucket["evaluating"] += n
            elif status == TaskStatus.BLOCKED:
                bucket["blocked"] += n
            elif status == TaskStatus.PARKED:
                bucket["parked"] += n
            elif status == TaskStatus.FAILED:
                bucket["failed"] += n
            elif status == TaskStatus.COMPLETED:
                bucket["completed"] += n
            elif status == TaskStatus.CANCELLED:
                bucket["cancelled"] += n

    _empty_counts = {
        "queued": 0,
        "running": 0,
        "evaluating": 0,
        "blocked": 0,
        "parked": 0,
        "failed": 0,
        "completed": 0,
        "cancelled": 0,
        "total": 0,
    }
    return {
        "items": [
            {
                "id": str(row.id),
                "name": row.name,
                "created_at": row.created_at.isoformat() if row.created_at else None,
                "status": row.status,
                "user_id": row.user_id,
                "github_owner": row.github_owner,
                "github_repo": row.github_repo,
                "is_self_repo": row.is_self_repo,
                "autonomous_enabled": row.autonomous_enabled,
                "paused": row.paused,
                "archived": row.archived,
                "build_config": _redact_build_config(
                    row.build_config or {}, row.my_role
                ),
                "build_config_warnings": build_config_lint_warnings(row.build_config),
                "merge_verification_verdict": compute_merge_verification_verdict(row),
                "merge_gate_runtime": merge_gate_runtime_block(row),
                "sr_catch_rate_targets": row.sr_catch_rate_targets or {},
                "running_update_acknowledged_at": (
                    row.running_update_acknowledged_at.isoformat()
                    if row.running_update_acknowledged_at
                    else None
                ),
                "directions_count": row.directions_count,
                "my_role": row.my_role,
                "task_status_counts": task_status_counts.get(
                    str(row.id), _empty_counts
                ),
                "onboarding_proof": build_onboarding_proof_block(row),
            }
            for row in page_rows
        ],
        "has_more": has_more,
    }


@router.get("/projects/{project_id}")
@limiter.limit("30/minute")
async def get_project(
    request: Request,
    project_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(get_current_user),
):
    """Return the full project record including all JSONB detail columns."""
    query = (
        select(Project, ProjectMember.role)
        .join(ProjectMember, ProjectMember.project_id == Project.id)
        .where(
            Project.id == project_id,
            ProjectMember.user_id == current_user["user_id"],
        )
    )
    row = (await session.execute(query)).one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail="Project not found")
    project, role = row
    problem_model_dict = project.problem_model or {}
    proof_problem_model = enrich_problem_model_capability_proofs(
        problem_model_dict,
        getattr(project, "capability_proof_records", None),
    )
    size_health = None
    if problem_model_dict:
        try:
            pm = ProblemModel(**problem_model_dict)
            size_health = compute_model_size_signal(pm)
        except Exception as exc:  # noqa: BLE001
            logger.debug(
                "[STRATEGIC_STATE] problem model size signal skipped: %s",
                exc,
            )
    return {
        "id": str(project.id),
        "name": project.name,
        "user_id": project.user_id,
        "status": project.status,
        "github_owner": project.github_owner,
        "github_repo": project.github_repo,
        "created_at": project.created_at.isoformat() if project.created_at else None,
        "directions": project.directions,
        "vision": project.vision or {},
        "design_doc": project.design_doc or {},
        "problem_model": proof_problem_model,
        "problem_model_size_health": size_health,
        "running_update": project.running_update,
        "running_update_acknowledged_at": (
            project.running_update_acknowledged_at.isoformat()
            if project.running_update_acknowledged_at
            else None
        ),
        "self_reflection_log": project.self_reflection_log or [],
        "build_config": _redact_build_config(project.build_config or {}, role),
        "build_config_warnings": build_config_lint_warnings(project.build_config),
        "merge_verification_verdict": compute_merge_verification_verdict(project),
        "merge_gate_runtime": merge_gate_runtime_block(project),
        "autonomous_enabled": project.autonomous_enabled,
        "paused": project.paused,
        "is_self_repo": project.is_self_repo,
        "sr_catch_rate_targets": project.sr_catch_rate_targets or {},
        "approval_gate_config": project.approval_gate_config or {},
        "capability_proof_summary": _build_capability_proof_summary(
            project.capability_proof_records or {}
        ),
        "capability_adoption_verdicts": get_trial_verdicts(project),
        "my_role": role,
        "onboarding_proof": build_onboarding_proof_block(project),
    }


@router.get("/projects/{project_id}/summary")
@limiter.limit("30/minute")
async def get_project_summary(
    request: Request,
    project_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(get_current_user),
):
    """Return a lightweight project summary excluding heavy JSONB fields.

    Omits design_doc and self_reflection_log (which can be hundreds of KB)
    so mount-time fetches stay small. Use GET /api/admin/projects/{id} for the
    full record when design_doc or self_reflection_log are explicitly needed.
    """
    query = (
        select(Project, ProjectMember.role)
        .join(ProjectMember, ProjectMember.project_id == Project.id)
        .where(
            Project.id == project_id,
            ProjectMember.user_id == current_user["user_id"],
        )
    )
    row = (await session.execute(query)).one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail="Project not found")
    project, role = row
    problem_model_dict = project.problem_model or {}
    proof_problem_model = enrich_problem_model_capability_proofs(
        problem_model_dict,
        getattr(project, "capability_proof_records", None),
    )
    size_health = None
    if problem_model_dict:
        try:
            pm = ProblemModel(**problem_model_dict)
            size_health = compute_model_size_signal(pm)
        except Exception as exc:  # noqa: BLE001
            logger.debug(
                "[STRATEGIC_STATE] problem model size signal skipped: %s",
                exc,
            )
    return {
        "id": str(project.id),
        "name": project.name,
        "user_id": project.user_id,
        "status": project.status,
        "github_owner": project.github_owner,
        "github_repo": project.github_repo,
        "created_at": project.created_at.isoformat() if project.created_at else None,
        "directions": project.directions,
        "vision": project.vision or {},
        "problem_model": proof_problem_model,
        "problem_model_size_health": size_health,
        "running_update": project.running_update,
        "running_update_acknowledged_at": (
            project.running_update_acknowledged_at.isoformat()
            if project.running_update_acknowledged_at
            else None
        ),
        "build_config": _redact_build_config(project.build_config or {}, role),
        "build_config_warnings": build_config_lint_warnings(project.build_config),
        "merge_verification_verdict": compute_merge_verification_verdict(project),
        "merge_gate_runtime": merge_gate_runtime_block(project),
        "autonomous_enabled": project.autonomous_enabled,
        "paused": project.paused,
        "is_self_repo": project.is_self_repo,
        "sr_catch_rate_targets": project.sr_catch_rate_targets or {},
        "approval_gate_config": project.approval_gate_config or {},
        "capability_proof_summary": _build_capability_proof_summary(
            project.capability_proof_records or {}
        ),
        "capability_adoption_verdicts": get_trial_verdicts(project),
        "my_role": role,
        "onboarding_proof": build_onboarding_proof_block(project),
    }


class ProjectUpdate(BaseModel):
    name: str | None = None
    # Project lifecycle status. Mainly for correcting a wrongly-terminal status
    # (e.g. reactivating a project that was auto-terminalized). Setting CANCELLED
    # here is rejected — cancellation must go through the dedicated /cancel
    # endpoint, which also tears down running VMs.
    status: ProjectStatus | None = None
    directions: List[Dict[str, Any]] | None = None
    vision: Dict[str, Any] | None = None
    # Optimistic concurrency: supply the _meta.revision value the client
    # loaded.  Server rejects with 409 if another writer has advanced it.
    vision_base_revision: int | None = None
    design_doc: Dict[str, Any] | None = None
    # Optimistic concurrency: supply the _meta.revision value the client
    # loaded.  Server rejects with 409 if another writer has advanced it.
    design_doc_base_revision: int | None = None
    build_config: Dict[str, Any] | None = None
    autonomous_enabled: bool | None = None
    # Per-category SR catch-rate targets, e.g. {'security': 0.80}.
    # Absent key = no target; empty dict {} clears all targets.
    sr_catch_rate_targets: Dict[str, Any] | None = None
    # User-editable subset of problem_model — intent, constraints, context.
    # Gaps / capabilities / friction are agent-owned and not writable here.
    problem_model_patch: Dict[str, Any] | None = None
    # Optimistic concurrency: supply the problem_model revision the client
    # loaded.  Server rejects with 409 if another writer has advanced it.
    problem_model_base_revision: int | None = None
    # Per-project approval-gate file lists (infra_files / critical_files).
    # Partial update: only the keys supplied are replaced. Takes effect for
    # agents immediately via the existing claim-response seam.
    approval_gate_config: Dict[str, Any] | None = None


_OWNER_ONLY_UPDATE_FIELDS = frozenset(
    {
        "build_config",
        "autonomous_enabled",
        "design_doc",
        "vision",
        "approval_gate_config",
        "status",
    }
)


@router.patch("/projects/{project_id}")
@limiter.limit("30/minute")
async def update_project(
    request: Request,
    project_id: uuid.UUID,
    payload: ProjectUpdate,
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(get_current_user),
):
    """Apply operator updates while preserving project-owned branch metadata.

    Owner-only fields are gated separately. Build-config updates merge onto the
    stored config so branch namespaces, task prefixes, staging prefixes, stale
    prefixes, protected branches, and enable_self_improvement survive normal
    save cycles unless the caller explicitly replaces them.
    """
    project = await _get_user_project_with_role(
        project_id, current_user["user_id"], session, "member"
    )

    # Fetch the caller's actual role so we can gate owner-only fields.
    caller_member = (
        await session.execute(
            select(ProjectMember).where(
                ProjectMember.project_id == project_id,
                ProjectMember.user_id == current_user["user_id"],
            )
        )
    ).scalar_one_or_none()
    caller_role = caller_member.role if caller_member else "member"

    if caller_role != "owner" and any(
        getattr(payload, f) is not None for f in _OWNER_ONLY_UPDATE_FIELDS
    ):
        raise HTTPException(
            status_code=403,
            detail="Only project owners may change build_config, autonomous_enabled, design_doc, or vision.",
        )

    # Lock the project row whenever directions, design_doc, or vision are
    # being overwritten so concurrent agent writers cannot silently clobber.
    needs_lock = (
        payload.directions is not None
        or payload.design_doc is not None
        or payload.vision is not None
        or payload.approval_gate_config is not None
        or payload.problem_model_patch is not None
    )
    if needs_lock:
        project = await lock_project_or_429(session, project.id)
        attrs = [
            a
            for a, p in (
                ("directions", payload.directions),
                ("design_doc", payload.design_doc),
                ("vision", payload.vision),
                ("approval_gate_config", payload.approval_gate_config),
                ("problem_model", payload.problem_model_patch),
            )
            if p is not None
        ]
        await session.refresh(project, attribute_names=attrs)

    if payload.name is not None:
        project.name = payload.name
    if payload.status is not None:
        # Cancellation must go through POST /api/projects/{id}/cancel, which also
        # terminates running VMs; a bare status flip here would strand live VMs.
        if payload.status == ProjectStatus.CANCELLED:
            raise HTTPException(
                status_code=400,
                detail="Use the /cancel endpoint to cancel a project (it also tears down VMs).",
            )
        project.status = payload.status
    if payload.autonomous_enabled is not None:
        project.autonomous_enabled = payload.autonomous_enabled
    if payload.directions is not None:
        # Auto-generate ids for any directions missing one
        normalized = []
        for d in payload.directions:
            entry = dict(d)
            if not entry.get("id"):
                entry["id"] = f"d-{uuid.uuid4().hex[:8]}"
            if entry.get("priority") not in ("high", "medium", "low"):
                raise HTTPException(
                    status_code=400, detail=f"Invalid priority: {entry.get('priority')}"
                )
            if not isinstance(entry.get("text"), str) or not entry["text"].strip():
                raise HTTPException(
                    status_code=400, detail="Each direction must have non-empty text"
                )
            normalized.append(entry)
        project.directions = normalized
        flag_modified(project, "directions")
    if payload.vision is not None:
        stored_vision = project.vision or {}
        stored_rev = stored_vision.get("revision", 0) or 0
        if (
            payload.vision_base_revision is not None
            and payload.vision_base_revision != stored_rev
        ):
            raise HTTPException(
                status_code=409,
                detail=(
                    f"vision was modified by another writer "
                    f"(base_revision={payload.vision_base_revision}, current={stored_rev}). "
                    "Reload and retry."
                ),
            )
        vision = dict(payload.vision)
        vision["revision"] = stored_rev + 1
        vision["last_updated"] = _utcnow().isoformat()
        project.vision = vision
        flag_modified(project, "vision")
    if payload.design_doc is not None:
        stored_meta = (project.design_doc or {}).get("_meta", {}) or {}
        stored_rev = stored_meta.get("revision", 0) or 0
        if (
            payload.design_doc_base_revision is not None
            and payload.design_doc_base_revision != stored_rev
        ):
            raise HTTPException(
                status_code=409,
                detail=(
                    f"design_doc was modified by another writer "
                    f"(base_revision={payload.design_doc_base_revision}, current={stored_rev}). "
                    "Reload and retry."
                ),
            )
        doc = dict(payload.design_doc)
        # Remove the divergent top-level "revision" key; canonical counter is _meta.revision
        doc.pop("revision", None)
        # Preserve system/operator-owned underscore-prefixed keys that the
        # design-doc editor never surfaces.  The agent list/get endpoint filters
        # out ``_``-prefixed keys (admin_agent_ops.update_design_doc) and blocks
        # writing them, so a UI round-trip carries only the public keys — a
        # wholesale replace from the editor would otherwise silently DROP durable
        # config such as ``_auto_remediation`` / ``_issue_implementation`` /
        # ``_auto_review`` / ``_file_to_system`` (this is exactly how an operator's
        # enabled auto-remediation setting disappears after a later doc edit).
        # An incoming payload that DOES include a given underscore key still wins
        # (explicit operator intent).  ``_meta`` is rebuilt below, so skip it here.
        for _sys_key, _sys_val in (project.design_doc or {}).items():
            if (
                isinstance(_sys_key, str)
                and _sys_key.startswith("_")
                and _sys_key != "_meta"
                and _sys_key not in doc
            ):
                doc[_sys_key] = _sys_val
        meta = dict(stored_meta)
        meta["revision"] = stored_rev + 1
        meta["last_updated"] = _utcnow().isoformat()
        meta["updated_by"] = "user"
        doc["_meta"] = meta
        # Strip migrated telemetry keys before persist (safety net).
        from app.project_telemetry import strip_migrated_doc_keys as _strip_migrated_keys

        _strip_migrated_keys(doc)
        project.design_doc = doc
        flag_modified(project, "design_doc")
    if payload.build_config is not None:
        from pydantic import ValidationError

        from app.schemas import BuildConfig as BuildConfigSchema

        try:
            existing_bc = project.build_config or {}
            incoming = dict(payload.build_config)
            # Preserve operator-owned branch naming on round-trips.
            for _field in (
                "task_branch_namespace",
                "task_branch_prefix",
                "pr_title_prefix",
            ):
                if not incoming.get(_field) and existing_bc.get(_field):
                    incoming[_field] = existing_bc[_field]
            # Preserve per-project branch config using key-absence semantics:
            # an explicit empty list [] means "disable" and must not be lost.
            for _field in (
                "staging_branch_prefixes",
                "stale_branch_prefixes",
                "protected_branches",
                "enable_self_improvement",
                "requires_ci_checks",
            ):
                if _field not in incoming and _field in existing_bc:
                    incoming[_field] = existing_bc[_field]
            merged = {**existing_bc, **incoming}
            validated = BuildConfigSchema(**merged) if merged else BuildConfigSchema()
            project.build_config = validated.model_dump(exclude_none=True)
        except ValidationError as e:
            raise HTTPException(
                status_code=400, detail="Invalid build_config structure"
            ) from e
        flag_modified(project, "build_config")

    if payload.sr_catch_rate_targets is not None:
        # Validate: values must be floats in [0, 1]
        for cat, val in payload.sr_catch_rate_targets.items():
            if not isinstance(val, (int, float)) or not (0.0 <= float(val) <= 1.0):
                raise HTTPException(
                    status_code=400,
                    detail=f"sr_catch_rate_targets[{cat!r}] must be a float in [0, 1]",
                )
        project.sr_catch_rate_targets = {
            k: float(v) for k, v in payload.sr_catch_rate_targets.items()
        }
        flag_modified(project, "sr_catch_rate_targets")

    if payload.approval_gate_config is not None:
        # Reuse the dedicated config router's keys + path validation so both
        # write paths enforce identical semantics. Partial update: only the
        # supplied keys are replaced; others are preserved.
        from app.routers.admin_project_config import (
            _APPROVAL_GATE_KEYS,
            _validate_approval_gate_paths,
        )

        unknown = set(payload.approval_gate_config.keys()) - set(_APPROVAL_GATE_KEYS)
        if unknown:
            raise HTTPException(
                status_code=422,
                detail=f"Unknown approval_gate_config fields: {sorted(unknown)}",
            )
        cleaned = {
            key: _validate_approval_gate_paths(key, payload.approval_gate_config[key])
            for key in _APPROVAL_GATE_KEYS
            if key in payload.approval_gate_config
        }
        config = dict(project.approval_gate_config or {})
        config.update(cleaned)
        project.approval_gate_config = config
        flag_modified(project, "approval_gate_config")

    if payload.problem_model_patch is not None:
        # User-editable subset only — intent / context / constraints. Agent-
        # owned fields (gaps, capabilities, friction, archived_gaps,
        # pending_user_confirmation, revision_log) pass through unchanged.
        # Bump revision, stamp last_updated, mark last_updated_by="user"
        # so the agent can distinguish user edits during its next cycle.
        from pydantic import ValidationError as _PmValidationError

        from app.problem_model import (
            apply_user_intent_patch,
            parse_problem_model,
            sanitize_problem_model_raw,
        )

        # Stale-write guard runs first so a concurrent writer is reported as a
        # 409 before we attempt the merge. The editable-subset validation and
        # the operator-provenance re-stamp both live in apply_user_intent_patch,
        # so onboarding and this editor save share one correct path (the inline
        # shallow merge used to drop intent.source, silently un-protecting an
        # operator-edited purpose against the next strategic cycle).
        try:
            current = parse_problem_model(
                sanitize_problem_model_raw(project.problem_model or {})
            )
        except _PmValidationError as ve:
            raise HTTPException(
                status_code=400, detail=f"Stored problem_model is malformed: {ve}"
            ) from None
        if (
            payload.problem_model_base_revision is not None
            and payload.problem_model_base_revision != current.revision
        ):
            raise HTTPException(
                status_code=409,
                detail=(
                    f"problem_model was modified by another writer "
                    f"(base_revision={payload.problem_model_base_revision}, current={current.revision}). "
                    "Reload and retry."
                ),
            )
        try:
            validated = apply_user_intent_patch(
                sanitize_problem_model_raw(project.problem_model or {}),
                payload.problem_model_patch,
                _utcnow().isoformat(),
            )
        except ValueError as ve:
            raise HTTPException(status_code=400, detail=str(ve)) from None
        except _PmValidationError as ve:
            raise HTTPException(
                status_code=400, detail=f"Invalid problem_model patch: {ve}"
            ) from None
        from app.problem_model import enforce_problem_model_size as _enforce_pm_size

        _gc_summary, _oversize_section = _enforce_pm_size(validated)
        if _oversize_section:
            from app.alert_dispatch import (
                _maybe_create_problem_model_live_core_alert,
            )

            await _maybe_create_problem_model_live_core_alert(project.id, session)
            await session.commit()
            raise HTTPException(
                status_code=400,
                detail=(
                    f"Problem model live core exceeds byte budget after applying "
                    f"patch: {_oversize_section}. Reduce the size of active state "
                    f"(active gaps, live capabilities, or active friction)."
                ),
            )
        project.problem_model = validated.model_dump()
        flag_modified(project, "problem_model")

    session.add(project)
    await session.commit()
    await session.refresh(project)
    # Preserve the historical response shape (top-level Project fields) and
    # add a single advisory key — operators read it from the same payload
    # they already have. Warnings are computed from the validated, committed
    # build_config so the value reflects what was actually persisted.
    response = project.model_dump()
    response["build_config_warnings"] = build_config_lint_warnings(project.build_config)
    response["merge_verification_verdict"] = compute_merge_verification_verdict(project)
    response["merge_gate_runtime"] = merge_gate_runtime_block(project)
    return response


@router.post("/projects/{project_id}/detect-build-config")
@limiter.limit("10/minute")
async def detect_project_build_config(
    request: Request,
    project_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(get_current_user),
):
    """Re-run repo detection and preserve operator-owned overrides.

    This refreshes repo-derived commands/metadata from GitHub, then re-applies
    stored Git identity, branch namespace/prefixes, stale/protected branch
    lists, and enable_self_improvement before validating the final config.
    """
    project = await _get_user_project_with_role(
        project_id, current_user["user_id"], session, "owner"
    )
    if not project.github_owner or not project.github_repo:
        raise HTTPException(
            status_code=400, detail="Project has no GitHub repository configured"
        )

    from pydantic import ValidationError

    from app.build_config_detection import apply_operator_build_config_overrides
    from app.github import get_repo_installation_token
    from app.routers.admin_onboard import _detect_build_config
    from app.schemas import BuildConfig as BuildConfigSchema

    token = await get_repo_installation_token(project.github_owner, project.github_repo)
    if not token:
        raise HTTPException(
            status_code=502,
            detail="No GitHub installation token available for this repo",
        )

    detected = await _detect_build_config(
        token,
        project.github_owner,
        project.github_repo,
        # Preserve the legacy default-branch behavior for projects that still
        # have a NULL integration_branch column: omitting ref lets GitHub read
        # the repo's actual default branch instead of forcing synthetic "main".
        ref=project.integration_branch,
    )

    detected = apply_operator_build_config_overrides(
        detected, project.build_config or {}
    )

    try:
        validated = BuildConfigSchema(**detected) if detected else BuildConfigSchema()
        project.build_config = validated.model_dump(exclude_none=True)
    except ValidationError:
        # Detection produced a shape the schema doesn't accept — store raw so the user can fix it in the UI
        project.build_config = detected
    flag_modified(project, "build_config")
    await session.commit()
    await session.refresh(project)

    return {
        "build_config": project.build_config or {},
        "build_config_warnings": build_config_lint_warnings(project.build_config),
        "merge_verification_verdict": compute_merge_verification_verdict(project),
        "merge_gate_runtime": merge_gate_runtime_block(project),
    }


class CapabilityExemptionGrant(BaseModel):
    """Operator request to grant a proof exemption for a ``working`` capability.

    ``reason`` is the audit trail persisted alongside the operator's identity in
    the platform ledger — WHY this capability is being cleared without machine
    proof (e.g. "frontend-visual-verification-loop cannot be machine-verified").
    """

    capability_name: str = Field(..., min_length=1)
    reason: str | None = None


@router.post("/projects/{project_id}/capability-exemptions")
@limiter.limit("20/minute")
async def grant_capability_proof_exemption(
    request: Request,
    project_id: uuid.UUID,
    body: CapabilityExemptionGrant,
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(get_current_user),
):
    """Grant an OPERATOR proof exemption for a named ``working`` capability.

    This is the only authorized way to write the enforce-gate carve-out set: it
    appends a :func:`build_operator_exemption_record` (carrying the granting
    operator's identity — provenance the agent cannot forge) to the project's
    platform-only ``capability_proof_records`` ledger. An exempted capability
    clears every proof-gate seam WITHOUT machine proof and is excluded from the
    auto-graduation coverage denominator, so a genuinely unprovable capability no
    longer stalls the gate at ``observe`` forever.

    OWNER-guarded. Validates the capability name resolves to a declared ``working``
    capability in the project's problem_model (404 otherwise); a blank/unnormalizable
    name is rejected 400. Idempotent — a second grant for the same capability does
    not append a duplicate record.
    """
    from app.capability_registry import (
        CAPABILITY_EXEMPTION_PROOF_KIND,
        build_operator_exemption_record,
        normalize_capability_name,
        platform_exempt_capability_names,
        working_capability_names,
    )

    project = await _get_user_project_with_role(
        project_id, current_user["user_id"], session, "owner"
    )
    normalized = normalize_capability_name(body.capability_name)
    if not normalized:
        raise HTTPException(
            status_code=400, detail="capability_name is blank or unnormalizable"
        )
    working = working_capability_names(
        project.problem_model or {},
        proof_records=getattr(project, "capability_proof_records", None),
    )
    if normalized not in working:
        raise HTTPException(
            status_code=404,
            detail=(
                f"'{normalized}' is not a declared 'working' capability in this "
                "project's problem_model"
            ),
        )

    project = await lock_project_or_429(session, project.id)
    records = project.capability_proof_records
    records = dict(records) if isinstance(records, dict) else {}
    entries = records.get(normalized)
    entries = list(entries) if isinstance(entries, list) else []
    already_exempt = any(
        isinstance(e, dict)
        and str(e.get("proof_kind", "")).strip().lower()
        == CAPABILITY_EXEMPTION_PROOF_KIND
        for e in entries
    )
    if not already_exempt:
        entries.append(
            build_operator_exemption_record(
                normalized,
                authorized_by=current_user.get("email") or current_user["user_id"],
                reason=body.reason,
            )
        )
        records[normalized] = entries
        project.capability_proof_records = records
        flag_modified(project, "capability_proof_records")
        await session.commit()
        await session.refresh(project)

    return {
        "status": "granted",
        "capability_name": normalized,
        "already_exempt": already_exempt,
        "exempt_capabilities": sorted(
            platform_exempt_capability_names(project.capability_proof_records)
        ),
    }


@router.delete("/projects/{project_id}/capability-exemptions/{capability_name}")
@limiter.limit("20/minute")
async def revoke_capability_proof_exemption(
    request: Request,
    project_id: uuid.UUID,
    capability_name: str,
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(get_current_user),
):
    """Revoke the OPERATOR proof exemption for a named capability.

    Removes every operator-exemption record for the capability from the project's
    platform ledger, so it once again counts against the auto-graduation coverage
    denominator and must earn a real machine proof to clear the enforce gate.
    OWNER-guarded. Idempotent — revoking a capability with no exemption record
    returns ``removed: 0``. Does NOT require the capability to still be ``working``
    (an operator may revoke an exemption for a capability that was since removed).
    """
    from app.capability_registry import (
        CAPABILITY_EXEMPTION_PROOF_KIND,
        normalize_capability_name,
        platform_exempt_capability_names,
    )

    project = await _get_user_project_with_role(
        project_id, current_user["user_id"], session, "owner"
    )
    normalized = normalize_capability_name(capability_name)
    if not normalized:
        raise HTTPException(
            status_code=400, detail="capability_name is blank or unnormalizable"
        )

    project = await lock_project_or_429(session, project.id)
    records = project.capability_proof_records
    records = dict(records) if isinstance(records, dict) else {}
    entries = records.get(normalized)
    removed = 0
    if isinstance(entries, list):
        kept = [
            e
            for e in entries
            if not (
                isinstance(e, dict)
                and str(e.get("proof_kind", "")).strip().lower()
                == CAPABILITY_EXEMPTION_PROOF_KIND
            )
        ]
        removed = len(entries) - len(kept)
        if removed:
            if kept:
                records[normalized] = kept
            else:
                records.pop(normalized, None)
            project.capability_proof_records = records
            flag_modified(project, "capability_proof_records")
            await session.commit()
            await session.refresh(project)

    return {
        "status": "revoked",
        "capability_name": normalized,
        "removed": removed,
        "exempt_capabilities": sorted(
            platform_exempt_capability_names(project.capability_proof_records)
        ),
    }


@router.post("/projects/{project_id}/running-update/acknowledge")
@limiter.limit("30/minute")
async def acknowledge_running_update(
    request: Request,
    project_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(get_current_user),
):
    """Acknowledge the project's running_update — stamps the read-marker
    timestamp. The document itself persists; the strategic agent refines
    it on the next cycle using the acknowledgement timestamp to frame
    what's new since the user last caught up."""
    project = await _get_user_project_with_role(
        project_id, current_user["user_id"], session, "member"
    )
    project.running_update_acknowledged_at = _utcnow()
    session.add(project)
    await session.commit()
    return {
        "status": "acknowledged",
        "acknowledged_at": project.running_update_acknowledged_at.isoformat(),
    }


class RunningUpdateEdit(BaseModel):
    """Operator edit to the running_update body.

    ``body`` is the new full document. ``base`` is the document text the
    operator started editing from; when supplied it is compared against the
    current stored document under the row lock to detect a concurrent writer
    (agent cycle / routine digest) and reject the edit with a 409 so the
    operator can reload and retry — the same optimistic-concurrency discipline
    vision/design_doc use, adapted to a plain TEXT column with no embedded
    revision counter.
    """

    body: str
    base: str | None = None


@router.put("/projects/{project_id}/running-update")
@limiter.limit("30/minute")
async def edit_running_update(
    request: Request,
    project_id: uuid.UUID,
    payload: RunningUpdateEdit,
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(get_current_user),
):
    """Operator edit of the project's running_update body.

    Mirrors the operator-editable treatment vision and problem_model already
    get: the operator can correct a stale claim, add context, or flag a
    concern in the living status narrative. The write takes the project row
    lock and refreshes the field so a concurrent agent write is observed; when
    a ``base`` is supplied and differs from the current document a 409 is
    raised. A mid-cycle edit survives the next strategic cycle via the existing
    operator-wins reconciliation in ``running_update_merge``."""
    project = await _get_user_project_with_role(
        project_id, current_user["user_id"], session, "member"
    )
    project = await lock_project_or_429(session, project.id)
    await session.refresh(project, attribute_names=["running_update"])
    if payload.base is not None and (project.running_update or "") != payload.base:
        raise HTTPException(
            status_code=409,
            detail=(
                "running_update was modified by another writer since you began "
                "editing. Reload and retry."
            ),
        )
    project.running_update = payload.body
    session.add(project)
    await session.commit()
    return {
        "status": "updated",
        "running_update": project.running_update,
    }


class _DismissPendingBody(BaseModel):
    note: str | None = Field(None, max_length=5000)
    """Optional operator note to record in the revision log alongside the dismissal."""


@router.post("/projects/{project_id}/pending-confirm/{pending_id}/dismiss")
@limiter.limit("30/minute")
async def dismiss_pending_confirmation(
    request: Request,
    project_id: uuid.UUID,
    pending_id: str,
    payload: _DismissPendingBody = _DismissPendingBody(),
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(get_current_user),
):
    """Dismiss a strategist pending_user_confirmation item in-place.

    The strategist posts pending_user_confirmation items for external blockers
    (credentials needed, access needed, major decisions). Once the operator
    takes action they can dismiss the item here rather than waiting for the
    next autonomous strategic cycle to clear it.

    An optional ``note`` in the request body is recorded in the revision log
    so the strategist can see the operator's response on its next cycle.
    """
    from pydantic import ValidationError as _DismissVE

    from app.problem_model import append_revision_log, parse_problem_model, sanitize_problem_model_raw
    from app.time_utils import _utcnow

    project = await _get_user_project_with_role(
        project_id, current_user["user_id"], session, "member"
    )
    project = await lock_project_or_429(session, project.id)
    await session.refresh(project, attribute_names=["problem_model"])
    try:
        current_model = parse_problem_model(
            sanitize_problem_model_raw(project.problem_model or {})
        )
    except _DismissVE as ve:
        raise HTTPException(
            status_code=400, detail=f"Stored problem_model is malformed: {ve}"
        ) from None
    matched = next(
        (p for p in current_model.pending_user_confirmation if p.id == pending_id),
        None,
    )
    if matched is None:
        raise HTTPException(
            status_code=404, detail=f"Pending confirmation '{pending_id}' not found"
        )
    current_model.pending_user_confirmation = [
        p for p in current_model.pending_user_confirmation if p.id != pending_id
    ]
    current_model.revision += 1
    current_model.last_updated = _utcnow().isoformat()
    current_model.last_updated_by = "user"
    if payload.note:
        changes: dict = {
            "dismissed_proposal": matched.proposal,
            "dismissal_note": payload.note,
        }
        append_revision_log(current_model, f"operator dismissed {matched.type}", changes)
    # SHRINK path: always persist; alert when live core is still over budget.
    from app.problem_model import enforce_problem_model_size as _enforce_pm_size

    _gc_summary, _oversize_section = _enforce_pm_size(current_model)
    if _oversize_section:
        from app.alert_dispatch import (
            _maybe_create_problem_model_live_core_alert,
        )

        logger.warning(
            "dismiss_pending_confirmation: project %s problem_model live core "
            "still over budget after gc: %s",
            project_id,
            _oversize_section,
        )
        await _maybe_create_problem_model_live_core_alert(project_id, session)
    project.problem_model = current_model.model_dump()
    flag_modified(project, "problem_model")
    session.add(project)
    await session.commit()
    return {"status": "dismissed", "pending_id": pending_id}


class _AcceptPendingBody(BaseModel):
    answer: str | None = Field(None, max_length=5000)
    """For major_decision: the operator's answer text to record."""


@router.post("/projects/{project_id}/pending-confirm/{pending_id}/accept")
@limiter.limit("30/minute")
async def accept_pending_confirmation(
    request: Request,
    project_id: uuid.UUID,
    pending_id: str,
    payload: _AcceptPendingBody,
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(get_current_user),
):
    """Accept a pending_user_confirmation item with a type-specific action.

    - intent_change: applies the proposed text as the new intent.purpose and clears the item.
    - major_decision: records the operator's answer in the revision log and clears the item.
    - credentials_needed / access_needed: signals provided and clears the item.
    """
    from pydantic import ValidationError as _AcceptVE

    from app.problem_model import (
        apply_user_intent_patch,
        append_revision_log,
        parse_problem_model,
        sanitize_problem_model_raw,
    )
    from app.time_utils import _utcnow

    project = await _get_user_project_with_role(
        project_id, current_user["user_id"], session, "member"
    )
    project = await lock_project_or_429(session, project.id)
    await session.refresh(project, attribute_names=["problem_model"])
    try:
        current_model = parse_problem_model(
            sanitize_problem_model_raw(project.problem_model or {})
        )
    except _AcceptVE as ve:
        raise HTTPException(
            status_code=400, detail=f"Stored problem_model is malformed: {ve}"
        ) from None

    matched = next(
        (p for p in current_model.pending_user_confirmation if p.id == pending_id),
        None,
    )
    if matched is None:
        raise HTTPException(
            status_code=404, detail=f"Pending confirmation '{pending_id}' not found"
        )

    now = _utcnow().isoformat()

    if matched.type == "intent_change":
        # Apply the proposed text as the new operator-set intent.purpose.
        current_model = apply_user_intent_patch(
            sanitize_problem_model_raw(project.problem_model or {}),
            {"intent": {"purpose": matched.proposal}},
            now,
        )
        # Remove the pending item after applying the intent change.
        current_model.pending_user_confirmation = [
            p for p in current_model.pending_user_confirmation if p.id != pending_id
        ]
        if payload.answer:
            changes: dict = {
                "pending_accepted": {"id": pending_id, "type": matched.type},
                "accepted_proposal": matched.proposal,
                "operator_note": payload.answer,
            }
            append_revision_log(current_model, "operator accepted intent_change", changes)
    else:
        # For major_decision, credentials_needed, access_needed:
        # remove the item and record a revision log entry with any answer.
        current_model.pending_user_confirmation = [
            p for p in current_model.pending_user_confirmation if p.id != pending_id
        ]
        current_model.revision += 1
        current_model.last_updated = now
        current_model.last_updated_by = "user"
        changes: dict = {"pending_accepted": {"id": pending_id, "type": matched.type}}
        if payload.answer:
            changes["decision_answer"] = payload.answer
            changes["decision_question"] = matched.proposal
        append_revision_log(current_model, f"operator accepted {matched.type}", changes)

    # SHRINK path: always persist; alert when live core is still over budget.
    from app.problem_model import enforce_problem_model_size as _enforce_pm_size

    _gc_summary, _oversize_section = _enforce_pm_size(current_model)
    if _oversize_section:
        from app.alert_dispatch import (
            _maybe_create_problem_model_live_core_alert,
        )

        logger.warning(
            "accept_pending_confirmation: project %s problem_model live core "
            "still over budget after gc: %s",
            project_id,
            _oversize_section,
        )
        await _maybe_create_problem_model_live_core_alert(project_id, session)
    project.problem_model = current_model.model_dump()
    flag_modified(project, "problem_model")
    session.add(project)
    await session.commit()
    return {"status": "accepted", "pending_id": pending_id, "type": matched.type}


@router.delete("/projects/{project_id}")
@limiter.limit("10/minute")
async def delete_project_admin(
    request: Request,
    project_id: uuid.UUID,
    force: bool = Query(False),
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(get_current_user),
):
    """Delete a project and cascade to tasks/artifacts/history.
    Refuses if active tasks exist unless force=true."""
    # Phase C2 (L-C3): role check first (404/403 before lock contention surfaces
    # as 429), then acquire a Project row lock with skip_locked. Closes the
    # heartbeat-arrives-mid-delete race where a concurrent agent status POST
    # could mutate task rows between our cancel and FK-cascade delete.
    await _get_user_project_with_role(
        project_id, current_user["user_id"], session, "owner"
    )
    project = await lock_project_or_429(session, project_id)

    ACTIVE_STATUSES = [
        TaskStatus.QUEUED,
        TaskStatus.PROVISIONING,
        TaskStatus.IN_PROGRESS,
        TaskStatus.EVALUATING,
        # PARKED is live work waiting on an external wake — count it as active so
        # a non-force delete is refused while one exists, and a force delete
        # cancels it (its wake registration cascade-deletes with the task).
        TaskStatus.PARKED,
    ]
    # Bounded load: used for the existence check and per-task VM cleanup.
    # The bulk UPDATE below covers all matching rows regardless of this cap.
    active_tasks = (
        (
            await session.execute(
                select(Task)
                .where(Task.project_id == project_id, Task.status.in_(ACTIVE_STATUSES))
                .limit(1000)
            )
        )
        .scalars()
        .all()
    )

    if active_tasks and not force:
        raise HTTPException(
            status_code=409,
            detail=f"Project has {len(active_tasks)} active task(s). Cancel them first, or pass ?force=true.",
        )

    cancelled_count = 0
    if active_tasks and force:
        # Force: bulk-cancel all active tasks (covers rows beyond the 1000-row
        # load window), then delete VMs for the loaded subset.
        from app.vm_lifecycle import _ensure_vm_deleted

        # Fetch all affected task IDs before the bulk UPDATE so we can fire
        # terminal callbacks after commit — the bulk UPDATE bypasses the ORM
        # event listener that normally populates the JWT deny-list.
        # Bounded to 1000; the bulk UPDATE below covers ALL rows regardless.
        all_cancelled_ids = (
            (
                await session.execute(
                    select(Task.id).where(
                        Task.project_id == project_id,
                        Task.status.in_(ACTIVE_STATUSES),
                    ).limit(1000)
                )
            )
            .scalars()
            .all()
        )
        if len(all_cancelled_ids) >= 1000:
            logger.warning(
                "delete_project_admin: all_cancelled_ids reached limit 1000 for project %s — NOTIFY fanout truncated",
                project_id,
            )

        result = await session.execute(
            update(Task)
            .where(Task.project_id == project_id, Task.status.in_(ACTIVE_STATUSES))
            .values(
                status=TaskStatus.CANCELLED,
                latest_status_message="Project force-deleted",
            )
            .execution_options(synchronize_session=False)
        )
        cancelled_count = result.rowcount
        for task in active_tasks:
            await _ensure_vm_deleted(task, reason="project_force_delete")
        # Emit cross-instance NOTIFY before commit so sibling instances see the
        # revocation only after the bulk UPDATE is durable.  The post-commit
        # notify_tasks_terminated call still fires for in-process callback fanout.
        from app.jwt_revocation import emit_bulk_task_terminated_notify

        await emit_bulk_task_terminated_notify(session, all_cancelled_ids)
        await session.commit()
        notify_tasks_terminated(all_cancelled_ids)

    # Migration 0274 adds ON DELETE CASCADE to every remaining project-child FK
    # (messageslastseen, messagesnoiseprefs, messagessavedviews,
    # anomalyfingerprintstate, task, computejob, poolvm, poolidlecostledger,
    # poolidlespan, knowledgedocument, migrationreservation,
    # taskwakeregistration). The earlier 0058 migration already covered alert,
    # projectmember, projectmessage, strategiccycle, residualremediationcase,
    # and systemmaturity. Tables created after 0058 with explicit
    # ForeignKey(ondelete="CASCADE") in their model (direction, releaseversion,
    # qualificationverdict, auditfindingresolution, improvementlane,
    # evergreenbacklogitem) were correct from the start. With all FKs
    # cascading at the DB level, the project delete below is sufficient.
    from app.artifact_ref_cleanup import ensure_artifact_ref_cleanup_intent
    from app.artifact_ref_promotion import mark_project_artifact_ref_promotions_cleanup

    # No FK by design: the outbox row commits atomically with project deletion
    # and survives the cascade, including orphan-only / unknown-copy objects.
    await mark_project_artifact_ref_promotions_cleanup(session, project_id)
    await ensure_artifact_ref_cleanup_intent(session, project_id)
    await session.delete(project)
    await session.commit()

    # Evict from GitHub token cache (both the per-project entry and the repo's
    # mint-anchored install token — the project is gone, nothing should keep
    # serving a token for its repo).
    #
    # Then forget the project -> repo mapping itself. The project no longer
    # exists, so keeping it would leak an entry for the process lifetime AND
    # leave a stale mapping that lets a later invalidate_github_token_cache()
    # for this (recycled) id reach into a repo it no longer owns.
    from app.routers.agent import (
        forget_project_repo,
        invalidate_github_token_cache,
    )

    invalidate_github_token_cache(str(project_id))
    forget_project_repo(str(project_id))

    return {
        "message": f"Project {project_id} deleted",
        "active_tasks_cancelled": cancelled_count,
    }


@router.post("/projects/{project_id}/reset")
@limiter.limit("10/minute")
async def reset_project_admin(
    request: Request,
    project_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(get_current_user),
):
    """Reset a wedged project to a clean, runnable state without deleting it.

    Owner-only. Safely cancels and tears down any active tasks (reusing the
    same machinery as force-delete), then deletes all of the project's task
    and alert rows and clears transient runtime state (circuit breakers,
    in-memory strategic failure counts, persistent anomalies). Durable
    configuration — directions, vision, design doc, problem model, build
    config, members — is preserved by construction (never touched). The
    project is left in DEPLOYING so the orchestrator treats it as runnable.
    """
    # Role check first (404/403 before lock contention surfaces as 429), then
    # acquire the Project row lock — same ordering as delete to close the
    # heartbeat-arrives-mid-reset race.
    await _get_user_project_with_role(
        project_id, current_user["user_id"], session, "owner"
    )
    project = await lock_project_or_429(session, project_id)

    ACTIVE_STATUSES = [
        TaskStatus.QUEUED,
        TaskStatus.PROVISIONING,
        TaskStatus.IN_PROGRESS,
        TaskStatus.EVALUATING,
        # PARKED is live work waiting on an external wake — cancel it on reset so
        # the wake worker can't later wake a task into a reset project.
        TaskStatus.PARKED,
    ]
    # Bounded load for per-task VM cleanup; the bulk UPDATE below covers all
    # matching rows regardless of this cap.
    active_tasks = (
        (
            await session.execute(
                select(Task)
                .where(Task.project_id == project_id, Task.status.in_(ACTIVE_STATUSES))
                .order_by(Task.id)
                .limit(1000)
            )
        )
        .scalars()
        .all()
    )

    cancelled_count = 0
    if active_tasks:
        # Cancel + tear down active tasks before deleting their rows, reusing
        # the same safe cancellation/VM-teardown path as force-delete.
        from app.vm_lifecycle import _ensure_vm_deleted

        all_cancelled_ids = (
            (
                await session.execute(
                    select(Task.id)
                    .where(
                        Task.project_id == project_id,
                        Task.status.in_(ACTIVE_STATUSES),
                    )
                    .limit(1000)
                )
            )
            .scalars()
            .all()
        )

        result = await session.execute(
            update(Task)
            .where(Task.project_id == project_id, Task.status.in_(ACTIVE_STATUSES))
            .values(
                status=TaskStatus.CANCELLED,
                latest_status_message="Project reset to clean",
            )
            .execution_options(synchronize_session=False)
        )
        cancelled_count = result.rowcount
        if cancelled_count > len(all_cancelled_ids):
            logger.warning(
                "Project %s reset: %d active tasks but only %d fit in VM-cleanup cap (limit=1000); "
                "excess tasks cancelled without individual VM teardown or JWT notification",
                project_id, cancelled_count, len(all_cancelled_ids),
            )
        for task in active_tasks:
            await _ensure_vm_deleted(task, reason="project_reset")
        from app.jwt_revocation import emit_bulk_task_terminated_notify

        await emit_bulk_task_terminated_notify(session, all_cancelled_ids)
        await session.commit()
        notify_tasks_terminated(all_cancelled_ids)

    # Wipe all task history + alerts for the project, then return it to a
    # clean runnable state. Durable config columns are never touched.
    from app.models import Alert

    tasks_deleted = (
        await session.execute(delete(Task).where(Task.project_id == project_id))
    ).rowcount
    await session.execute(delete(Alert).where(Alert.project_id == project_id))
    project.status = ProjectStatus.DEPLOYING
    session.add(project)
    await session.commit()

    # Clear transient in-memory runtime/recovery state through one named seam.
    from app.anomaly_state import reset_project_state

    reset_project_state(str(project_id))

    # Evict from GitHub token cache so the next cycle re-fetches cleanly.
    from app.routers.agent import invalidate_github_token_cache

    invalidate_github_token_cache(str(project_id))

    return {
        "message": f"Project {project_id} reset to clean",
        "active_tasks_cancelled": cancelled_count,
        "tasks_deleted": tasks_deleted,
    }


@router.post("/projects/{project_id}/archive")
@limiter.limit("10/minute")
async def archive_project_admin(
    request: Request,
    project_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(get_current_user),
):
    """Archive a project: hide it from the active list and halt all its work.

    Owner-only and idempotent. Archiving sets ``archived=True`` so the project
    disappears from the default project list, the orchestrator stops dispatching
    its queued tasks, and the autonomous scheduler stops generating new work.
    All project data is retained so it can be unarchived back to normal
    operation later.
    """
    # Role check first (404/403 before lock contention surfaces as 429), then
    # acquire the Project row lock — same ordering as delete/reset.
    await _get_user_project_with_role(
        project_id, current_user["user_id"], session, "owner"
    )
    project = await lock_project_or_429(session, project_id)
    project.archived = True
    session.add(project)
    await session.commit()
    return {"message": f"Project {project_id} archived", "archived": True}


@router.post("/projects/{project_id}/unarchive")
@limiter.limit("10/minute")
async def unarchive_project_admin(
    request: Request,
    project_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(get_current_user),
):
    """Unarchive a project: restore it to the active list and normal eligibility.

    Owner-only and idempotent. Clears ``archived`` so the project reappears in
    the default list and the orchestrator/autonomous scheduler resume treating
    it as eligible for work.
    """
    await _get_user_project_with_role(
        project_id, current_user["user_id"], session, "owner"
    )
    project = await lock_project_or_429(session, project_id)
    project.archived = False
    session.add(project)
    await session.commit()
    return {"message": f"Project {project_id} unarchived", "archived": False}


@router.post("/autonomous/pause-all")
@limiter.limit("10/minute")
async def pause_all_autonomous(
    request: Request,
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(get_current_user),
):
    """Disable autonomous mode on all projects at once.

    Only pauses projects where the caller holds member+ role — viewers are
    documented read-only and may not mutate project state. This route takes no
    ``project_id``, so it is inherently the "pause everything I can" bulk shape:
    scoping viewer-only projects out silently is correct (a caller who is member
    on 3 projects and viewer on a 4th should pause the 3, not be rejected
    outright). ``skipped_count`` reports what was left running rather than
    hiding it.
    """
    user_project_ids = _member_project_ids(current_user["user_id"])
    user_project_count = int(
        (
            await session.scalar(
                select(func.count())
                .select_from(ProjectMember)
                .where(
                    ProjectMember.user_id == current_user["user_id"],
                    ProjectMember.role.in_(["member", "owner"]),
                )
            )
        )
        or 0
    )
    # Projects the caller can see but not pause (viewer-only), still autonomous.
    skipped_count = int(
        (
            await session.scalar(
                select(func.count())
                .select_from(Project)
                .where(
                    Project.id.in_(_user_project_ids(current_user["user_id"])),
                    Project.id.notin_(_member_project_ids(current_user["user_id"])),
                    Project.autonomous_enabled.is_(True),
                )
            )
        )
        or 0
    )
    projects = (
        (
            await session.execute(
                select(Project)
                .where(
                    Project.id.in_(user_project_ids)
                )
                .order_by(Project.id)
                .limit(500)
            )
        )
        .scalars()
        .all()
    )
    if user_project_count > 500:
        logger.warning(
            "pause-all: user has %d projects but cap limited fetch to 500; "
            "%d projects were NOT paused",
            user_project_count,
            user_project_count - 500,
        )
    for p in projects:
        p.autonomous_enabled = False
    await session.commit()
    return {"paused": len(projects), "skipped_count": skipped_count}


@router.get("/projects/{project_id}/directions/task-counts")
@limiter.limit("30/minute")
async def get_direction_task_counts(
    request: Request,
    project_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(get_current_user),
):
    await _get_user_project(project_id, current_user["user_id"], session)

    result = await session.execute(
        select(Task)
        .where(
            Task.project_id == project_id,
            Task.status.not_in([TaskStatus.CANCELLED, TaskStatus.FAILED]),
        )
        .options(
            load_only(
                Task.id,
                Task.status,
                Task.payloads,
                Task.context,
                Task.title,
                Task.latest_status_message,
                Task.created_at,
                raiseload=True,
            )
        )
        .limit(5000)
    )
    tasks = result.scalars().all()

    # Per-direction cap on the returned active-task preview. `counts` and
    # `counts_by_type` are still tallied across every matched task; only the
    # `active_tasks` list is bounded so this ~10s-polled response can't grow
    # without limit when a single direction accumulates thousands of live
    # tasks. We keep the most-recent N by created_at, sorting in Python AFTER
    # the scan so the query's (unordered) 5000-row set — and therefore the
    # counts — stays byte-identical to the previous behavior.
    _ACTIVE_TASKS_CAP = 50

    counts: Dict[str, int] = {}
    counts_by_type: Dict[str, Dict[str, int]] = {}
    active_raw: Dict[str, list[tuple]] = {}
    for task in tasks:
        payload = task.first_payload
        dir_ids = task_primary_direction_ids_shallow(task)
        if not dir_ids:
            continue
        task_type = payload.get("type") or "unknown"
        entry = {
            "id": str(task.id),
            "status": task.status.value
            if hasattr(task.status, "value")
            else str(task.status),
            "title": task.title or "",
            "latest_status_message": (task.latest_status_message or "")[:200],
        }
        for d_id in dir_ids:
            counts[d_id] = counts.get(d_id, 0) + 1
            counts_by_type.setdefault(d_id, {})
            counts_by_type[d_id][task_type] = counts_by_type[d_id].get(task_type, 0) + 1
            active_raw.setdefault(d_id, []).append((task.created_at, entry))

    active_tasks: Dict[str, list[dict]] = {}
    for d_id, entries in active_raw.items():
        # Sort on the created_at key only (never the tuple) so equal timestamps
        # don't force a comparison of the unorderable dict payloads.
        entries.sort(key=lambda e: e[0], reverse=True)
        active_tasks[d_id] = [item for _, item in entries[:_ACTIVE_TASKS_CAP]]

    return {
        "counts": counts,
        "counts_by_type": counts_by_type,
        "active_tasks": active_tasks,
    }


@router.patch("/projects/{project_id}/autonomous")
@limiter.limit("30/minute")
async def toggle_autonomous(
    request: Request,
    project_id: uuid.UUID,
    payload: Dict[str, Any],
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(get_current_user),
):
    project = await _get_user_project_with_role(
        project_id, current_user["user_id"], session, "owner"
    )

    if "enabled" not in payload:
        raise HTTPException(status_code=400, detail="'enabled' field required")

    project.autonomous_enabled = bool(payload["enabled"])
    # Reset failure counter when re-enabling
    if project.autonomous_enabled:
        from app.shared_state import autonomous_failure_counts

        autonomous_failure_counts.pop(str(project_id), None)
    session.add(project)
    await session.commit()
    return {"autonomous_enabled": project.autonomous_enabled}


@router.patch("/projects/{project_id}/pause")
@limiter.limit("30/minute")
async def set_project_paused(
    request: Request,
    project_id: uuid.UUID,
    payload: Dict[str, Any],
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(get_current_user),
):
    """Owner-only pause/resume switch for a project.

    Pause is a first-class persisted state, separate from the
    ``autonomous_enabled`` toggle and from terminal lifecycle ``status``.
    While paused, the orchestrator selects no new QUEUED work for the project
    on any dispatch path (cold or warm) and the autonomous spawn entry points
    skip it; in-flight PROVISIONING/IN_PROGRESS tasks drain. Resuming restores
    normal dispatch with no re-configuration. ``autonomous_enabled`` is left
    untouched so the two toggles compose independently.
    """
    project = await _get_user_project_with_role(
        project_id, current_user["user_id"], session, "owner"
    )

    if "paused" not in payload:
        raise HTTPException(status_code=400, detail="'paused' field required")

    project.paused = bool(payload["paused"])
    session.add(project)
    # [RESIDENT-REMOVAL] resident background-policy wakeup removed with the
    # resident subsystem (#15963); pause/resume no longer arms a resident wakeup.
    await session.commit()
    return {
        "paused": project.paused,
        "resident_wakeup_state": None,
    }


@router.get("/task-types")
@limiter.limit("30/minute")
async def get_task_types(
    request: Request,
    current_user: dict = Depends(get_current_user),
):
    """Return the enriched task-type registry as an operator catalog.

    Project-agnostic and static (the registry lives in code), so this performs
    no DB query — it serializes ``task_type_catalog()`` directly. It is the
    single backend source of truth the admin-ui catalog and task launcher read
    so the frontend capability list cannot drift from the backend registry.
    Gated by the router-level ``Depends(get_current_user)`` like its siblings.
    """
    from app.task_capabilities import task_type_catalog

    return {"task_types": task_type_catalog()}


@router.get("/task-types/cost-adherence")
@limiter.limit("30/minute")
async def get_task_type_cost_adherence(
    request: Request,
    days: int = Query(14, ge=1, le=90),
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(get_current_user),
):
    """Per-task-type actual VM cost vs the declared registry ceiling.

    DB-backed sibling of ``/task-types`` (which is intentionally DB-free): the
    catalog endpoint stays fast and static while the actual-cost comparison —
    which requires scanning recent terminal tasks — lives here. Returns, per
    registered task type, the average and p95 actual VM cost (including banked
    retry spend) over the last ``days`` days, a sample count, and an
    ``over_ceiling`` flag that fires only once a type has enough samples and its
    average exceeds its declared ceiling. Read-only and additive.

    The query explicitly loads every column the VM-cost accessor reads so the
    synchronous compute path never lazy-loads off the async session
    (MissingGreenlet). Scoped to the caller's projects.
    """
    from app.digest_metrics import compute_task_type_cost_adherence
    from app.models import ComputeJob
    from app.models import get_task_total_vm_cost_usd_or_none  # noqa: F401  (accessor used in compute fn)
    from app.task_capabilities import TASK_CAPABILITY_BY_VALUE

    window_start = _utcnow() - timedelta(days=days)
    user_pids = _user_project_ids(current_user["user_id"])

    result = await session.execute(
        select(Task)
        .where(
            Task.project_id.in_(user_pids),
            Task.created_at >= window_start,
            Task.status.in_(
                [TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.CANCELLED]
            ),
        )
        .options(
            load_only(
                Task.id,
                Task.status,
                Task.created_at,
                Task.payloads,
                Task.context,
                Task.vm_provisioned_at,
                Task.vm_stopped_at,
                Task.vm_spot,
                Task.vm_machine_type,
                Task.vm_runtime_cost_banked_db,
                raiseload=True,
            )
        )
        .limit(10000)
    )
    tasks = result.scalars().all()

    # Off-VM task types (e.g. compute_experiment) hold only a lightweight
    # monitoring VM; their real spend lives on the linked compute jobs' durable
    # ``final_cost_usd`` ledger. Aggregate that spend per task so the compute
    # function can measure those types honestly instead of from Task VM fields.
    # Lightweight second query (sum grouped by task_id, scoped to user projects);
    # keeps the compute transform DB-free.
    compute_cost_result = await session.execute(
        select(
            ComputeJob.task_id,
            func.coalesce(func.sum(ComputeJob.final_cost_usd), 0.0).label("spend"),
        )
        .where(
            ComputeJob.project_id.in_(user_pids),
            ComputeJob.task_id.isnot(None),
            ComputeJob.final_cost_usd.isnot(None),
        )
        .group_by(ComputeJob.task_id)
    )
    compute_job_cost_by_task = {
        str(task_id): float(spend)
        for task_id, spend in compute_cost_result.all()
        if task_id is not None
    }

    ceilings = {
        value: cap.cost_ceiling_usd for value, cap in TASK_CAPABILITY_BY_VALUE.items()
    }
    rows = compute_task_type_cost_adherence(
        tasks,
        window_start=window_start,
        ceilings=ceilings,
        compute_job_cost_by_task=compute_job_cost_by_task,
    )
    return {"window_days": days, "task_types": rows}


@router.get("/autonomous-status")
@limiter.limit("30/minute")
async def get_autonomous_status(
    request: Request,
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(get_current_user),
):
    from app.shared_state import autonomous_failure_counts, runtime_config
    from app.autonomous_research import project_parallelism_cap
    from app.autonomous_spawn_budget import (
        autonomous_spawn_budget_config_from_runtime,
        classify_autonomous_spawn_budget_category,
        compute_autonomous_spawn_budget_snapshot,
    )

    user_pids = (
        await session.execute(
            _user_project_ids(current_user["user_id"]).limit(500)
        )
    ).scalars().all()
    if len(user_pids) >= 500:
        logger.warning(
            "get_autonomous_status: user %s has >=500 project memberships — results truncated",
            current_user["user_id"],
        )
    projects = (
        (
            await session.execute(
                select(Project)
                .where(Project.id.in_(user_pids))
                .order_by(Project.created_at.desc())
                .limit(200)
                .options(
                    load_only(
                        Project.id,
                        Project.name,
                        Project.autonomous_enabled,
                        Project.build_config,
                        raiseload=True,
                    )
                )
            )
        )
        .scalars()
        .all()
    )
    if len(projects) >= 500:
        logger.warning(
            "get_autonomous_status: project list truncated at 500 for user %s",
            current_user["user_id"],
        )

    one_hour_ago = _utcnow() - timedelta(hours=1)
    budget_snapshot = None
    try:
        budget_snapshot = await compute_autonomous_spawn_budget_snapshot(
            session, runtime_config=runtime_config, now=_utcnow()
        )
    except Exception:  # noqa: BLE001
        logger.warning(
            "[AUTONOMOUS-STATUS] spawn budget snapshot failed open for user %s",
            current_user["user_id"],
            exc_info=True,
        )
    project_stats: dict = {
        project.id: {
            "tasks_spawned_last_hour": 0,
            "initiative_tasks_spawned_last_hour": 0,
            "servicing_tasks_spawned_last_hour": 0,
            "other_tasks_spawned_last_hour": 0,
            "research_create_tasks_spawned_last_hour": 0,
            "last_research_at": None,
        }
        for project in projects
    }
    if user_pids:
        budget_config = autonomous_spawn_budget_config_from_runtime(runtime_config)
        recent_autonomous_rows = (
            await session.execute(
                select(
                    Task.project_id,
                    Task.payloads,
                    Task.context,
                    Task.parent_task_id,
                    Task.depends_on,
                    Task.created_at,
                ).where(
                    Task.project_id.in_(user_pids),
                    Task.created_by == "ORCHESTRATOR",
                    Task.created_at >= one_hour_ago,
                ).limit(200)
            )
        ).all()
        if len(recent_autonomous_rows) >= 200:
            logger.warning(
                "get_autonomous_status: recent autonomous rows truncated at 200 for user %s",
                current_user["user_id"],
            )
        for row in recent_autonomous_rows:
            stats = project_stats.get(row.project_id)
            if stats is None:
                continue
            payloads = row.payloads if isinstance(row.payloads, list) else []
            payload = payloads[0] if payloads and isinstance(payloads[0], dict) else {}
            task_type = str(payload.get("type") or "").strip()
            category = classify_autonomous_spawn_budget_category(
                task_type=task_type,
                config=budget_config,
                parent_task_id=row.parent_task_id,
                depends_on=row.depends_on,
                payload=payload,
                context=row.context if isinstance(row.context, dict) else {},
            )
            stats["tasks_spawned_last_hour"] += 1
            stats[f"{category}_tasks_spawned_last_hour"] += 1
            if task_type in {"research", "create"}:
                stats["research_create_tasks_spawned_last_hour"] += 1
                last_research_at = stats["last_research_at"]
                if last_research_at is None or row.created_at > last_research_at:
                    stats["last_research_at"] = row.created_at

    if resolve_dialect_name(session) == "sqlite":
        initiative_type_expr = func.json_extract(Task.payloads, "$[0].type")
    else:
        initiative_type_expr = Task.payloads[0]["type"].as_string()
    if user_pids:
        last_research_rows = (
            await session.execute(
                select(
                    Task.project_id,
                    func.max(Task.created_at).label("max_created_at"),
                )
                .where(
                    Task.project_id.in_(user_pids),
                    Task.created_by == "ORCHESTRATOR",
                    Task.created_at >= _utcnow() - timedelta(days=30),
                    initiative_type_expr.in_(["research", "create"]),
                )
                .group_by(Task.project_id)
                .limit(200)
            )
        ).all()
        if len(last_research_rows) >= 200:
            logger.warning(
                "get_autonomous_status: last_research_rows truncated at 200 for user %s",
                current_user["user_id"],
            )
        last_research_map = {
            row.project_id: row.max_created_at for row in last_research_rows
        }
    else:
        last_research_map = {}

    fleet_default = runtime_config["max_autonomous_tasks_per_hour"]
    result = []
    for project in projects:
        project_id_str = str(project.id)
        effective_cap = project_parallelism_cap(
            project,
            "max_autonomous_tasks_per_hour",
            "max_autonomous_tasks_per_hour",
            12,
        )
        bc = getattr(project, "build_config", None) or {}
        override_val = bc.get("max_autonomous_tasks_per_hour")
        has_override = (
            isinstance(override_val, int)
            and not isinstance(override_val, bool)
            and override_val >= 1
        )
        result.append(
            {
                "project_id": project_id_str,
                "project_name": project.name,
                "autonomous_enabled": project.autonomous_enabled,
                "consecutive_failures": autonomous_failure_counts.get(
                    project_id_str, 0
                ),
                "last_research_at": last_research_map[project.id].isoformat()
                if last_research_map.get(project.id)
                else None,
                "tasks_spawned_last_hour": project_stats[project.id][
                    "tasks_spawned_last_hour"
                ],
                "initiative_tasks_spawned_last_hour": project_stats[project.id][
                    "initiative_tasks_spawned_last_hour"
                ],
                "servicing_tasks_spawned_last_hour": project_stats[project.id][
                    "servicing_tasks_spawned_last_hour"
                ],
                "other_tasks_spawned_last_hour": project_stats[project.id][
                    "other_tasks_spawned_last_hour"
                ],
                "research_create_tasks_spawned_last_hour": project_stats[project.id][
                    "research_create_tasks_spawned_last_hour"
                ],
                "rate_limit": effective_cap,
                "fleet_default_rate_limit": fleet_default,
                "rate_limit_source": "project_override"
                if has_override
                else "fleet_default",
            }
        )

    return {
        "projects": result,
        "global_rate_limit": budget_snapshot.as_log_fields()
        if budget_snapshot is not None
        else None,
    }


@router.post("/projects/{project_id}/bootstrap/retry")
@limiter.limit("5/minute")
async def retry_project_bootstrap(
    request: Request,
    project_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(get_current_user),
):
    """Enqueue a recovery repo_bootstrap task for a degraded or never-bootstrapped project.

    Returns a JSON body with ``state`` describing the outcome:
    - ``spawned`` — a new task was created; ``task_id`` is present.
    - ``already_active`` — a bootstrap task is already running; no duplicate created.
    - ``already_completed`` — bootstrap_completed_at is set; no task created.
    - ``not_eligible`` — project is a self-repo or empty-repo; not bootstrappable.
    """
    from app.repo_bootstrap import spawn_bootstrap_task

    project = await _get_user_project_with_role(
        project_id, current_user["user_id"], session, "owner"
    )

    result = await spawn_bootstrap_task(
        project,
        session,
        is_first_bootstrap=False,
        spawn_reason="operator_retry",
    )
    if result.outcome == "spawned" or result.build_config_changed:
        await session.commit()

    return {
        "state": result.outcome,
        "task_id": result.task_id,
        "reason": result.reason,
    }


@router.post("/projects/{project_id}/bootstrap/refresh")
@limiter.limit("5/minute")
async def refresh_project_bootstrap(
    request: Request,
    project_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    current_user: dict = Depends(get_current_user),
):
    """Re-run repo_bootstrap for an already-COMPLETED project to refresh its overview.

    Unlike ``/bootstrap/retry`` (which serves degraded/never-run projects), this
    bypasses the ``bootstrap_completed_at`` not-eligible gate so an operator can
    request a fresh overview for a healthy project. It is a one-shot action: it
    does NOT clear ``bootstrap_completed_at`` and does NOT arm automatic retries.

    Returns a JSON body with ``state`` describing the outcome:
    - ``spawned`` — a new bootstrap task was created; ``task_id`` is present.
    - ``already_active`` — a bootstrap task is already running; no duplicate created.
    - ``not_eligible`` — project is a self-repo or empty-repo; not bootstrappable.
    """
    from app.repo_bootstrap import REFRESH_SPAWN_REASON, spawn_bootstrap_task

    project = await _get_user_project_with_role(
        project_id, current_user["user_id"], session, "owner"
    )

    result = await spawn_bootstrap_task(
        project,
        session,
        is_first_bootstrap=False,
        spawn_reason=REFRESH_SPAWN_REASON,
    )
    if result.outcome == "spawned" or result.build_config_changed:
        await session.commit()

    return {
        "state": result.outcome,
        "task_id": result.task_id,
        "reason": result.reason,
    }
