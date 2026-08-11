"""Hotfix audit-trail recovery used by the release qualification sweep.

This module owns the small recovery seam that finds promoted hotfixes whose
completion-debt or operator alert was lost and restores only the missing
artifact.  The sweep module re-exports these helpers so its existing patch and
import surface remains stable.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app import release_version
from app.database import AsyncSessionLocal
from app.models import Alert


def _hotfix_audit_was_attempted(qual: dict[str, Any]) -> bool:
    """Recognize promoted and durably persisted hotfix wait states."""
    promotion_pending = qual.get("promotion_pending")
    persisted_wait = (
        qual.get("status") == "pending"
        and isinstance(promotion_pending, dict)
        and promotion_pending.get("pr_number") is not None
    )
    return qual.get("status") == "promoted" or persisted_wait


async def _reconcile_hotfix_audit_trail(
    version_id: Any, *, session_factory=None
) -> bool:
    """Restore a hotfix's debt/Alert after its request session was lost.

    The hotfix promotion commits in its own session because ``promote`` releases
    its lock mid-flight.  Re-read the committed provenance under a fresh
    transaction and repair only the missing artifact while holding the version
    lock.  The request path uses this same function, so it cannot race the
    periodic recovery writer.
    """
    from app import release_qualifier
    from app.completion_debt import (
        hotfix_deploy_alert_dedupe_key,
        hotfix_deploy_debt_dedupe_key,
    )
    from app.models import CompletionDebtEntry

    factory = session_factory or AsyncSessionLocal
    async with factory() as audit_session:
        version = (
            (
                await audit_session.execute(
                    select(release_version.ReleaseVersion)
                    .where(release_version.ReleaseVersion.id == version_id)
                    .with_for_update()
                )
            )
            .scalars()
            .first()
        )
        qual = dict(version.last_qualification or {}) if version else {}
        candidate_sha = qual.get("candidate_sha")
        if not (
            qual.get("hotfix_promote") is True
            and isinstance(candidate_sha, str)
            and candidate_sha
            and _hotfix_audit_was_attempted(qual)
        ):
            return False
        debt_key = hotfix_deploy_debt_dedupe_key(version.project_id, candidate_sha)
        alert_key = hotfix_deploy_alert_dedupe_key(version.project_id, candidate_sha)
        debt = (
            await audit_session.execute(
                select(CompletionDebtEntry).where(
                    CompletionDebtEntry.dedupe_key == debt_key
                )
            )
        ).scalars().first()
        alert = (
            await audit_session.execute(
                select(Alert).where(
                    Alert.project_id == version.project_id,
                    Alert.dedupe_key == alert_key,
                )
            )
        ).scalars().first()
        if debt is not None and alert is not None:
            return False
        await release_qualifier._record_hotfix_audit_trail(
            audit_session,
            version.project_id,
            candidate_sha=candidate_sha,
            actor=qual.get("hotfix_promote_by"),
            reason=qual.get("hotfix_promote_reason"),
            scope_modules=qual.get("hotfix_promote_scope_modules"),
            diff_file_list=qual.get("hotfix_promote_diff_file_list"),
            prior_deployed_sha=qual.get("hotfix_promote_prior_deployed_sha"),
            record_debt=debt is None,
            record_alert=alert is None,
        )
        await audit_session.commit()
        return True


async def _hotfix_audit_recovery_candidate_ids(session: AsyncSession) -> list[Any]:
    """Return hotfix versions whose debt or Alert is missing.

    The first query uses JSON provenance to exclude ordinary release history.
    Two bulk artifact reads then exclude completed hotfixes before the sweep
    opens a dedicated transaction or takes a row lock.  The under-lock helper
    remains the authority because request completion can race this snapshot.
    """
    from app.completion_debt import (
        hotfix_deploy_alert_dedupe_key,
        hotfix_deploy_debt_dedupe_key,
    )
    from app.models import CompletionDebtEntry

    hotfix_rows = list(
        (
            await session.execute(
                select(
                    release_version.ReleaseVersion.id,
                    release_version.ReleaseVersion.project_id,
                    release_version.ReleaseVersion.last_qualification,
                ).where(
                    release_version.ReleaseVersion.status.in_(("open", "promoted")),
                    release_version.ReleaseVersion.last_qualification[
                        "hotfix_promote"
                    ]
                    .as_boolean()
                    .is_(True),
                )
            )
        ).all()
    )
    candidates: list[tuple[Any, str, str]] = []
    for version_id, project_id, raw_qual in hotfix_rows:
        qual = dict(raw_qual or {})
        candidate_sha = qual.get("candidate_sha")
        if not (
            isinstance(candidate_sha, str)
            and candidate_sha
            and _hotfix_audit_was_attempted(qual)
        ):
            continue
        candidates.append(
            (
                version_id,
                hotfix_deploy_debt_dedupe_key(project_id, candidate_sha),
                hotfix_deploy_alert_dedupe_key(project_id, candidate_sha),
            )
        )
    if not candidates:
        return []

    debt_keys = {debt_key for _, debt_key, _ in candidates}
    alert_keys = {alert_key for _, _, alert_key in candidates}
    existing_debt_keys = set(
        (
            await session.execute(
                select(CompletionDebtEntry.dedupe_key).where(
                    CompletionDebtEntry.dedupe_key.in_(debt_keys)
                )
            )
        ).scalars()
    )
    existing_alert_keys = set(
        (
            await session.execute(
                select(Alert.dedupe_key).where(Alert.dedupe_key.in_(alert_keys))
            )
        ).scalars()
    )
    return [
        version_id
        for version_id, debt_key, alert_key in candidates
        if debt_key not in existing_debt_keys or alert_key not in existing_alert_keys
    ]
