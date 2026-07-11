"""Shared verifiable-evidence primitive for the strategic self-model.

One vocabulary for "is this claim actually proven?", reused across every claim
surface (gaps today; capabilities / acceptance criteria as the design rolls
out) instead of each surface reinventing its own axes. This is deliberately
IO-free and dependency-free — a leaf module that both the durable model
(``problem_model.Gap``) and the standing re-verification check
(``metrics_integrity_sweep``) import without pulling in either.

The vocabulary reuses (rather than reinvents) the axes the acceptance layer
already proved out:

* ``EvidenceStatus`` mirrors the acceptance-gate distinction between
  machine-checked proof and merely-asserted work: ``proven`` means the same
  bar as :func:`app.acceptance_gate.acceptance_is_proven` /
  ``_evidence_proves_covered`` — an evidence ref explicitly marked
  ``proven=True`` — so "proven" means the same thing everywhere.
* ``ProofKind`` is the UNION of the kinds already in use across the codebase:
  ``named_test`` / ``downstream_artifact`` / ``visual`` /
  ``observable_behavior`` (acceptance_gate), ``reachability`` /
  ``operator_attestation`` (CriterionEvidence.source_kind), ``trial_adoption``
  (capability_proof), and ``backing_path`` (capability backing-path
  verification). No new kinds are minted here.

Nothing in this module performs IO or re-verification: ``classify_evidence``
looks only at the stored ``proven`` flags. A caller that re-checks a ref
against ground truth (e.g. a ``named_test`` / ``backing_path`` whose file was
removed at HEAD) is the one that decides ``contradicted`` and overrides the
stored classification — this module never guesses.
"""

from __future__ import annotations

from typing import Any, Literal

# Pydantic (< py3.12) requires typing_extensions.TypedDict for a TypedDict used
# as a model field type (see problem_model.Gap.evidence_refs).
from typing_extensions import TypedDict

# The five evidence states a claim's supporting refs can be in.
#   proven       — at least one ref is machine-checked proven (proven=True).
#   partial      — refs exist but none is proven yet.
#   unproven     — no refs at all (the claim is asserted with zero evidence).
#   contradicted — ground-truth re-verification disproved a previously-proven
#                  ref (e.g. its backing file was removed). Never inferred from
#                  the stored flags alone — a caller passes/overrides it.
#   unverified   — structurally uncheckable (malformed refs, or an indeterminate
#                  re-verification that a caller must NOT launder into absence).
EvidenceStatus = Literal["proven", "partial", "unproven", "contradicted", "unverified"]

# The union of every proof kind already in use across the strategic self-model.
# ``ref`` on an EvidenceRef holds a criterion_key / proof_ref / artifact_id /
# file-or-test path depending on which kind it is.
ProofKind = Literal[
    "named_test",           # acceptance_gate — a test exercising a changed seam
    "downstream_artifact",  # acceptance_gate — a consumed produced artifact
    "visual",               # acceptance_gate — a viewport screenshot pair
    "observable_behavior",  # acceptance_gate — soft, diff-asserted behavior
    "reachability",         # CriterionEvidence.source_kind — reachable code path
    "operator_attestation", # CriterionEvidence.source_kind — human attestation
    "trial_adoption",       # capability_proof — a capability adopted on trial
    "backing_path",         # capability verification — a file backing the claim
]

#: The kinds whose ``ref`` names a repo file/test path and can therefore be
#: re-verified against HEAD (file exists / was removed). Other kinds point at
#: logical ids (criterion keys, artifact ids) that a path check cannot resolve.
PATH_BACKED_KINDS: frozenset[str] = frozenset({"named_test", "backing_path"})

#: Proof kinds that are *structural-only* — they prove the declared files exist
#: at HEAD but NOT that anything was machine-verified to work. A ``backing_path``
#: proof only asserts "the code that backs this claim is in the repo"; it is not
#: the same corroboration bar as a ``named_test`` that actually ran green. When a
#: proven-evidence set consists SOLELY of these kinds, the tier-aware classifier
#: (:func:`classify_evidence_tiered`) demotes it from ``proven`` to ``partial`` so
#: an enforce-mode gate is not cleared by a self-reported file-existence proof.
BACKING_ONLY_KINDS: frozenset[str] = frozenset({"backing_path"})

#: Claimed statuses that assert a gap's work is done/settled. A done-type claim
#: whose evidence does not back it up is the "working/closed but unproven/absent"
#: contradiction. ``active``/``blocked`` are in-progress and never contradict.
_CLAIMED_DONE_STATUSES: frozenset[str] = frozenset(
    {"proven", "working", "closed", "done", "complete", "resolved"}
)

#: Evidence states that fail to back a done-type claim: no refs (``unproven``),
#: refs that exist but were never machine-proven (``partial``), or a proof
#: disproved by re-verification (``contradicted``). ``proven`` backs the claim;
#: ``unverified`` is DELIBERATELY excluded — an indeterminate check must never be
#: laundered into a contradiction (mirrors the tri-state honesty rule in
#: source_ref_validation).
_UNBACKED_STATUSES: frozenset[str] = frozenset(
    {"unproven", "partial", "contradicted"}
)


class EvidenceRef(TypedDict, total=False):
    """One structured pointer at the evidence backing a claim.

    ``total=False`` so every field is optional — older stored payloads and
    partially-populated refs both validate. ``kind`` says what ``ref`` points
    at; ``proven`` is the single machine-checked bar shared with the acceptance
    layer; ``source_id`` / ``recorded_at`` are optional provenance.
    """

    kind: ProofKind
    ref: str
    proven: bool
    source_id: str | None
    recorded_at: str | None


def classify_evidence(refs: Any) -> EvidenceStatus:
    """Classify a list of evidence refs into an :data:`EvidenceStatus`.

    Pure and stored-flag-only — it never performs IO or re-verification:

    * ``proven``     — at least one ref is explicitly ``proven=True``.
    * ``partial``    — refs exist but none is proven.
    * ``unproven``   — the list is present but empty (a claim with zero refs).
    * ``unverified`` — structurally uncheckable: ``refs`` is not a list, or it
      contains only non-dict entries we cannot read a ``proven`` flag from.

    ``contradicted`` is never returned here — it can only come from a caller
    that re-verified a proven ref against ground truth and disproved it.
    """
    if not isinstance(refs, list):
        return "unverified"
    if not refs:
        return "unproven"
    checkable = [r for r in refs if isinstance(r, dict)]
    if not checkable:
        # Refs are present but structurally unreadable — do not claim "unproven"
        # (which reads as a definite absence of proof); stay honest.
        return "unverified"
    if any(r.get("proven") is True for r in checkable):
        return "proven"
    return "partial"


def classify_evidence_tiered(refs: Any) -> EvidenceStatus:
    """Classify evidence refs, tiering structural-only proofs below machine-verified.

    Identical to :func:`classify_evidence` in every case EXCEPT the ``proven``
    one: when the ONLY ``proven=True`` refs are all of a
    :data:`BACKING_ONLY_KINDS` kind (currently just ``backing_path``), this
    returns ``"partial"`` instead of ``"proven"``. A ``backing_path`` proof only
    asserts that the declared files exist at HEAD — it is a self-report that code
    is present, not machine-verified corroboration that it works. So a capability
    whose sole proof is ``backing_path`` must not clear an enforce-mode gate as
    strongly as a ``named_test`` that actually ran green.

    The mixed case is preserved as ``proven``: when at least one
    non-``backing_path`` ``proven=True`` ref exists alongside a ``backing_path``
    one, the structural proof is corroborated by machine-verified evidence and the
    set reads ``proven``. A ``proven=True`` ref whose ``kind`` is missing/blank is
    treated as NON-backing (so it still reads ``proven``), staying conservative —
    an unlabeled proof is never silently demoted.

    ``partial`` / ``unproven`` / ``unverified`` are returned identically to
    :func:`classify_evidence`; ``contradicted`` is never returned here.
    """
    if not isinstance(refs, list):
        return "unverified"
    if not refs:
        return "unproven"
    checkable = [r for r in refs if isinstance(r, dict)]
    if not checkable:
        return "unverified"
    proven = [r for r in checkable if r.get("proven") is True]
    if not proven:
        return "partial"
    # At least one proven ref. If EVERY proven ref is a structural-only kind
    # (backing_path), demote to partial — no machine-verified proof corroborates
    # it. A missing/blank ``kind`` counts as non-backing, so it keeps ``proven``.
    if all(r.get("kind") in BACKING_ONLY_KINDS for r in proven):
        return "partial"
    return "proven"


def claim_contradicts_evidence(
    claimed_status: Any, evidence_status: Any
) -> bool:
    """Whether a claimed status is contradicted by its evidence status.

    The single place the "working/closed but unproven/absent" contradiction is
    defined. Returns ``True`` only when the claim asserts the work is
    done/settled (:data:`_CLAIMED_DONE_STATUSES`) AND the evidence fails to back
    it (:data:`_UNBACKED_STATUSES` — ``unproven`` / ``partial`` /
    ``contradicted``). Only machine-checked ``proven`` evidence backs a
    done-type claim, mirroring the acceptance-layer bar.

    An in-progress claim (``active``/``blocked``) never contradicts — work
    without proof yet is expected, not a lie. ``unverified`` evidence never
    contradicts either: an indeterminate re-verification (git/API timeout, rate
    limit) must NOT be laundered into a contradiction.
    """
    if claimed_status is None:
        return False
    claimed = str(claimed_status).strip().lower()
    if claimed not in _CLAIMED_DONE_STATUSES:
        return False
    return str(evidence_status).strip().lower() in _UNBACKED_STATUSES
