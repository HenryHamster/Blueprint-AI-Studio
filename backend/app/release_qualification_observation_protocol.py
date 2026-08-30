"""Bounded late-observation protocol for qualification finalization."""

from __future__ import annotations

from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app import release_version
from app.models import Project

_MODULE_SIZE_GOVERNANCE_CAP: int = 100

ALREADY_ON_MAIN = "candidate_already_on_main"
CI_RECOVERY = "ci_recovery_observation"
HOTFIX_SCOPE = "hotfix_scope_preview"
LATE_OBSERVATION_KINDS = frozenset({ALREADY_ON_MAIN, CI_RECOVERY, HOTFIX_SCOPE})


class FinalizeObservationRequired(RuntimeError):
    """Finalization reached a predicate whose provider fact was not observed."""

    def __init__(self, kind: str):
        if kind not in LATE_OBSERVATION_KINDS:
            raise ValueError(f"Unsupported finalize observation kind: {kind}")
        self.kind = kind
        super().__init__(kind)


async def observe_finalize_requirement(
    session: AsyncSession,
    version: release_version.ReleaseVersion,
    kind: str,
    candidate_sha: str | None,
) -> Any:
    """Observe exactly one predicate-relevant provider fact without locks."""
    from app import release_qualifier as rq

    if kind == ALREADY_ON_MAIN:
        return await rq._candidate_already_on_main_at_finalize(
            session, version, candidate_sha
        )
    if kind == CI_RECOVERY:
        return await rq._observe_ci_unresolved_promotion_pending(
            session, version, candidate_sha
        )
    if kind == HOTFIX_SCOPE:
        try:
            return await rq.check_hotfix_scope_preview(
                session, version.project_id, candidate_sha=candidate_sha
            )
        except Exception:  # noqa: BLE001 - guidance stays best-effort
            return None
    raise ValueError(f"Unsupported finalize observation kind: {kind}")


async def extend_finalize_observation_packet(
    session: AsyncSession,
    version_id,
    packet: dict[str, Any],
    kind: str,
) -> dict[str, Any] | None:
    """Rollback locks, exact-check authority, then add one unlocked fact."""
    from app import release_qualifier as rq

    await session.rollback()
    version = await session.get(
        release_version.ReleaseVersion, version_id, populate_existing=True
    )
    if version is None or kind in packet:
        return None
    project = await session.get(Project, version.project_id, populate_existing=True)
    if (
        project is None
        or rq.qualification_finalize_authority_fingerprint(version)
        != packet.get("version_authority_fingerprint")
        or rq.qualification_project_observation_fingerprint(project)
        != packet.get("project_observation_fingerprint")
    ):
        await session.rollback()
        return None
    value = await observe_finalize_requirement(
        session,
        version,
        kind,
        (version.last_qualification or {}).get("candidate_sha"),
    )
    await session.rollback()
    return {**packet, kind: value}
