"""Shared effective-qualification-state resolution.

Several read surfaces derive a single ``qualification_state`` from a release
version's raw ``last_qualification`` record plus up to four optional
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
4. **ledger sub_score overlay** — overlay the QualificationDimensionResult
   ledger's per-``(candidate_sha, dimension)`` scores onto the effective
   state's ``sub_scores`` (via :func:`patch_qualification_state_from_dimension_states`)
   so the top-level ``qualification_state`` cannot contradict the per-dimension
   ledger the same surfaces show after a blob-overwriting re-qualification.

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
it GUARANTEES all four enrichments (serving aggregate, audit child, finalized
verdict, ledger sub_score overlay) are applied in the same fixed order so the
surfaces cannot silently diverge. Callers may thread in enrichments they already
resolved for other response fields to avoid a redundant query, but the helper
backfills any they did not so the guarantee holds regardless of caller.
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


def resolve_dimension_states(
    last_qualification: dict[str, Any] | None,
) -> list[dict[str, Any]]:
    """Derive per-dimension qualification state from a raw ``last_qualification`` dict.

    Pure (no DB access). Returns a list of dimension-state dicts, one per
    qualification dimension, each with keys: ``key``, ``label``, ``status``
    (one of ``"passed"``, ``"failed"``, ``"unavailable"``, ``"not_applicable"``,
    ``"pending"``), ``score`` (float or None), ``reason`` (str or None).

    Degradation rules (in priority order):
      1. ``last_qualification`` is None/empty → all dimensions ``"pending"``
      2. ``status == "infra_failure"`` → all dimensions ``"unavailable"`` with
         the stored ``error`` as reason
      3. ``status in ("pending", "qualifying")`` with no ``promotion_pending``
         sub-key → all dimensions ``"pending"``
      4. Otherwise derive per-dimension from ``blocker``, ``sub_scores``,
         and evidence fields.
    """
    q: dict[str, Any] = last_qualification if isinstance(last_qualification, dict) else {}

    _DIMENSIONS: list[tuple[str, str]] = [
        ("audit", "Audit"),
        ("test_erosion", "Test Erosion"),
        ("smoketest", "Smoketest"),
        ("ci", "CI"),
        ("migration_identity", "Migration Identity"),
        ("migration_apply", "Migration Apply"),
        ("merge_grade", "Merge Grade"),
        ("challenge", "Challenge"),
        ("acceptance_window", "Acceptance Window"),
        ("criterion_coverage", "Criterion Coverage"),
        ("debt_pressure", "Debt Pressure"),
        ("goal_alignment", "Goal Alignment"),
        ("capability_proof", "Capability Proof"),
    ]

    _BLOCKER_TO_DIM: dict[str, str] = {
        "ci_not_green": "ci",
        "ci_unresolved": "ci",
        "migration_identity_collision": "migration_identity",
        "migration_apply_drift": "migration_apply",
        "merge_grade_misses": "merge_grade",
        "criterion_coverage_incomplete": "criterion_coverage",
        "smoketest_startup_failed": "smoketest",
        "audit_missing": "audit",
        "test_file_deleted": "test_erosion",
        "capability_proof_gate_blocked": "capability_proof",
    }

    def _all(status: str, reason: str | None = None) -> list[dict[str, Any]]:
        return [
            {"key": k, "label": lb, "status": status, "score": None, "reason": reason}
            for k, lb in _DIMENSIONS
        ]

    if not q:
        return _all("pending")

    status = q.get("status")

    if status == "infra_failure":
        error = q.get("error") or "infra_failure"
        return _all("unavailable", error)

    if status in ("pending", "qualifying") and not q.get("promotion_pending"):
        return _all("pending")

    blocker: str | None = q.get("blocker")
    sub_scores: dict[str, Any] = q.get("sub_scores") or {}
    ci_resolution: dict[str, Any] = q.get("ci_resolution") or {}
    smoketest_enabled: bool = bool(q.get("smoketest_enabled", True))
    migration_safety = q.get("migration_safety")
    migration_apply_safety = q.get("migration_apply_safety")
    merge_grade_status = q.get("merge_grade_status")
    merge_grade_coverage = q.get("merge_grade_coverage")
    criterion_coverage: dict[str, Any] = q.get("criterion_coverage") or {}
    goal_alignment = q.get("goal_alignment")
    # Derive the capability-proof gate mode from the real blob shape the release
    # qualifier writes: capability_proof_detail={"mode": off|observe|enforce}.
    # (There is no top-level capability_proof_gate_mode key — reading one always
    # defaulted to "off", leaving this dimension dead even in enforce mode.)
    capability_proof_detail: dict[str, Any] = q.get("capability_proof_detail") or {}
    proof_gate_mode = str(capability_proof_detail.get("mode", "off") or "off")

    # The blocker dimension (if any) is always "failed".
    blocker_dim: str | None = _BLOCKER_TO_DIM.get(blocker) if blocker else None
    # When audit_missing fires, test_erosion is also effectively unavailable
    # (both scores come from the same audit report).
    audit_missing = blocker == "audit_missing"

    result: list[dict[str, Any]] = []
    for key, label in _DIMENSIONS:
        dim_status: str
        score: float | None = None
        reason: str | None = None

        if key == blocker_dim:
            dim_status = "failed"
            reason = blocker
        elif key == "test_erosion" and audit_missing:
            dim_status = "unavailable"
            reason = "audit_missing"
        elif key == "smoketest" and not smoketest_enabled:
            dim_status = "not_applicable"
        elif key in ("audit", "test_erosion"):
            raw = sub_scores.get(key)
            if raw is not None and float(raw) > 0.0:
                dim_status = "passed"
                score = float(raw)
            elif raw is not None:
                # score is 0.0 — either genuinely zero or a placeholder when
                # blocked; surface as unavailable when no blocker fired for it.
                dim_status = "unavailable"
            else:
                dim_status = "unavailable"
        elif key == "smoketest":
            raw = sub_scores.get("smoketest")
            if raw is not None and float(raw) > 0.0:
                dim_status = "passed"
                score = float(raw)
            elif raw is not None:
                dim_status = "unavailable"
            else:
                dim_status = "unavailable"
        elif key == "ci":
            ci_state = ci_resolution.get("state")
            if ci_state and ci_state not in ("", None):
                dim_status = "passed"
                reason = ci_state
            else:
                dim_status = "unavailable"
        elif key == "migration_identity":
            if migration_safety == "ok":
                dim_status = "passed"
            elif migration_safety == "block":
                dim_status = "failed"
                reason = "migration_identity_collision"
            elif migration_safety == "defer":
                dim_status = "unavailable"
                reason = "migration_identity_defer"
            else:
                dim_status = "unavailable"
        elif key == "migration_apply":
            if migration_apply_safety == "ok":
                dim_status = "passed"
            elif migration_apply_safety == "block":
                dim_status = "failed"
                reason = "migration_apply_drift"
            elif migration_apply_safety == "defer":
                dim_status = "unavailable"
                reason = "migration_apply_defer"
            elif migration_apply_safety is None:
                dim_status = "not_applicable"
            else:
                dim_status = "unavailable"
        elif key == "merge_grade":
            if merge_grade_status == "pass":
                dim_status = "passed"
                if merge_grade_coverage is not None:
                    score = float(merge_grade_coverage)
            elif merge_grade_status in ("not_applicable", "superseded_exempt",
                                         "already_on_main_exempt"):
                dim_status = "not_applicable"
            elif merge_grade_status == "fail":
                dim_status = "failed"
                reason = "merge_grade_misses"
            else:
                dim_status = "unavailable"
        elif key == "criterion_coverage":
            # Redesigned goal-verification gate: the acceptance criteria ARE the
            # merge-grade's per-category verdicts, and the gate blocks on
            # ``criterion_coverage_incomplete`` iff ANY active category is not
            # ``met`` (``partial`` gates like ``not_met``) OR a residual gap
            # remains.  The legacy evidence-coverage COUNTS (``total``/``covered``)
            # no longer drive that gate — attested/backfilled criteria are NOT
            # counted as ``covered`` — so deriving this dimension from counts made
            # an all-``met`` category set surface as ``failed``, directly
            # contradicting a promotion the gate allowed.  Derive from the SAME
            # ``category_verdicts`` the gate consumes when they are present; only
            # fall back to the legacy counts when no per-category verdicts exist
            # (no rubric / ledger absent — the pre-redesign path).
            category_verdicts = q.get("category_verdicts")
            if isinstance(category_verdicts, list) and category_verdicts:
                residual = q.get("residual_uncaptured_gap")
                any_unmet = any(
                    (v or {}).get("status") != "met" for v in category_verdicts
                )
                if any_unmet or residual:
                    dim_status = "failed"
                    reason = "criterion_coverage_incomplete"
                else:
                    dim_status = "passed"
                    score = 1.0
            else:
                total = int(criterion_coverage.get("total") or 0)
                covered = int(criterion_coverage.get("covered") or 0)
                if total == 0:
                    dim_status = "not_applicable"
                elif covered >= total:
                    dim_status = "passed"
                    score = 1.0
                else:
                    dim_status = "failed"
                    reason = "criterion_coverage_incomplete"
        elif key == "debt_pressure":
            raw = sub_scores.get("debt_pressure")
            if raw is not None:
                dim_status = "passed"
                score = float(raw)
            else:
                dim_status = "unavailable"
        elif key == "goal_alignment":
            if goal_alignment is None:
                dim_status = "unavailable"
            elif isinstance(goal_alignment, dict):
                ga_status = goal_alignment.get("status") or goal_alignment.get("verdict")
                if ga_status in ("pass", "met", "aligned"):
                    dim_status = "passed"
                elif ga_status in ("fail", "unmet", "misaligned"):
                    dim_status = "failed"
                    reason = ga_status
                else:
                    dim_status = "passed"
            else:
                dim_status = "passed"
        elif key == "challenge":
            challenge_verdicts = q.get("challenge_verdicts")
            if challenge_verdicts:
                dim_status = "passed"
            else:
                dim_status = "unavailable"
        elif key == "acceptance_window":
            acc_fraction = sub_scores.get("acceptance_window_fraction")
            if acc_fraction is not None:
                dim_status = "passed"
                score = float(acc_fraction)
            else:
                dim_status = "unavailable"
        elif key == "capability_proof":
            # Mirror the ledger's capability-proof semantics (release_qualifier
            # ~3593): OFF → unavailable (gate inactive), OBSERVE/ENFORCE → passed.
            # A real enforce-mode block surfaces as "failed" via blocker_dim above
            # (capability_proof_gate_blocked → capability_proof).
            if proof_gate_mode in ("observe", "enforce"):
                dim_status = "passed"
            else:
                dim_status = "unavailable"
                reason = proof_gate_mode
        else:
            dim_status = "unavailable"

        result.append(
            {"key": key, "label": label, "status": dim_status, "score": score, "reason": reason}
        )

    return result


# Dimensions the QualificationDimensionResult ledger writes (11 total, 11 of which
# overlap with the 13 UI dimensions). The 2 UI-only dimensions NOT in the ledger
# (criterion_coverage, goal_alignment) must still be derived from the blob.
_LEDGER_DIMENSION_KEYS: frozenset[str] = frozenset({
    "audit", "test_erosion", "smoketest", "ci",
    "migration_identity", "migration_apply", "merge_grade", "debt_pressure",
    "capability_proof", "challenge", "acceptance_window",
})


def _ledger_status_to_ui(status: str) -> str:
    """Map the ledger's status vocabulary to the UI dimension-state vocabulary.

    The ledger writes ``passed``/``blocked``/``failed``/``unavailable``.
    The UI expects ``passed``/``failed``/``unavailable``/``not_applicable``/``pending``.
    ``blocked`` is the only vocabulary difference — mapped to ``failed`` for the UI.
    """
    return "failed" if status == "blocked" else status


async def resolve_dimension_states_from_ledger(
    session: AsyncSession,
    version: ReleaseVersion,
) -> list[dict[str, Any]]:
    """Resolve per-dimension qualification state from the QualificationDimensionResult ledger.

    When ledger rows exist for the version's current ``candidate_sha``, derive
    dimension states from the ledger for the 11 overlapping dimensions (audit,
    test_erosion, smoketest, ci, migration_identity, migration_apply, merge_grade,
    debt_pressure, capability_proof, challenge, acceptance_window) plus the
    synchronous blob derivation for the 2 dimensions the ledger does not write
    (criterion_coverage, goal_alignment). When no ledger rows exist for this
    candidate SHA, fall back entirely to the original
    :func:`resolve_dimension_states` (pure, from ``last_qualification``).

    Requires a DB session for the ledger query. Pure (no side effects).
    """
    qual = version.last_qualification if isinstance(version.last_qualification, dict) else {}
    candidate_sha = qual.get("candidate_sha")

    if not candidate_sha:
        return resolve_dimension_states(version.last_qualification)

    from app.models import QualificationDimensionResult  # noqa: PLC0415

    rows = (
        (
            await session.execute(
                select(QualificationDimensionResult).where(
                    QualificationDimensionResult.release_version_id == version.id,
                    QualificationDimensionResult.candidate_sha == candidate_sha,
                )
            )
        )
        .scalars()
        .all()
    )

    if not rows:
        return resolve_dimension_states(version.last_qualification)

    # Start with the full blob-derived state (all 13 dimensions). Then overwrite
    # the 11 ledger-backed dimensions with ledger data. The 2 blob-only dimensions
    # (criterion_coverage, goal_alignment) are preserved from the blob.
    result = resolve_dimension_states(version.last_qualification)
    ledger_map = {row.dimension: row for row in rows}

    for entry in result:
        key = entry["key"]
        if key in _LEDGER_DIMENSION_KEYS:
            row = ledger_map.get(key)
            if row is not None:
                entry["status"] = _ledger_status_to_ui(row.status)
                entry["score"] = row.sub_score
                entry["reason"] = row.blocker

    return result


def patch_qualification_state_from_dimension_states(
    qual_state: "dict[str, Any] | None",
    dimension_states: "list[dict[str, Any]]",
) -> "dict[str, Any] | None":
    """Patch qual_state sub_scores from ledger-backed dimension_states scores.

    Pure (no DB access). Returns a shallow copy of qual_state with the sub_scores
    dict updated from the scores carried by dimension_states, so that the top-level
    qualification_state and the per-dimension breakdown tell one consistent story
    even after the last_qualification blob was overwritten by a re-qualification or
    infra-failure that the ledger survived.

    Only dimensions whose score is non-None are patched. The key mapping from
    dimension key to sub_scores key follows the same convention resolve_dimension_states
    uses when reading from the blob:
      - audit, test_erosion, smoketest, debt_pressure → same key in sub_scores
      - acceptance_window → sub_scores["acceptance_window_fraction"]

    Returns None when qual_state is None; returns qual_state unchanged when
    dimension_states carries no non-None scores for the mapped dimensions.
    """
    if qual_state is None:
        return None

    _DIM_TO_SUB_SCORE: dict[str, str] = {
        "audit": "audit",
        "test_erosion": "test_erosion",
        "smoketest": "smoketest",
        "debt_pressure": "debt_pressure",
        "acceptance_window": "acceptance_window_fraction",
    }

    patches: dict[str, Any] = {}
    for dim in dimension_states:
        score = dim.get("score")
        if score is None:
            continue
        dim_key = dim.get("key")
        if dim_key is None:
            continue
        sub_key = _DIM_TO_SUB_SCORE.get(dim_key)
        if sub_key is not None:
            patches[sub_key] = score

    if not patches:
        return qual_state

    result = {**qual_state}
    result["sub_scores"] = {**(result.get("sub_scores") or {}), **patches}
    return result


async def resolve_open_version_qualification_state(
    session: AsyncSession,
    version: ReleaseVersion,
    project: Project | None = None,
    *,
    serving: Tuple[dict[str, Any], list[dict[str, Any]]] | None = None,
    audit_child: Any = _UNSET,
    latest_verdict: Any = _UNSET,
    dimension_states: Any = _UNSET,
) -> Tuple[dict[str, Any] | None, dict[str, Any] | None]:
    """Resolve an OPEN version's ``qualification_state`` with ALL enrichments.

    The single async seam every open-version read surface (strategic payload,
    agent fetch verb, admin lifecycle endpoint, system-state snapshot) routes
    through, so they cannot silently apply different enrichment subsets and tell
    contradictory stories in the stale-pending / audit-crashed windows.

    Returns ``(qualification_state, disagreement)`` — the
    :func:`resolve_effective_qualification_state` output — with all FOUR
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
    * **ledger sub_score overlay** — after the effective state is computed, the
      ledger-backed per-dimension facts are overlaid onto its ``sub_scores`` via
      :func:`patch_qualification_state_from_dimension_states` so the top-level
      ``qualification_state`` cannot contradict the per-dimension ledger the same
      surfaces show (e.g. after a blob-overwriting infra-failure re-qualification
      drops a resolved ``audit`` score the ledger preserved). The overlay is
      idempotent and safe in the no-ledger fallback (blob-derived scores patched
      back onto the same blob is a no-op), so it is applied unconditionally.
      ``dimension_states`` — when left unset the ledger-backed states are resolved
      here via :func:`resolve_dimension_states_from_ledger` (best-effort: a
      failure logs and degrades to the un-patched effective state so a broken
      ledger query never blocks payload/response assembly). Passing a
      pre-resolved list (as the admin endpoint does for its own
      ``dimension_states`` response field) skips the second query.
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

    qual_state, disagreement = resolve_effective_qualification_state(
        version,
        project,
        latest_verdict=latest_verdict,
        audit_child=audit_child,
        serving=serving,
    )

    # Fourth enrichment: overlay the ledger-backed per-dimension sub_scores onto
    # the effective state so every open-version surface tells one story even when
    # the last_qualification blob was overwritten by a re-qualification / infra
    # failure the ledger survived. Idempotent + no-op in the no-ledger fallback.
    dim_states = dimension_states
    if dim_states is _UNSET:
        dim_states = None
        try:
            dim_states = await resolve_dimension_states_from_ledger(session, version)
        except Exception:
            logger.exception(
                "[QUAL_STATE] ledger dimension-state resolution failed for version "
                "%s; resolving qualification_state without ledger sub_score overlay",
                getattr(version, "id", None),
            )
            dim_states = None
    if dim_states is not None:
        qual_state = patch_qualification_state_from_dimension_states(
            qual_state, dim_states
        )

    return qual_state, disagreement
