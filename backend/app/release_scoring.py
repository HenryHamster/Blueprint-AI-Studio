"""Pure scoring layer for the release qualifier.

Extracted from ``release_qualifier.py`` to keep the qualifier's async
coordinator code (run_qualification, finalize, sweeps, promote) separate from
the deterministic scoring math.  Everything here is **pure**: no AsyncSession,
no I/O, no module state beyond constants — it maps artifact dicts and CI
results to numeric sub-scores and a composite promotion verdict.

The qualifier re-exports every symbol below from ``app.release_qualifier`` so
existing importers and test patch points (``app.release_qualifier.<name>``)
keep working unchanged.
"""

from __future__ import annotations

import hashlib
import logging
import re
from typing import Any

from app.ci_resolution import (
    _CI_RED,
)
from app.migration_reconciliation import effective_authorized_divergences

logger = logging.getLogger(__name__)


# Canonical fleet default for the promotion confidence gate.  The packaged
# promotion_policy.yaml value and runtime-config seed must agree with this
# value; the release-qualifier guard tests that contract.  Runtime and
# per-project policy overrides are intentionally resolved above this default.
PROMOTION_CONFIDENCE_THRESHOLD = 0.90


# Hardcoded fallback used when promotion_policy.yaml is missing, unreadable, or
# malformed.  MUST match the documented defaults in promotion_policy.yaml so a
# parse failure degrades to the intended rubric rather than a silent surprise.
_DEFAULT_POLICY: dict[str, Any] = {
    "weights": {"audit": 4 / 7, "test_erosion": 3 / 7},
    "threshold": PROMOTION_CONFIDENCE_THRESHOLD,
}

# total_open count at which _score_completion_debt_pressure reaches 0.0.
# A linear decay: 0 items → 1.0; _DEBT_PRESSURE_CEIL items → 0.0.
_DEBT_PRESSURE_CEIL = 20

# Hard cap on the completion_debt blend factor.  completion_debt is a SEPARATE
# blend weight, NOT part of the audit + test_erosion core-weight sum
# (see ``compute_composite``): the existing sub-scores are scaled by
# ``(1 - w_debt)`` and the debt term is added, so the total stays in [0, 1].
# Bounded at 0.5 so the debt signal can never dominate the quality sub-scores.
MAX_COMPLETION_DEBT_WEIGHT = 0.5

# Default completion_debt blend weight applied when the operator graduates
# ``completion_debt_weight_mode`` to "enforce" but left the weight unset/zero —
# so graduation actually changes the composite instead of being a no-op.
DEFAULT_ENFORCE_COMPLETION_DEBT_WEIGHT = 0.15

# Confidence penalty applied per the goal-verification redesign.  v1 grades every
# acceptance-criterion CATEGORY at the JUDGED tier — an uninvested LLM reading the
# merged diff — which is a lower-confidence signal than machine/test-cited
# evidence (the v2 "machine" tier).  When no hard gate fires (every active
# category is ``met``), confidence is docked in proportion to the fraction of MET
# categories still resting on the judged tier: ``JUDGED_TIER_PENALTY_WEIGHT *
# (judged_met / met)``, applied as ``confidence *= (1 - penalty)``.  Deliberately
# small so a healthy all-judged candidate (the whole of v1) stays promotable while
# still scoring visibly below a machine-verified one.
#
# This is the ONLY confidence penalty on the acceptance axis.  It replaced the
# retired criterion-"coverage" evidence-tier penalties (attestation / backfill),
# which keyed off evidence accumulated per criterion rather than a graded verdict.
JUDGED_TIER_PENALTY_WEIGHT = 0.05

# Recognised per-category verdict statuses.  ``partial`` gates identically to
# ``not_met`` (only ``met`` clears the gate); the distinction is informational.
_CATEGORY_STATUSES: frozenset[str] = frozenset({"met", "not_met", "partial"})

# Recognised citation kinds.  v1 emits only ``diff`` citations (the grader reads
# the merged diff); ``test`` is reserved for the v2 machine/test-citation tier.
_CITATION_KINDS: frozenset[str] = frozenset({"diff", "test"})
# Caps so a runaway artifact cannot bloat the persisted verdict / payload.
_MAX_CITATIONS = 8
_CITATION_REF_MAX = 200


def coerce_completion_debt_weight(weights_raw: Any) -> float:
    """Read the OPTIONAL ``completion_debt`` blend weight from a weights mapping.

    Returns ``0.0`` when the key is absent or unparseable — this is what keeps a
    legacy three-key ``{audit, test_erosion, smoketest}`` policy fully backward
    compatible (an effective completion_debt weight of 0.0 = the shipped observe
    behaviour).  A present numeric value is clamped to
    ``[0.0, MAX_COMPLETION_DEBT_WEIGHT]``.  Pure — no I/O, no runtime_config.
    """
    if not isinstance(weights_raw, dict):
        return 0.0
    raw = weights_raw.get("completion_debt")
    if raw is None or isinstance(raw, bool):
        return 0.0
    try:
        v = float(raw)
    except (TypeError, ValueError):
        return 0.0
    if v < 0.0:
        return 0.0
    if v > MAX_COMPLETION_DEBT_WEIGHT:
        return MAX_COMPLETION_DEBT_WEIGHT
    return v


def _clamp01(value: float) -> float:
    """Clamp a numeric sub-score into [0.0, 1.0]."""
    try:
        v = float(value)
    except (TypeError, ValueError):
        return 0.0
    if v < 0.0:
        return 0.0
    if v > 1.0:
        return 1.0
    return v


def promotion_threshold_for_policy(policy: dict[str, Any]) -> float:
    """Return the threshold the composite promotion gate must enforce.

    A resolved policy may carry an operator-configured threshold.  The shared
    canonical default is used only when a caller supplies an incomplete policy,
    keeping fallback behavior and the enforcement path on the same definition.
    """
    return float(policy.get("threshold", PROMOTION_CONFIDENCE_THRESHOLD))


# Default coverage threshold used ONLY when the merge-grade artifact omits an
# explicit ``covers_goal`` boolean but does report a numeric ``coverage_score``.
# The agent's explicit boolean is authoritative when present.
_MERGE_GRADE_COVERAGE_THRESHOLD = 0.5

# Single-word placeholder strings that convey no real version objective.
# A goal that normalises to one of these is treated as contentless — identical
# to an empty goal (no merge-grade spawned, not_applicable at scoring time).
# Example: version.goal accidentally set to "test" blocked v18 promotion.
_CONTENTLESS_GOAL_WORDS: frozenset[str] = frozenset(
    {"test", "n/a", "na", "tbd", "todo", "placeholder", "none", "null", "empty", "unknown"}
)


def is_contentless_goal(goal_text: str) -> bool:
    """Return True when *goal_text* is a placeholder string with no real objective.

    Only exact single-word matches are contentless so that a real goal like
    "test the authentication flow" is not inadvertently suppressed.
    """
    return goal_text.strip().lower() in _CONTENTLESS_GOAL_WORDS


def _normalize_citations(raw: Any) -> list[dict[str, str]]:
    """Normalise a per-category citation list to ``[{kind, ref}, ...]``.

    v1 accepts ``diff`` and ``test`` citation kinds (``test`` reserved for v2);
    an unknown kind is coerced to ``diff``.  Bare strings are read as ``diff``
    refs.  Entries without a usable ``ref`` are dropped, and the list is capped
    at ``_MAX_CITATIONS`` so an over-eager grader cannot bloat the ledger row.
    """
    if not isinstance(raw, list):
        return []
    out: list[dict[str, str]] = []
    for c in raw:
        if isinstance(c, dict):
            kind = str(c.get("kind") or "").strip().lower()
            if kind not in _CITATION_KINDS:
                kind = "diff"
            ref = str(
                c.get("ref") or c.get("path") or c.get("location") or ""
            ).strip()
        elif isinstance(c, str):
            kind = "diff"
            ref = c.strip()
        else:
            continue
        if ref:
            out.append({"kind": kind, "ref": ref[:_CITATION_REF_MAX]})
        if len(out) >= _MAX_CITATIONS:
            break
    return out


def _cited_test_is_machine_verified(
    citations: list[dict[str, str]],
    verified_merge_evidence: dict[str, Any] | None,
) -> str | None:
    """Return the validated test ref proving a category at the MACHINE tier, else None.

    The grader's ``{"kind": "test", "ref": ...}`` citation is a SOFT claim ("test
    X covers category C").  It is upgraded to the machine tier only when the cited
    test is machine-CONFIRMED against the candidate — the same fail-closed proof
    shape as the ``named_test`` acceptance gate, applied to the merge-grade's
    cross-task candidate window:

      1. the cited test file is among the candidate's VERIFIED-merged test files —
         a test that shipped in a merge gated by a REAL CI/test verdict (see
         ``landed_signal.is_verified_merge``), which is how "ran green on the
         candidate SHA" is machine-confirmed here; AND
      2. it semantically corresponds to a changed production seam that shipped in
         those verified merges (reuses ``acceptance_gate._test_semantically_grounded``
         so the test module maps to a real changed production module).

    ``verified_merge_evidence`` is ``{"test_files": [...], "seam_files": [...]}``
    gathered by the qualifier from the version window (both restricted to verified
    merges).  When it is absent/empty — no candidate CI evidence available at grade
    time — NO citation validates and the caller keeps ``tier="judged"``: machine
    tier is never granted on an unvalidated claim (fail toward the lower tier).

    ``ref`` is read like ``_evaluate_named_test`` reads a declared test: a pytest
    node id (``path::test_name``) or a bare file path, split on ``::`` to the path.
    """
    if not verified_merge_evidence:
        return None
    test_files = verified_merge_evidence.get("test_files") or []
    seam_files = verified_merge_evidence.get("seam_files") or []
    if not test_files:
        return None
    # Reuse the PURE named_test proof helpers (no I/O).  Lazy import keeps this
    # scoring module free of the heavier ``acceptance_gate`` import graph at load.
    from app.acceptance_gate import _paths_match, _test_semantically_grounded

    seam_list = [str(s) for s in seam_files]
    for c in citations:
        if c.get("kind") != "test":
            continue
        cited_path = str(c.get("ref") or "").partition("::")[0].strip()
        if not cited_path:
            continue
        if not any(_paths_match(str(vf), cited_path) for vf in test_files):
            # The cited test is NOT among the candidate's verified-merged tests —
            # it may never have run green under real CI on this candidate.
            continue
        if _test_semantically_grounded(cited_path, seam_list):
            return cited_path
    return None


def _normalize_category_verdicts(
    artifact: dict[str, Any] | None,
    active_categories: list[dict[str, Any]],
    verified_merge_evidence: dict[str, Any] | None = None,
) -> tuple[list[dict[str, Any]], str | None]:
    """Grade the artifact's per-category verdicts against the ACTIVE rubric.

    The rubric is the set of ACTIVE ``VersionAcceptanceCriterion`` rows — each is
    a CATEGORY the merged diff must satisfy.  v1 = JUDGED tier only: the grader
    reads the merged diff and, per category, decides ``met`` / ``partial`` /
    ``not_met``, CITES the diff location(s), and writes the gap.  Enforcement
    (uncited "it's fine" → ``not_met``):

      * A category the artifact never graded → ``not_met`` (it was not assessed).
      * A category graded ``met`` with NO citations → downgraded to ``not_met``
        (an uncited pass is not evidence).
      * ``partial`` is preserved as an informational distinction but gates
        identically to ``not_met`` downstream.
      * An unknown/absent status → ``not_met``.

    Tier (v2 machine/test-citation upgrade): a ``met`` category defaults to
    ``tier="judged"`` (an LLM read the diff).  It is upgraded to ``tier="machine"``
    ONLY when the grader attached a ``{"kind": "test", "ref": ...}`` citation whose
    test is machine-validated against ``verified_merge_evidence`` (the candidate's
    verified-merged test + seam files) — see :func:`_cited_test_is_machine_verified`.
    A test citation that cannot be validated (or no evidence supplied) keeps the
    category at ``judged`` — machine tier is never granted on an unvalidated claim.
    ``not_met`` / ``partial`` verdicts are always ``judged``.

    Returns ``(category_verdicts, residual_uncaptured_gap)`` where
    ``residual_uncaptured_gap`` is the grader's answer to "is there a goal aspect
    that NONE of the categories capture?" (a non-empty string hard-blocks).
    """
    emitted: dict[str, dict[str, Any]] = {}
    raw_list = artifact.get("category_verdicts") if isinstance(artifact, dict) else None
    if isinstance(raw_list, list):
        for item in raw_list:
            if isinstance(item, dict):
                key = str(item.get("category_key") or item.get("key") or "").strip()
                if key:
                    emitted[key] = item

    verdicts: list[dict[str, Any]] = []
    for cat in active_categories:
        key = str(cat.get("category_key") or cat.get("criterion_key") or "").strip()
        if not key:
            continue
        raw = emitted.get(key)
        if not isinstance(raw, dict):
            verdicts.append(
                {
                    "category_key": key,
                    "status": "not_met",
                    "tier": "judged",
                    "gap": "Not graded by the blind merge-grade against the merged diff.",
                    "citations": [],
                }
            )
            continue
        status = str(raw.get("status") or "").strip().lower()
        if status not in _CATEGORY_STATUSES:
            status = "not_met"
        citations = _normalize_citations(raw.get("citations"))
        gap_raw = raw.get("gap")
        gap = (
            str(gap_raw).strip()[:_FINDING_SUMMARY_MAX]
            if gap_raw not in (None, "")
            else None
        )
        if status == "met" and not citations:
            # v1 rule: an uncited "it's fine" is not evidence — force not_met.
            status = "not_met"
            gap = gap or (
                "Marked met but cited no diff location; an uncited pass is not evidence."
            )
        if status in ("not_met", "partial") and not gap:
            gap = "No gap description supplied by the grader."
        # Tier: a MET category whose test citation is machine-validated against the
        # candidate's verified merges earns the machine tier; everything else stays
        # judged (fail toward the lower tier).
        tier = "judged"
        if status == "met" and _cited_test_is_machine_verified(
            citations, verified_merge_evidence
        ):
            tier = "machine"
        verdicts.append(
            {
                "category_key": key,
                "status": status,
                "tier": tier,
                # ``met`` may legitimately carry no gap; not_met/partial always do.
                "gap": gap if status != "met" else (gap or None),
                "citations": citations,
            }
        )

    residual_raw: Any = None
    if isinstance(artifact, dict):
        residual_raw = artifact.get("residual_uncaptured_gap")
        if residual_raw in (None, ""):
            residual_raw = artifact.get("residual")
    residual = (
        str(residual_raw).strip()[:_FINDING_SUMMARY_MAX]
        if isinstance(residual_raw, str) and residual_raw.strip()
        else None
    )
    return verdicts, residual


def evaluate_merge_grade(
    goal: str | None,
    artifact: dict[str, Any] | None,
    active_categories: list[dict[str, Any]] | None = None,
    verified_merge_evidence: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Normalise a blind merge-grade artifact into a goal-coverage verdict.

    Pure / deterministic — the qualifier reads the ``RELEASE_MERGE_GRADE``
    artifact and passes (goal, artifact) here.  Semantics:

      * ``goal`` empty / unset, or a contentless placeholder (e.g. "test",
        "n/a") → ``status="not_applicable"`` (nothing to grade against).  The
        gate is a no-op PASS: ``blocked`` False.
      * ``artifact`` not a usable dict, or carries neither an explicit
        ``covers_goal`` bool nor a numeric ``coverage_score`` → ``status=
        "unparseable"``.  The caller routes this to a HOLD (infra_failure) — a
        required grade that produced no usable verdict must not silently pass.
      * an explicit ``covers_goal`` bool is authoritative; otherwise a numeric
        ``coverage_score`` is thresholded at ``_MERGE_GRADE_COVERAGE_THRESHOLD``.
        ``covers_goal`` True → ``status="pass"``, ``blocked`` False; False →
        ``status="fail"``, ``blocked`` True (the hard promotion blocker).

    The ``status``/``covers_goal``/``coverage``/``biggest_gap`` keys are the
    HOLISTIC back-compat signal (still read by ``compute_goal_coverage_drift``,
    the ``merge_grade_*`` verdict columns, and the armed ``merge_grade_misses``
    gate).  When ``active_categories`` is supplied (the ACTIVE acceptance-criteria
    rubric), the goal-verification redesign ADDS a per-category grade:
    ``category_verdicts`` (a list of ``{category_key, status, tier, gap,
    citations}`` — see :func:`_normalize_category_verdicts`) and
    ``residual_uncaptured_gap`` (the grader's "gap no category captures" answer).
    These drive the NEW hard gate in :func:`compute_composite`.  When
    ``active_categories`` is absent/empty (no rubric) both are ``None`` and only
    the holistic signal applies — preserving the pre-redesign no-criteria path.

    ``verified_merge_evidence`` (v2 machine tier) — an optional
    ``{"test_files": [...], "seam_files": [...]}`` snapshot of the candidate's
    VERIFIED-merged test + production-seam files, gathered by the qualifier.  It
    upgrades a ``met`` category's ``tier`` from ``judged`` to ``machine`` when the
    grader cited a test that validates against it (see
    :func:`_cited_test_is_machine_verified`); ``None`` leaves every met category at
    the judged tier (v1 behaviour).

    Empty/contentless goal + an ACTIVE rubric: the acceptance-criteria contract is
    itself gradeable independent of the goal string (the release audit ships
    ``payload.categories`` even when ``version_goal`` is absent/contentless), so
    the per-category verdicts are still graded and gated.  The holistic
    ``covers_goal``/``coverage``/``biggest_gap`` signal stays ``None`` (there is
    no goal string to cover) and ``status`` stays ``"not_applicable"`` so the
    holistic ``merge_grade_misses`` gate stays inert.  A grader that omits the
    ``category_verdicts`` key (or returns a non-dict artifact) in this state is
    reported ``category_grade_unsupported`` — same HOLD semantics as the goal-ful
    path, never a fabricated ``not_met``.

    When ``active_categories`` is supplied but the artifact omits the
    ``category_verdicts`` key ENTIRELY, the grader cannot produce per-category
    output (a stale agent image predating the protocol) — that is NOT the same as
    a grader that assessed the rubric and found it unmet.  Reported as
    ``status="category_grade_unsupported"`` so the caller can HOLD with an
    actionable reason instead of fabricating a ``not_met`` verdict per criterion.

    Returns ``{status, covers_goal, coverage, biggest_gap, blocked, detail,
    category_verdicts, residual_uncaptured_gap}``. ``status`` is one of ``pass`` |
    ``fail`` | ``not_applicable`` | ``unparseable`` | ``category_grade_unsupported``.
    """
    goal_text = (goal or "").strip()
    if not goal_text or is_contentless_goal(goal_text):
        # A contentless/unset goal normally means there is nothing to grade
        # against (``not_applicable``).  But an ACTIVE acceptance-criteria rubric
        # is a gradeable contract in its own right: the release audit ships
        # ``categories`` even when ``version_goal`` is absent/contentless, and the
        # per-category verdicts must still be graded and gated.  Without this, a
        # version with a rubric but no goal string produces zero verdicts, every
        # active criterion is never-graded, and the caller's fail-closed fallback
        # blocks it on ``criterion_coverage_incomplete`` forever — even after the
        # grader emits a per-category grade.  So when ``active_categories`` is
        # supplied, grade the per-category contract; the holistic ``covers_goal``
        # signal stays ``None`` (there is no goal string to cover) and ``status``
        # stays ``not_applicable`` so the holistic ``merge_grade_misses`` gate
        # stays inert.
        if active_categories:
            if not isinstance(artifact, dict) or "category_verdicts" not in artifact:
                # The grader cannot produce per-category output (a non-dict
                # artifact, or a dict with no ``category_verdicts`` key at all —
                # a stale agent image predating the per-category protocol).  Same
                # HOLD semantics as the goal-ful path below: report
                # ``category_grade_unsupported`` rather than fabricating a
                # ``not_met`` verdict for every active criterion.
                return {
                    "status": "category_grade_unsupported",
                    "covers_goal": None,
                    "coverage": None,
                    "biggest_gap": None,
                    "blocked": True,
                    "detail": artifact if isinstance(artifact, dict) else None,
                    "category_verdicts": None,
                    "residual_uncaptured_gap": None,
                }
            category_verdicts, residual_uncaptured_gap = _normalize_category_verdicts(
                artifact, active_categories, verified_merge_evidence
            )
            return {
                "status": "not_applicable",
                "covers_goal": None,
                "coverage": None,
                "biggest_gap": None,
                "blocked": False,
                "detail": artifact,
                "category_verdicts": category_verdicts,
                "residual_uncaptured_gap": residual_uncaptured_gap,
            }
        return {
            "status": "not_applicable",
            "covers_goal": None,
            "coverage": None,
            "biggest_gap": None,
            "blocked": False,
            "detail": None,
            "category_verdicts": None,
            "residual_uncaptured_gap": None,
        }
    if not isinstance(artifact, dict):
        return {
            "status": "unparseable",
            "covers_goal": None,
            "coverage": None,
            "biggest_gap": None,
            "blocked": True,
            "detail": None,
            "category_verdicts": None,
            "residual_uncaptured_gap": None,
        }

    raw_covers = artifact.get("covers_goal")
    raw_score = artifact.get("coverage_score")
    coverage: float | None = None
    if isinstance(raw_score, (int, float)) and not isinstance(raw_score, bool):
        coverage = _clamp01(raw_score)

    if isinstance(raw_covers, bool):
        covers_goal = raw_covers
    elif coverage is not None:
        covers_goal = coverage >= _MERGE_GRADE_COVERAGE_THRESHOLD
    else:
        # Neither an explicit boolean nor a numeric score → no usable verdict.
        return {
            "status": "unparseable",
            "covers_goal": None,
            "coverage": coverage,
            "biggest_gap": None,
            "blocked": True,
            "detail": artifact,
            "category_verdicts": None,
            "residual_uncaptured_gap": None,
        }

    biggest_gap_raw = artifact.get("biggest_gap")
    biggest_gap = (
        str(biggest_gap_raw).strip()[:_FINDING_SUMMARY_MAX]
        if biggest_gap_raw not in (None, "")
        else None
    )
    # Per-category grade (goal-verification redesign).  Only computed when an
    # ACTIVE rubric is supplied; otherwise the holistic signal above stands alone.
    category_verdicts: list[dict[str, Any]] | None = None
    residual_uncaptured_gap: str | None = None
    if active_categories:
        if "category_verdicts" not in artifact:
            # The grader returned a usable HOLISTIC verdict but omitted the
            # per-category section ENTIRELY — not one key, not an empty list.  A
            # grader that ACTUALLY assessed the rubric always emits the key (the
            # prompt marks it REQUIRED), so its total absence means the grader
            # CANNOT produce per-category output: it predates the per-category
            # protocol (a stale agent image), not that it looked and found the
            # criteria unmet.
            #
            # Those two are indistinguishable downstream and must NOT be conflated.
            # `_normalize_category_verdicts` maps "never graded" → `not_met`, so
            # falling through here would fabricate a `not_met` verdict for EVERY
            # active criterion, hard-block on `criterion_coverage_incomplete`, and —
            # because the ledger is sha-KEYED and verdicts never regress — freeze
            # that fabricated failure against this candidate forever.  It would also
            # report a reason ("graded, not met") that is simply false.
            #
            # Report the real state instead.  The caller routes this to a HOLD, so
            # it still never promotes over an ungraded acceptance contract (no
            # fail-open) — it just says WHY, and stops lying about it.
            return {
                "status": "category_grade_unsupported",
                "covers_goal": covers_goal,
                "coverage": coverage,
                "biggest_gap": biggest_gap,
                "blocked": True,
                "detail": artifact,
                "category_verdicts": None,
                "residual_uncaptured_gap": None,
            }
        category_verdicts, residual_uncaptured_gap = _normalize_category_verdicts(
            artifact, active_categories, verified_merge_evidence
        )
    return {
        "status": "pass" if covers_goal else "fail",
        "covers_goal": covers_goal,
        "coverage": coverage,
        "biggest_gap": biggest_gap,
        "blocked": not covers_goal,
        "detail": artifact,
        "category_verdicts": category_verdicts,
        "residual_uncaptured_gap": residual_uncaptured_gap,
    }


# Number of tasks merged after the last merge-grade before the grade is
# considered stale (goal-coverage read may no longer reflect current merged work).
_DRIFT_STALE_MERGE_THRESHOLD = 5


def compute_goal_coverage_drift(
    goal: str | None,
    *,
    merge_grade_status: str | None,
    merge_grade_coverage: float | None = None,
    merge_grade_biggest_gap: str | None = None,
    goal_alignment: Any | None = None,
    merges_since_grade: int = 0,
) -> dict[str, Any]:
    """Derive a cheap goal-coverage divergence signal from already-persisted fields.

    Pure / side-effect-free — no I/O, no LLM call. Inputs come from the latest
    ``QualificationVerdict`` columns and a cheap merged-task count delta.

    Return dict keys:
      ``signal``  — ``"on_track"`` | ``"drifting"`` | ``"no_grade_yet"`` |
                    ``"not_applicable"`` | ``"superseded_exempt"``
      ``coverage``       — persisted coverage score (float | None)
      ``biggest_gap``    — persisted biggest-gap string (str | None)
      ``merges_since_grade`` — how many tasks merged since the grade was taken
      ``stale``          — True when merges_since_grade >= threshold
      ``detail``         — short human-readable explanation

    Divergence fires when EITHER:
      * the last grade already showed a goal miss (``merge_grade_status == "fail"``)
      * material new work accumulated since the last grade (stale + was on_track)
    """
    goal_text = (goal or "").strip()
    if not goal_text or is_contentless_goal(goal_text):
        return {
            "signal": "not_applicable",
            "coverage": None,
            "biggest_gap": None,
            "merges_since_grade": merges_since_grade,
            "stale": False,
            "detail": "No version goal set — coverage drift not applicable.",
        }

    if merge_grade_status is None:
        stale_cold_start = merges_since_grade >= _DRIFT_STALE_MERGE_THRESHOLD
        if stale_cold_start:
            return {
                "signal": "drifting",
                "coverage": None,
                "biggest_gap": merge_grade_biggest_gap,
                "merges_since_grade": merges_since_grade,
                "stale": True,
                "detail": (
                    f"No merge-grade recorded yet, but {merges_since_grade} task(s) "
                    "have merged since the version opened — goal coverage is unknown "
                    "with material accumulated work (cold-start drift)."
                ),
            }
        return {
            "signal": "no_grade_yet",
            "coverage": None,
            "biggest_gap": merge_grade_biggest_gap,
            "merges_since_grade": merges_since_grade,
            "stale": False,
            "detail": "No merge-grade has been taken for this version yet.",
        }

    stale = merges_since_grade >= _DRIFT_STALE_MERGE_THRESHOLD
    coverage = merge_grade_coverage
    biggest_gap = merge_grade_biggest_gap

    # Prefer goal_alignment.biggest_gap when it carries one and merge_grade doesn't
    if biggest_gap is None and isinstance(goal_alignment, dict):
        ga_gap = goal_alignment.get("biggest_gap")
        if ga_gap:
            biggest_gap = str(ga_gap).strip()[:_FINDING_SUMMARY_MAX]

    if merge_grade_status == "not_applicable":
        return {
            "signal": "not_applicable",
            "coverage": coverage,
            "biggest_gap": biggest_gap,
            "merges_since_grade": merges_since_grade,
            "stale": False,
            "detail": "Merge-grade was not applicable at grade time (goal was empty or contentless).",
        }

    if merge_grade_status == "superseded_exempt":
        return {
            "signal": "superseded_exempt",
            "coverage": coverage,
            "biggest_gap": biggest_gap,
            "merges_since_grade": merges_since_grade,
            "stale": False,
            "detail": "Merge-grade was exempted as superseded; no coverage verdict available.",
        }

    if merge_grade_status == "already_on_main_exempt":
        return {
            "signal": "superseded_exempt",
            "coverage": coverage,
            "biggest_gap": biggest_gap,
            "merges_since_grade": merges_since_grade,
            "stale": False,
            "detail": (
                "Merge-grade was exempted because the candidate is already on the "
                "promotion target; no coverage verdict available."
            ),
        }

    if merge_grade_status not in ("pass", "fail"):
        return {
            "signal": "no_grade_yet",
            "coverage": None,
            "biggest_gap": biggest_gap,
            "merges_since_grade": merges_since_grade,
            "stale": False,
            "detail": f"Merge-grade status '{merge_grade_status}' is not a positive verdict — no coverage signal.",
        }

    if merge_grade_status == "fail":
        return {
            "signal": "drifting",
            "coverage": coverage,
            "biggest_gap": biggest_gap,
            "merges_since_grade": merges_since_grade,
            "stale": stale,
            "detail": (
                "Last merge-grade flagged a goal miss"
                + (
                    f"; {merges_since_grade} additional tasks merged since grade."
                    if merges_since_grade
                    else "."
                )
            ),
        }

    # merge_grade_status == "pass" only reaches here
    if stale:
        return {
            "signal": "drifting",
            "coverage": coverage,
            "biggest_gap": biggest_gap,
            "merges_since_grade": merges_since_grade,
            "stale": True,
            "detail": (
                f"{merges_since_grade} tasks merged since last merge-grade — "
                "coverage read may no longer reflect current merged work."
            ),
        }

    return {
        "signal": "on_track",
        "coverage": coverage,
        "biggest_gap": biggest_gap,
        "merges_since_grade": merges_since_grade,
        "stale": False,
        "detail": "Last merge-grade passed; no material new work since.",
    }


def _score_completion_debt_pressure(
    debt_snapshot: dict[str, Any] | None,
) -> float:
    """Convert an open-debt snapshot to a [0, 1] pressure score.

    Returns 1.0 when there is no open debt (optimal state).  Decays linearly
    toward 0.0 as the actionable open count grows, reaching 0.0 at
    ``_DEBT_PRESSURE_CEIL`` open items.  A scoring-only
    ``project_actionable_open`` count is preferred when present; older callers
    that only provide ``total_open`` keep the legacy behavior.  A missing or
    empty snapshot is treated as zero debt (1.0).
    """
    if not debt_snapshot:
        return 1.0
    open_count = int(
        debt_snapshot.get("project_actionable_open")
        if "project_actionable_open" in debt_snapshot
        else debt_snapshot.get("total_open") or 0
    )
    if open_count <= 0:
        return 1.0
    return max(0.0, 1.0 - open_count / _DEBT_PRESSURE_CEIL)


# --- Deployed-side env-preflight judge (Slice 3, design 245212a4) ------------
# Cloud Run injects this into every revision; its presence is the canonical
# "am I serving in production on Cloud Run" signal.  Mirrored from
# ``app.required_runtime_env.CLOUD_RUN_SIGNAL_ENV`` / ``app.main`` so the pure
# scoring layer can derive the deploy-target scope from a presence snapshot
# WITHOUT importing the (os-touching) manifest module.
_CLOUD_RUN_SIGNAL_ENV = "K_REVISION"


def _score_env_preflight(
    env_preflight_artifact: dict[str, Any] | None,
    deploy_target_env_presence: dict[str, bool] | None,
) -> tuple[float | None, list[str], str]:
    """Judge a candidate's declared env prerequisites against the deploy target.

    PURE / PRESENCE-ONLY.  The deployed orchestrator IS the prod Cloud Run
    service, so ``deploy_target_env_presence`` — a ``{name: bool}`` snapshot of
    the deployed process's OWN ``os.environ`` (built by the caller with only
    ``bool(os.getenv(name))``, NEVER a value) — is a faithful presence-proxy of
    the deploy-target env.  This function reads that snapshot as booleans ONLY:
    it never receives, compares, logs, or returns an env VALUE, so a value that
    a malformed caller smuggled into the snapshot is coerced through ``bool()``
    and can never leak into the returned score / missing list / scope.

    ``env_preflight_artifact`` is the candidate-side content artifact (Slice 2):
    ``{required_env: [{name, scope}], ...}`` introspected from the candidate
    tree's OWN manifest.  Scope semantics:

      * ``deploy_target_scope`` is ``"cloud_run"`` when the snapshot shows
        ``K_REVISION`` present (the deployed process is on Cloud Run), else
        ``"always"``.
      * A required var is IN SCOPE when its ``scope`` is active for the deploy
        target: ``always`` is always active; ``cloud_run`` is active only when
        ``deploy_target_scope == "cloud_run"``.

    Returns ``(sub_score, missing_names, deploy_target_scope)``:

      * ``sub_score`` — ``1.0`` when every in-scope required var is present,
        ``0.0`` when any is absent, ``None`` when no usable manifest was supplied
        (nothing to judge — the caller records the dimension unavailable).
      * ``missing_names`` — sorted, de-duplicated list of in-scope required var
        NAMES absent from the snapshot (empty when none missing).
      * ``deploy_target_scope`` — always returned so the verdict blob can record
        which scope the judge evaluated against, even when the manifest is absent.
    """
    presence = (
        deploy_target_env_presence
        if isinstance(deploy_target_env_presence, dict)
        else {}
    )
    # Deploy-target scope from the snapshot's Cloud Run signal (presence-only).
    deploy_target_scope = (
        "cloud_run" if bool(presence.get(_CLOUD_RUN_SIGNAL_ENV)) else "always"
    )
    active_scopes = (
        {"always", "cloud_run"}
        if deploy_target_scope == "cloud_run"
        else {"always"}
    )

    required = (
        env_preflight_artifact.get("required_env")
        if isinstance(env_preflight_artifact, dict)
        else None
    )
    if not isinstance(required, list):
        # No parseable required_env list in the artifact — return None so the
        # caller (compute_composite) can distinguish "evidence absent / malformed"
        # from "evidence present but prerequisites missing".  When
        # env_preflight_enabled is True, compute_composite maps None → the
        # boot_prereq_evidence_missing hard blocker.  External repos that have
        # no required_runtime_env.py at all are handled upstream:
        # build_env_preflight_from_source returns {required_env: []} (empty list,
        # vacuously satisfied → 1.0) so score never reaches None for a genuinely
        # manifest-free tree.
        return None, [], deploy_target_scope

    missing: set[str] = set()
    for entry in required:
        if not isinstance(entry, dict):
            continue
        name = entry.get("name")
        scope = entry.get("scope")
        if not isinstance(name, str) or not name:
            continue
        if not isinstance(scope, str) or scope not in active_scopes:
            continue
        # PRESENCE-ONLY: an absent name and a name whose snapshot bool is falsey
        # both count as missing.  ``bool(...)`` coerces any smuggled value.
        if not bool(presence.get(name)):
            missing.add(name)

    if missing:
        return 0.0, sorted(missing), deploy_target_scope
    return 1.0, [], deploy_target_scope


def compute_composite(
    ci_green: bool,
    audit_score: float,
    test_erosion: float,
    smoketest_score: float,
    policy: dict[str, Any],
    *,
    smoketest_enabled: bool = True,
    ci_state: str = _CI_RED,
    ci_required: bool = True,
    migration_collision: bool = False,
    migration_apply_blocked: bool = False,
    category_verdicts: list[dict[str, Any]] | None = None,
    residual_uncaptured_gap: str | None = None,
    merge_grade_misses: bool = False,
    merge_grade_exempt: bool = False,
    debt_pressure_score: float | None = None,
    capability_proof_blocked: bool = False,
    env_preflight_enabled: bool = False,
    env_preflight_score: float | None = None,
    env_preflight_missing: list[str] | None = None,
    env_preflight_ack_override: bool = False,
) -> dict[str, Any]:
    """Compute the composite promotion confidence for a release candidate.

    Hard blockers — each forces ``confidence = 0.0`` and ``promote = False``.
    They are evaluated in this order, and ``blocker`` reports the first match:
      * ``not ci_green`` + ``ci_state == _CI_RED``  — confirmed failing build
        (``"ci_not_green"``).  Always a hard blocker, regardless of
        ``ci_required`` — never promote over a known-failing build.
      * ``not ci_green`` + ``ci_state != _CI_RED`` + unresolved CI must block
        (``"ci_unresolved"``).  ``ci_required`` can only relax a definitive
        ``ci_state == "no_checks"`` absence that the caller already proved is
        topologically allowed. Pending checks and degraded GitHub/API lookups
        still block even when ``ci_required`` is False.
      * ``migration_collision`` is True — a deterministic Alembic
        revision-identity collision between the candidate and the deployed
        promotion target (the back-merge renumber class, prod incident
        2026-06-04) (``"migration_identity_collision"``).  The caller
        (``release_qualifier._finalize_qualification``) precomputes this with
        ``find_content_divergent_revisions`` and the existing GitHub fetch
        helpers — this function stays pure and only consumes the boolean.
      * ``migration_apply_blocked`` is True — the candidate's migration chain,
        applied the deploy way against production's REAL recorded schema state
        (live ``alembic_version`` stamp + present tables) on an isolated scratch
        DB, leaves the schema missing a required object (the renumber-skip
        class, v5 computejob incident) (``"migration_apply_drift"``).  This is a
        SECOND, distinct deterministic dimension from ``migration_collision``:
        the filename-identity check structurally cannot see a live DB stamped
        ahead of its real schema.  Checked AFTER the identity collision so a
        candidate failing both reports the identity collision first.  The
        caller precomputes this via
        ``migration_apply_gate.run_migration_apply_gate`` — this function stays
        pure and only consumes the boolean.
      * ``merge_grade_misses`` is True — the required blind merge-grade ran and
        found that the candidate misses the frozen version goal
        (``"merge_grade_misses"``).  Checked before the category gate so an
        empty or not-yet-mapped acceptance-criteria set cannot mask the grade.
      * GOAL-VERIFICATION GATE (redesign) — ``category_verdicts`` is a per-active-
        category grade (the acceptance criteria ARE the categories).  Blocks
        (``"criterion_coverage_incomplete"``) when ANY active category's status
        is not ``met`` (``partial`` gates identically to ``not_met``; the
        distinction is informational), OR when ``residual_uncaptured_gap`` is a
        non-empty string (the grader found a goal aspect NO category captures).
        This REPLACES the retired coverage-count gate AND the
        ``uncorroborated_attestation`` gate — a category resting on
        lower-confidence evidence no longer hard-blocks; instead it is folded into
        the judged-tier confidence penalty (below).  The blocker STRING is kept
        (``criterion_coverage_incomplete``) so the downstream re-drive / trigger /
        state-resolver plumbing keyed on it keeps working.  ``category_verdicts``
        ``None`` (no rubric, or a caller passing ``None`` for back-compat) skips
        the gate entirely — preserving the pre-redesign no-criteria path.
        ``sub_scores["unmet_categories"]`` / ``["residual_uncaptured_gap"]``
        record WHY for observability.

        ``merge_grade_exempt`` (finding 85fa667a4d7fcae6): a merge-grade
        exemption (``superseded_exempt`` / ``already_on_main_exempt``) exempts
        ONLY the HOLISTIC ``merge_grade_misses`` gate — the caller clears that
        boolean.  It does NOT exempt this per-category coverage gate: an exempt
        candidate whose ACTIVE acceptance criteria are graded and come back
        ``not_met`` must still block on ``criterion_coverage_incomplete``, exactly
        as ``main``'s retired unconditional coverage block did.  Previously the
        caller skipped grading entirely on the exempt path (passing
        ``category_verdicts=None``), silently loosening coverage; the caller now
        grades and passes the verdicts, and this flag is recorded in
        ``sub_scores["merge_grade_exempt"]`` for provenance without weakening the
        gate.
      * ``capability_proof_blocked`` is True — the capability proof gate is in
        ``"enforce"`` mode and at least one ``working`` capability lacks
        ``proven`` evidence (``"capability_proof_gate_blocked"``).  Governs the
        integrity of the capability proof gate: a promotion over unproven
        capability claims bypasses the gate's entire purpose, so this is a hard
        blocker.  Checked after criterion coverage so the acceptance-criteria
        gate is not masked, and before the quality dimensions so a candidate
        whose capability claims are unproven is rejected regardless of audit
        quality.
      * ``env_preflight_enabled`` is True AND ``env_preflight_score`` is None —
        the candidate produced no boot/env-prerequisite evidence at all (the
        evidence producer never ran, or ran but produced no usable manifest).
        Without evidence the boot gate cannot be certified
        (``"boot_prereq_evidence_missing"``).  This is a DETERMINISTIC quality
        verdict, never an infra-failure retry loop — the fix is producing evidence
        on the candidate tree (EXECUTION_SITE_CANDIDATE).  Checked BEFORE
        ``boot_prereq_missing`` because individual prerequisite presence is moot
        when no evidence exists.  The ``env_preflight_ack_override`` escape valve
        does NOT suppress this block — an ack presupposes evidence exists and
        only suppresses ``boot_prereq_missing``.
      * ``env_preflight_enabled`` is True AND ``env_preflight_missing`` is a
        non-empty list — the DEPLOYED-side env-preflight judge (Slice 3, design
        245212a4) found at least one candidate-declared, in-scope boot
        prerequisite ABSENT from the deploy-target env (``"boot_prereq_missing"``).
        Presence-only: ``env_preflight_missing`` carries NAMES, never values.
        ``sub_scores["env_preflight_missing"]`` records the names for operators.
        When ``env_preflight_enabled`` is False the block is skipped,
        ``sub_scores["env_preflight"]`` is not recorded, and the composite is
        byte-for-byte identical to the pre-Slice-3 behaviour (the judge
        contributes NO weight to the audit + test_erosion sum — it
        is a pure hard gate, like the migration gates).  Note: the
        ``runtime_config`` default is ``True`` (Armed), so at runtime the gate
        is active unless the operator explicitly disables it.  The function
        parameter default is ``False`` for backward-compatibility with callers
        that do not pass the flag.
        ``env_preflight_ack_override`` (Slice 4) is the OWNER-only escape valve:
        when True it suppresses ONLY this ``boot_prereq_missing`` block (for the
        same-deploy-binding case where the deployed environ does not yet reflect
        the pending secret) and records ``sub_scores["env_preflight_ack_override"]
        = True``.  It touches no other gate — an ordered blocker above still wins
        and a blocker below still fires; the missing NAMES are still recorded.
      * Legacy ``smoketest_score`` / ``smoketest_enabled`` arguments are
        accepted for caller compatibility but are inert; qualifier VM smoke is
        retired.
      * ``audit_score`` is None     — the release_audit report is missing or
        unusable (``"audit_missing"``).  Checked before test-erosion because a
        missing audit report nulls both inputs.
      * ``test_erosion`` is None    — the diff deleted a test file
        (``"test_file_deleted"``); a real deletion, only meaningful when the
        audit report itself was present.

    Historical three-key policies are accepted, their retired smoke weight is
    discarded, and the active audit + test-erosion weights are renormalized.

    The numeric inputs are clamped to [0,1] before weighting. When no hard
    blocker fires, ``confidence`` is the policy-weighted sum of the active
    sub-scores.  ``promote`` is True iff ``confidence >= threshold``.

    Returns the historical ``{confidence, promote, sub_scores, blocker}`` keys
    plus additive ``failing_gates`` and ``confidence_ceiling`` diagnostics.
    ``blocker`` remains the first failing gate, while a hard-blocked
    ``confidence`` remains exactly ``0.0``.
    """
    weights = policy.get("weights") or _DEFAULT_POLICY["weights"]
    threshold = promotion_threshold_for_policy(policy)
    # Qualifier VM smoke is retired. Accept legacy three-key policies, ignore
    # their smoke weight, and renormalize the two active deterministic signals.
    w_audit = float(weights.get("audit", _DEFAULT_POLICY["weights"]["audit"]))
    w_test = float(
        weights.get("test_erosion", _DEFAULT_POLICY["weights"]["test_erosion"])
    )
    from decimal import Decimal

    active_audit = Decimal(str(w_audit))
    active_test = Decimal(str(w_test))
    active_sum = active_audit + active_test
    if active_sum <= 0:
        w_audit = _DEFAULT_POLICY["weights"]["audit"]
        w_test = _DEFAULT_POLICY["weights"]["test_erosion"]
    else:
        w_audit = float(active_audit / active_sum)
        w_test = float(active_test / active_sum)

    # Hard blockers — checked before any weighting. ``None`` is the sentinel
    # for "this active dimension hit an irrecoverable blocker"; the caller
    # passes None for test_erosion when a test file was deleted.
    #
    # Ordering matters when several inputs are None at once.  A FAILED
    # release_audit posts no RELEASE_AUDIT_REPORT, so _score_audit(None)
    # returns (audit_score=None, test_erosion=None) — BOTH None even though no
    # test file was actually deleted.  ``audit_score is None`` must therefore
    # be checked BEFORE ``test_erosion is None`` so an audit that never ran is
    # reported as "audit_missing" rather than mislabelled "test_file_deleted":
    # a None test_erosion only genuinely means a deleted test file when the
    # audit report itself was present (audit_score not None).
    failing_gates: list[str] = []
    if not ci_green and ci_state == _CI_RED:
        # A confirmed red build is ALWAYS a hard blocker — never promote over a
        # known-failing build, regardless of whether the project gates per-PR
        # merges on CI.
        blocker = "ci_not_green"
        failing_gates.append(blocker)
    elif not ci_green and (ci_required or ci_state != "no_checks"):
        # Unresolved CI (pending or GitHub I/O degraded) is a resolution /
        # infrastructure concern, not a quality signal, and must keep blocking.
        # The only relaxed state is a definitive absence of checks that the
        # caller already classified as topology-safe by setting ci_required
        # False. A project-wide CI relaxation must not mask pending/API errors.
        blocker = "ci_unresolved"
        failing_gates.append(blocker)
    if migration_collision:
        # A deterministic Alembic revision-identity collision between the
        # candidate and the deployed promotion target — checked ahead of the
        # audit-derived blockers so it is a clear, attributed gate rather than a
        # soft, score-eroding audit finding the spawned auditor might miss.
        blocker = "migration_identity_collision"
        failing_gates.append(blocker)
    if migration_apply_blocked:
        # The candidate's migration chain, replayed the deploy way against
        # production's REAL recorded schema state on an isolated scratch DB,
        # leaves the schema missing a required object (the renumber-skip class,
        # v5 computejob incident).  A SECOND deterministic dimension distinct
        # from the filename-identity collision above — checked after it so a
        # candidate failing both reports the identity collision first.
        blocker = "migration_apply_drift"
        failing_gates.append(blocker)
    if env_preflight_enabled and env_preflight_score is None:
        # No boot/env-prerequisite evidence was produced by the candidate — the
        # evidence producer never ran, or ran but produced no usable manifest.
        # This is a DETERMINISTIC quality verdict (never an infra-failure retry
        # loop — the v22 migration-gate lesson).  The fix is producing evidence
        # on the candidate tree (EXECUTION_SITE_CANDIDATE).  Checked AFTER the
        # migration gates so a candidate failing both reports migrations first,
        # and BEFORE ``boot_prereq_missing`` because with no evidence we cannot
        # certify the boot gate at all — individual prerequisite presence is moot.
        blocker = "boot_prereq_evidence_missing"
        failing_gates.append(blocker)
    if env_preflight_enabled and env_preflight_missing and not env_preflight_ack_override:
        # DEPLOYED-side env-preflight judge (Slice 3, design 245212a4).
        # runtime_config default: True (Armed).  A candidate-declared,
        # in-scope boot prerequisite is ABSENT from the deploy-target env — the
        # v25 class where a new startup gate crashes prod because its secret is
        # unbound.  Presence-only: env_preflight_missing carries NAMES, never
        # values.  Grouped with the migration gates as a deploy-safety hard block,
        # checked after them so a candidate failing both reports migrations first.
        #
        # ``env_preflight_ack_override`` (Slice 4) is the OWNER-only escape valve:
        # when the operator intends to bind the missing secret AS PART OF the same
        # deploy, THIS deployed process's environ does not yet reflect it and the
        # judge would false-block.  The override suppresses ONLY this block (an
        # explicit acknowledged override, recorded in sub_scores and the audit blob
        # by the caller — never a silent bypass) and touches no other gate: an
        # ordered blocker above it (CI, migrations) still wins, and a blocker below
        # (capability-proof, coverage, quality) still fires on its own merits.
        blocker = "boot_prereq_missing"
        failing_gates.append(blocker)
    if merge_grade_misses:
        # The armable-dark blind-grade gate: coverage_verdict == "misses" AND the
        # operator has raised merge_grade_block_threshold above 0.0.  Distinct from
        # the canonical goal-verification gate so operators see the right label.
        blocker = "merge_grade_misses"
        failing_gates.append(blocker)
    if category_verdicts is not None and any(
        (v or {}).get("status") != "met" for v in category_verdicts
    ):
        # GOAL-VERIFICATION GATE (redesign): the acceptance criteria ARE the
        # merge-grade's categories.  ANY active category not at ``met`` blocks
        # (``partial`` gates identically to ``not_met`` — only ``met`` clears).
        # This replaces the retired coverage-count arithmetic AND the
        # uncorroborated-attestation gate; a lower-confidence (judged-tier)
        # category no longer hard-blocks — that is folded into the confidence
        # penalty below.  The blocker STRING is preserved so downstream re-drive /
        # trigger / state-resolver plumbing keyed on it keeps firing.
        blocker = "criterion_coverage_incomplete"
        failing_gates.append(blocker)
    if residual_uncaptured_gap and "criterion_coverage_incomplete" not in failing_gates:
        # RESIDUAL gate (redesign): the grader found a goal aspect that NONE of
        # the categories capture — the rubric is incomplete for this candidate, so
        # even an all-``met`` category set cannot certify the goal.  Same blocker
        # string, distinguished for operators via sub_scores["residual_uncaptured_gap"].
        blocker = "criterion_coverage_incomplete"
        failing_gates.append(blocker)
    if capability_proof_blocked:
        # The capability proof gate is in ENFORCE mode and there are working
        # capabilities whose proof status is not ``proven`` — the gate would
        # block dependents that name them.  Block promotion until the unproven
        # capabilities are either proven or the gate is dialled back to observe.
        blocker = "capability_proof_gate_blocked"
        failing_gates.append(blocker)
    if audit_score is None:
        # An audit that produced no usable score is a soft signal, but without
        # it the composite is meaningless — treat as a blocker so a missing
        # audit can never promote on test_erosion alone.  Reported
        # ahead of test_file_deleted because a missing report nulls both.
        blocker = "audit_missing"
        failing_gates.append(blocker)
    elif test_erosion is None:
        blocker = "test_file_deleted"
        failing_gates.append(blocker)

    # Preserve the historical ordered blocker while retaining every independent
    # failing gate for diagnosis.  The list is intentionally de-duplicated:
    # category and residual failures are two observations of the same gate.
    failing_gates = list(dict.fromkeys(failing_gates))
    blocker: str | None = failing_gates[0] if failing_gates else None

    # Sub-scores are always reported (clamped) for observability, even when a
    # blocker fired and the composite is forced to zero.
    sub_scores = {
        "audit": _clamp01(audit_score) if audit_score is not None else 0.0,
        "test_erosion": _clamp01(test_erosion) if test_erosion is not None else 0.0,
    }
    # Debt-pressure sub-score: always recorded when provided (observe mode);
    # only blended into confidence when completion_debt_weight > 0 in policy.
    if debt_pressure_score is not None:
        sub_scores["debt_pressure"] = _clamp01(debt_pressure_score)

    # Env-preflight sub-score + missing names (Slice 3).  Only
    # recorded when the judge is enabled, so a disabled subsystem leaves the
    # sub_scores dict byte-for-byte unchanged.  env_preflight is a pure hard gate
    # (block on any missing) — it never contributes a weight to the composite sum,
    # so an all-present candidate scores identically to the pre-Slice-3 behaviour.
    if env_preflight_enabled:
        if env_preflight_score is not None:
            sub_scores["env_preflight"] = _clamp01(env_preflight_score)
        if env_preflight_missing:
            # Presence-only: NAMES, never values.
            sub_scores["env_preflight_missing"] = [
                str(n) for n in env_preflight_missing
            ]
            # Slice 4: an OWNER acknowledged that the missing prerequisite will be
            # bound in the same deploy revision, so the hard block above was
            # suppressed for this run.  Record the override FACT (presence-only:
            # the fact + the already-recorded missing NAMES, never a value) so the
            # bypass is auditable rather than silent.
            if env_preflight_ack_override:
                sub_scores["env_preflight_ack_override"] = True

    # Goal-verification observability (redesign): surface the per-category grade
    # and residual so operators see WHY the gate fired (or would fire), not just
    # the blocker string.  Recorded whenever a per-category grade was supplied,
    # even when a different blocker won the ordered chain.
    if category_verdicts is not None:
        _unmet = [
            str((v or {}).get("category_key") or "")
            for v in category_verdicts
            if (v or {}).get("status") != "met"
        ]
        sub_scores["category_count"] = len(category_verdicts)
        sub_scores["met_category_count"] = len(category_verdicts) - len(_unmet)
        if _unmet:
            sub_scores["unmet_categories"] = [k for k in _unmet if k]
        # Fraction of MET categories still resting on the judged tier (v1 = all).
        _met = [v for v in category_verdicts if (v or {}).get("status") == "met"]
        if _met:
            _judged = sum(1 for v in _met if (v or {}).get("tier", "judged") != "machine")
            sub_scores["judged_tier_met_fraction"] = round(_judged / len(_met), 4)
    if residual_uncaptured_gap:
        sub_scores["residual_uncaptured_gap"] = str(residual_uncaptured_gap)
    # Provenance (finding 85fa667a4d7fcae6): record that this candidate reached
    # scoring on a merge-grade-exempt path so an operator can see the per-category
    # coverage gate STILL applied (only the holistic merge_grade_misses gate was
    # exempted).  Purely informational — the gate outcome above is unchanged.
    if merge_grade_exempt:
        sub_scores["merge_grade_exempt"] = True

    sub_scores["failing_gates"] = failing_gates

    # Compute the score the candidate could reach if its hard gates were
    # cleared, using the exact active weight, debt, and judged-tier arithmetic
    # below.  This is deliberately computed before the hard-block return so a
    # blocked verdict still explains whether promotion is reachable without
    # pretending that the blocked candidate promoted.
    w_debt = float((policy.get("weights") or {}).get("completion_debt", 0.0))
    w_debt = max(0.0, min(w_debt, 0.5))
    existing_confidence = (
        w_audit * sub_scores["audit"] + w_test * sub_scores["test_erosion"]
    )
    if w_debt > 0.0 and debt_pressure_score is not None:
        confidence_ceiling = (
            (1.0 - w_debt) * existing_confidence
            + w_debt * sub_scores["debt_pressure"]
        )
    else:
        confidence_ceiling = existing_confidence
    category_penalty_factor = 1.0
    if category_verdicts:
        _met = [v for v in category_verdicts if (v or {}).get("status") == "met"]
        if _met:
            _judged = sum(
                1 for v in _met if (v or {}).get("tier", "judged") != "machine"
            )
            category_penalty_factor = 1.0 - JUDGED_TIER_PENALTY_WEIGHT * (
                _judged / len(_met)
            )
            confidence_ceiling *= category_penalty_factor
    confidence_ceiling = round(_clamp01(confidence_ceiling), 4)
    sub_scores["confidence_ceiling"] = confidence_ceiling

    # Solve the active scoring equation here, beside its canonical weights, so
    # reports can name the actionable term instead of inferring it from the
    # primary blocker. Only feasible, currently unmet requirements are emitted;
    # a term needing more than 1.0 is not independently actionable.
    required_sub_scores: dict[str, float] = {}
    if confidence_ceiling < threshold:
        target = threshold / category_penalty_factor
        if w_debt > 0.0 and debt_pressure_score is not None:
            target = (target - w_debt * sub_scores["debt_pressure"]) / (1.0 - w_debt)
        if w_audit > 0.0:
            required_audit = (target - w_test * sub_scores["test_erosion"]) / w_audit
            if sub_scores["audit"] < required_audit <= 1.0:
                required_sub_scores["audit"] = round(required_audit, 4)
        if w_test > 0.0:
            required_test = (target - w_audit * sub_scores["audit"]) / w_test
            if sub_scores["test_erosion"] < required_test <= 1.0:
                required_sub_scores["test_erosion"] = round(required_test, 4)
    if required_sub_scores:
        sub_scores["required_sub_scores"] = required_sub_scores

    if blocker is not None:
        return {
            "confidence": 0.0,
            "promote": False,
            "sub_scores": sub_scores,
            "blocker": blocker,
            "failing_gates": failing_gates,
            "confidence_ceiling": confidence_ceiling,
            "required_sub_scores": required_sub_scores,
        }

    # Debt-pressure weight (defaults to 0.0 = observe mode: recorded but not
    # blended).  When > 0 the existing sub-scores are scaled by (1 - w_debt)
    # and the debt-pressure term is added so the total remains in [0, 1].
    existing_confidence = (
        w_audit * sub_scores["audit"]
        + w_test * sub_scores["test_erosion"]
    )
    if w_debt > 0.0 and debt_pressure_score is not None:
        # Enforce mode: scale existing confidence down by (1 - w_debt) and
        # blend in the debt-pressure term so total weight stays at 1.0.
        confidence = (1.0 - w_debt) * existing_confidence + w_debt * sub_scores["debt_pressure"]
    else:
        confidence = existing_confidence

    # Judged-tier confidence penalty (goal-verification redesign): reaching here
    # means every active category is ``met`` (the gate passed).  v1 grades every
    # category at the JUDGED tier — an LLM reading the merged diff — a lower-
    # confidence signal than machine/test-cited evidence (the v2 machine tier).
    # Dock confidence in proportion to the fraction of MET categories still on the
    # judged tier, so an all-judged candidate scores visibly below a
    # machine-verified one WITHOUT hard-blocking.
    if category_verdicts:
        _met = [v for v in category_verdicts if (v or {}).get("status") == "met"]
        if _met:
            _judged = sum(1 for v in _met if (v or {}).get("tier", "judged") != "machine")
            penalty = JUDGED_TIER_PENALTY_WEIGHT * (_judged / len(_met))
            confidence *= 1.0 - penalty

    confidence = round(_clamp01(confidence), 4)
    return {
        "confidence": confidence,
        "promote": confidence >= threshold,
        "sub_scores": sub_scores,
        "blocker": None,
        "failing_gates": failing_gates,
        "confidence_ceiling": confidence,
        "required_sub_scores": required_sub_scores,
    }


# --- Execution-site classification of promotion blockers --------------------
# Every hard-blocker code compute_composite() (and the qualifier's downstream
# gates) can emit is classified by WHERE the responsible check runs, so an
# operator or agent can tell — WITHOUT hand-diagnosing the release machinery —
# whether a fix landing on automain will actually take effect before the very
# promotion it is meant to unblock.
#
#   "deployed"  — the check runs in the DEPLOYED (main) process at the deployed
#                 SHA and compares the candidate against the deployed promotion
#                 target / production's recorded schema.  A fix to this check on
#                 automain is INERT until it is itself promoted (release-machinery
#                 self-reference): the divergence self-clears only on promotion.
#   "candidate" — the check runs against CANDIDATE content (the merged diff, the
#                 candidate's CI, or a candidate-side grade).  A fix on
#                 automain resolves the blocker on the NEXT qualification, no
#                 redeploy required.
EXECUTION_SITE_DEPLOYED = "deployed"
EXECUTION_SITE_CANDIDATE = "candidate"
EXECUTION_SITE_OPERATOR_ENV = "operator_env"

BLOCKER_EXECUTION_SITE: dict[str, str] = {
    # Deployed-side: both compare the candidate against the deployed promotion
    # target / production's live recorded schema; the divergence self-clears only
    # once the version itself promotes, so a fix on automain is inert until then.
    "migration_identity_collision": EXECUTION_SITE_DEPLOYED,
    "migration_apply_drift": EXECUTION_SITE_DEPLOYED,
    # Operator-env site (Slice 4, design 245212a4): the env-preflight judge runs in
    # the DEPLOYED process and compares the candidate's declared boot prerequisites
    # against the deployed process's OWN environ (a presence-proxy of the deploy
    # target).  Its fix is NEITHER an automain code change (candidate) NOR
    # "promote the version" (deployed): it is an OPERATOR action — bind the
    # secret/env in the Cloud Run deploy target, which rolls a new revision so the
    # deployed orchestrator's environ then contains it and the block self-clears.
    # An OWNER may pre-acknowledge that binding via ``ack_env_will_be_bound`` when
    # it lands in the same deploy (see compute_composite's env_preflight_ack_override).
    "boot_prereq_missing": EXECUTION_SITE_OPERATOR_ENV,
    # Candidate-side: the evidence producer must run on the candidate tree.
    # A fix on automain that causes the producer to run clears this on the
    # next qualification — no deploy or operator action required.
    "boot_prereq_evidence_missing": EXECUTION_SITE_CANDIDATE,
    # Candidate-side: resolvable by an automain fix that lands in the next
    # candidate tree — CI or the merged diff.
    "ci_not_green": EXECUTION_SITE_CANDIDATE,
    "ci_unresolved": EXECUTION_SITE_CANDIDATE,
    "audit_missing": EXECUTION_SITE_CANDIDATE,
    "test_file_deleted": EXECUTION_SITE_CANDIDATE,
    # Candidate-side: a genuine criterion failure (the report is valid and
    # every key resolves to an ACTIVE criterion in the open rubric, but one or
    # more criteria failed) — an automain fix to the candidate tree clears it
    # on the next qualification.
    "walkthrough_e2e_failed": EXECUTION_SITE_CANDIDATE,
    # Deployed-side: report-key validation (schema mismatch, duplicate/invalid
    # entries, canonical collisions, unknown criteria) is performed by the
    # DEPLOYED qualifier against the DEPLOYED rubric.  No fix on automain can
    # change how the currently-serving validator parses the report, so the
    # block is inert until this version itself promotes.
    "walkthrough_e2e_contract_failed": EXECUTION_SITE_DEPLOYED,
    # Deployed-side: the category verdict ledger is composed and rendered by
    # the deployed qualifier.  Its recovery/minting path (acceptance-window,
    # unverified-merge, and metrics-integrity evidence) also imports and runs
    # in that process.  A change to any of that machinery on automain is inert
    # until deployed, so direct operators to the narrow hotfix lane rather
    # than claiming that another candidate qualification can pick it up.
    "criterion_coverage_incomplete": EXECUTION_SITE_DEPLOYED,
    "merge_grade_misses": EXECUTION_SITE_CANDIDATE,
    "capability_proof_gate_blocked": EXECUTION_SITE_CANDIDATE,
    # NOTE: ``uncorroborated_attestation`` is deliberately absent — the
    # attestation gate it classified was replaced by the per-category
    # goal-verification gate (which blocks as ``criterion_coverage_incomplete``),
    # and no code path emits the code any more.  Classifying a blocker nothing
    # emits is dead vocabulary that reads as a live gate to operators.
}

# Short operator-facing phrase per execution-site, appended to blocker labels.
EXECUTION_SITE_LABELS: dict[str, str] = {
    EXECUTION_SITE_DEPLOYED: (
        "this check runs DEPLOYED-side — a fix on automain is inert until this "
        "version itself promotes"
    ),
    EXECUTION_SITE_CANDIDATE: (
        "this check runs CANDIDATE-side — a fix on automain clears it on the next "
        "qualification"
    ),
    EXECUTION_SITE_OPERATOR_ENV: (
        "this check requires an operator to bind an env/secret in the deploy "
        "target; it clears on the next revision once the binding exists"
    ),
}


def blocker_execution_site(blocker: str | None) -> str | None:
    """Classify a promotion-blocker code by where the responsible check runs.

    Returns ``"deployed"`` (inert-until-promote), ``"candidate"`` (clearable by
    an automain fix), ``"operator_env"`` (requires an operator to bind an
    env/secret in the deploy target — clears on the next revision), or ``None``
    when there is no blocker or the code is not classified (unknown /
    force-promote sentinels such as ``qualification_superseded``).
    """
    if not blocker:
        return None
    return BLOCKER_EXECUTION_SITE.get(blocker)


def blocker_execution_site_label(blocker: str | None) -> str | None:
    """Human-readable execution-site phrase for a blocker, or None if unclassified."""
    site = blocker_execution_site(blocker)
    if site is None:
        return None
    return EXECUTION_SITE_LABELS.get(site)


_AUDIT_DEDUCTIONS: dict[str, float] = {
    "critical": 0.30,
    "blocker": 0.30,
    "high": 0.15,
    "medium": 0.07,
    "moderate": 0.07,
    "warning": 0.07,
    "low": 0.02,
    "info": 0.02,
}
# Fallback deduction for any non-empty severity label not listed above.
_AUDIT_DEDUCTION_UNKNOWN: float = 0.02

# Severities that require a substantive verification_note before the deduction
# is applied.  Low/info findings are cheap enough to trust without explicit
# substantiation; everything else (medium/moderate/warning/high/critical/blocker)
# carries real deduction weight and must be confirmed against the full repo.
_SEVERITIES_REQUIRING_VERIFICATION: frozenset[str] = frozenset(
    {"critical", "blocker", "high", "medium", "moderate", "warning"}
)


def _finding_is_verified(f: dict, sev: str) -> bool:
    """Return True when the finding may be counted toward the audit deduction.

    Low/info findings are always counted.  For higher-severity findings the
    reviewer must have read the relevant unchanged files and recorded the
    supporting evidence in ``verification_note`` (≥ 20 non-whitespace chars).
    Findings that fail this check are silently excluded from the score so
    diff-only speculation cannot suppress the audit sub-score.
    """
    if sev not in _SEVERITIES_REQUIRING_VERIFICATION:
        return True
    note = f.get("verification_note", "")
    if not isinstance(note, str):
        return False
    return sum(1 for c in note if not c.isspace()) >= 20


# Phrases that mark an audit finding as being about the Alembic
# revision-identity / back-merge-renumber concern — the exact dimension the
# deterministic migration-identity gate (compute_composite migration_collision
# + check_qualification_migration_identity) already owns.  A finding is
# delegated to that gate (and excluded from the audit deduction) ONLY when it
# names a registry-listed divergence's revision id AND uses one of these
# identity/renumber phrases.  Genuine migration-BODY defects (bad DDL, missing
# downgrade, data-loss risk) use none of these phrases, so they keep deducting.
_REVISION_IDENTITY_PHRASES: tuple[str, ...] = (
    "revision id",
    "revision-id",
    "revision identity",
    "revision-identity",
    "renumber",
    "re-number",
    "back-merge",
    "backmerge",
    "back merge",
    "collision",
    "reused",
    "re-used",
    "duplicate revision",
    "same revision",
)


def _finding_subject_is_authorized_divergence(f: dict) -> bool:
    """Return True when a finding's subject is an authorized, registry-listed
    Alembic revision-identity divergence already owned by the deterministic
    migration-identity gate.

    The release audit is a general LLM scan over ``git diff main...<candidate>``;
    that diff necessarily contains the authorized ``0143`` back-merge renumber
    until the candidate actually promotes, so a competent auditor flags it as a
    correctness/architecture finding.  Counting that deduction double-penalizes
    the same divergence the gate (compute_composite ``migration_collision``)
    already evaluates and deploy-time reconciliation handles.  This delegates
    such findings to the gate by excluding them from the audit sub-score.

    The match is deliberately narrow: the finding text must name a
    registry-listed divergence's revision id AND use a revision-identity /
    renumber phrase (or name both the deployed and integration migration
    identities of that revision).  Findings about a migration's BODY (broken
    DDL, missing downgrade, destructive re-apply) match neither and continue to
    deduct normally.
    """
    if not isinstance(f, dict):
        return False
    text = " ".join(
        str(f.get(k, ""))
        for k in (
            "summary",
            "description",
            "title",
            "text",
            "file",
            "verification_note",
        )
    ).lower()
    if not text:
        return False
    # Effective list = baked floor UNION operator-approved runtime store, resolved
    # at call time so a newly-added runtime exemption de-dups its audit finding
    # without a redeploy.
    for div in effective_authorized_divergences():
        if div.revision.lower() not in text:
            continue
        matched = any(p in text for p in _REVISION_IDENTITY_PHRASES) or (
            # The back-merge identity swap named explicitly (both migration
            # identities of the reused revision) is itself the divergence concern.
            div.deployed_identity.lower() in text
            and div.integration_identity.lower() in text
        )
        if matched:
            if div.is_runtime:
                logger.info(
                    "[release-audit] authorized-divergence finding de-dup for "
                    "revision %r via RUNTIME exemption authorized by %r: %s",
                    div.revision,
                    div.added_by,
                    div.reason,
                )
            return True
    return False


def _finding_matches_candidate_surface(
    finding: Any,
    candidate_surface_paths: dict[str, str] | None,
) -> bool:
    """Return False ONLY when the finding is *provably* stale evidence.

    Proven by exactly one condition: the finding names a path the candidate's
    audited-surface map covers, records a blob SHA, and that SHA differs from
    the candidate's — the defect this gate closes (an audit graded against
    blobs the candidate no longer contains).  Absence of blob identity is NOT
    evidence of staleness, so everything else keeps deducting/carrying: an
    unavailable map (``None``); a non-dict finding; a cross-cutting ``file: ""``
    finding (a sanctioned auditor shape — ``agent/src/handlers/audits.ts``)
    with no single blob to match; a path outside the audited-surface subset
    (``.github/``, ``*.md``, ``__fixtures__``) the map does not cover.
    """
    if candidate_surface_paths is None or not isinstance(finding, dict):
        return True
    path = str(finding.get("file") or finding.get("path") or "").strip()
    candidate_blob = candidate_surface_paths.get(path) if path else None
    blob_sha = finding.get("blob_sha")
    if not candidate_blob or not isinstance(blob_sha, str) or not blob_sha.strip():
        return True
    return candidate_blob == blob_sha.strip()


def _score_audit(
    report: dict[str, Any] | None,
    *,
    candidate_surface_paths: dict[str, str] | None = None,
) -> tuple[float | None, float | None]:
    """Derive ``(audit_score, test_erosion_score)`` from a RELEASE_AUDIT_REPORT.

    The agent emits ``{audit_score, test_erosion: {deleted_test_files,
    assertion_delta, test_count_delta}, findings}`` (agent/src/handlers/
    specialized.ts).

    ``audit_score`` is derived deterministically from the ``findings`` list
    using ``_AUDIT_DEDUCTIONS`` weights and clamped to [0, 1].  The agent's
    self-reported ``audit_score`` is kept only as an observability cross-check;
    a warning is logged when the two diverge by >0.10.

    Returns ``audit_score`` (None when report is absent/unusable) and a
    ``test_erosion_score`` in [0,1] where 1.0 == no erosion.  ``test_erosion``
    is None — the hard-blocker sentinel — ONLY when a deleted test file is
    explicitly classified ``genuine_erosion`` (via
    ``report["deleted_test_file_classifications"]``).
    The gate is fail-OPEN: an explicit ``dead_code_removal`` tag, an absent tag,
    and an ambiguous tag all pass; only an explicit ``genuine_erosion`` tag
    hard-blocks.  A net reduction in tests or assertions applies a graded penalty.
    """
    if not isinstance(report, dict):
        return None, None

    # Derive audit_score deterministically from findings.
    # Mirror _extract_audit_findings: fall back to "issues" key when "findings"
    # is absent or not a list, and resolve severity via "level" as alternate field.
    findings = report.get("findings")
    if not isinstance(findings, list):
        findings = report.get("issues")
    derived: float = 1.0
    if isinstance(findings, list):
        for f in findings:
            if isinstance(f, dict):
                if not _finding_matches_candidate_surface(f, candidate_surface_paths):
                    continue
                sev = str(f.get("severity") or f.get("level") or "").lower()
                if not sev or not _finding_is_verified(f, sev):
                    continue
                if _finding_subject_is_authorized_divergence(f):
                    # Owned by the deterministic migration-identity gate — do
                    # not double-count it as an audit deduction.
                    continue
                derived -= _AUDIT_DEDUCTIONS.get(sev, _AUDIT_DEDUCTION_UNKNOWN)
    derived = _clamp01(derived)
    missing_reason = _audit_missing_reason(report)
    audit_score: float | None = derived
    if missing_reason in (
        "parseable_but_unscoreable",
        "all_findings_unverified",
        "no_findings_key",
    ):
        # The report parsed but yields NO trustworthy score, so returning the
        # clean-looking ``derived`` would fail GREEN over real defects:
        #   * ``parseable_but_unscoreable`` — no finding carried a usable severity.
        #   * ``all_findings_unverified`` — EVERY higher-severity (critical/high/…)
        #     finding was dropped for lacking a ≥20-char verification_note, so the
        #     deduction loop left ``derived`` at 1.0 while unsubstantiated criticals
        #     go unsurfaced (the false-green class this fix closes).
        #   * ``no_findings_key`` — the artifact carries NEITHER a "findings" nor
        #     an "issues" key, so the deduction loop never ran and ``derived``
        #     stayed 1.0.  A report missing the findings section entirely is
        #     MALFORMED, not clean (contrast ``empty_findings_list``, where an
        #     explicit ``findings: []`` legitimately says the auditor found
        #     nothing) — score it unmeasured so ``compute_composite`` routes to
        #     the ``audit_missing`` hard blocker instead of promoting on a
        #     phantom-clean audit.
        # NULL the score in both cases so ``compute_composite`` routes to the
        # ``audit_missing`` hard blocker (surface it, don't fail green) instead of
        # promoting on a phantom-clean audit.  A report with at least ONE verified
        # higher-severity finding is still scoreable and keeps its derived score
        # (``all_findings_unverified`` does not fire) — correct scoring is preserved
        # whenever verification notes are present.
        audit_score = None
    else:
        # Observability: log when the model's self-reported score diverges materially.
        raw_audit = report.get("audit_score")
        if isinstance(raw_audit, (int, float)) and not isinstance(raw_audit, bool):
            reported = _clamp01(raw_audit)
            if abs(derived - reported) > 0.10:
                logger.warning(
                    "[RELEASE_QUALIFIER] audit_score drift: derived=%.3f reported=%.3f"
                    " (delta=%.3f) — findings-derived score is authoritative",
                    derived,
                    reported,
                    abs(derived - reported),
                )

    # Prefer computed_test_erosion (deterministic git-diff computation, authoritative)
    # over the LLM's self-reported test_erosion.  Fall back to test_erosion when
    # computed_test_erosion is absent (older artifacts or agent-side failure).
    computed_erosion = report.get("computed_test_erosion")
    llm_erosion = report.get("test_erosion")

    if isinstance(computed_erosion, dict):
        # Cross-check: warn when the LLM's deleted_test_files disagrees with the
        # deterministic computation — helps surface model blind spots in review.
        if isinstance(llm_erosion, dict):
            llm_deleted = llm_erosion.get("deleted_test_files")
            computed_deleted = computed_erosion.get("deleted_test_files")
            if (
                isinstance(llm_deleted, list)
                and isinstance(computed_deleted, list)
                and {str(x) for x in llm_deleted} != {str(x) for x in computed_deleted}
            ):
                logger.warning(
                    "[RELEASE_QUALIFIER] test_erosion divergence: "
                    "computed deleted=%s llm deleted=%s — computed is authoritative",
                    computed_deleted,
                    llm_deleted,
                )
        erosion: dict[str, Any] | None = computed_erosion
    else:
        erosion = llm_erosion

    if not isinstance(erosion, dict):
        # No test-erosion block — assume neutral (no erosion detected).
        return audit_score, 1.0

    deleted = erosion.get("deleted_test_files")
    if isinstance(deleted, list) and len(deleted) > 0:
        # Per-file LLM classification replaces the old hardcoded
        # AUTHORIZED_TEST_DELETIONS allowlist (which required a backend deploy to
        # widen).  The release audit agent classifies each deterministically-
        # detected deleted test file as either ``dead_code_removal`` (subject
        # code co-deleted or zero remaining callers) or ``genuine_erosion`` (the
        # subject is demonstrably still present and the test was removed to stop
        # failing tests from blocking a build).
        #
        # Disposition is FAIL-OPEN (operator steer): the ONLY blocking outcome is
        # an explicit ``genuine_erosion`` classification.  ``dead_code_removal``,
        # absent, empty, and ambiguous classifications all pass.  This reverts the
        # short-lived fail-closed disposition (#11268), which blocked legitimate
        # co-deletions whenever the auditor simply didn't emit a tag — the common
        # case for the operator's dead-code/declutter cuts.  Deterministic
        # detection of the deleted set is unchanged, so nothing is missed; only
        # the default disposition for an un-flagged deletion flips to allow.
        #
        # The candidate-bound per-file classification path introduced in 84b965a9
        # is preserved: the gate reads ``deleted_test_file_classifications`` from
        # the audit report at scoring time, so the deadlock that afflicted the
        # old AUTHORIZED_TEST_DELETIONS allowlist (which required a backend
        # deploy to widen) cannot return.
        # Start with LLM classifications (lowest priority).
        classifications: dict[str, str] = {}
        raw_cls = report.get("deleted_test_file_classifications")
        if isinstance(raw_cls, list):
            for item in raw_cls:
                if isinstance(item, dict) and isinstance(item.get("path"), str):
                    classifications[item["path"]] = str(item.get("classification", ""))
        # Overlay deterministic co-deletion classifications from computed_test_erosion
        # (higher authority: derived from git diff --name-status, not LLM re-derivation).
        # Only dead_code_removal is emitted deterministically; never genuine_erosion.
        # A file cleared here passes without needing an LLM tag.
        raw_det_cls = (
            computed_erosion.get("deleted_test_file_classifications")
            if isinstance(computed_erosion, dict)
            else None
        )
        if isinstance(raw_det_cls, list):
            for item in raw_det_cls:
                if (
                    isinstance(item, dict)
                    and isinstance(item.get("path"), str)
                    and str(item.get("classification", "")) == "dead_code_removal"
                ):
                    classifications[item["path"]] = "dead_code_removal"
        for f in deleted:
            cls = classifications.get(str(f), "")
            if cls == "genuine_erosion":
                # FAIL-OPEN (operator steer): ONLY an explicit ``genuine_erosion``
                # classification hard-blocks.  ``dead_code_removal``, absent, empty,
                # and ambiguous classifications all PASS — a co-deleted or dead-code
                # test removal must never block promotion, and the operator is
                # permissive about test deletion ("we have too many tests").  The
                # rare deliberate-erosion case (subject demonstrably still present,
                # test stripped to hide a failing build) still blocks, but only when
                # the auditor affirmatively says so.
                return audit_score, None
            # dead_code_removal / absent / ambiguous — allow (fail-open).

    # Graded penalty for net assertion / test-count reduction.  Each negative
    # delta knocks the score down; positive deltas leave it at the ceiling.
    score = 1.0
    test_delta = erosion.get("test_count_delta")
    if isinstance(test_delta, (int, float)) and test_delta < 0:
        # 0.10 penalty per net-removed test, capped so it can't go below 0.
        score -= min(0.5, abs(float(test_delta)) * 0.10)
    assertion_delta = erosion.get("assertion_delta")
    if isinstance(assertion_delta, (int, float)) and assertion_delta < 0:
        # 0.02 penalty per net-removed assertion, capped.
        score -= min(0.3, abs(float(assertion_delta)) * 0.02)
    return audit_score, _clamp01(score)


def _compute_audit_reconciliation(
    report: dict[str, Any] | None,
    *,
    candidate_surface_paths: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Compute audit-score reconciliation metadata for observability.

    Records the model's self-reported score alongside the deterministic
    findings-derived score so an operator can tell, for a given qualification
    run, whether the two agree and how many findings were dropped for lacking
    substantiation.

    Returns a dict with:
    - ``model_reported_score`` — the LLM's self-reported ``audit_score``,
      clamped to [0,1], or None when absent/non-numeric.
    - ``findings_derived_score`` — deterministic score from findings list
      (same deduction table as ``_score_audit``), or None when no findings
      are present.
    - ``score_delta`` — ``abs(findings_derived - model_reported)``, rounded
      to 4 dp, or None when either component is absent.
    - ``excluded_findings_count`` — findings excluded for an absent/too-short
      verification note or a blob proven absent from the candidate surface.

    This function is pure and does NOT change the gating math; call it
    alongside ``_score_audit`` to attach reconciliation fields to
    ``last_qualification`` / ``QualificationVerdict`` records.
    """
    empty: dict[str, Any] = {
        "model_reported_score": None,
        "findings_derived_score": None,
        "score_delta": None,
        "excluded_findings_count": 0,
    }
    if not isinstance(report, dict):
        return empty

    # Model-reported score (self-assessment from the LLM audit output).
    raw_audit = report.get("audit_score")
    model_reported: float | None = None
    if isinstance(raw_audit, (int, float)) and not isinstance(raw_audit, bool):
        model_reported = _clamp01(raw_audit)

    # Deterministic findings-derived score — mirrors the deduction loop in
    # ``_score_audit`` but additionally tracks excluded findings.
    findings = report.get("findings")
    if not isinstance(findings, list):
        findings = report.get("issues")

    findings_derived: float | None = None
    excluded_count: int = 0
    if isinstance(findings, list):
        derived: float = 1.0
        for f in findings:
            if isinstance(f, dict):
                if not _finding_matches_candidate_surface(f, candidate_surface_paths):
                    excluded_count += 1
                    continue
                sev = str(f.get("severity") or f.get("level") or "").lower()
                if sev:
                    if _finding_is_verified(f, sev):
                        if _finding_subject_is_authorized_divergence(f):
                            # Delegated to the migration-identity gate — mirror
                            # _score_audit and apply no deduction here.
                            continue
                        derived -= _AUDIT_DEDUCTIONS.get(sev, _AUDIT_DEDUCTION_UNKNOWN)
                    elif sev in _SEVERITIES_REQUIRING_VERIFICATION:
                        excluded_count += 1
        findings_derived = _clamp01(derived)

    score_delta: float | None = None
    if model_reported is not None and findings_derived is not None:
        score_delta = round(abs(findings_derived - model_reported), 4)

    return {
        "model_reported_score": model_reported,
        "findings_derived_score": findings_derived,
        "score_delta": score_delta,
        "excluded_findings_count": excluded_count,
    }


def _audit_missing_reason(report: dict[str, Any] | None) -> str | None:
    """Return a structured sub-status string when a parseable audit report yields
    no scoring signal, or ``None`` when the report is scoreable or absent.

    Callers should invoke this alongside ``_score_audit`` to attach observability
    metadata when ``audit_score`` is ``None`` or ``0.0`` despite a parseable JSON
    artifact.  Possible return values:

    - ``'no_findings_key'``         — neither "findings" nor "issues" key present
    - ``'empty_findings_list'``     — findings list exists but is empty
    - ``'audit_score_null_no_findings'`` — findings empty AND no self-reported score
    - ``'parseable_but_unscoreable'`` — findings list has entries but none are dicts
      with a severity/level field, so no deductions were applied
    """
    if not isinstance(report, dict):
        return None

    findings = report.get("findings")
    if not isinstance(findings, list):
        findings = report.get("issues")

    if findings is None:
        return "no_findings_key"

    if not isinstance(findings, list) or len(findings) == 0:
        raw_audit = report.get("audit_score")
        has_score = isinstance(raw_audit, (int, float)) and not isinstance(
            raw_audit, bool
        )
        if not has_score:
            return "audit_score_null_no_findings"
        return "empty_findings_list"

    # findings is a non-empty list — check whether any entry had a usable severity
    scoreable = any(
        isinstance(f, dict) and (f.get("severity") or f.get("level")) for f in findings
    )
    if not scoreable:
        return "parseable_but_unscoreable"

    # Detect the case where every higher-severity finding was rejected by the
    # verification gate.  When this fires, _score_audit returns 1.0 (no
    # deductions applied) which is indistinguishable from a clean audit — this
    # reason code makes the gap visible to operators via the verdict row.
    higher_sev_findings = [
        f
        for f in findings
        if isinstance(f, dict)
        and str(f.get("severity") or f.get("level") or "").lower()
        in _SEVERITIES_REQUIRING_VERIFICATION
    ]
    if higher_sev_findings and all(
        not _finding_is_verified(
            f, str(f.get("severity") or f.get("level") or "").lower()
        )
        for f in higher_sev_findings
    ):
        return "all_findings_unverified"

    return None


# Cap on findings carried into last_qualification (and thus the strategic
# agent's payload). Kept small so the version_phase block stays well under the
# pack budget while still giving the agent enough to target the audit sub-score.
_MAX_CARRIED_FINDINGS = 12
_FINDING_SUMMARY_MAX = 280
# Maximum binding-record entries persisted alongside audit_reconciliation.
_BINDING_RECORD_CAP = 20
# Minimum fraction of shared significant tokens (length >= 3) required for a
# fuzzy summary match between an audit finding and a challenge verdict when the
# challenger derives findings independently (no primary_findings in payload).
_FINDING_MATCH_TOKEN_THRESHOLD = 0.6
# Lower number sorts first (most severe). Unknown severities sort last.
# Order must match _AUDIT_DEDUCTIONS severity weighting:
# - critical/blocker (0.30 deduction) = rank 0
# - high (0.15 deduction) = rank 1
# - medium/moderate/warning (0.07 deduction) = rank 2
# - low/info (0.02 deduction) = rank 3
_SEVERITY_RANK = {
    "critical": 0,
    "blocker": 0,
    "high": 1,
    "medium": 2,
    "moderate": 2,
    "warning": 2,
    "low": 3,
    "info": 3,
}

_CHALLENGE_FINDING_MAX = 12
_CHALLENGE_ASSESSMENT_MAX = 600
_CHALLENGE_SEVERITIES: frozenset[str] = frozenset({"low", "medium", "high"})
_CHALLENGE_AGREEMENTS: frozenset[str] = frozenset({"agree", "partial", "disagree"})


def _raw_audit_findings(report: dict[str, Any] | None) -> list[Any]:
    if not isinstance(report, dict):
        return []
    raw = report.get("findings")
    if not isinstance(raw, list) or not raw:
        raw = report.get("issues")
    return raw if isinstance(raw, list) else []


def _compact_challenge_finding(finding: Any) -> dict[str, str] | None:
    if isinstance(finding, dict):
        sev = str(finding.get("severity") or finding.get("level") or "unknown").lower()
        summary = (
            finding.get("summary")
            or finding.get("description")
            or finding.get("title")
            or finding.get("text")
            or ""
        )
        file_path = str(finding.get("file") or finding.get("path") or "").strip()
        note = str(finding.get("verification_note") or "").strip()
        origin = str(finding.get("origin") or "").strip()
    else:
        sev = "unknown"
        summary = str(finding)
        file_path = ""
        note = ""
        origin = ""

    summary = str(summary).strip()[:_FINDING_SUMMARY_MAX]
    if not summary:
        return None

    compact = {"severity": sev, "summary": summary}
    if file_path:
        compact["file"] = file_path[:_DETAIL_NAME_MAX]
    if note:
        compact["verification_note"] = note[:_FINDING_SUMMARY_MAX]
    if origin:
        compact["origin"] = origin[:80]
    return compact


def _compact_challenge_findings(
    findings: list[Any],
) -> tuple[list[dict[str, str]], bool]:
    compacted = [
        compact
        for finding in findings
        if (compact := _compact_challenge_finding(finding)) is not None
    ]
    compacted.sort(key=lambda d: _SEVERITY_RANK.get(d["severity"], 3))
    truncated = len(compacted) > _CHALLENGE_FINDING_MAX
    return compacted[:_CHALLENGE_FINDING_MAX], truncated


def _finding_match_key(finding: dict[str, str]) -> tuple[str, str]:
    return (
        str(finding.get("file") or "").strip().lower(),
        str(finding.get("summary") or "").strip().lower(),
    )


def _audit_finding_key(summary: str, file: str | None) -> str:
    """Deterministic content key for a carried audit finding.

    Mirrors the finding-hash convention used elsewhere
    (``finding_deduplication.finding_hash`` → sha256 truncated to 16 hex) and
    the ``_finding_match_key`` composition (normalized ``file`` + ``summary``).
    The same finding content yields the same key across re-extractions so a
    resolution ledger can durably key findings; a different ``file`` yields a
    different key.  Computed from the already-normalized/truncated ``summary``
    and ``file`` the carried payload stores, so the key is stable for what the
    consumer actually sees.
    """
    norm_file = str(file or "").strip().lower()
    norm_summary = str(summary or "").strip().lower()
    return hashlib.sha256(f"{norm_file}|{norm_summary}".encode()).hexdigest()[:16]


def _compute_audit_challenge_synthesis(
    primary_report: dict[str, Any] | None,
    challenge_report: dict[str, Any] | None,
    *,
    challenge_reason: str | None = None,
) -> dict[str, Any]:
    """Compute non-gating release-audit challenge synthesis."""
    if challenge_reason:
        return {
            "status": "unavailable",
            "reason": challenge_reason,
            "reported_agreement": None,
            "agreement_assessment": None,
            "challenge_severity": None,
            "agreement": {"findings_both_flagged": [], "count": 0},
            "disagreement": {
                "primary_only": [],
                "challenger_only": [],
                "overstated_findings": [],
            },
            "counts": {
                "primary_findings": len(
                    _compact_challenge_findings(_raw_audit_findings(primary_report))[0]
                ),
                "challenger_missing": 0,
                "challenger_overstated": 0,
            },
            "challenge_findings_truncated": False,
        }
    if not isinstance(challenge_report, dict):
        return _compute_audit_challenge_synthesis(
            primary_report, None, challenge_reason="challenge_absent"
        )

    primary_findings, primary_truncated = _compact_challenge_findings(
        _raw_audit_findings(primary_report)
    )
    missing_raw = challenge_report.get("missing_findings")
    overstated_raw = challenge_report.get("overstated_findings")
    missing_findings, missing_truncated = _compact_challenge_findings(
        missing_raw if isinstance(missing_raw, list) else []
    )
    overstated_findings, overstated_truncated = _compact_challenge_findings(
        overstated_raw if isinstance(overstated_raw, list) else []
    )

    primary_keys = {_finding_match_key(finding) for finding in primary_findings}
    missing_keys = {_finding_match_key(finding) for finding in missing_findings}
    overstated_keys = {_finding_match_key(finding) for finding in overstated_findings}

    reported_agreement_raw = str(challenge_report.get("agreement") or "").lower()
    reported_agreement = (
        reported_agreement_raw
        if reported_agreement_raw in _CHALLENGE_AGREEMENTS
        else None
    )
    severity_raw = str(challenge_report.get("challenge_severity") or "").lower()
    challenge_severity = (
        severity_raw if severity_raw in _CHALLENGE_SEVERITIES else "low"
    )
    assessment_raw = challenge_report.get("agreement_assessment")
    agreement_assessment = (
        str(assessment_raw).strip()[:_CHALLENGE_ASSESSMENT_MAX]
        if assessment_raw is not None
        else ""
    )

    if (
        reported_agreement == "agree"
        and not missing_findings
        and not overstated_findings
    ):
        both_flagged = primary_findings
        primary_only: list[dict[str, str]] = []
    else:
        both_flagged = [
            finding
            for finding in primary_findings
            if _finding_match_key(finding) in missing_keys
        ]
        primary_only = [
            finding
            for finding in primary_findings
            if _finding_match_key(finding) not in missing_keys | overstated_keys
        ]

    challenger_only = [
        finding
        for finding in missing_findings
        if _finding_match_key(finding) not in primary_keys
    ]

    return {
        "status": "available",
        "reason": None,
        "reported_agreement": reported_agreement,
        "agreement_assessment": agreement_assessment,
        "challenge_severity": challenge_severity,
        "agreement": {
            "findings_both_flagged": both_flagged,
            "count": len(both_flagged),
        },
        "disagreement": {
            "primary_only": primary_only,
            "challenger_only": challenger_only,
            "overstated_findings": overstated_findings,
        },
        "counts": {
            "primary_findings": len(primary_findings),
            "challenger_missing": len(missing_findings),
            "challenger_overstated": len(overstated_findings),
        },
        "challenge_findings_truncated": primary_truncated
        or missing_truncated
        or overstated_truncated,
    }


def _compute_challenge_synthesis_from_binding_verdicts(
    primary_report: dict[str, Any] | None,
    challenge_verdicts: dict[str, Any] | None,
    *,
    candidate_sha: str | None = None,
    challenge_reason: str | None = None,
) -> dict[str, Any]:
    """Compute audit-challenge synthesis from the binding RELEASE_AUDIT_CHALLENGE artifact.

    Unlike _compute_audit_challenge_synthesis (advisory format), this function
    derives its synthesis from the binding verdict artifact emitted by the live
    audit-challenge handler (challenged_candidate_sha + verdicts[]).
    """
    if challenge_reason or not isinstance(challenge_verdicts, dict):
        return {
            "status": "unavailable",
            "reason": challenge_reason or "challenge_absent",
            "binding": True,
            "confirmed_count": 0,
            "refuted_count": 0,
            "uncertain_count": 0,
            "reported_agreement": None,
            "challenged_candidate_sha": None,
        }
    challenged_sha = str(
        challenge_verdicts.get("challenged_candidate_sha") or ""
    ).strip()
    if candidate_sha and challenged_sha and challenged_sha != candidate_sha:
        return {
            "status": "unavailable",
            "reason": "audit_challenge_sha_mismatch",
            "binding": True,
            "confirmed_count": 0,
            "refuted_count": 0,
            "uncertain_count": 0,
            "reported_agreement": None,
            "challenged_candidate_sha": challenged_sha or None,
        }
    if candidate_sha and not challenged_sha:
        challenged_sha = candidate_sha
    verdicts_list = challenge_verdicts.get("verdicts")
    if not isinstance(verdicts_list, list):
        return {
            "status": "unavailable",
            "reason": "challenge_absent",
            "binding": True,
            "confirmed_count": 0,
            "refuted_count": 0,
            "uncertain_count": 0,
            "reported_agreement": None,
            "challenged_candidate_sha": challenged_sha or None,
        }
    confirmed = sum(
        1
        for v in verdicts_list
        if isinstance(v, dict) and str(v.get("verdict") or "").lower() == "confirmed"
    )
    refuted = sum(
        1
        for v in verdicts_list
        if isinstance(v, dict)
        and str(v.get("verdict") or "").lower() == "refuted_false_positive"
    )
    uncertain = sum(
        1
        for v in verdicts_list
        if isinstance(v, dict) and str(v.get("verdict") or "").lower() == "uncertain"
    )
    total = confirmed + refuted + uncertain
    conclusive = confirmed + refuted
    if total == 0:
        reported_agreement = None
    elif uncertain > 0:
        reported_agreement = "partial"
    elif refuted == 0 and confirmed > 0:
        reported_agreement = "agree"
    elif conclusive > 0 and refuted > conclusive / 2:
        reported_agreement = "disagree"
    else:
        reported_agreement = "partial"
    return {
        "status": "available",
        "binding": True,
        "reason": None,
        "confirmed_count": confirmed,
        "refuted_count": refuted,
        "uncertain_count": uncertain,
        "reported_agreement": reported_agreement,
        "challenged_candidate_sha": challenged_sha or None,
    }


def _extract_audit_findings(
    report: dict[str, Any] | None,
    *,
    candidate_surface_paths: dict[str, str] | None = None,
) -> list[dict[str, str]]:
    """Pull a compact, severity-sorted findings list from a RELEASE_AUDIT_REPORT.

    The audit sub-score is usually the binding constraint on promotion, but the
    composite only records the *number*, not WHAT to fix.  ``_score_audit``
    discards the findings after scoring, leaving the strategic agent blind to
    its own audit feedback — so it does generic stabilize cleanup instead of
    targeting the actual blockers.  This carries the findings (severity + short
    summary) into ``last_qualification`` so they reach the agent's payload via
    ``release_version.to_phase_block``.

    Returns ``[]`` when the report has no usable findings.  Capped at
    ``_MAX_CARRIED_FINDINGS`` and each summary truncated to keep the payload
    small; ordered most-severe first.
    """
    if not isinstance(report, dict):
        return []
    raw = report.get("findings")
    if not isinstance(raw, list) or not raw:
        # Some audits key the list as "issues".
        raw = report.get("issues")
    if not isinstance(raw, list):
        return []

    out: list[dict[str, str]] = []
    for f in raw:
        if candidate_surface_paths is not None and not _finding_matches_candidate_surface(
            f, candidate_surface_paths
        ):
            continue
        file_path = ""
        if isinstance(f, dict):
            sev = str(f.get("severity") or f.get("level") or "unknown").lower()
            if not _finding_is_verified(f, sev):
                # Mirror the scoring gate: unsubstantiated medium/high/critical
                # findings do not affect the audit sub-score and must not be
                # carried as actionable blockers.
                continue
            summary = (
                f.get("summary")
                or f.get("description")
                or f.get("title")
                or f.get("text")
                or ""
            )
            file_path = str(f.get("file") or f.get("path") or "").strip()
        else:
            sev = "unknown"
            summary = str(f)
        summary = str(summary).strip()[:_FINDING_SUMMARY_MAX]
        if summary:
            item: dict[str, str] = {"severity": sev, "summary": summary}
            if file_path:
                item["file"] = file_path[:_DETAIL_NAME_MAX]
            blob_sha = (
                str(f.get("blob_sha") or "").strip() if isinstance(f, dict) else ""
            )
            if blob_sha:
                item["blob_sha"] = blob_sha[:64]
                item["surface_key"] = hashlib.sha256(
                    f"{file_path.lower()}|{blob_sha}".encode()
                ).hexdigest()[:16]
            # Deterministic content key so the same finding is stably
            # identifiable across re-extractions (resolution-ledger keying).
            item["key"] = _audit_finding_key(summary, item.get("file"))
            out.append(item)

    out.sort(key=lambda d: _SEVERITY_RANK.get(d["severity"], 3))
    return out[:_MAX_CARRIED_FINDINGS]


def _extract_excluded_audit_findings(
    report: dict[str, Any] | None,
    *,
    candidate_surface_paths: dict[str, str] | None = None,
) -> list[dict[str, str]]:
    """Pull the higher-severity findings the verification gate EXCLUDED from scoring.

    The inverse of ``_extract_audit_findings``: that function carries only
    *verified* findings (those that affected the audit sub-score), deliberately
    dropping higher-severity findings whose ``verification_note`` was absent or
    too short.  That suppression is correct for scoring, but it leaves the
    strategic agent looking at a near-perfect audit sub-score and an empty
    findings list even when the audit reported several critical/high findings —
    so the agent makes a "blanket audit assumption" and treats the dimension as
    clean.

    This carries those excluded findings into ``last_qualification`` for DISPLAY
    ONLY, each marked ``unverified="true"`` so the agent surfaces the omitted
    risk and can choose to investigate it — never treating them as confirmed
    blockers.  Only severities in ``_SEVERITIES_REQUIRING_VERIFICATION`` can be
    excluded (low/info are always counted), so the list is inherently
    higher-severity.

    Returns ``[]`` when the report has no excluded findings (all verified, only
    low-severity, or no usable report).  Same cap, sort, and truncation as
    ``_extract_audit_findings`` so the agent renderer stays uniform.
    """
    if not isinstance(report, dict):
        return []
    raw = report.get("findings")
    if not isinstance(raw, list) or not raw:
        raw = report.get("issues")
    if not isinstance(raw, list):
        return []

    out: list[dict[str, str]] = []
    for f in raw:
        if not isinstance(f, dict):
            continue
        sev = str(f.get("severity") or f.get("level") or "unknown").lower()
        stale = not _finding_matches_candidate_surface(f, candidate_surface_paths)
        # Only higher-severity findings that the gate dropped for lacking a
        # substantive verification_note belong here. Verified findings (and
        # low/info, which never require verification) are carried by
        # _extract_audit_findings instead.
        if not stale and sev not in _SEVERITIES_REQUIRING_VERIFICATION:
            continue
        if not stale and _finding_is_verified(f, sev):
            continue
        summary = (
            f.get("summary")
            or f.get("description")
            or f.get("title")
            or f.get("text")
            or ""
        )
        file_path = str(f.get("file") or f.get("path") or "").strip()
        summary = str(summary).strip()[:_FINDING_SUMMARY_MAX]
        if summary:
            item: dict[str, str] = {
                "severity": sev,
                "summary": summary,
                "unverified": "true",
            }
            if stale:
                item["reason"] = "stale_blob_not_in_candidate"
            if file_path:
                item["file"] = file_path[:_DETAIL_NAME_MAX]
            # Stamp blob identity (as _extract_audit_findings does) so a later
            # incremental merge can evaluate staleness for THIS list too.
            blob_sha = str(f.get("blob_sha") or "").strip()
            if blob_sha:
                item["blob_sha"] = blob_sha[:64]
                item["surface_key"] = hashlib.sha256(
                    f"{file_path.lower()}|{blob_sha}".encode()
                ).hexdigest()[:16]
            item["key"] = _audit_finding_key(summary, item.get("file"))
            out.append(item)

    out.sort(key=lambda d: _SEVERITY_RANK.get(d["severity"], 3))
    return out[:_MAX_CARRIED_FINDINGS]


def _audit_findings_carry_meta(
    report: dict[str, Any] | None,
    *,
    candidate_surface_paths: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Pre-cap totals + truncation flags for the two carried-findings lists.

    ``_extract_audit_findings`` / ``_extract_excluded_audit_findings`` cap at
    ``_MAX_CARRIED_FINDINGS`` with no in-band signal that entries were dropped —
    the v10 verdict carried exactly 12 findings (at the cap) so consumers could
    not tell whether the list was complete.  This returns the pre-cap count and
    an explicit truncated flag for each list, computed with the SAME filter
    predicates the extract functions use so the totals agree with the capped
    lists those functions return for the same report.
    """
    raw = _raw_audit_findings(report)

    verified_total = 0
    excluded_total = 0
    for f in raw:
        if isinstance(f, dict):
            sev = str(f.get("severity") or f.get("level") or "unknown").lower()
            summary = (
                f.get("summary")
                or f.get("description")
                or f.get("title")
                or f.get("text")
                or ""
            )
        else:
            sev = "unknown"
            summary = str(f)
        summary = str(summary).strip()[:_FINDING_SUMMARY_MAX]
        if not summary:
            continue
        if not _finding_matches_candidate_surface(f, candidate_surface_paths):
            excluded_total += 1
            continue
        # Verified list: mirror _extract_audit_findings' scoring gate.
        if isinstance(f, dict) and not _finding_is_verified(f, sev):
            # Excluded list: only higher-severity findings the gate dropped.
            if sev in _SEVERITIES_REQUIRING_VERIFICATION:
                excluded_total += 1
        else:
            verified_total += 1

    return {
        "audit_findings_total": verified_total,
        "audit_findings_truncated": verified_total > _MAX_CARRIED_FINDINGS,
        "excluded_audit_findings_total": excluded_total,
        "excluded_audit_findings_truncated": excluded_total > _MAX_CARRIED_FINDINGS,
    }


# Caps on the test-erosion detail carried into last_qualification.
# Kept small (mirroring _MAX_CARRIED_FINDINGS) so the version_phase block stays
# under the strategic pack budget while still naming the specific blocker.
_MAX_DELETED_TEST_FILES = 8
_DETAIL_NAME_MAX = 200


def _extract_test_erosion_detail(
    report: dict[str, Any] | None,
) -> dict[str, Any] | None:
    """Pull a test-erosion detail from a RELEASE_AUDIT_REPORT.

    Stores ALL non-zero numerical deltas (positive and negative) so the detail
    is lossless for cumulative reconstruction in scoped audit merging.  Also
    captures ``deleted_test_files`` when present.

    ``deleted_test_files`` is capped at ``_MAX_DELETED_TEST_FILES``.
    Returns ``None`` when nothing is present (no deletions and all deltas
    are zero).
    """
    if not isinstance(report, dict):
        return None

    computed_erosion = report.get("computed_test_erosion")
    erosion = (
        computed_erosion
        if isinstance(computed_erosion, dict)
        else report.get("test_erosion")
    )
    if not isinstance(erosion, dict):
        return None

    detail: dict[str, Any] = {}

    raw_deleted = erosion.get("deleted_test_files")
    if isinstance(raw_deleted, list):
        deleted = [
            str(p).strip()[:_DETAIL_NAME_MAX] for p in raw_deleted if str(p).strip()
        ]
        if deleted:
            detail["deleted_test_files"] = deleted[:_MAX_DELETED_TEST_FILES]
            if len(deleted) > _MAX_DELETED_TEST_FILES:
                detail["deleted_test_files_truncated"] = (
                    len(deleted) - _MAX_DELETED_TEST_FILES
                )

    test_delta = erosion.get("test_count_delta")
    if (
        isinstance(test_delta, (int, float))
        and not isinstance(test_delta, bool)
        and test_delta != 0
    ):
        detail["test_count_delta"] = (
            int(test_delta) if float(test_delta).is_integer() else test_delta
        )

    assertion_delta = erosion.get("assertion_delta")
    if (
        isinstance(assertion_delta, (int, float))
        and not isinstance(assertion_delta, bool)
        and assertion_delta != 0
    ):
        detail["assertion_delta"] = (
            int(assertion_delta)
            if float(assertion_delta).is_integer()
            else assertion_delta
        )

    return detail or None


def _fmt_sub_scores(
    sub_scores: dict[str, Any] | None,
    *,
    smoketest_enabled: bool = True,
) -> str:
    """Render the active audit and test-erosion scores for messages.

    ``smoketest_enabled`` remains in the signature for old callers but is
    intentionally ignored. Archived smoke scores never appear in a current
    qualification summary.
    """
    if not sub_scores:
        return "n/a"
    return " · ".join(
        [
            f"audit {float(sub_scores.get('audit', 0.0)):.2f}",
            f"test {float(sub_scores.get('test_erosion', 0.0)):.2f}",
        ]
    )

# ---------------------------------------------------------------------------
# Challenge-verdict reconciliation — fail-closed audit challenge gate.
# ---------------------------------------------------------------------------


def _finding_summary_tokens(text: str) -> frozenset[str]:
    """Return significant lowercase word tokens (length >= 3) from a summary.

    Used as the fallback for fuzzy matching between independently-derived audit
    findings and challenge verdicts whose free-text summaries differ in wording
    while describing the same issue.
    """
    return frozenset(
        tok for tok in re.findall(r"[a-z0-9]+", text.lower()) if len(tok) >= 3
    )


def _finding_summary_for_binding(finding: dict) -> str:
    return str(
        finding.get("summary")
        or finding.get("description")
        or finding.get("title")
        or finding.get("text")
        or ""
    ).strip()[:_FINDING_SUMMARY_MAX]


def _score_finding_to_challenge(finding: dict, verdict_entry: dict) -> float | None:
    """Score a candidate verdict against an audit finding, or None if no match.

    The scale is intentionally two-tiered:

    * **2.0** — exact summary text match.  This tier beats any fuzzy score so
      that best-match assignment always prefers an exact match over a fuzzy one
      when a verdict could plausibly match multiple findings.
    * **(0.0, 1.0]** — token-overlap fuzzy match.  Confirmed/uncertain verdicts
      use the significant-token min-denominator (original behaviour, unchanged
      threshold).  Refuted false positives use a stricter Jaccard-style
      denominator so a short subset summary cannot over-bind to a broader
      finding.  This still covers the common case where the challenge runs in
      parallel without ``primary_findings`` and independently re-describes the
      same issue.
    * ``None`` — no match (file-path conflict or similarity below threshold).

    Confirmed/uncertain verdict severity is deliberately NOT used for scoring:
    the primary audit and the challenger may independently re-bucket the same
    issue at different severities, so content must win even when the labels
    drift.  Refuted false positives are different because they remove a finding
    from scoring; those verdicts must be at least as severe as the finding they
    bind to.
    """
    if not isinstance(verdict_entry, dict):
        return None
    f_summary = _finding_summary_for_binding(finding)
    f_file = str(finding.get("file") or finding.get("file_path") or "").strip()
    cv_summary = str(verdict_entry.get("summary") or "").strip()
    cv_file = str(verdict_entry.get("file") or "").strip()

    if f_file and cv_file and f_file != cv_file:
        return None

    if (
        str(verdict_entry.get("verdict") or "").lower().strip()
        == "refuted_false_positive"
    ):
        f_severity = str(finding.get("severity") or finding.get("level") or "").lower()
        cv_severity = str(verdict_entry.get("severity") or "").lower()
        finding_rank = _SEVERITY_RANK.get(f_severity, 99)
        verdict_rank = _SEVERITY_RANK.get(cv_severity, 99)
        if verdict_rank > finding_rank:
            return None

    # Tier-1: exact summary match always beats any fuzzy score.
    if cv_summary == f_summary:
        return 2.0

    # Tier-2: significant-token fuzzy match.
    # refuted_false_positive uses Jaccard (union denominator) so a subset summary
    # cannot score 1.0 against every finding that contains its tokens — it only
    # matches a finding whose token set is close in size to the verdict's.
    # confirmed/uncertain keep the original min-denominator behaviour.
    f_tokens = _finding_summary_tokens(f_summary)
    cv_tokens = _finding_summary_tokens(cv_summary)
    if f_tokens and cv_tokens:
        overlap = len(f_tokens & cv_tokens)
        is_refutation = (
            str(verdict_entry.get("verdict") or "").lower().strip()
            == "refuted_false_positive"
        )
        if is_refutation:
            union_size = len(f_tokens | cv_tokens)
            if (
                union_size > 0
                and overlap / union_size >= _FINDING_MATCH_TOKEN_THRESHOLD
            ):
                return overlap / union_size
        else:
            min_size = min(len(f_tokens), len(cv_tokens))
            if min_size > 0 and overlap / min_size >= _FINDING_MATCH_TOKEN_THRESHOLD:
                return overlap / min_size

    return None


def _primary_finding_index_for_verdict(verdict_entry: dict) -> int | None:
    """Return a valid 1-based primary finding index from a challenge verdict."""
    if not isinstance(verdict_entry, dict):
        return None
    raw = verdict_entry.get("primary_finding_index")
    if isinstance(raw, bool) or not isinstance(raw, int):
        return None
    return raw if raw > 0 else None


def _primary_finding_index_resolves(
    verdict_entry: dict,
    primary_order_fis: list[int],
) -> bool:
    index = _primary_finding_index_for_verdict(verdict_entry)
    return index is not None and index <= len(primary_order_fis)


def _indexed_verdict_can_bind(finding: dict, verdict_entry: dict) -> bool:
    """Validate non-text safety checks for an index-bound challenge verdict."""
    if not isinstance(verdict_entry, dict):
        return False
    if (
        str(verdict_entry.get("verdict") or "").lower().strip()
        != "refuted_false_positive"
    ):
        return True

    f_severity = str(finding.get("severity") or finding.get("level") or "").lower()
    cv_severity = str(verdict_entry.get("severity") or "").lower()
    finding_rank = _SEVERITY_RANK.get(f_severity, 99)
    verdict_rank = _SEVERITY_RANK.get(cv_severity, 99)
    return verdict_rank <= finding_rank


def _reconcile_audit_with_challenge(
    report: dict[str, Any] | None,
    challenge_envelope: dict[str, Any] | None,
    candidate_sha: str | None,
) -> tuple[
    str | None, dict[str, Any] | None, list[dict[str, Any]], list[dict[str, Any]]
]:
    """Reconcile verified audit findings with challenge verdicts (fail-closed)."""
    if not isinstance(report, dict):
        return None, report, [], []

    findings = report.get("findings")
    if not isinstance(findings, list):
        findings = report.get("issues")
    if not isinstance(findings, list):
        return None, report, [], []

    deductible: list[tuple[int, dict]] = []
    for idx, f in enumerate(findings):
        if not isinstance(f, dict):
            continue
        sev = str(f.get("severity") or f.get("level") or "").lower()
        if sev not in _SEVERITIES_REQUIRING_VERIFICATION:
            continue
        if not _finding_is_verified(f, sev):
            continue
        if _finding_subject_is_authorized_divergence(f):
            continue
        if not _finding_summary_for_binding(f):
            continue
        deductible.append((idx, f))

    if not deductible:
        # Even with no primary deductible findings, scan challenger verdicts for
        # independently-confirmed >=HIGH severity issues the primary audit missed.
        if isinstance(challenge_envelope, dict):
            _challenged_sha = str(
                challenge_envelope.get("challenged_candidate_sha") or ""
            ).strip()
            if candidate_sha and _challenged_sha == candidate_sha:
                _raw = challenge_envelope.get("verdicts")
                if isinstance(_raw, list):
                    for _cv in _raw:
                        if not isinstance(_cv, dict):
                            continue
                        if str(_cv.get("verdict") or "").lower().strip() != "confirmed":
                            continue
                        _sev = str(_cv.get("severity") or "").lower().strip()
                        _rank = _SEVERITY_RANK.get(_sev, 99)
                        if _rank != 99 and _rank <= 1:
                            return (
                                "audit_challenge_independent_blocker",
                                None,
                                [],
                                [
                                    {
                                        "severity": _sev,
                                        "summary": str(
                                            _cv.get("summary") or ""
                                        ).strip()[:_FINDING_SUMMARY_MAX],
                                        "file": str(_cv.get("file") or "").strip(),
                                        "reason": str(_cv.get("reason") or "").strip()[
                                            :_FINDING_SUMMARY_MAX
                                        ],
                                        "source": "challenger_independent",
                                    }
                                ],
                            )
        return None, report, [], []

    challenge_verdicts_list: list = []
    sha_matched = False
    if isinstance(challenge_envelope, dict):
        challenged_sha = str(
            challenge_envelope.get("challenged_candidate_sha") or ""
        ).strip()
        if candidate_sha and challenged_sha == candidate_sha:
            sha_matched = True
            raw = challenge_envelope.get("verdicts")
            if isinstance(raw, list):
                challenge_verdicts_list = raw

    if not sha_matched or not challenge_verdicts_list:
        if sha_matched and isinstance(challenge_envelope.get("verdicts"), list):
            hold_code = "audit_challenge_empty_verdicts"
        else:
            hold_code = (
                "audit_challenge_sha_mismatch"
                if (
                    isinstance(challenge_envelope, dict)
                    and challenge_envelope.get("challenged_candidate_sha")
                )
                else "audit_challenge_missing"
            )
        return hold_code, None, [], []

    challenger_refuted: list[dict[str, Any]] = []
    remove_idxs: set[int] = set()
    available_verdicts = list(challenge_verdicts_list)
    # Only include findings whose summary is non-empty in the indexed list.
    # _primary_findings_for_challenge_retry (release_qualifier.py) skips
    # findings with an empty summary before numbering them for the agent, so the
    # index domain here must match that filtered subset exactly.
    primary_order_fis = sorted(
        [fi for fi, (_, f) in enumerate(deductible) if _finding_summary_for_binding(f)],
        key=lambda fi: _SEVERITY_RANK.get(
            str(
                deductible[fi][1].get("severity")
                or deductible[fi][1].get("level")
                or ""
            ).lower(),
            99,
        ),
    )

    # Retry challenges receive the primary audit's carried findings as a
    # numbered list.  When a verdict carries a valid primary_finding_index, bind
    # by that stable identity first and bypass fuzzy summary matching entirely.
    # Refutations still keep the severity ceiling because they remove audit
    # deductions.
    match_vi: dict[int, int] = {}  # vi -> fi currently matched
    indexed_vi: set[int] = set()  # vi values bound via primary_finding_index
    for vi, cv in enumerate(available_verdicts):
        index = _primary_finding_index_for_verdict(cv)
        if index is None or index > len(primary_order_fis):
            continue
        fi = primary_order_fis[index - 1]
        if fi in match_vi.values() or vi in match_vi:
            continue
        _, f = deductible[fi]
        if _indexed_verdict_can_bind(f, cv):
            match_vi[vi] = fi
            indexed_vi.add(vi)

    # Build per-finding adjacency lists sorted by score descending.
    # Add a small severity-agreement bonus so a verdict prefers the finding
    # whose severity label matches its own over one that only scores the same
    # via text overlap alone.
    adj: list[list[int]] = [[] for _ in range(len(deductible))]
    for fi, (_, f) in enumerate(deductible):
        if fi in match_vi.values():
            continue
        f_sev = str(f.get("severity") or f.get("level") or "").lower()
        pairs: list[tuple[float, int]] = []
        for vi, cv in enumerate(available_verdicts):
            if vi in match_vi or _primary_finding_index_resolves(cv, primary_order_fis):
                continue
            s = _score_finding_to_challenge(f, cv)
            if s is not None:
                cv_sev = str(cv.get("severity") or "").lower()
                bonus = 0.001 if cv_sev and cv_sev == f_sev else 0.0
                pairs.append((s + bonus, vi))
        pairs.sort(key=lambda x: -x[0])
        adj[fi] = [vi for _, vi in pairs]

    # Maximum-cardinality bipartite matching via DFS augmenting paths.
    # This finds a complete assignment whenever one exists, unlike greedy
    # sort-and-assign which can strand a finding when a high-scoring verdict
    # is shared between two findings.
    def _try_augment(fi: int, visited: set[int]) -> bool:
        for vi in adj[fi]:
            if vi in visited:
                continue
            visited.add(vi)
            if vi not in match_vi or _try_augment(match_vi[vi], visited):
                match_vi[vi] = fi
                return True
        return False

    for fi in range(len(deductible)):
        _try_augment(fi, set())

    assignment: dict[int, int] = {fi: vi for vi, fi in match_vi.items()}

    # All deductible findings must have exactly one verdict (fail-closed).
    # Distinguish verdicts-present-but-unbindable from artifact-absent.
    if len(assignment) < len(deductible):
        return "audit_challenge_binding_incomplete", None, [], []

    # Validate refutation bindings: the augmenting-path DFS maximises cardinality
    # but not match quality — a later finding can steal an exact refuted_false_positive
    # verdict from the finding it best matches, leaving a worse-matched finding bound
    # to the refutation.  Because refuted verdicts remove findings from audit scoring,
    # an ambiguous refutation binding can suppress a valid high-severity deduction.
    # Fail closed: if any refuted verdict was assigned to a finding that is NOT its
    # best-scoring match (i.e. a higher-scoring finding exists and was assigned
    # elsewhere), the challenge output is ambiguous — hold rather than risk a wrong
    # finding removal.
    for vi, fi in match_vi.items():
        cv = available_verdicts[vi]
        if str(cv.get("verdict") or "").lower().strip() != "refuted_false_positive":
            continue
        if _primary_finding_index_resolves(cv, primary_order_fis):
            continue
        _, f = deductible[fi]
        bound_score = _score_finding_to_challenge(f, cv) or 0.0
        for other_fi, (_, other_f) in enumerate(deductible):
            if other_fi == fi:
                continue
            other_score = _score_finding_to_challenge(other_f, cv)
            if other_score is not None and other_score > bound_score:
                return "audit_challenge_uncertain", None, [], []

    binding_record: list[dict[str, Any]] = []
    for fi, (idx, f) in enumerate(deductible):
        matched = available_verdicts[assignment[fi]]
        verdict_val = str(matched.get("verdict") or "").lower().strip()
        if verdict_val == "confirmed":
            pass
        elif verdict_val == "refuted_false_positive":
            reason = str(matched.get("reason") or "")
            if sum(1 for c in reason if not c.isspace()) < 20:
                return "audit_challenge_uncertain", None, [], []
            remove_idxs.add(idx)
            sev = str(f.get("severity") or f.get("level") or "").lower()
            summary = _finding_summary_for_binding(f)
            challenger_refuted.append(
                {
                    "severity": sev,
                    "summary": summary,
                    "file": str(f.get("file") or "").strip(),
                    "challenger_reason": str(matched.get("reason") or "").strip()[
                        :_FINDING_SUMMARY_MAX
                    ],
                }
            )
        else:
            return "audit_challenge_uncertain", None, [], []
        if len(binding_record) < _BINDING_RECORD_CAP:
            vi = assignment[fi]
            binding_record.append(
                {
                    "finding_summary": _finding_summary_for_binding(f),
                    "finding_severity": str(
                        f.get("severity") or f.get("level") or ""
                    ).lower(),
                    "finding_file": str(f.get("file") or "").strip(),
                    "verdict": verdict_val,
                    "verdict_summary": str(matched.get("summary") or "").strip()[
                        :_FINDING_SUMMARY_MAX
                    ],
                    "binding_method": "indexed" if vi in indexed_vi else "fuzzy",
                }
            )

    # Scan unmatched challenger verdicts for independent confirmed blockers.
    # The challenger may identify >=HIGH issues the primary audit missed; these
    # must gate qualification even though no primary finding was matched to them.
    for vi, cv in enumerate(available_verdicts):
        if vi in match_vi:
            continue
        if str(cv.get("verdict") or "").lower().strip() != "confirmed":
            continue
        sev = str(cv.get("severity") or "").lower().strip()
        rank = _SEVERITY_RANK.get(sev, 99)
        if rank != 99 and rank <= 1:
            return (
                "audit_challenge_independent_blocker",
                None,
                [],
                [
                    {
                        "severity": sev,
                        "summary": str(cv.get("summary") or "").strip()[
                            :_FINDING_SUMMARY_MAX
                        ],
                        "file": str(cv.get("file") or "").strip(),
                        "reason": str(cv.get("reason") or "").strip()[
                            :_FINDING_SUMMARY_MAX
                        ],
                        "source": "challenger_independent",
                    }
                ],
            )

    if not remove_idxs:
        return None, report, [], binding_record

    modified_findings = [f for i, f in enumerate(findings) if i not in remove_idxs]
    modified_report = dict(report)
    key = "findings" if isinstance(report.get("findings"), list) else "issues"
    modified_report[key] = modified_findings
    return None, modified_report, challenger_refuted, binding_record


# Minimum sample size for each cohort before telemetry rates are computed.
# Below this the cohort is marked insufficient_sample to avoid misleading zeros.
_MIN_TELEMETRY_SAMPLE = 3


def compute_challenged_vs_unchallenged_telemetry(
    verdicts: list[dict[str, Any]],
) -> dict[str, Any]:
    """Compare challenged vs unchallenged release-audit cohorts using durable signals."""
    challenged: list[dict[str, Any]] = []
    unchallenged: list[dict[str, Any]] = []

    for v in verdicts:
        if not isinstance(v, dict):
            unchallenged.append(v)
            continue
        recon = v.get("audit_reconciliation")
        challenge = recon.get("audit_challenge") if isinstance(recon, dict) else None
        candidate_sha = str(v.get("candidate_sha") or "").strip()
        challenge_sha = (
            str(challenge.get("challenged_candidate_sha") or "").strip()
            if isinstance(challenge, dict)
            else ""
        )
        if (
            isinstance(challenge, dict)
            and challenge.get("status") == "available"
            and (
                not candidate_sha or not challenge_sha or challenge_sha == candidate_sha
            )
        ):
            challenged.append(v)
        else:
            unchallenged.append(v)

    def _cohort_stats(
        cohort: list[dict[str, Any]], *, include_disagreement: bool
    ) -> dict[str, Any]:
        n = len(cohort)
        if n < _MIN_TELEMETRY_SAMPLE:
            return {"sample_size": n, "insufficient_sample": True}

        exclusion_count = sum(
            1
            for v in cohort
            if isinstance(v.get("audit_reconciliation"), dict)
            and int(v["audit_reconciliation"].get("excluded_findings_count") or 0) > 0
        )
        exclusion_rate = round(exclusion_count / n, 4)

        resolved = [
            v
            for v in cohort
            if v.get("terminal_status") == "promoted"
            and v.get("post_promotion_outcome")
            in ("serving_confirmed", "deploy_failed")
        ]
        resolved_sample = len(resolved)
        wrong_decision_count = sum(
            1 for v in resolved if v.get("post_promotion_outcome") == "deploy_failed"
        )
        wrong_decision_rate = (
            round(wrong_decision_count / resolved_sample, 4)
            if resolved_sample > 0
            else None
        )

        stats: dict[str, Any] = {
            "sample_size": n,
            "insufficient_sample": False,
            "exclusion_rate": exclusion_rate,
            "exclusion_count": exclusion_count,
            "wrong_decision_rate": wrong_decision_rate,
            "wrong_decision_resolved_sample": resolved_sample,
        }

        if include_disagreement:
            disagree_count = sum(
                1
                for v in cohort
                if isinstance(v.get("audit_reconciliation"), dict)
                and isinstance(v["audit_reconciliation"].get("audit_challenge"), dict)
                and v["audit_reconciliation"]["audit_challenge"].get(
                    "reported_agreement"
                )
                != "agree"
            )
            stats["disagreement_rate"] = round(disagree_count / n, 4)
            stats["disagreement_count"] = disagree_count

            # challenge_refuted_count: total findings the adversarial challenger
            # explicitly marked refuted_false_positive across all challenged verdicts.
            # Distinct from exclusion_rate (unverified primary findings) — this is
            # findings the challenger actively refuted, not verification-excluded ones.
            # challenge_refuted_rate is the fraction of all findings reviewed that were
            # refuted: refuted_count / (confirmed_count + refuted_count + uncertain_count).
            challenge_refuted_count = sum(
                int(
                    v["audit_reconciliation"]["audit_challenge"].get("refuted_count")
                    or 0
                )
                for v in cohort
                if isinstance(v.get("audit_reconciliation"), dict)
                and isinstance(v["audit_reconciliation"].get("audit_challenge"), dict)
                and v["audit_reconciliation"]["audit_challenge"].get("status")
                == "available"
            )
            challenge_total_reviewed = sum(
                int(v["audit_reconciliation"]["audit_challenge"].get("confirmed_count") or 0)
                + int(v["audit_reconciliation"]["audit_challenge"].get("refuted_count") or 0)
                + int(v["audit_reconciliation"]["audit_challenge"].get("uncertain_count") or 0)
                for v in cohort
                if isinstance(v.get("audit_reconciliation"), dict)
                and isinstance(v["audit_reconciliation"].get("audit_challenge"), dict)
                and v["audit_reconciliation"]["audit_challenge"].get("status")
                == "available"
            )
            stats["challenge_refuted_count"] = challenge_refuted_count
            stats["challenge_refuted_rate"] = (
                round(challenge_refuted_count / challenge_total_reviewed, 4)
                if challenge_total_reviewed > 0
                else 0.0
            )

        return stats

    return {
        "challenged": _cohort_stats(challenged, include_disagreement=True),
        "unchallenged": _cohort_stats(unchallenged, include_disagreement=False),
        "total_verdicts": len(verdicts),
    }


def compute_enforce_confidence_preview(
    existing_confidence: float,
    debt_pressure_score: float,
    *,
    configured_weight: float | None = None,
) -> dict[str, Any]:
    """Compute the hypothetical confidence if ``completion_debt_weight_mode`` were "enforce".

    Pure / side-effect-free.  Called in observe mode to record what enforce would
    have done, so qualifications accumulate decision-ready evidence for graduating
    the knob without affecting the live composite.

    The effective weight mirrors ``_apply_completion_debt_weight_mode``'s enforce
    branch exactly:
      * ``configured_weight > 0`` → use it (clamped to [0, MAX_COMPLETION_DEBT_WEIGHT]).
      * ``configured_weight <= 0`` or ``None`` → fall back to
        ``DEFAULT_ENFORCE_COMPLETION_DEBT_WEIGHT`` so a graduation with no explicit
        weight still changes the composite.

    Returns ``{"enforce_confidence": float, "effective_weight": float}``.
    """
    if configured_weight is not None and configured_weight > 0.0:
        effective = max(0.0, min(float(configured_weight), MAX_COMPLETION_DEBT_WEIGHT))
    else:
        effective = DEFAULT_ENFORCE_COMPLETION_DEBT_WEIGHT

    enforce_confidence = round(
        _clamp01(
            (1.0 - effective) * _clamp01(existing_confidence)
            + effective * _clamp01(debt_pressure_score)
        ),
        4,
    )
    return {"enforce_confidence": enforce_confidence, "effective_weight": effective}
