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
from datetime import datetime
from typing import Any, Tuple, TYPE_CHECKING

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import QualificationVerdict

if TYPE_CHECKING:  # pragma: no cover - typing only
    from app.models import ReleaseVersion, Project

logger = logging.getLogger("backend.app.qualification_state_resolver")


def _parse_iso_naive(raw: Any) -> datetime | None:
    """Parse an ISO-8601 string to a naive datetime, or return None on failure."""
    if not isinstance(raw, str) or not raw:
        return None
    try:
        dt = datetime.fromisoformat(raw)
        # Strip tzinfo to produce a naive UTC datetime comparable to DB values.
        return dt.replace(tzinfo=None)
    except ValueError:
        return None


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
    newest verdict row already carries a finalized ``terminal_status`` for the
    same ``candidate_sha``. In that window the verdict is canonical: the
    returned ``effective_qual`` adopts the verdict's terminal status (and drops
    ``promotion_pending``) so downstream ``summarize_qualification_state`` sees
    the resolved outcome instead of the stale ``promotion_in_flight``, and
    ``disagreement`` records both sides for surfacing.

    Otherwise the input dict is returned unchanged with ``disagreement=None``.
    """
    q = qual if isinstance(qual, dict) else {}
    candidate_sha = q.get("candidate_sha")
    promotion_pending = q.get("promotion_pending")
    if (
        latest_verdict is not None
        and q.get("status") == "pending"
        and isinstance(promotion_pending, dict)
        and isinstance(candidate_sha, str)
        and candidate_sha
        and getattr(latest_verdict, "candidate_sha", None) == candidate_sha
        and latest_verdict.terminal_status in FINALIZED_TERMINAL_STATUSES
    ):
        # Temporal guard: a finalized verdict for the SAME sha from a PREVIOUS
        # attempt (dry_run before a force-promote, not_promoted before a
        # same-sha re-run, promotion_failed before a retry) must NOT suppress
        # the live promotion_pending.  When BOTH timestamps are present and
        # the verdict predates the pending attempt we can confirm the verdict
        # is stale and fail closed.  When either timestamp is absent (legacy /
        # timeless promotion_pending shapes) we cannot distinguish a stale
        # re-run from a record that was simply written before timestamp fields
        # existed — trust the finalized verdict and substitute.
        verdict_ts: datetime | None = getattr(latest_verdict, "created_at", None)
        if not isinstance(verdict_ts, datetime):
            verdict_ts = None
        # Prefer promotion_pending.enabled_at; fall back to qual.started_at.
        pending_ts: datetime | None = _parse_iso_naive(
            promotion_pending.get("enabled_at")
        ) or _parse_iso_naive(q.get("started_at"))
        # Strip tzinfo from verdict_ts so both sides are naive UTC.
        if verdict_ts is not None:
            verdict_ts = verdict_ts.replace(tzinfo=None)
        if pending_ts is not None and verdict_ts is not None and verdict_ts <= pending_ts:
            # Confirmed stale: verdict predates this promotion attempt.
            return q, None
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


async def resolve_promotion_blocking_result(
    session: AsyncSession,
    version: ReleaseVersion,
) -> "dict[str, Any] | None":
    """Return a promotion-blocking status dict, or None when not mid-promotion.

    Shared predicate for all four promotion-blocking operator levers
    (run_qualification, force_promote, abandon_current_version, set_version_phase).
    Mirrors ``_promotion_in_flight_result`` in ``release_qualifier`` but applies
    finalized-verdict substitution via :func:`apply_verdict_substitution` so that
    a stale-pending record with a finalized ``QualificationVerdict`` row is not
    misclassified as ``promotion_in_flight`` / ``promotion_check_wait``.

    Returns a dict with ``status``, ``promotion_pr_number``, ``promotion_pr_url``,
    and any freshness keys when the version is genuinely mid-promotion, or ``None``
    when the effective state is not in ``PROMOTION_BLOCKING_STATES``.
    """
    from app.release_version import (
        summarize_qualification_state,
        PROMOTION_BLOCKING_STATES,
        _PROMOTION_FRESHNESS_KEYS,
    )

    latest_verdict = await resolve_newest_qualification_verdict(session, version)
    qual = (
        version.last_qualification
        if isinstance(version.last_qualification, dict)
        else {}
    )
    effective_qual, _ = apply_verdict_substitution(qual, latest_verdict)
    qual_state = summarize_qualification_state(effective_qual)
    if qual_state and qual_state.get("state") in PROMOTION_BLOCKING_STATES:
        result: dict[str, Any] = {
            # "status" mirrors _promotion_in_flight_result's return shape
            # (used by run_qualification / force_promote / set_version_phase).
            # "state" is included so VersionCutBlockedByPromotion.promotion_freshness
            # consumers (the version-cut router) that read .get("state") still work.
            "status": qual_state["state"],
            "state": qual_state["state"],
            "promotion_pr_number": qual_state.get("promotion_pr_number"),
            "promotion_pr_url": qual_state.get("promotion_pr_url"),
        }
        for key in _PROMOTION_FRESHNESS_KEYS:
            if key in qual_state:
                result[key] = qual_state[key]
        return result
    return None


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
