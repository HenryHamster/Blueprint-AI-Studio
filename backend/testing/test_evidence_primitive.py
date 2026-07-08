"""Unit tests for the shared verifiable-evidence primitive.

Covers the closed vocabularies (EvidenceStatus / ProofKind / PATH_BACKED_KINDS)
and the two pure classifiers (classify_evidence, claim_contradicts_evidence).
The module is IO-free, so these are plain synchronous unit tests.
"""

from typing import get_args

from app.evidence_primitive import (
    PATH_BACKED_KINDS,
    EvidenceStatus,
    ProofKind,
    claim_contradicts_evidence,
    classify_evidence,
)


# --- Vocabulary membership ---------------------------------------------------


def test_evidence_status_literal_members():
    assert set(get_args(EvidenceStatus)) == {
        "proven",
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


# --- classify_evidence -------------------------------------------------------


def test_classify_proven_when_any_ref_proven():
    refs = [
        {"kind": "named_test", "ref": "a/b.py", "proven": False},
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
