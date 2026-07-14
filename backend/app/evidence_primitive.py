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
  (capability_proof), ``backing_path`` (capability backing-path verification),
  and ``named_test_present`` (a conventionally-named test file exists at HEAD
  and imports the backing module, but was NEVER executed — a structural-only
  signal distinct from a ``named_test`` that actually ran green).

Nothing in this module performs IO or re-verification: ``classify_evidence``
looks only at the stored ``proven`` flags. A caller that re-checks a ref
against ground truth (e.g. a ``named_test`` / ``backing_path`` whose file was
removed at HEAD) is the one that decides ``contradicted`` and overrides the
stored classification — this module never guesses.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Literal

# Pydantic (< py3.12) requires typing_extensions.TypedDict for a TypedDict used
# as a model field type (see problem_model.Gap.evidence_refs).
from typing_extensions import TypedDict

# The six evidence states a claim's supporting refs can be in.
#   proven       — at least one high-confidence ref is machine-checked proven
#                  (proven=True).
#   path_proven  — all proven refs are low-confidence (kind=backing_path only).
#                  The files exist at HEAD but there is no behavioural verification
#                  — a distinct tier below "proven" that the enforce gate blocks.
#   partial      — refs exist but none backs the claim: either none is proven
#                  yet, or the only proven refs are bare human attestation
#                  (operator_attestation) with no structural machine-check at all.
#   unproven     — no refs at all (the claim is asserted with zero evidence).
#   contradicted — ground-truth re-verification disproved a previously-proven
#                  ref (e.g. its backing file was removed). Never inferred from
#                  the stored flags alone — a caller passes/overrides it.
#   unverified   — structurally uncheckable (malformed refs, or an indeterminate
#                  re-verification that a caller must NOT launder into absence).
EvidenceStatus = Literal[
    "proven", "path_proven", "partial", "unproven", "contradicted", "unverified"
]

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
    "named_test_present",   # capability verification — a convention-named test
                            # file exists at HEAD and imports the backing module,
                            # but was NEVER executed (structural-only, path-tier)
]

#: Every recognized :data:`ProofKind`, as a runtime-checkable set. The
#: ``ProofKind`` Literal is the documented vocabulary but a type alias cannot be
#: membership-tested at runtime, so recording seams that must refuse an
#: unrecognized (e.g. agent-forged) proof kind before minting a proof check
#: against this set. Keep in lockstep with the ``ProofKind`` Literal above.
KNOWN_PROOF_KINDS: frozenset[str] = frozenset(
    {
        "named_test",
        "downstream_artifact",
        "visual",
        "observable_behavior",
        "reachability",
        "operator_attestation",
        "trial_adoption",
        "backing_path",
        "named_test_present",
    }
)

#: The kinds whose ``ref`` names a repo file/test path and can therefore be
#: re-verified against HEAD (file exists / was removed). Other kinds point at
#: logical ids (criterion keys, artifact ids) that a path check cannot resolve.
PATH_BACKED_KINDS: frozenset[str] = frozenset(
    {"named_test", "backing_path", "named_test_present"}
)

#: Proof kinds that are *structural-only* — they prove the declared files exist
#: at HEAD but NOT that anything was machine-verified to work. A ``backing_path``
#: proof only asserts "the code that backs this claim is in the repo";
#: ``named_test_present`` asserts a bit more — a convention-named test file exists
#: at HEAD and imports the backing module — but STILL never executed the test, so
#: it is not the same corroboration bar as a ``named_test`` that actually ran
#: green. When a proven-evidence set consists SOLELY of these kinds, the
#: classifiers demote it from ``proven`` to ``path_proven`` so an enforce-mode gate
#: is not cleared by a self-reported file-existence proof.
BACKING_ONLY_KINDS: frozenset[str] = frozenset(
    {"backing_path", "named_test_present"}
)

#: Proof kinds that carry NO structural machine-check at all — a human simply
#: asserted the claim. ``operator_attestation`` is a person's word that a
#: criterion holds; unlike ``backing_path`` it does not even prove a file exists
#: at HEAD, so it is the weakest tier. When a proven-evidence set consists SOLELY
#: of these kinds, the classifiers demote it to ``partial`` (no structural backing
#: whatsoever). This keeps the shared primitive aligned with the acceptance layer,
#: which already tiers an ``operator_attestation`` row to a distinct, lower
#: ``attested`` coverage state (mapped to ``partial``) rather than full
#: ``covered`` — so a human attestation is never laundered into ``proven`` parity
#: with machine-verified evidence on any surface adopting this contract.
ATTESTATION_ONLY_KINDS: frozenset[str] = frozenset({"operator_attestation"})

#: The union of every non-machine-verified proof kind: structural-only file
#: existence (``backing_path``) plus bare human attestation
#: (``operator_attestation``). A proven set drawn ENTIRELY from these kinds has no
#: machine-verified corroboration and is tiered below ``proven`` by
#: :func:`_proven_tier`.
_NON_MACHINE_KINDS: frozenset[str] = BACKING_ONLY_KINDS | ATTESTATION_ONLY_KINDS

#: Total order over :data:`EvidenceStatus`, ascending in confidence. This is the
#: single canonical ranking of the tiers the seam already owns — the vocabulary
#: and its classifiers live here, so the ORDER over those tiers belongs here too
#: rather than being re-encoded ad hoc in each consumer. The three no-backing
#: states (``contradicted`` / ``unverified`` / ``unproven``) share rank 0: none
#: of them supplies any corroboration, so no consumer should prefer one over
#: another. ``partial`` (refs exist but none proven, or attestation-only) sits
#: above them, then structural-only ``path_proven`` (files exist at HEAD), then
#: fully machine-verified ``proven`` at the top.
EVIDENCE_STATUS_RANK: dict[str, int] = {
    "contradicted": 0,
    "unverified": 0,
    "unproven": 0,
    "partial": 1,
    "path_proven": 2,
    "proven": 3,
}


def evidence_status_rank(status: Any) -> int:
    """Rank an :data:`EvidenceStatus` on the canonical confidence order.

    Pure and IO-free. Normalizes ``status`` case-insensitively (mirroring
    :func:`claim_contradicts_evidence`) and returns its
    :data:`EVIDENCE_STATUS_RANK` ordinal; any unknown / malformed status ranks
    ``0`` (the no-corroboration floor) so a consumer folding two tiers never
    silently upgrades on a value it does not understand.

    Use this whenever code must compare two evidence tiers — e.g. "never
    downgrade a stronger tier when folding a second signal in" — so every
    surface shares one ordering instead of re-deriving it.
    """
    if status is None:
        return 0
    return EVIDENCE_STATUS_RANK.get(str(status).strip().lower(), 0)


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


#: How long a machine-checked proof stays trusted before a caller that opts into
#: freshness re-verification treats it as stale. A ``named_test`` / ``backing_path``
#: proof records the ``recorded_at`` (and ``commit_sha``) of the moment it was
#: earned, but nothing re-runs it afterwards — the backing file or test may have
#: been deleted or materially changed at HEAD since. A proof older than this
#: window is therefore no longer trusted to clear the enforce gate on its own; it
#: is demoted to a non-``proven`` tier (an honest "must re-verify", never
#: laundered into ``contradicted``) so the standing earn loop re-verifies it
#: against HEAD and re-stamps a fresh proof. Chosen well above the hourly earn
#: cadence so a live capability is continuously re-proven long before it expires,
#: while a genuinely abandoned proof does eventually go stale.
PROOF_FRESHNESS_TTL: timedelta = timedelta(days=30)


def _parse_recorded_at(value: Any) -> datetime | None:
    """Parse a stored ISO-8601 ``recorded_at`` into a naive-UTC datetime.

    Returns ``None`` for anything unparseable (missing, non-string, malformed, or
    a bare date with no time) — the caller treats an unreadable timestamp as
    *indeterminate*, never as expired, mirroring the tri-state honesty rule the
    rest of this module follows. Tz-aware inputs (including a ``Z`` suffix) are
    normalized to naive UTC so the comparison never mixes aware/naive datetimes.
    """
    if not isinstance(value, str) or not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None
    if dt.tzinfo is not None:
        dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt


def _ref_is_expired(ref: dict[str, Any], now: datetime, ttl: timedelta) -> bool:
    """Whether a proof ref's ``recorded_at`` is older than ``ttl`` relative to ``now``.

    A ref whose ``recorded_at`` is missing or malformed is NEVER expired — an
    indeterminate age must not be laundered into staleness, exactly as an
    indeterminate re-verification is never laundered into ``contradicted``. This
    keeps every legacy ref that predates timestamped proofs classifying as it
    always did.
    """
    recorded = _parse_recorded_at(ref.get("recorded_at"))
    if recorded is None:
        return False
    now_naive = now
    if now_naive.tzinfo is not None:
        now_naive = now_naive.astimezone(timezone.utc).replace(tzinfo=None)
    return (now_naive - recorded) > ttl


def _active_proven(
    checkable: list[dict[str, Any]],
    now: datetime | None,
    ttl: timedelta | None,
) -> list[dict[str, Any]]:
    """The ``proven=True`` refs that are still fresh (or all of them if opted out).

    When ``now is None`` freshness is not evaluated at all — every ``proven=True``
    ref is returned, preserving the legacy stored-flag-only behaviour for callers
    that do not opt in. When ``now`` is supplied, refs whose ``recorded_at`` is
    older than ``ttl`` (defaulting to :data:`PROOF_FRESHNESS_TTL`) are dropped, so
    a proven set consisting solely of expired proofs collapses to empty and the
    classifiers fall to ``partial`` — the honest "must re-verify" state.
    """
    proven = [r for r in checkable if r.get("proven") is True]
    if now is None:
        return proven
    effective_ttl = ttl if ttl is not None else PROOF_FRESHNESS_TTL
    return [r for r in proven if not _ref_is_expired(r, now, effective_ttl)]


def _proven_tier(proven: list[dict[str, Any]]) -> EvidenceStatus:
    """Tier a NON-EMPTY list of ``proven=True`` refs below full ``proven``.

    Shared by both classifiers so the ``proven`` case reads identically
    everywhere:

    * If EVERY proven ref is a non-machine kind (:data:`_NON_MACHINE_KINDS`):
      there is no machine-verified corroboration, so the set is tiered down.
      A ``backing_path`` at least shows a declared file exists at HEAD
      (structural) → ``path_proven``; an attestation-only set is bare human
      word with nothing structural → ``partial``. A mix of the two keeps the
      structural ``path_proven`` floor (a file does exist).
    * Otherwise at least one machine-verified — or unlabeled — proof exists, so
      the set reads full ``proven``. A ``proven=True`` ref whose ``kind`` is
      missing/blank counts as non-backing (never silently demoted).
    """
    if all(r.get("kind") in _NON_MACHINE_KINDS for r in proven):
        if any(r.get("kind") in BACKING_ONLY_KINDS for r in proven):
            return "path_proven"
        return "partial"
    return "proven"


def classify_evidence(
    refs: Any,
    *,
    now: datetime | None = None,
    ttl: timedelta | None = None,
) -> EvidenceStatus:
    """Classify a list of evidence refs into an :data:`EvidenceStatus`.

    THE single classifier for this vocabulary — :func:`classify_evidence_tiered`
    is an alias of this function, not a second implementation (see the alias note
    below). Every surface therefore grades a given ref set identically.

    Pure and stored-flag-only — it never performs IO or re-verification:

    * ``proven``      — at least one ref is a machine-verified (or unlabeled)
      ``proven=True`` proof. The mixed case counts: one machine-verified proof
      alongside a structural or attestation one still reads ``proven``. A
      ``proven=True`` ref whose ``kind`` is missing/blank is treated as
      NON-backing (so it still reads ``proven``) — an unlabeled proof is never
      silently demoted.
    * ``path_proven`` — the only proven refs are structural-only
      (:data:`BACKING_ONLY_KINDS`, e.g. ``backing_path`` /
      ``named_test_present``): the declared files exist at HEAD but nothing was
      machine-verified, so an enforce-mode gate is NOT cleared by them.
    * ``partial``     — refs exist but none backs the claim: none is proven, OR
      the only proven refs are bare attestation (:data:`ATTESTATION_ONLY_KINDS`,
      i.e. ``operator_attestation``) with no structural backing at all. A human's
      word is never laundered into ``proven`` parity with a ``named_test`` that
      ran green — mirroring the acceptance layer, which already tiers an
      attestation to a lower ``attested`` state rather than full ``covered``.
    * ``unproven``    — the list is present but empty (a claim with zero refs).
    * ``unverified``  — structurally uncheckable: ``refs`` is not a list, or it
      contains only non-dict entries we cannot read a ``proven`` flag from.

    ``contradicted`` is never returned here — it can only come from a caller
    that re-verified a proven ref against ground truth and disproved it.

    **Freshness (opt-in).** When ``now`` is supplied, ``proven=True`` refs whose
    ``recorded_at`` is older than ``ttl`` (defaulting to
    :data:`PROOF_FRESHNESS_TTL`) are dropped before tiering, so a write-once proof
    that has gone stale no longer reads ``proven``/``path_proven`` and falls to
    ``partial`` — an honest "must re-verify". A ref with a missing/malformed
    ``recorded_at`` is treated as indeterminate and never expires. When ``now`` is
    ``None`` (the default) freshness is not evaluated at all and behaviour is
    unchanged from the stored-flag-only classifier.
    """
    if not isinstance(refs, list):
        return "unverified"
    if not refs:
        return "unproven"
    checkable = [r for r in refs if isinstance(r, dict)]
    if not checkable:
        return "unverified"
    proven = _active_proven(checkable, now, ttl)
    if proven:
        return _proven_tier(proven)
    return "partial"


#: Backwards-compatible alias of :func:`classify_evidence` (audit finding #8).
#:
#: These were once two functions: a "plain" classifier and a "tiered" one that
#: demoted a proven set with no machine-verified ref. The tiering was then folded
#: into the plain classifier (both delegate to :func:`_proven_tier`), leaving two
#: byte-for-byte equivalent implementations and a stale docstring story about a
#: distinction that no longer existed — an open invitation for the two copies to
#: drift apart under later edits.
#:
#: There is now exactly ONE implementation. This name is kept as a pure alias (an
#: assignment, deliberately NOT a wrapper function, so drift is impossible by
#: construction) because a dozen call sites and tests import it and the name still
#: reads well at the enforce-gate seams that care about the tiering. New code
#: should prefer :func:`classify_evidence`.
classify_evidence_tiered = classify_evidence


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
