"""Provenance validation helpers for research findings.

Extracted from app.research_completion to keep that module focused on the
research-completion handler. Contains verification-basis tier constants,
the HEAD-provenance reconciliation functions, the launch-staleness recompute,
and the GitHub-context-aware ungrounded-claim summarizer.
"""

import logging
from typing import Any, Dict

from app.models import Project
from app.source_ref_validation import (
    cited_paths_changed_since,
    detect_absent_symbols,
    resolve_branch_verification_context,
    _parse_path_symbol_refs,
)

logger = logging.getLogger(__name__)

# Verification-basis tiers, ordered from highest evidence (the finding's claim
# was checked against real code) to lowest (asserted/deduced). Defined once here
# so the normalization and the HEAD-reconciliation below share a single source
# of truth instead of repeating the literal tier strings.
VERIFICATION_BASIS_TIERS = (
    "code_verified",
    "path_verified",
    "pattern_inferred",
    "deduced",
)
VALID_VERIFICATION_BASES = frozenset(VERIFICATION_BASIS_TIERS)
# Tiers that assert the finding was verified against real code. A self-reported
# value in this set is only honest if the finding's own citations resolve at
# HEAD; when they don't, it is capped to _DOWNGRADED_VERIFICATION_BASIS.
_HIGH_EVIDENCE_BASES = frozenset({"code_verified", "path_verified"})
# Honest fallback when a high-evidence self-claim is contradicted by broken
# citations: the highest tier that does NOT assert verification-against-code.
_DOWNGRADED_VERIFICATION_BASIS = "pattern_inferred"


def _reconcile_verification_basis(
    finding: Dict[str, Any],
    broken_provenance: Dict[str, Any] | None,
    ungrounded: Dict[str, Any] | None,
) -> bool:
    """Cap a self-reported high-evidence verification_basis against HEAD truth.

    The tiers in :data:`_HIGH_EVIDENCE_BASES` assert the finding was checked
    against real code. When the already-computed ingestion validation marks the
    finding's file sources invalid (``broken_provenance`` from
    :func:`summarize_broken_refs`) or finds ungrounded ``path:symbol`` claims
    (``ungrounded`` from :func:`_summarize_ungrounded_claims_grounded`), that
    self-assertion is contradicted by ground truth, so the stored basis is
    downgraded to :data:`_DOWNGRADED_VERIFICATION_BASIS` — an honest tier that
    makes no verified-against-code claim.

    Findings with no citations, or whose citations validate cleanly, keep their
    reported basis (both args are None). Mutates ``finding`` in place; returns
    True when a downgrade occurred.
    """
    current = finding.get("verification_basis")
    if current not in _HIGH_EVIDENCE_BASES:
        return False
    if not broken_provenance and not ungrounded:
        return False
    broken_count = int((broken_provenance or {}).get("broken_source_ref_count", 0) or 0)
    ungrounded_count = int((ungrounded or {}).get("ungrounded_claim_count", 0) or 0)
    finding["verification_basis_downgraded_from"] = current
    finding["verification_basis_downgrade_reason"] = (
        "HEAD validation failed: "
        f"{broken_count} broken source ref{'s' if broken_count != 1 else ''}, "
        f"{ungrounded_count} ungrounded claim{'s' if ungrounded_count != 1 else ''}"
    )
    finding["verification_basis"] = _DOWNGRADED_VERIFICATION_BASIS
    logger.info(
        "[RESEARCH] Capping verification_basis %s→%s for '%s' "
        "(citations unresolved at HEAD: broken_refs=%d, ungrounded_claims=%d)",
        current,
        _DOWNGRADED_VERIFICATION_BASIS,
        finding.get("title", "<untitled>"),
        broken_count,
        ungrounded_count,
    )
    return True


def _staleness_provenance_warning(
    finding: Dict[str, Any],
) -> Dict[str, Any] | None:
    """Return a provenance warning when deferred findings need HEAD revalidation."""
    if finding.get("needs_revalidation") is not True:
        return None
    if (
        finding.get("needs_revalidation_reason")
        == "cited_files_changed_since_generation"
    ):
        return {"cited_files_changed_since_generation": True}
    return None


def _merge_provenance_warnings(
    finding: Dict[str, Any],
    *warnings: Dict[str, Any] | None,
) -> None:
    """Merge provenance warnings onto a finding, preserving existing signals."""
    merged: Dict[str, Any] = {}
    existing = finding.get("_provenance_warnings")
    if isinstance(existing, dict):
        merged.update(existing)
    for warning in warnings:
        if warning:
            merged.update(warning)
    if merged:
        finding["_provenance_warnings"] = merged
    else:
        finding.pop("_provenance_warnings", None)


async def recompute_launch_staleness(session: Any, task: Any) -> bool:
    """Re-check cited-file freshness against the CURRENT integration HEAD at launch.

    A finding-derived task carries the ``generated_head_sha`` it was generated
    against and the file paths it cites (``_finding_cited_paths``), both stamped
    onto its context at spawn. But the integration branch advances while the task
    sits QUEUED, so the ingestion-time staleness warning is structurally inert
    (generated HEAD == ingestion HEAD). This recomputes the diff against the branch
    TIP at dispatch and, ONLY on an affirmative change verdict, merges
    ``{'cited_files_changed_since_generation': True}`` into the context's
    ``_provenance_warnings`` — reusing the exact shape the ingestion path emits.

    Returns True iff it mutated ``task.context`` (the caller must persist + mark the
    column modified). Conservative and best-effort by construction: a missing
    ``generated_head_sha``, no cited paths, no research parent, an unresolvable
    project, a None/False verdict, or ANY exception all leave the context untouched
    and return False. It never raises and never flags on an indeterminate result,
    so it can never block or delay dispatch nor false-positive a stale finding.
    """
    try:
        context = task.context or {}
        gen_sha = context.get("generated_head_sha")
        cited_paths = context.get("_finding_cited_paths")
        if not gen_sha or not isinstance(cited_paths, list) or not cited_paths:
            return False
        # Only annotate findings derived from a research parent.
        if not (
            context.get("research_task_id") or context.get("parent_research_task_id")
        ):
            return False
        project = (
            await session.get(Project, task.project_id) if task.project_id else None
        )
        if project is None:
            return False
        src_ref_ctx = await resolve_branch_verification_context(session, project)
        changed = await cited_paths_changed_since(gen_sha, cited_paths, **src_ref_ctx)
        if changed is not True:
            return False
        new_context = dict(context)
        _merge_provenance_warnings(
            new_context, {"cited_files_changed_since_generation": True}
        )
        task.context = new_context
        return True
    except Exception:  # noqa: BLE001 — launch recompute must never block dispatch
        logger.debug(
            "[RESEARCH] launch-time cited-file staleness recompute failed",
            exc_info=True,
        )
        return False


async def _summarize_ungrounded_claims_grounded(
    claims: list | None,
    sources: list | None,
    ctx: Dict[str, Any],
) -> Dict[str, Any] | None:
    """GitHub-context-aware variant of :func:`summarize_ungrounded_claims`.

    The shared ``summarize_ungrounded_claims`` helper checks each
    ``path:symbol`` reference against the local ``/tmp/workspace`` git
    checkout, which the backend does not have — so at research-finding
    ingestion the symbol-level grounding silently verifies nothing outside the
    agent VM. This threads the already-resolved GitHub repo/branch verification
    context (``ctx`` — the same dict the file-level source-ref validation uses)
    through :func:`detect_absent_symbols` so the symbol check runs against the
    project's real integration branch, reusing the GitHub file-content fetch
    behind ``detect_absent_symbols`` rather than adding a second content path.

    Returns the same ``{"ungrounded_claim_count", "ungrounded_refs"}`` shape as
    ``summarize_ungrounded_claims`` (or None when nothing is ungrounded).
    Conservative semantics are preserved: when no GitHub credentials resolved
    (``ctx`` lacks a token), ``detect_absent_symbols`` falls back to local git
    HEAD whose indeterminate result never flags, so an indeterminate check
    never produces an ungrounded ref.
    """
    if not isinstance(claims, list):
        claims = []
    seen_refs: set = set()
    ungrounded_refs: list = []
    ungrounded_claim_count = 0
    for claim in claims:
        absent = await detect_absent_symbols(text_fields=[claim], **ctx)
        if absent:
            ungrounded_claim_count += 1
        for ref in absent:
            if ref not in seen_refs:
                seen_refs.add(ref)
                ungrounded_refs.append(ref)
    # File-source excerpts contribute refs but never increment a claim count,
    # mirroring summarize_ungrounded_claims' count_claim=False handling.
    excerpts = [
        s.get("quote") or s.get("excerpt")
        for s in (sources or [])
        if isinstance(s, dict) and str(s.get("type") or "").lower() == "file"
    ]
    if excerpts:
        for ref in await detect_absent_symbols(text_fields=excerpts, **ctx):
            if ref not in seen_refs:
                seen_refs.add(ref)
                ungrounded_refs.append(ref)
    if not ungrounded_refs:
        return None
    return {
        "ungrounded_claim_count": ungrounded_claim_count,
        "ungrounded_refs": ungrounded_refs,
    }


def _count_total_symbol_refs(claims: list | None, sources: list | None) -> int:
    """Count distinct ``path:symbol`` references across a finding's claims/excerpts.

    The denominator for the reproduce-on-HEAD verdict: paired with the
    ungrounded count from :func:`_summarize_ungrounded_claims_grounded`, it lets
    :func:`classify_reproduction` tell "every cited symbol is absent" (reject)
    apart from "some cited symbol still resolves" (partial grounding → pass).
    Deduplicates ``(path, symbol)`` pairs the same way the ungrounded count does,
    pulling from claims plus file-source excerpts (``quote``/``excerpt``).
    """
    seen: set = set()
    texts: list = list(claims or [])
    texts.extend(
        s.get("quote") or s.get("excerpt")
        for s in sources or []
        if isinstance(s, dict) and str(s.get("type") or "").lower() == "file"
    )
    for text in texts:
        for pair in _parse_path_symbol_refs(text):
            seen.add(pair)
    return len(seen)


def _file_source_present_count(sources: list | None) -> int:
    """Count file-type sources that survive validation (not marked invalid).

    A source is counted present unless :func:`validate_source_refs` stamped it
    ``valid: False`` — so an INDETERMINATE file ref (left untouched) counts as
    present, keeping an unknowable citation from ever driving a rejection.
    """
    count = 0
    for s in sources or []:
        if not isinstance(s, dict):
            continue
        if str(s.get("type") or "").lower() != "file":
            continue
        if not s.get("ref"):
            continue
        if s.get("valid") is False:
            continue
        count += 1
    return count
