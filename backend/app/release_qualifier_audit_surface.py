"""Audited-surface hashing + scoped-delta reuse for release qualification.

Extracted from ``app.release_qualifier`` (chronic size-offender
decomposition) — BEHAVIOUR-PRESERVING move.  ``release_qualifier`` re-exports
every symbol in this module, so ``rq.*`` / ``_rq.*`` /
``mocker.patch("app.release_qualifier.<sym>")`` callers keep resolving to these
implementations unchanged.

Cohesive responsibility: the durable identity of an audit's judged code
surface (``compute_audited_surface_hash`` / ``audited_surface_path_map``) and
the scoped-delta merge logic that lets a later incremental audit re-use an
earlier verdict for unchanged paths (Guard 3 / the fixing-CI-must-not-reaudit
lever).
"""
from __future__ import annotations

import hashlib
from typing import Any

from app.release_scoring import (
    _extract_audit_findings,
    _extract_excluded_audit_findings,
    _score_audit,
)

# Conclusive qualification verdicts — when a candidate commit receives one of
# these verdicts, the outcome is settled and idempotent (relying on lucky
# re-rolls of a coin-flip margin is not sound). A re-run is only permitted
# when the integration branch advances to a new commit. Infra/error outcomes
# (infra_failure, error, etc.) are non-conclusive and always re-run, since they
# produced no real quality verdict.
_CONCLUSIVE_VERDICT_STATUSES: frozenset[str] = frozenset(
    {"promoted", "not_promoted", "dry_run"}
)


def _is_audited_surface_path(path: str) -> bool:
    """True when a file is part of the code surface a release audit judges.

    Excludes the mechanically-regenerated artifacts an audit does NOT judge —
    emitter-inventory snapshots and other ``__fixtures__``/``__snapshots__``,
    the module-size limits config, CI workflow files, generated test-timing
    data, and docs.  A candidate that differs from a prior audited candidate
    ONLY in these excluded files has the SAME audited surface, so its audit
    verdict can be reused (Guard 3) instead of re-rolling the ~15min LLM audit.
    Everything else — app code AND test logic — is IN the surface, so a real
    change always re-audits.
    """
    p = path.lower()
    if p.startswith(".github/"):
        return False
    if "/__fixtures__/" in p or "/__snapshots__/" in p:
        return False
    if p.endswith("module_size_limits.json"):
        return False
    if p.endswith(".test_durations"):
        return False
    return not p.endswith(".md")

def compute_audited_surface_hash(tree_entries: list[dict] | None) -> str | None:
    """Content hash of the audited code surface at a candidate ref.

    Built from the git blob SHAs of every tracked file EXCEPT the mechanically-
    regenerated artifacts a code audit does not judge (see
    ``_is_audited_surface_path``).  Two candidates whose audited surface is
    byte-identical hash identically, so the qualifier can REUSE a prior audit
    verdict for the newer commit (Guard 3) instead of re-rolling the audit — the
    "fixing CI / regenerating a snapshot must not re-run the whole qualification"
    lever (Henry, 2026-07-02).  Cheap: the whole tree (path + blob SHA) comes
    from ONE ``list_repo_tree_paths_at_ref`` call, no per-file reads.  Returns
    None when the tree is unavailable — fail-open: no reuse, the normal
    SHA-keyed cache path applies.
    """
    return _audited_surface_hash_from_map(audited_surface_path_map(tree_entries))

def _audited_surface_hash_from_map(surface: dict[str, str]) -> str | None:
    """Hash an already-normalized audited-surface map."""
    relevant = sorted(surface.items())
    if not relevant:
        return None
    h = hashlib.sha256()
    for path, sha in relevant:
        h.update(path.encode("utf-8"))
        h.update(b"\0")
        h.update(sha.encode("utf-8"))
        h.update(b"\n")
    return h.hexdigest()

def audited_surface_path_map(tree_entries: list[dict] | None) -> dict[str, str]:
    """Return the durable ``path -> blob_sha`` identity of an audited tree."""
    if not tree_entries:
        return {}
    return {
        str(entry["path"]): str(entry["sha"])
        for entry in tree_entries
        if isinstance(entry, dict)
        and entry.get("sha")
        and entry.get("path")
        and _is_audited_surface_path(str(entry["path"]))
    }

def _changed_audited_surface_paths(
    prior: dict[str, str],
    current: dict[str, str],
) -> list[str]:
    """Return added, removed, or blob-changed audited paths."""
    return sorted(
        path
        for path in set(prior) | set(current)
        if prior.get(path) != current.get(path)
    )

def _audit_detail_surface_map(detail: Any) -> dict[str, str] | None:
    """Read and validate a persisted per-path audited-surface identity."""
    if not isinstance(detail, dict):
        return None
    raw = detail.get("audited_surface_paths")
    if not isinstance(raw, dict) or not raw:
        return None
    normalized: dict[str, str] = {}
    for path, blob_sha in raw.items():
        if not isinstance(path, str) or not path or not isinstance(blob_sha, str):
            return None
        if not blob_sha:
            return None
        normalized[path] = blob_sha
    return normalized

def _audit_detail_is_delta_reusable(detail: Any) -> bool:
    """True when detail contains a complete, path-addressable audit fact."""
    complete = bool(
        _audit_detail_surface_map(detail)
        and isinstance(detail, dict)
        and not detail.get("audit_findings_truncated")
        and not detail.get("excluded_audit_findings_truncated")
        and isinstance(detail.get("audit_findings"), list)
        and isinstance(detail.get("excluded_audit_findings"), list)
    )
    if not complete:
        return False
    return all(
        _audit_finding_path(finding)
        for finding in [
            *detail.get("audit_findings", []),
            *detail.get("excluded_audit_findings", []),
        ]
    )

def _audit_report_raw_findings(report: Any) -> list[Any]:
    if not isinstance(report, dict):
        return []
    findings = report.get("findings")
    if not isinstance(findings, list):
        findings = report.get("issues")
    return list(findings) if isinstance(findings, list) else []

def _audit_finding_identity(finding: Any) -> str:
    if not isinstance(finding, dict):
        return f"text:{finding!s}"
    # A surface key identifies a file/blob, not a unique finding.
    for key in ("key",):
        value = str(finding.get(key) or "").strip()
        if value:
            return f"{key}:{value}"
    return "|".join(
        (
            str(finding.get("file") or finding.get("path") or "").lower(),
            str(finding.get("severity") or finding.get("level") or "").lower(),
            str(
                finding.get("summary")
                or finding.get("description")
                or finding.get("title")
                or finding.get("text")
                or ""
            ).strip(),
        )
    )

def _audit_finding_path(finding: Any) -> str | None:
    if not isinstance(finding, dict):
        return None
    path = str(finding.get("file") or finding.get("path") or "").strip()
    return path or None

def _reviewed_audit_paths(report: Any) -> set[str]:
    """Return the paths explicitly superseded by a scoped audit report."""
    if not isinstance(report, dict):
        return set()
    reviewed = report.get("reviewed_paths")
    if not isinstance(reviewed, dict):
        scope = report.get("audit_scope")
        reviewed = scope.get("reviewed_paths") if isinstance(scope, dict) else None
    if isinstance(reviewed, dict):
        return {
            str(path)
            for path, verdict in reviewed.items()
            if isinstance(path, str)
            and path
            and str(verdict or "").lower() in {"clean", "finding"}
        }
    scope = report.get("audit_scope")
    changed = scope.get("changed_paths") if isinstance(scope, dict) else None
    if not isinstance(changed, list):
        return set()
    return {str(path) for path in changed if isinstance(path, str) and path}

def _audit_report_confirms_incremental_base(report: Any, expected_base_sha: str) -> bool:
    scope = report.get("audit_scope") if isinstance(report, dict) else None
    return bool(
        isinstance(scope, dict)
        and scope.get("mode") == "incremental"
        and expected_base_sha
        and str(scope.get("effective_base_sha") or "").strip() == expected_base_sha
    )

def _audit_report_confirms_goal_category_range(report: Any, expected_full_range: str) -> bool:
    # Fail-open: absent report/scope/echo all mean the grader ran the full
    # fallback (older agents) or the report is unrelated to goal grading.  Hold
    # ONLY when a present echo disagrees with the full version delta.
    if not isinstance(report, dict):
        return True
    scope = report.get("audit_scope")
    if not isinstance(scope, dict):
        return True
    echoed = scope.get("goal_category_diff_range")
    if not isinstance(echoed, str) or not echoed.strip():
        return True
    return echoed.strip() == expected_full_range

def _carried_finding_as_report_item(
    finding: dict[str, Any],
    *,
    verified: bool,
) -> dict[str, Any]:
    """Restore a compact ledger finding to a scoreable report item."""
    item = dict(finding)
    if verified:
        # Higher-severity findings only entered the durable audit fact after
        # verification/challenge. Preserve that established trust when the
        # unchanged path is carried into a later scoped report.
        item["verification_note"] = (
            "Carried from a prior verified per-path audit dimension fact."
        )
    else:
        item.pop("verification_note", None)
    return item

def _merge_scoped_audit_report_with_prior_fact(
    scored_report: dict[str, Any] | None,
    original_report: dict[str, Any] | None,
    prior_detail: Any,
    *,
    expected_base_sha: str | None = None,
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    """Merge unchanged-path facts into a freshly reviewed delta report.

    The agent-authored ``reviewed_paths`` map is authoritative when present;
    otherwise ``audit_scope.changed_paths`` is the superseded set. Paths in that
    set are replaced by the fresh result (including an explicit clean result).
    Findings on every other path are carried from the durable prior audit fact.
    """
    if not isinstance(scored_report, dict) or not isinstance(original_report, dict):
        return scored_report, original_report
    if not _audit_detail_is_delta_reusable(prior_detail):
        return scored_report, original_report
    if not _audit_report_confirms_incremental_base(
        original_report, str(expected_base_sha or "").strip()
    ):
        return scored_report, original_report
    reviewed_paths = _reviewed_audit_paths(original_report)
    if not reviewed_paths:
        # Missing scope means the agent either performed a full fallback or
        # emitted an older report. In both cases, do not compose an inferred delta.
        return scored_report, original_report

    prior_verified = prior_detail.get("audit_findings") or []
    prior_excluded = prior_detail.get("excluded_audit_findings") or []
    fresh_verified = _extract_audit_findings(scored_report)
    fresh_excluded = _extract_excluded_audit_findings(original_report)
    fresh_identities = {
        _audit_finding_identity(finding)
        for finding in [*fresh_verified, *fresh_excluded]
    }

    def _carry(findings: list[Any], *, verified: bool) -> list[dict[str, Any]]:
        carried: list[dict[str, Any]] = []
        seen = set(fresh_identities)
        for finding in findings:
            path = _audit_finding_path(finding)
            if not isinstance(finding, dict) or not path or path in reviewed_paths:
                continue
            identity = _audit_finding_identity(finding)
            if identity in seen:
                continue
            seen.add(identity)
            carried.append(_carried_finding_as_report_item(finding, verified=verified))
        return carried

    carried_verified = _carry(prior_verified, verified=True)
    carried_excluded = _carry(prior_excluded, verified=False)
    merged_scored = {
        **scored_report,
        "findings": [
            *_audit_report_raw_findings(scored_report),
            *carried_verified,
        ],
    }
    merged_original = {
        **original_report,
        "findings": [
            *_audit_report_raw_findings(original_report),
            *carried_excluded,
        ],
    }
    return merged_scored, merged_original

def _merge_scoped_test_erosion_with_prior_fact(
    report: dict[str, Any], prior_detail: Any, *, prior_blocked: bool
) -> tuple[float | None, dict[str, Any] | None]:
    """Compose cumulative erosion for an agent-confirmed incremental audit.

    Sums deltas losslessly and detects restored test files via the current
    scope's ``changed_paths``: a prior-deleted file that fell within the
    current scoped diff but is NOT in current's ``deleted_test_files`` is
    considered restored, dropping it from the cumulative list.  Prior blocking
    is only preserved when at least one prior-deleted file remains present.
    """
    current = report.get("computed_test_erosion")
    if not isinstance(current, dict):
        current = report.get("test_erosion")
    current = dict(current) if isinstance(current, dict) else {}
    prior = dict(prior_detail) if isinstance(prior_detail, dict) else {}
    merged = dict(current)

    # Lossless delta summation: both prior and current now store ALL numerical
    # deltas (positive and negative) so the cumulative sum is correct.
    for field in ("test_count_delta", "assertion_delta"):
        old, new = prior.get(field, 0), current.get(field, 0)
        if all(
            isinstance(value, (int, float)) and not isinstance(value, bool)
            for value in (old, new)
        ):
            merged[field] = old + new

    # Build the set of paths examined in this scoped incremental diff.
    _scope = report.get("audit_scope") if isinstance(report, dict) else None
    _changed_paths: set[str] = set()
    if isinstance(_scope, dict):
        _raw = _scope.get("changed_paths")
        if isinstance(_raw, list):
            _changed_paths = {str(p) for p in _raw if isinstance(p, str) and p}

    current_deleted = {
        str(p) for p in current.get("deleted_test_files", []) if str(p)
    }

    # Carry forward prior-deleted files UNLESS they were restored (i.e. fell
    # within this scope's changed paths but are NOT in current's deletions).
    prior_deleted = [
        str(p) for p in prior.get("deleted_test_files", []) if str(p)
    ]
    merged_deleted: list[str] = []
    prior_still_present = False
    for path in prior_deleted:
        if path in _changed_paths and path not in current_deleted:
            continue  # restored in this scoped diff
        merged_deleted.append(path)
        prior_still_present = True

    for path in sorted(current_deleted):
        if path not in merged_deleted:
            merged_deleted.append(path)

    if merged_deleted:
        merged["deleted_test_files"] = merged_deleted

    merged_report = {**report, "computed_test_erosion": merged}
    _, score = _score_audit(merged_report)
    if prior_blocked and prior_still_present:
        score = None
    return score, merged if merged else None

def _audit_dimension_detail(
    *,
    surface_paths: Any,
    audit_findings: list[dict[str, Any]],
    excluded_audit_findings: list[dict[str, Any]],
    findings_meta: dict[str, Any],
    audit_missing_reason: str | None,
    audit_scope: Any,
    holistic_review: dict[str, Any] | None = None,
    holistic_contract_fingerprint: str | None = None,
) -> dict[str, Any]:
    """Build the durable, complete per-path audit fact payload."""
    return {
        "audited_surface_paths": (
            dict(surface_paths) if isinstance(surface_paths, dict) else {}
        ),
        "audit_findings": audit_findings,
        "excluded_audit_findings": excluded_audit_findings,
        "audit_findings_total": findings_meta.get("audit_findings_total", 0),
        "audit_findings_truncated": bool(findings_meta.get("audit_findings_truncated")),
        "excluded_audit_findings_total": findings_meta.get(
            "excluded_audit_findings_total", 0
        ),
        "excluded_audit_findings_truncated": bool(
            findings_meta.get("excluded_audit_findings_truncated")
        ),
        "audit_missing_reason": audit_missing_reason,
        "audit_scope": audit_scope,
        "holistic_review": holistic_review,
        "holistic_contract_fingerprint": holistic_contract_fingerprint,
    }

def _prior_audit_clean_for_incremental_base(
    prior_qualification: dict[str, Any] | None,
    candidate_sha: str,
) -> bool:
    """True when a prior qualification can anchor an incremental audit base.

    Incremental review is safe only after an audit left no verified *or*
    unverified findings; missing or inconclusive evidence fails closed.  This is
    the single cleanliness predicate behind BOTH the qualification-sourced base
    and the ledger-hint base below, so the two adoption paths cannot diverge.
    """
    if not isinstance(prior_qualification, dict):
        return False
    if prior_qualification.get("status") not in {
        *_CONCLUSIVE_VERDICT_STATUSES,
        "promotion_failed",
    }:
        return False
    prior_sha = str(prior_qualification.get("candidate_sha") or "").strip()
    if not prior_sha or prior_sha == candidate_sha:
        return False
    sub_scores = prior_qualification.get("sub_scores")
    if not isinstance(sub_scores, dict) or not isinstance(
        sub_scores.get("audit"), (int, float)
    ):
        return False
    if prior_qualification.get("audit_findings"):
        return False
    if prior_qualification.get("excluded_audit_findings"):
        return False
    return (
        prior_qualification.get("audit_findings_total") in (0, None)
        and prior_qualification.get("excluded_audit_findings_total") in (0, None)
    )

def _incremental_audit_base_sha(
    prior_qualification: dict[str, Any] | None,
    candidate_sha: str,
    *,
    prior_audit_candidate_sha: str | None = None,
) -> str | None:
    """Return the prior fully-cleared audit SHA eligible as a delta baseline.

    Incremental review is safe only after an audit left no verified *or*
    unverified findings. A version may still be ``not_promoted`` for an
    independent gate, so status alone is deliberately not a proxy for audit
    cleanliness. Missing legacy fields fail closed to a full audit.

    The provenance shape is also suitable for Phase 1's
    ``QualificationDimensionResult.detail`` JSON. This branch intentionally
    stores it in ``last_qualification`` until that ledger is backmerged.
    """
    ledger_sha = str(prior_audit_candidate_sha or "").strip()
    if (
        ledger_sha
        and ledger_sha != candidate_sha
        and _prior_audit_clean_for_incremental_base(prior_qualification, candidate_sha)
    ):
        # The agent treats this as a hint and independently verifies ancestry
        # and range resolvability before selecting its incremental two-dot diff.
        # It is adopted only when the prior audit left no verified or unverified
        # findings — the same precondition enforced below — so a dirty prior
        # audit cannot silently drop findings from the new audit's view.
        return ledger_sha
    if not _prior_audit_clean_for_incremental_base(prior_qualification, candidate_sha):
        return None
    return str(prior_qualification.get("candidate_sha") or "").strip()

def _promotion_failed_reuse_eligible(
    qual: dict[str, Any] | None, candidate_diff_hash: str | None
) -> bool:
    """True when a ``promotion_failed`` verdict can be re-used to RE-PROMOTE.

    A ``promotion_failed`` verdict ALWAYS means the audit PASSED and only the
    PROMOTION step failed (PR CI red / staleness / merge) — ``promote()`` is
    only ever reached from ``_finalize_qualification`` for a would-promote
    composite.  When the harvested fix landed ENTIRELY in an audit-EXCLUDED path
    (CI workflow / test fixture / config), the live audited-surface hash is
    byte-identical to the passing candidate, so that audit verdict still holds:
    the CURRENT candidate can be re-promoted WITHOUT re-running the ~15min audit
    (which also re-rolls a non-deterministic score — the v21 treadmill, passed
    0.9657 → CI-failed → re-qualified 0.6171).  Henry, 2026-07-02.

    Strictly gated (any miss ⇒ fall through to the normal re-audit — a CHANGED
    surface, i.e. an app-code fix, MUST re-audit): the prior status is
    ``promotion_failed`` AND the LIVE ``candidate_diff_hash`` is truthy AND it
    equals the prior verdict's ``candidate_diff_hash``.  The cap
    (``_MAX_PROMOTION_FAILURE_REQUALIFY_ATTEMPTS``) is enforced separately by the
    caller so a persistently-red candidate cannot re-promote forever.
    """
    if not isinstance(qual, dict):
        return False
    if qual.get("status") != "promotion_failed":
        return False
    prior_hash = qual.get("candidate_diff_hash")
    return bool(candidate_diff_hash and prior_hash and candidate_diff_hash == prior_hash)
