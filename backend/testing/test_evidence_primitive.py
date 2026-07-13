"""Unit tests for the shared verifiable-evidence primitive.

Covers the closed vocabularies (EvidenceStatus / ProofKind / PATH_BACKED_KINDS)
and the two pure classifiers (classify_evidence, claim_contradicts_evidence).
The module is IO-free, so these are plain synchronous unit tests.
"""

from typing import get_args

from app.evidence_primitive import (
    ATTESTATION_ONLY_KINDS,
    BACKING_ONLY_KINDS,
    EVIDENCE_STATUS_RANK,
    PATH_BACKED_KINDS,
    EvidenceStatus,
    ProofKind,
    claim_contradicts_evidence,
    classify_evidence,
    classify_evidence_tiered,
    evidence_status_rank,
)


# --- Vocabulary membership ---------------------------------------------------


def test_evidence_status_literal_members():
    assert set(get_args(EvidenceStatus)) == {
        "proven",
        "path_proven",
        "partial",
        "unproven",
        "contradicted",
        "unverified",
    }


def test_proof_kind_is_union_of_existing_kinds():
    # The union of every proof kind in use across the self-model.
    # ``named_test_present`` is the structural-only tier: a convention-named test
    # file exists at HEAD and imports the backing module, but was never executed.
    assert set(get_args(ProofKind)) == {
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


def test_path_backed_kinds_are_file_pathish():
    assert set(PATH_BACKED_KINDS) == {
        "named_test",
        "backing_path",
        "named_test_present",
    }
    # Every path-backed kind is a real ProofKind.
    assert set(get_args(ProofKind)) >= PATH_BACKED_KINDS


def test_backing_only_kinds_is_backing_path():
    # Only structural-only (file-existence) kinds belong here; neither a
    # backing_path proof nor a never-executed named_test_present proof is
    # machine-verified corroboration.
    assert set(BACKING_ONLY_KINDS) == {"backing_path", "named_test_present"}
    # Every backing-only kind is a real ProofKind.
    assert set(get_args(ProofKind)) >= BACKING_ONLY_KINDS


def test_attestation_only_kinds_is_operator_attestation():
    # Only bare human-attestation kinds belong here; an operator_attestation
    # alone carries no structural machine-check.
    assert set(ATTESTATION_ONLY_KINDS) == {"operator_attestation"}
    # Every attestation-only kind is a real ProofKind.
    assert set(get_args(ProofKind)) >= ATTESTATION_ONLY_KINDS
    # Structural-only and attestation-only tiers are disjoint — a backing_path
    # (file exists at HEAD) is a strictly different, higher tier than bare word.
    assert set(ATTESTATION_ONLY_KINDS).isdisjoint(BACKING_ONLY_KINDS)


# --- classify_evidence -------------------------------------------------------


def test_classify_path_proven_when_only_backing_path_proven():
    """A backing_path ref with proven=True yields 'path_proven' — the files
    exist at HEAD but there is no behavioural verification."""
    refs = [
        {"kind": "named_test", "ref": "a/b.py", "proven": False},
        {"kind": "backing_path", "ref": "c/d.py", "proven": True},
    ]
    assert classify_evidence(refs) == "path_proven"


def test_classify_proven_when_high_confidence_ref_proven():
    """A single high-confidence proven ref (named_test) lifts the classification
    to 'proven' even when other refs are unproven or low-confidence."""
    refs = [
        {"kind": "named_test", "ref": "a/b.py", "proven": True},
        {"kind": "backing_path", "ref": "c/d.py", "proven": False},
    ]
    assert classify_evidence(refs) == "proven"


def test_classify_proven_when_mixed_high_and_low_confidence():
    """A mix of high-confidence (named_test) and low-confidence (backing_path)
    proven refs returns 'proven' — a single high-confidence ref lifts the whole."""
    refs = [
        {"kind": "named_test", "ref": "a/b.py", "proven": True},
        {"kind": "backing_path", "ref": "c/d.py", "proven": True},
    ]
    assert classify_evidence(refs) == "proven"


def test_classify_partial_when_refs_exist_none_proven():
    refs = [
        {"kind": "named_test", "ref": "a/b.py", "proven": False},
        {"kind": "observable_behavior", "ref": "did a thing"},
    ]
    assert classify_evidence(refs) == "partial"


def test_classify_unproven_when_no_refs():
    assert classify_evidence([]) == "unproven"


def test_classify_unverified_when_not_a_list():
    assert classify_evidence(None) == "unverified"
    assert classify_evidence("nope") == "unverified"
    assert classify_evidence({"kind": "named_test"}) == "unverified"


def test_classify_unverified_when_refs_structurally_unreadable():
    # Present-but-unreadable refs must NOT read as a definite absence of proof.
    assert classify_evidence(["not-a-dict", 42]) == "unverified"


def test_classify_never_returns_contradicted_on_its_own():
    # contradicted is caller-supplied (ground-truth re-verification) only.
    refs = [{"kind": "named_test", "ref": "a/b.py", "proven": True}]
    assert classify_evidence(refs) != "contradicted"


def test_classify_treats_only_true_as_proven():
    # A truthy-but-not-True proven flag is not machine-checked proof.
    refs = [{"kind": "named_test", "ref": "a/b.py", "proven": 1}]
    assert classify_evidence(refs) == "partial"


# --- classify_evidence_tiered ------------------------------------------------


def test_tiered_backing_path_only_returns_path_proven():
    # A proven set whose ONLY proof is backing_path returns path_proven in both
    # classifiers — files exist at HEAD but nothing was machine-verified.
    refs = [{"kind": "backing_path", "ref": "backend/app/foo.py", "proven": True}]
    assert classify_evidence_tiered(refs) == "path_proven"
    assert classify_evidence(refs) == "path_proven"


def test_tiered_multiple_backing_path_only_returns_path_proven():
    # Even several backing_path proofs together are structural-only → path_proven.
    refs = [
        {"kind": "backing_path", "ref": "backend/app/a.py", "proven": True},
        {"kind": "backing_path", "ref": "backend/app/b.py", "proven": True},
    ]
    assert classify_evidence_tiered(refs) == "path_proven"


def test_tiered_mixed_backing_and_named_test_returns_proven():
    # A backing_path proof corroborated by a machine-verified named_test reads
    # proven — the mixed case is NOT demoted.
    refs = [
        {"kind": "backing_path", "ref": "backend/app/foo.py", "proven": True},
        {"kind": "named_test", "ref": "test_foo.py::test_bar", "proven": True},
    ]
    assert classify_evidence_tiered(refs) == "proven"


def test_tiered_named_test_only_returns_proven():
    # A lone machine-verified named_test is proven, exactly like the plain path.
    refs = [{"kind": "named_test", "ref": "test_foo.py::test_bar", "proven": True}]
    assert classify_evidence_tiered(refs) == "proven"


def test_tiered_named_test_present_only_returns_path_proven():
    # A ``named_test_present`` proof — a convention test file exists at HEAD and
    # imports the module, but was NEVER executed — is structural-only and must
    # NOT be laundered into the executed ``proven`` tier. Both classifiers demote
    # it to ``path_proven`` so it cannot clear the enforce gate.
    refs = [
        {"kind": "named_test_present", "ref": "test_foo.py", "proven": True}
    ]
    assert classify_evidence_tiered(refs) == "path_proven"
    assert classify_evidence(refs) == "path_proven"


def test_tiered_named_test_present_with_backing_path_returns_path_proven():
    # Two structural-only kinds together are still structural-only → path_proven.
    refs = [
        {"kind": "named_test_present", "ref": "test_foo.py", "proven": True},
        {"kind": "backing_path", "ref": "backend/app/foo.py", "proven": True},
    ]
    assert classify_evidence_tiered(refs) == "path_proven"


def test_tiered_named_test_present_plus_executed_named_test_returns_proven():
    # A never-executed named_test_present corroborated by an executed named_test
    # reads proven — the mixed case keeps the machine-verified floor.
    refs = [
        {"kind": "named_test_present", "ref": "test_foo.py", "proven": True},
        {"kind": "named_test", "ref": "test_foo.py::test_bar", "proven": True},
    ]
    assert classify_evidence_tiered(refs) == "proven"


def test_tiered_proven_ref_with_missing_kind_returns_proven():
    # An unlabeled proven ref counts as non-backing → stays proven (conservative:
    # never silently demote a proof whose kind we cannot read).
    refs = [{"ref": "something", "proven": True}]
    assert classify_evidence_tiered(refs) == "proven"


def test_tiered_no_refs_returns_unproven():
    assert classify_evidence_tiered([]) == "unproven"


def test_tiered_partial_unchanged():
    # Refs present but none proven → partial, identical to classify_evidence.
    refs = [
        {"kind": "backing_path", "ref": "backend/app/foo.py", "proven": False},
        {"kind": "named_test", "ref": "test_foo.py", "proven": False},
    ]
    assert classify_evidence_tiered(refs) == "partial"
    assert classify_evidence(refs) == "partial"


def test_tiered_unverified_parity_with_classify_evidence():
    # Structurally uncheckable inputs classify identically to the plain path.
    assert classify_evidence_tiered(None) == "unverified"
    assert classify_evidence_tiered("nope") == "unverified"
    assert classify_evidence_tiered(["not-a-dict", 42]) == "unverified"


def test_tiered_backing_path_truthy_not_true_is_partial():
    # A truthy-but-not-True proven flag is not proof at all → partial (no proven
    # ref exists), matching classify_evidence.
    refs = [{"kind": "backing_path", "ref": "backend/app/foo.py", "proven": 1}]
    assert classify_evidence_tiered(refs) == "partial"


def test_tiered_never_returns_contradicted():
    refs = [{"kind": "backing_path", "ref": "a/b.py", "proven": True}]
    assert classify_evidence_tiered(refs) != "contradicted"


# --- attestation-only tiering ------------------------------------------------


def test_tiered_demotes_attestation_only():
    """A proven set whose ONLY proof is operator_attestation is demoted below
    ``proven`` — a human's word is not machine-verified corroboration and, unlike
    a backing_path, does not even prove a file exists at HEAD, so it is the
    weakest tier: ``partial`` (not ``path_proven``, not ``proven``). Both
    classifiers agree so no surface adopting the shared contract launders an
    attestation into proven-parity."""
    refs = [
        {"kind": "operator_attestation", "ref": "c-abc123", "proven": True},
    ]
    assert classify_evidence_tiered(refs) == "partial"
    assert classify_evidence(refs) == "partial"


def test_tiered_multiple_attestation_only_returns_partial():
    # Several attestation proofs together are still bare word → partial; there is
    # no structural backing to lift them, even in aggregate.
    refs = [
        {"kind": "operator_attestation", "ref": "c-a", "proven": True},
        {"kind": "operator_attestation", "ref": "c-b", "proven": True},
    ]
    assert classify_evidence_tiered(refs) == "partial"
    assert classify_evidence(refs) == "partial"


def test_tiered_attestation_plus_named_test_returns_proven():
    # A machine-verified named_test corroborating an attestation lifts the whole
    # set to proven — the mixed-with-machine case is NOT demoted.
    refs = [
        {"kind": "operator_attestation", "ref": "c-abc123", "proven": True},
        {"kind": "named_test", "ref": "test_foo.py::test_bar", "proven": True},
    ]
    assert classify_evidence_tiered(refs) == "proven"
    assert classify_evidence(refs) == "proven"


def test_tiered_attestation_plus_backing_path_returns_path_proven():
    # Attestation gives nothing structural, but a backing_path alongside it does
    # prove a file exists at HEAD → path_proven (the structural floor), never
    # proven (still no machine-verification).
    refs = [
        {"kind": "operator_attestation", "ref": "c-abc123", "proven": True},
        {"kind": "backing_path", "ref": "backend/app/foo.py", "proven": True},
    ]
    assert classify_evidence_tiered(refs) == "path_proven"
    assert classify_evidence(refs) == "path_proven"


def test_tiered_attestation_truthy_not_true_is_partial():
    # A truthy-but-not-True proven flag is not proof at all → partial (no proven
    # ref exists), matching the backing_path truthy case.
    refs = [{"kind": "operator_attestation", "ref": "c-abc123", "proven": 1}]
    assert classify_evidence_tiered(refs) == "partial"
    assert classify_evidence(refs) == "partial"


def test_attestation_only_done_claim_is_contradicted():
    # v25 intent: an attestation-only proof set no longer backs a done-type claim.
    # classify_evidence returns partial, which claim_contradicts_evidence flags as
    # the "claimed not earned" contradiction.
    refs = [{"kind": "operator_attestation", "ref": "c-abc123", "proven": True}]
    assert claim_contradicts_evidence("closed", classify_evidence(refs)) is True
    assert (
        claim_contradicts_evidence("closed", classify_evidence_tiered(refs)) is True
    )


# --- claim_contradicts_evidence ----------------------------------------------


def test_done_claim_contradicted_by_unbacked_evidence():
    # Only machine-checked proven evidence backs a done-type claim; unproven,
    # partial (refs but nothing proven), and contradicted all fail it.
    assert claim_contradicts_evidence("closed", "unproven") is True
    assert claim_contradicts_evidence("closed", "partial") is True
    assert claim_contradicts_evidence("working", "contradicted") is True
    assert claim_contradicts_evidence("proven", "unproven") is True


def test_done_claim_not_contradicted_by_proven_evidence():
    assert claim_contradicts_evidence("closed", "proven") is False
    assert claim_contradicts_evidence("working", "proven") is False


def test_done_claim_not_contradicted_by_path_proven_evidence():
    """path_proven means the files exist at HEAD — there IS some backing,
    just not enough for the enforce gate. It must not be treated as a
    contradiction (which would flag a false positive)."""
    assert claim_contradicts_evidence("closed", "path_proven") is False
    assert claim_contradicts_evidence("working", "path_proven") is False


def test_indeterminate_evidence_never_contradicts():
    # An unverified re-check must never be laundered into a contradiction.
    assert claim_contradicts_evidence("closed", "unverified") is False


def test_in_progress_claim_never_contradicts():
    # active/blocked are work-in-progress; unproven evidence is expected there.
    assert claim_contradicts_evidence("active", "unproven") is False
    assert claim_contradicts_evidence("blocked", "contradicted") is False


def test_none_claim_never_contradicts():
    assert claim_contradicts_evidence(None, "unproven") is False


def test_claim_status_is_case_insensitive():
    assert claim_contradicts_evidence("CLOSED", "UNPROVEN") is True


# --- Freshness / expiry (opt-in) ---------------------------------------------


def _fresh_now():
    from datetime import datetime

    return datetime(2026, 7, 12, 0, 0, 0)


def test_expired_proven_ref_demotes_to_partial():
    """A proven ref older than PROOF_FRESHNESS_TTL drops out when now is supplied."""
    from datetime import timedelta

    from app.evidence_primitive import PROOF_FRESHNESS_TTL

    now = _fresh_now()
    stale = (now - PROOF_FRESHNESS_TTL - timedelta(days=1)).isoformat()
    ref = {"kind": "named_test", "ref": "t", "proven": True, "recorded_at": stale}
    # Both classifiers agree the stale-only set is no longer proven.
    assert classify_evidence([ref], now=now) == "partial"
    assert classify_evidence_tiered([ref], now=now) == "partial"


def test_fresh_proven_ref_still_proven():
    from datetime import timedelta

    now = _fresh_now()
    fresh = (now - timedelta(days=1)).isoformat()
    ref = {"kind": "named_test", "ref": "t", "proven": True, "recorded_at": fresh}
    assert classify_evidence([ref], now=now) == "proven"
    assert classify_evidence_tiered([ref], now=now) == "proven"


def test_freshness_boundary_z_suffix_parsed():
    """A 'Z'-suffixed recorded_at is parsed and compared correctly."""
    from datetime import timedelta

    from app.evidence_primitive import PROOF_FRESHNESS_TTL

    now = _fresh_now()
    stale = (now - PROOF_FRESHNESS_TTL - timedelta(days=1)).isoformat() + "Z"
    ref = {"kind": "named_test", "ref": "t", "proven": True, "recorded_at": stale}
    assert classify_evidence_tiered([ref], now=now) == "partial"


def test_now_none_never_expires():
    """Default (now=None) evaluates no freshness — ancient proofs stay proven."""
    ref = {
        "kind": "named_test",
        "ref": "t",
        "proven": True,
        "recorded_at": "2000-01-01T00:00:00",
    }
    assert classify_evidence(ref if isinstance(ref, list) else [ref]) == "proven"
    assert classify_evidence_tiered([ref]) == "proven"


def test_malformed_recorded_at_never_expires():
    now = _fresh_now()
    bad = {"kind": "named_test", "ref": "t", "proven": True, "recorded_at": "xx"}
    missing = {"kind": "named_test", "ref": "t", "proven": True}
    assert classify_evidence_tiered([bad], now=now) == "proven"
    assert classify_evidence_tiered([missing], now=now) == "proven"


def test_expired_backing_path_only_falls_to_partial():
    """An expired backing_path-only set collapses to partial (no proven left)."""
    from datetime import timedelta

    from app.evidence_primitive import PROOF_FRESHNESS_TTL

    now = _fresh_now()
    stale = (now - PROOF_FRESHNESS_TTL - timedelta(days=1)).isoformat()
    ref = {"kind": "backing_path", "ref": "f.py", "proven": True, "recorded_at": stale}
    # Fresh would be path_proven; expired drops the only proven ref → partial.
    assert classify_evidence_tiered([ref], now=now) == "partial"


# --- Tier total-order (evidence_status_rank / EVIDENCE_STATUS_RANK) -----------


def test_c0f76cf44_evidence_primitive_tier():
    """Acceptance seam for c-0f76cf44: the evidence-primitive tier order is a
    strict, monotone total order that a fold can trust to "never downgrade a
    stronger tier".

    Exercises the hoisted ordering end-to-end: fully machine-verified ``proven``
    outranks structural-only ``path_proven``, which outranks ``partial``, which
    outranks the three no-corroboration states — and those three tie at the
    floor so no consumer prefers one indeterminate/absent signal over another.
    """
    # Strict ascending chain across the confidence tiers.
    assert (
        evidence_status_rank("unproven")
        < evidence_status_rank("partial")
        < evidence_status_rank("path_proven")
        < evidence_status_rank("proven")
    )
    # The three no-backing states share the floor rank — none corroborates.
    assert (
        evidence_status_rank("contradicted")
        == evidence_status_rank("unverified")
        == evidence_status_rank("unproven")
        == 0
    )
    # path_proven ranks strictly between partial and proven (the structural tier).
    assert (
        evidence_status_rank("partial")
        < evidence_status_rank("path_proven")
        < evidence_status_rank("proven")
    )


def test_evidence_status_rank_case_insensitive():
    assert evidence_status_rank("PROVEN") == evidence_status_rank("proven")
    assert evidence_status_rank("  Path_Proven  ") == evidence_status_rank(
        "path_proven"
    )


def test_evidence_status_rank_unknown_is_zero():
    # Unknown / malformed / None statuses fall to the no-corroboration floor so
    # a fold never silently upgrades on a value it does not understand.
    assert evidence_status_rank("nonsense") == 0
    assert evidence_status_rank("") == 0
    assert evidence_status_rank(None) == 0
    assert evidence_status_rank(object()) == 0


def test_evidence_status_rank_covers_every_status():
    # Every EvidenceStatus member must have an explicit rank (no member falls to
    # the unknown-zero default by accident).
    for status in get_args(EvidenceStatus):
        assert status in EVIDENCE_STATUS_RANK
        assert evidence_status_rank(status) == EVIDENCE_STATUS_RANK[status]


def test_evidence_status_rank_matches_backing_path_tier():
    # A backing_path-only proven set classifies to path_proven, which must rank
    # above attestation-only (partial) and below machine-verified (proven).
    path_proven = classify_evidence_tiered(
        [{"kind": "backing_path", "proven": True}]
    )
    attested = classify_evidence_tiered(
        [{"kind": "operator_attestation", "proven": True}]
    )
    machine = classify_evidence_tiered([{"kind": "named_test", "proven": True}])
    assert (
        evidence_status_rank(attested)
        < evidence_status_rank(path_proven)
        < evidence_status_rank(machine)
    )
