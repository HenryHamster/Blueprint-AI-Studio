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

import logging
from typing import Any

logger = logging.getLogger(__name__)

# Hardcoded fallback used when promotion_policy.yaml is missing, unreadable, or
# malformed.  MUST match the documented defaults in promotion_policy.yaml so a
# parse failure degrades to the intended rubric rather than a silent surprise.
_DEFAULT_POLICY: dict[str, Any] = {
    "weights": {"audit": 0.40, "test_erosion": 0.30, "smoketest": 0.30},
    "threshold": 0.90,
}


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


def compute_composite(
    ci_green: bool,
    audit_score: float,
    test_erosion: float,
    smoketest_score: float,
    policy: dict[str, Any],
    *,
    smoketest_enabled: bool = True,
    ci_state: str = "red",  # _CI_RED — defined later in module; use literal here
) -> dict[str, Any]:
    """Compute the composite promotion confidence for a release candidate.

    Hard blockers — each forces ``confidence = 0.0`` and ``promote = False``.
    They are evaluated in this order, and ``blocker`` reports the first match:
      * ``not ci_green`` + ``ci_state == _CI_RED``  — confirmed failing build
        (``"ci_not_green"``).
      * ``not ci_green`` + ``ci_state != _CI_RED``  — CI could not be evaluated
        (pending, no check-runs, or I/O degraded) (``"ci_unresolved"``).
      * ``smoketest_score`` is None — the smoketest never produced a usable
        score, treated as a startup-suite failure
        (``"smoketest_startup_failed"``).  ONLY checked when
        ``smoketest_enabled`` is True (see below).
      * ``audit_score`` is None     — the release_audit report is missing or
        unusable (``"audit_missing"``).  Checked before test-erosion because a
        missing audit report nulls both inputs.
      * ``test_erosion`` is None    — the diff deleted a test file
        (``"test_file_deleted"``); a real deletion, only meaningful when the
        audit report itself was present.

    ``smoketest_enabled`` (audit H3): the qualifier-smoketest subsystem is
    gated by ``runtime_config["smoketest_enabled"]``, which defaults False.
    When the qualifying run had it disabled it spawned NO qualifier-smoketest
    child and there is no SMOKETEST_RESULT artifact — so ``smoketest_score``
    is unconditionally None.  Treating that None as the
    ``smoketest_startup_failed`` hard blocker would make EVERY candidate fail
    while the subsystem is off.  Therefore, when ``smoketest_enabled`` is
    False this function: (a) skips the smoketest hard blocker, (b) drops the
    smoketest dimension from the weighted sum, and (c) renormalizes the
    remaining ``audit`` + ``test_erosion`` weights so they sum to 1.0.  When
    True it behaves exactly as the design-doc rubric specifies.

    The numeric inputs are clamped to [0,1] before weighting.  When no hard
    blocker fires, ``confidence`` is the policy-weighted sum of the active
    sub-scores.  ``promote`` is True iff ``confidence >= threshold``.

    Returns ``{confidence, promote, sub_scores, blocker}`` where ``blocker`` is
    a short human-readable string when a hard blocker fired, else None.
    """
    weights = policy.get("weights") or _DEFAULT_POLICY["weights"]
    threshold = float(policy.get("threshold", _DEFAULT_POLICY["threshold"]))
    w_audit = float(weights.get("audit", 0.40))
    w_test = float(weights.get("test_erosion", 0.30))
    w_smoke = float(weights.get("smoketest", 0.30))

    if not smoketest_enabled:
        # Audit H3: drop the smoketest dimension and renormalize audit +
        # test_erosion so the two remaining weights sum to 1.0.  A degenerate
        # policy where audit + test_erosion both round to 0 falls back to the
        # documented 0.40/0.30 default split so confidence is never NaN.
        audit_test_sum = w_audit + w_test
        if audit_test_sum > 0:
            w_audit = w_audit / audit_test_sum
            w_test = w_test / audit_test_sum
        else:
            default_sum = (
                _DEFAULT_POLICY["weights"]["audit"]
                + _DEFAULT_POLICY["weights"]["test_erosion"]
            )
            w_audit = _DEFAULT_POLICY["weights"]["audit"] / default_sum
            w_test = _DEFAULT_POLICY["weights"]["test_erosion"] / default_sum
        w_smoke = 0.0

    # Hard blockers — checked before any weighting.  ``None`` is the sentinel
    # for "this dimension hit an irrecoverable blocker"; the caller passes None
    # for test_erosion on a deleted test file and None for smoketest on a
    # startup-suite failure.
    #
    # Ordering matters when several inputs are None at once.  A FAILED
    # release_audit posts no RELEASE_AUDIT_REPORT, so _score_audit(None)
    # returns (audit_score=None, test_erosion=None) — BOTH None even though no
    # test file was actually deleted.  ``audit_score is None`` must therefore
    # be checked BEFORE ``test_erosion is None`` so an audit that never ran is
    # reported as "audit_missing" rather than mislabelled "test_file_deleted":
    # a None test_erosion only genuinely means a deleted test file when the
    # audit report itself was present (audit_score not None).
    blocker: str | None = None
    if not ci_green:
        # Distinguish a confirmed red build from an unresolved CI state (pending,
        # no check-runs, or GitHub I/O degraded).  Only a real red build is a
        # quality blocker; unresolved CI is a resolution/infrastructure concern.
        blocker = "ci_not_green" if ci_state == _CI_RED else "ci_unresolved"
    elif smoketest_enabled and smoketest_score is None:
        # Only a hard blocker when the smoketest subsystem was actually
        # enabled — a disabled subsystem legitimately produces no score.
        blocker = "smoketest_startup_failed"
    elif audit_score is None:
        # An audit that produced no usable score is a soft signal, but without
        # it the composite is meaningless — treat as a blocker so a missing
        # audit can never promote on smoketest + test_erosion alone.  Reported
        # ahead of test_file_deleted because a missing report nulls both.
        blocker = "audit_missing"
    elif test_erosion is None:
        blocker = "test_file_deleted"

    # Sub-scores are always reported (clamped) for observability, even when a
    # blocker fired and the composite is forced to zero.
    sub_scores = {
        "audit": _clamp01(audit_score) if audit_score is not None else 0.0,
        "test_erosion": _clamp01(test_erosion) if test_erosion is not None else 0.0,
        "smoketest": _clamp01(smoketest_score) if smoketest_score is not None else 0.0,
    }

    if blocker is not None:
        return {
            "confidence": 0.0,
            "promote": False,
            "sub_scores": sub_scores,
            "blocker": blocker,
        }

    # When the smoketest dimension is dropped its weight is 0.0, so the term
    # contributes nothing regardless of the (unused) smoketest sub-score.
    confidence = (
        w_audit * sub_scores["audit"]
        + w_test * sub_scores["test_erosion"]
        + w_smoke * sub_scores["smoketest"]
    )
    confidence = round(_clamp01(confidence), 4)
    return {
        "confidence": confidence,
        "promote": confidence >= threshold,
        "sub_scores": sub_scores,
        "blocker": None,
    }


# CI resolution states for a qualifier candidate commit.
_CI_GREEN = "green"  # a real completed, all-passing build exists
_CI_RED = "red"  # a completed build with at least one failure
_CI_PENDING = "pending"  # checks in progress, or no check-runs found anywhere
_CI_NO_CHECKS = "no_checks"  # the commit itself carries no check-runs


def _classify_check_runs(result: dict[str, Any] | None) -> str:
    """Map a strict ``get_check_runs`` result to a qualifier CI state."""
    if not isinstance(result, dict):
        return _CI_PENDING
    status = result.get("status")
    if status == "no_checks":
        return _CI_NO_CHECKS
    if status == "completed":
        conclusion = result.get("conclusion")
        if conclusion == "success":
            return _CI_GREEN
        if conclusion == "failure":
            return _CI_RED
        return _CI_PENDING
    return _CI_PENDING


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


def _score_audit(report: dict[str, Any] | None) -> tuple[float | None, float | None]:
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
    is None — the hard-blocker sentinel — when the diff deleted a test file.
    A net reduction in tests or assertions applies a graded penalty.
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
                sev = str(f.get("severity") or f.get("level") or "").lower()
                if sev and _finding_is_verified(f, sev):
                    derived -= _AUDIT_DEDUCTIONS.get(sev, _AUDIT_DEDUCTION_UNKNOWN)
    derived = _clamp01(derived)
    missing_reason = _audit_missing_reason(report)
    audit_score: float | None = derived
    if missing_reason == "parseable_but_unscoreable":
        # The report parsed, but no findings entry carried a usable severity.
        # Treat that as an unusable audit signal instead of a clean 1.0.
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
        # Hard blocker — any deleted test file forces a non-promote.
        return audit_score, None

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

    return None


# Cap on findings carried into last_qualification (and thus the strategic
# agent's payload). Kept small so the version_phase block stays well under the
# pack budget while still giving the agent enough to target the audit sub-score.
_MAX_CARRIED_FINDINGS = 12
_FINDING_SUMMARY_MAX = 280
# Lower number sorts first (most severe). Unknown severities sort last.
_SEVERITY_RANK = {"critical": 0, "high": 0, "medium": 1, "moderate": 1, "low": 2}


def _extract_audit_findings(report: dict[str, Any] | None) -> list[dict[str, str]]:
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
        else:
            sev = "unknown"
            summary = str(f)
        summary = str(summary).strip()[:_FINDING_SUMMARY_MAX]
        if summary:
            out.append({"severity": sev, "summary": summary})

    out.sort(key=lambda d: _SEVERITY_RANK.get(d["severity"], 3))
    return out[:_MAX_CARRIED_FINDINGS]


def _score_smoketest(result: dict[str, Any] | None) -> float | None:
    """Derive a ``smoketest_score`` in [0,1] from a SMOKETEST_RESULT.

    The smoketest emits ``{passed, tests: [{status}], suites: {startup:
    {passed}, ...}}`` (agent/src + routers/agent_status.py).

    Returns the pass ratio (passed tests / total tests).  Returns None — the
    hard-blocker sentinel — when the startup suite failed or no result is
    available, since a failed startup suite means the candidate can't even
    boot.
    """
    if not isinstance(result, dict):
        return None

    suites = result.get("suites")
    if isinstance(suites, dict):
        startup = suites.get("startup")
        if isinstance(startup, dict) and startup.get("passed") is False:
            # Startup suite failed — hard blocker.
            return None

    tests = result.get("tests")
    if not isinstance(tests, list) or not tests:
        # No tests ran: if the overall result claims passed, treat as a full
        # score; otherwise as zero (but not a hard blocker — startup is the
        # blocker, handled above).
        return 1.0 if result.get("passed") is True else 0.0

    total = len(tests)
    # Only status=="pass" counts toward the score — a "skip" (or any non-pass
    # status) contributes 0.  This is intentionally conservative: a skipped
    # qualifier test is unverified coverage, so it should pull the score down
    # rather than be treated as a pass.
    passed = sum(1 for t in tests if isinstance(t, dict) and t.get("status") == "pass")
    return _clamp01(passed / total) if total else 0.0


def _fmt_sub_scores(
    sub_scores: dict[str, Any] | None,
    *,
    smoketest_enabled: bool = True,
) -> str:
    """Render sub-scores as ``audit 0.95 · test 1.00 · smoke 0.80`` for messages.

    The smoketest term is omitted when the subsystem was disabled for the run —
    its sub-score is a meaningless 0.0 placeholder in that case.
    """
    if not sub_scores:
        return "n/a"
    parts = [
        f"audit {float(sub_scores.get('audit', 0.0)):.2f}",
        f"test {float(sub_scores.get('test_erosion', 0.0)):.2f}",
    ]
    if smoketest_enabled:
        parts.append(f"smoke {float(sub_scores.get('smoketest', 0.0)):.2f}")
    return " · ".join(parts)
