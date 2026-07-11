"""Unit tests for the shared verifiable-evidence primitive.

Covers the closed vocabularies (EvidenceStatus / ProofKind / PATH_BACKED_KINDS)
and the two pure classifiers (classify_evidence, claim_contradicts_evidence).
The module is IO-free, so these are plain synchronous unit tests.
"""

from typing import get_args

from app.evidence_primitive import (
    BACKING_ONLY_KINDS,
    PATH_BACKED_KINDS,
    EvidenceStatus,
    ProofKind,
    claim_contradicts_evidence,
    classify_evidence,
    classify_evidence_tiered,
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
    # The union of every proof kind already in use across the self-model — no
    # new kinds minted here.
    assert set(get_args(ProofKind)) == {
        "named_test",
        "downstream_artifact",
        "visual",
        "observable_behavior",
        "reachability",
        "operator_attestation",
        "trial_adoption",
        "backing_path",
    }


def test_path_backed_kinds_are_file_pathish():
    assert set(PATH_BACKED_KINDS) == {"named_test", "backing_path"}
    # Every path-backed kind is a real ProofKind.
    assert set(get_args(ProofKind)) >= PATH_BACKED_KINDS


def test_backing_only_kinds_is_backing_path():
    # Only structural-only (file-existence) kinds belong here; a backing_path
    # proof alone is not machine-verified corroboration.
    assert set(BACKING_ONLY_KINDS) == {"backing_path"}
    # Every backing-only kind is a real ProofKind.
    assert set(get_args(ProofKind)) >= BACKING_ONLY_KINDS


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
