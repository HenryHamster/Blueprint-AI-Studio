"""Unlocked qualification observations and exact short-transaction fences."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import func as sa_func
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.attributes import flag_modified

from app import github, release_qualification_control, release_version
from app.failure_reason import exception_reason
from app.models import Project
from app.time_utils import _utcnow

_MODULE_SIZE_GOVERNANCE_CAP: int = 500
logger = logging.getLogger(__name__)
def _release_qualifier_module():
    from app import release_qualifier as rq

    return rq

def qualification_authority_fingerprint(
    version: release_version.ReleaseVersion,
) -> str:
    authority = {
        "project_id": str(version.project_id),
        "version_id": str(version.id),
        "version_number": int(version.version_number),
        "status": version.status,
        "phase": version.phase,
        "phase_changed_at": (
            version.phase_changed_at.isoformat()
            if version.phase_changed_at is not None
            else None
        ),
        "candidate_sha": version.candidate_sha,
        "base_sha": version.base_sha,
        "goal": version.goal,
        "goal_updated_at": version.goal_updated_at,
        "last_qualification": version.last_qualification,
        "active_qualification_attempt_id": (
            str(version.active_qualification_attempt_id)
            if version.active_qualification_attempt_id is not None
            else None
        ),
        "qualification_attempts": int(version.qualification_attempts or 0),
        "qualification_retry_episode": int(
            version.qualification_retry_episode or 0
        ),
    }
    payload = json.dumps(
        authority, sort_keys=True, separators=(",", ":"), default=str
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def qualification_project_observation_fingerprint(project: Project) -> str:
    rq = _release_qualifier_module()
    integration_branch, promotion_target = rq._lts_branch_names(project)
    authority = {
        "project_id": str(project.id),
        "user_id": str(project.user_id),
        "github_owner": str(project.github_owner or "").strip(),
        "github_repo": str(project.github_repo or "").strip(),
        "integration_branch": integration_branch,
        "promotion_target": promotion_target,
        "build_config": project.build_config,
    }
    payload = json.dumps(
        authority, sort_keys=True, separators=(",", ":"), default=str
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def qualification_finalize_authority_fingerprint(
    version: release_version.ReleaseVersion,
) -> str:
    return qualification_authority_fingerprint(version)


async def _lock_exact_qualification_authority(
    session: AsyncSession,
    *,
    project_id: uuid.UUID,
    version_id: uuid.UUID,
    version_number: int,
    expected_phase_changed_at: datetime | None,
    expected_authority_fingerprint: str | None = None,
    expected_project_observation_fingerprint: str | None = None,
    fresh_automatic: bool = False,
    qualification_attempt_id: uuid.UUID | None = None,
) -> tuple[release_version.ReleaseVersion | None, dict[str, Any] | None]:
    admission_block = await release_qualification_control.qualification_start_block(
        session, project_id, version_number, fresh_automatic=fresh_automatic
    )
    if admission_block is not None:
        return None, admission_block
    locked_project = await session.get(Project, project_id, populate_existing=True)
    if locked_project is None:
        return None, {"status": "project_not_found"}
    if (
        expected_project_observation_fingerprint is not None
        and qualification_project_observation_fingerprint(locked_project)
        != expected_project_observation_fingerprint
    ):
        return None, {"status": "superseded_project_observation_state"}
    locked_version = (
        await session.execute(
            select(release_version.ReleaseVersion)
            .where(release_version.ReleaseVersion.id == version_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).scalars().first()
    if locked_version is None:
        return None, {"status": "version_not_found"}
    if (
        locked_version.project_id != project_id
        or locked_version.version_number != version_number
    ):
        return None, {"status": "version_authority_mismatch"}
    if locked_version.status != "open":
        return None, {"status": "version_not_open"}
    if locked_version.phase != "stabilize":
        return None, {"status": "wrong_phase"}
    if locked_version.phase_changed_at != expected_phase_changed_at:
        return None, {"status": "superseded_phase_epoch"}
    continuation_block = release_qualification_control.qualification_continuation_block(
        locked_version, qualification_attempt_id
    )
    if continuation_block is not None:
        return None, continuation_block
    if (
        expected_authority_fingerprint is not None
        and qualification_authority_fingerprint(locked_version)
        != expected_authority_fingerprint
    ):
        return None, {"status": "superseded_qualification_state"}
    return locked_version, None


async def _observe_ci_unresolved_promotion_pending(
    session: AsyncSession,
    version: release_version.ReleaseVersion,
    candidate_sha: str | None,
) -> dict[str, Any]:
    if not candidate_sha:
        return {"state": "not_applicable"}
    project = await session.get(Project, version.project_id)
    if project is None:
        return {"state": "unavailable", "reason": "project_not_found"}
    rq = _release_qualifier_module()
    integration_branch, promotion_target = rq._lts_branch_names(project)
    if integration_branch == promotion_target:
        return {"state": "not_applicable", "reason": "single_branch"}
    resolved = await rq._resolve_github_with_session(session, project)
    if resolved is None:
        return {"state": "unavailable", "reason": "no_credentials"}
    token, owner, repo = resolved
    try:
        receipt = await rq._resolve_exact_promotion_receipt(
            token, owner, repo, candidate_sha, version, project=project
        )
    except Exception as exc:  # noqa: BLE001 - exact receipt uncertainty defers
        logger.warning(
            "[RELEASE_QUALIFIER] exact promotion receipt observation failed "
            "for v%d (%s)",
            version.version_number,
            exc,
        )
        return {
            "state": "exact_receipt_unavailable", "reason": exception_reason(exc)
        }
    if receipt is not None:
        migration_status: str | None = None
        migration_reason: str | None = None
        receipt_pr_number = receipt.get("pr_number")
        if receipt_pr_number is not None:
            from app.rollup_migration_validation import (
                _validate_migration_chain_post_merge,
            )

            try:
                migration_status, migration_reason = (
                    await _validate_migration_chain_post_merge(
                        token,
                        owner,
                        repo,
                        int(receipt_pr_number),
                        receipt["merge_commit_sha"],
                    )
                )
            except Exception as exc:  # noqa: BLE001 - validation uncertainty defers
                logger.warning(
                    "[RELEASE_QUALIFIER] exact promotion receipt migration "
                    "validation failed for v%d (%s)",
                    version.version_number,
                    exc,
                )
                return {
                    "state": "exact_receipt_validation_unavailable",
                    "reason": exception_reason(exc),
                }
        return {
            "state": "exact_receipt",
            "receipt": receipt,
            "migration_status": migration_status,
            "migration_reason": migration_reason,
        }
    try:
        release_branch = f"release-v{version.version_number}"
        open_prs = await github.get_open_pull_requests(
            token, owner, repo, base=promotion_target
        )
        open_release_pr = next(
            (
                pr
                for pr in open_prs
                if (pr.get("head") or {}).get("ref") == release_branch
                and (pr.get("head") or {}).get("sha") == candidate_sha
                and (pr.get("base") or {}).get("ref") == promotion_target
                and pr.get("state", "open") == "open"
            ),
            None,
        )
        if open_release_pr is None:
            return {"state": "none"}
        return {
            "state": "open_exact_pr",
            "pr_number": open_release_pr.get("number"),
            "pr_url": open_release_pr.get("html_url"),
        }
    except Exception as exc:  # noqa: BLE001 - open-PR fallback remains best effort
        logger.warning(
            "[RELEASE_QUALIFIER] ci_unresolved recovery observation failed for "
            "v%d (%s)",
            version.version_number,
            exc,
        )
        return {"state": "unavailable", "reason": exception_reason(exc)}


async def _apply_ci_unresolved_promotion_observation(
    session: AsyncSession,
    version: release_version.ReleaseVersion,
    candidate_sha: str | None,
    base_record: dict[str, Any],
    observation: dict[str, Any],
) -> dict[str, Any] | None:
    if not candidate_sha:
        return None
    rq = _release_qualifier_module()
    state = observation.get("state")
    if state in {
        "exact_receipt_unavailable",
        "exact_receipt_validation_unavailable",
    }:
        return {
            "status": "pending",
            "reason": (
                "promotion_receipt_validation_deferred"
                if "validation" in state
                else "promotion_receipt_resolution_deferred"
            ),
            "detail": observation.get("reason"),
        }
    if state == "exact_receipt":
        receipt = observation.get("receipt")
        if not isinstance(receipt, dict):
            return None
        migration_status = observation.get("migration_status")
        if migration_status == "defer":
            return {
                "status": "pending",
                "reason": "promotion_receipt_validation_deferred",
            }
        confidence = rq._recover_confidence_from_ci_unresolved(base_record)
        if migration_status == "block":
            return await rq._record_promotion_failure(
                session,
                version,
                confidence,
                "migration_guard_blocked",
                detail=observation.get("migration_reason"),
                pr_url=receipt.get("pr_url"),
                lock_released=False,
            )
        return await rq._record_promotion_success(
            session,
            version,
            candidate_sha,
            confidence,
            pr_url=receipt.get("pr_url") or rq._idempotent_pr_url(base_record),
            empty_diff=bool(receipt.get("empty_diff")),
            promoted_sha=receipt["merge_commit_sha"],
            already_landed=True,
            lock_released=False,
        )
    if state != "open_exact_pr":
        return None
    pr_number = observation.get("pr_number")
    pr_url = observation.get("pr_url") or rq._idempotent_pr_url(base_record)
    q = dict(version.last_qualification or {})
    q.update(rq._recover_confidence_from_ci_unresolved(base_record))
    q["status"] = "pending"
    q["promotion_pending"] = {
        "pr_number": pr_number,
        "pr_url": pr_url,
        "managed_check_wait": True,
        "enabled_at": _utcnow().isoformat(),
        "restored_by_ci_unresolved_guard": True,
    }
    version.last_qualification = q
    flag_modified(version, "last_qualification")
    session.add(version)
    return {
        "status": "pending_managed_check_wait",
        "pr_number": pr_number,
        "pr_url": pr_url,
        "managed_check_wait": True,
    }


async def _candidate_already_on_main_at_finalize(
    session: AsyncSession,
    version: release_version.ReleaseVersion,
    candidate_sha: str | None,
) -> bool:
    """Fail closed when an already-landed merge-grade exemption is unprovable."""

    if not candidate_sha:
        return False
    rq = _release_qualifier_module()
    try:
        project = await session.get(Project, version.project_id)
        if project is None:
            return False
        integration_branch, promotion_target = rq._lts_branch_names(project)
        if integration_branch == promotion_target:
            return False
        resolved = await rq._resolve_github_with_session(session, project)
        if resolved is None:
            return False
        token, owner, repo = resolved
        return await rq._candidate_already_on_main(
            token, owner, repo, candidate_sha, version, project=project
        )
    except Exception as exc:  # noqa: BLE001 - exemption remains fail closed
        logger.warning(
            "[RELEASE_QUALIFIER] merge-grade already-on-main exemption check "
            "failed for v%d candidate %s (%s) — keeping the gate armed",
            version.version_number,
            (candidate_sha or "")[:8],
            exc,
        )
        return False


async def _count_project_actionable_open_completion_debt(
    session: AsyncSession, project_id: Any
) -> int:
    if project_id is None:
        return 0
    from app.models import CompletionDebtEntry

    stmt = (
        select(sa_func.count())
        .select_from(CompletionDebtEntry)
        .where(
            CompletionDebtEntry.project_id == project_id,
            CompletionDebtEntry.status == "open",
        )
    )
    return int((await session.execute(stmt)).scalar_one() or 0)


def _deployed_env_presence_snapshot(
    env_preflight_artifact: dict[str, Any] | None,
) -> dict[str, bool]:
    """Return presence only; environment values are never read or persisted."""

    from app.required_runtime_env import CLOUD_RUN_SIGNAL_ENV

    names: set[str] = {CLOUD_RUN_SIGNAL_ENV}
    if isinstance(env_preflight_artifact, dict):
        for entry in env_preflight_artifact.get("required_env") or []:
            if isinstance(entry, dict):
                name = entry.get("name")
                if isinstance(name, str) and name:
                    names.add(name)
    return {name: bool(os.getenv(name)) for name in names}


async def _measure_candidate_operator_authorship(
    session: AsyncSession,
    version: release_version.ReleaseVersion,
    *,
    candidate_sha_override: str | None = None,
) -> dict[str, Any]:
    """Measure the immutable base-to-head candidate authorship range."""

    rq = _release_qualifier_module()
    base_sha, candidate_sha = rq.candidate_authorship_range(version)
    if isinstance(candidate_sha_override, str) and candidate_sha_override:
        candidate_sha = candidate_sha_override
    project = await session.get(Project, version.project_id)
    if base_sha is None and isinstance(candidate_sha, str) and project is not None:
        derived_base_sha, derive_reason = await rq.resolve_candidate_base_sha(
            candidate_sha, session=session, project=project
        )
        if derived_base_sha is not None:
            base_sha = derived_base_sha
        else:
            logger.warning(
                "[RELEASE_QUALIFIER] v%d candidate authorship base derivation "
                "failed (%s)",
                version.version_number,
                derive_reason,
            )
            return {
                "state": "unresolved",
                "count": None,
                "offenders": [],
                "range": {"base_sha": None, "head_sha": candidate_sha},
                "reason": f"base_sha_unset_v{version.version_number}_{derive_reason}",
            }
    try:
        return await rq.count_non_bot_authored_commits(
            base_sha, candidate_sha, session=session, project=project
        )
    except Exception as exc:  # noqa: BLE001 - evidence remains fail closed
        logger.warning(
            "[RELEASE_QUALIFIER] candidate authorship measurement failed for "
            "v%d: %s",
            version.version_number,
            exc,
        )
        return {
            "state": "unresolved",
            "count": None,
            "offenders": [],
            "range": {"base_sha": base_sha, "head_sha": candidate_sha},
            "reason": "candidate_authorship_measurement_unavailable",
        }


def _authorship_receipt_from_measurement(
    version: release_version.ReleaseVersion,
    measurement: dict[str, Any],
    *,
    candidate_sha_override: str | None = None,
) -> tuple[dict[str, Any], tuple[str | None, str | None]]:
    rq = _release_qualifier_module()
    base_sha, head_sha = rq.candidate_authorship_range(version)
    if isinstance(candidate_sha_override, str) and candidate_sha_override:
        head_sha = candidate_sha_override
    if base_sha is None:
        measured_range = (
            measurement.get("range") if isinstance(measurement, dict) else None
        )
        if (
            isinstance(measured_range, dict)
            and isinstance(measured_range.get("base_sha"), str)
            and measured_range.get("head_sha") == head_sha
        ):
            base_sha = measured_range["base_sha"]
    receipt = rq.authorship_receipt_for_measurement(
        measurement, base_sha, head_sha
    )
    return receipt, (base_sha, head_sha)


async def _measured_authorship_receipt_for_audit_spawn(
    session: AsyncSession,
    version: release_version.ReleaseVersion,
    *,
    candidate_sha_override: str | None = None,
) -> tuple[dict[str, Any], tuple[str | None, str | None]]:
    rq = _release_qualifier_module()
    measurement_version = release_version.ReleaseVersion(
        id=version.id,
        project_id=version.project_id,
        version_number=version.version_number,
        base_sha=version.base_sha,
        candidate_sha=candidate_sha_override or version.candidate_sha,
    )
    measurement = await rq._measure_candidate_operator_authorship(
        session, measurement_version
    )
    return _authorship_receipt_from_measurement(
        version, measurement, candidate_sha_override=candidate_sha_override
    )
