"""Shared effective-qualification-state resolution.

Several read surfaces derive a single ``qualification_state`` from a release
version's raw ``last_qualification`` record plus up to three optional
reconciliation *enrichments*:

1. **serving aggregate** — the aggregated per-artifact serving verdict
   (``aggregate_serving_status`` / ``reason_code`` / ``artifacts``) resolved by
   :func:`app.release_artifacts.version_serving_summary`.
2. **audit-crash evidence** — the version-scoped audit child envelope
   (``audit_child``) that lets a dead audit reconcile to ``audit_crashed``
   instead of a misleading ``qualifying``.
3. **finalized-verdict substitution** — swap a transient
   ``pending`` + ``promotion_pending`` record for a
   :class:`~app.models.QualificationVerdict` row's finalized
   ``terminal_status`` when the verdict is already resolved.

Historically each surface (the strategic payload, the agent fetch verb, the
admin release-version endpoints, the system-state snapshot) applied a
*different subset* of these enrichments, so they told contradictory stories in
exactly the windows that matter. This module houses the shared primitives so
every surface layers the enrichments in one fixed order and calls the single
:func:`app.release_version.summarize_qualification_state` classifier.

The classifier primitives (:func:`apply_verdict_substitution`,
:func:`resolve_effective_qualification_state`) are pure / synchronous: the
``serving`` tuple and ``audit_child`` envelope are resolved by the caller (they
need a DB session) and threaded in.

This module ALSO exposes two async DB-touching helpers,
:func:`resolve_newest_qualification_verdict` and
:func:`resolve_open_version_qualification_state`. The latter is the single seam
every OPEN-version read surface routes through: given a session + open version,
it GUARANTEES all three enrichments (serving aggregate, audit child, finalized
verdict) are applied in the same fixed order so the surfaces cannot silently
diverge. Callers may thread in enrichments they already resolved for other
response fields to avoid a redundant query, but the helper backfills any they
did not so the guarantee holds regardless of caller.
"""

from __future__ import annotations

import logging
from typing import Any, Tuple, TYPE_CHECKING

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import QualificationVerdict

if TYPE_CHECKING:  # pragma: no cover - typing only
    from app.models import ReleaseVersion, Project

logger = logging.getLogger("backend.app.qualification_state_resolver")

# Sentinel distinguishing "caller did not resolve this enrichment" (backfill it)
# from "caller resolved it and it is genuinely ``None``" (respect the ``None``).
_UNSET: Any = object()


# Verdict ``terminal_status`` values that represent a FINALIZED promotion
# outcome. When the newest verdict row carries one of these, it is canonical and
# supersedes a transient ``pending`` + ``promotion_pending`` ``last_qualification``
# record (the promotion PR has already resolved one way or the other).
FINALIZED_TERMINAL_STATUSES = frozenset(
    {"promoted", "not_promoted", "dry_run", "promotion_failed"}
)


def apply_verdict_substitution(
    qual: dict[str, Any] | None,
    latest_verdict: QualificationVerdict | None,
) -> Tuple[dict[str, Any], dict[str, Any] | None]:
    """Substitute a finalized verdict's ``terminal_status`` for a stale pending record.

    Pure. Returns ``(effective_qual, disagreement)``.

    ``disagreement`` is ``None`` unless the raw ``last_qualification`` sits in a
    transient ``pending`` state with a ``promotion_pending`` sub-key *while* the
    newest verdict row already carries a finalized ``terminal_status``. In that
    window the verdict is canonical: the returned ``effective_qual`` adopts the
    verdict's terminal status (and drops ``promotion_pending``) so downstream
    ``summarize_qualification_state`` sees the resolved outcome instead of the
    stale ``promotion_in_flight``, and ``disagreement`` records both sides for
    surfacing.

    Otherwise the input dict is returned unchanged with ``disagreement=None``.
    """
    q = qual if isinstance(qual, dict) else {}
    if (
        latest_verdict is not None
        and q.get("status") == "pending"
        and isinstance(q.get("promotion_pending"), dict)
        and latest_verdict.terminal_status in FINALIZED_TERMINAL_STATUSES
    ):
        effective = {**q, "status": latest_verdict.terminal_status}
        effective.pop("promotion_pending", None)
        disagreement = {
            "last_qualification_status": "pending",
            "verdict_terminal_status": latest_verdict.terminal_status,
        }
        return effective, disagreement
    return q, None


def resolve_effective_qualification_state(
    version: ReleaseVersion,
    project: Project | None = None,
    *,
    latest_verdict: QualificationVerdict | None = None,
    audit_child: dict[str, Any] | None = None,
    serving: Tuple[dict[str, Any], list[dict[str, Any]]] | None = None,
) -> Tuple[dict[str, Any] | None, dict[str, Any] | None]:
    """Resolve a version's effective ``qualification_state`` with all enrichments.

    Layers the three reconciliation enrichments in a fixed order over the raw
    ``last_qualification`` and returns ``(qualification_state, disagreement)``
    where ``qualification_state`` is the
    :func:`app.release_version.summarize_qualification_state` output (or ``None``
    when unqualified) and ``disagreement`` is the finalized-verdict
    disagreement dict (or ``None``).

    * ``serving`` — a pre-resolved ``(aggregate, artifacts)`` tuple from
      :func:`app.release_artifacts.version_serving_summary`. Callers that already
      computed it (to build other response fields) pass it to avoid a second
      resolve; when ``None`` it is resolved here.
    * ``audit_child`` — opt-in version-scoped audit envelope for audit-crash
      reconciliation; ``None`` preserves the pre-enrichment behavior.
    * ``latest_verdict`` — opt-in finalized-verdict substitution (see
      :func:`apply_verdict_substitution`); ``None`` disables it.
    """
    from app.release_artifacts import version_serving_summary
    from app.release_version import summarize_qualification_state

    if serving is None:
        aggregate, artifacts = version_serving_summary(version, project)
    else:
        aggregate, artifacts = serving

    qual = (
        version.last_qualification
        if isinstance(version.last_qualification, dict)
        else {}
    )
    effective_qual, disagreement = apply_verdict_substitution(qual, latest_verdict)

    state = summarize_qualification_state(
        effective_qual,
        version.serving_status,
        version.deploy_failure_reason,
        audit_child=audit_child,
        aggregate_serving_status=aggregate["serving_status"],
        aggregate_deploy_failure_reason=aggregate["reason_code"],
        artifact_evidence=artifacts,
    )
    return state, disagreement


async def resolve_newest_qualification_verdict(
    session: AsyncSession,
    version: ReleaseVersion,
) -> QualificationVerdict | None:
    """Return the newest ``QualificationVerdict`` row for ``version``.

    This is the single canonical newest-verdict query used for finalized-verdict
    substitution across every open-version read surface: scoped to the version
    (``release_version_id == version.id``), ordered newest-first, limit 1, with
    NO ``audit_findings`` filter (a finalized promotion verdict — e.g. an
    ``infra_failure`` or ``promotion_failed`` outcome — may legitimately carry no
    findings, and the substitution only cares about ``terminal_status``).
    Reusing this one helper keeps the surfaces from re-inlining subtly-different
    SELECTs.
    """
    return (
        (
            await session.execute(
                select(QualificationVerdict)
                .where(QualificationVerdict.release_version_id == version.id)
                .order_by(QualificationVerdict.created_at.desc())
                .limit(1)
            )
        )
        .scalars()
        .first()
    )


async def resolve_open_version_qualification_state(
    session: AsyncSession,
    version: ReleaseVersion,
    project: Project | None = None,
    *,
    serving: Tuple[dict[str, Any], list[dict[str, Any]]] | None = None,
    audit_child: Any = _UNSET,
    latest_verdict: Any = _UNSET,
) -> Tuple[dict[str, Any] | None, dict[str, Any] | None]:
    """Resolve an OPEN version's ``qualification_state`` with ALL enrichments.

    The single async seam every open-version read surface (strategic payload,
    agent fetch verb, admin lifecycle endpoint, system-state snapshot) routes
    through, so they cannot silently apply different enrichment subsets and tell
    contradictory stories in the stale-pending / audit-crashed windows.

    Returns ``(qualification_state, disagreement)`` — the
    :func:`resolve_effective_qualification_state` output — with all three
    reconciliation enrichments GUARANTEED applied in the fixed order:

    * ``serving`` — a pre-resolved ``(aggregate, artifacts)`` tuple (from
      :func:`app.release_artifacts.version_serving_summary`); ``None`` resolves
      it downstream. This is pure over already-loaded artifacts, so passing it is
      an optimization, not a correctness requirement.
    * ``audit_child`` — when left unset (``_UNSET``) the version-scoped audit
      child is resolved here via
      :func:`app.release_qualifier_audit.resolve_lifecycle_audit_evidence`
      (best-effort: a failure logs and degrades to ``None`` so a broken audit
      resolution never blocks payload/response assembly). Passing an explicit
      value (including ``None``) skips the query.
    * ``latest_verdict`` — when left unset the newest verdict is resolved via
      :func:`resolve_newest_qualification_verdict`. Passing an explicit value
      (including ``None``) skips the query so a caller that already loaded it can
      avoid a second round-trip.
    """
    if audit_child is _UNSET:
        audit_child = None
        try:
            from app.release_qualifier_audit import resolve_lifecycle_audit_evidence

            evidence = await resolve_lifecycle_audit_evidence(session, version)
            audit_child = evidence.get("latest_audit")
        except Exception:
            logger.exception(
                "[QUAL_STATE] audit-child resolution failed for version %s; "
                "resolving qualification_state without audit-crash reconciliation",
                getattr(version, "id", None),
            )
            audit_child = None

    if latest_verdict is _UNSET:
        latest_verdict = await resolve_newest_qualification_verdict(session, version)

    return resolve_effective_qualification_state(
        version,
        project,
        latest_verdict=latest_verdict,
        audit_child=audit_child,
        serving=serving,
    )
