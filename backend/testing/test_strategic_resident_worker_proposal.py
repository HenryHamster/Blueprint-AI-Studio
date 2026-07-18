"""Worker memory-proposal admission contract (M4 increment 5).

The admission gate is the security-critical heart of "admit worker memory
proposals only through Resident reconciliation": revision-linked, evidence-gated,
fail-closed. These lock every rejection path plus the happy path.
"""

import pytest
from pydantic import ValidationError

from app.strategic_resident_evidence_validation import TrustedEvidenceIndex
from app.strategic_resident_reconciliation_validation import DecisiveEvidenceRequired
from app.strategic_resident_worker_proposal import (
    WorkerMemoryProposal,
    WorkerProposalRejected,
    admit_worker_memory_proposals,
)


_HASH = "a" * 64
_EV_KEY = ("task", "task-1", "task:v1", "c" * 64)


def _index(*keys):
    return TrustedEvidenceIndex(context_hash="ctx", entries=frozenset(keys))


def _proposal(**overrides):
    payload = {
        "proposal_id": "p1",
        "layer": "working_strategy",
        "updates": {"priorities": {"ship_gate": True}},
        "rationale": "CI evidence supports tightening the gate.",
        "target_checkpoint_version": 4,
        "target_checkpoint_hash": _HASH,
        "evidence": (
            {
                "evidence_id": "e1",
                "kind": "task",
                "source_id": "task-1",
                "revision": "task:v1",
                "checksum": "c" * 64,
            },
        ),
    }
    payload.update(overrides)
    return WorkerMemoryProposal.model_validate(payload)


def test_admits_a_revision_valid_evidence_backed_proposal():
    admitted = admit_worker_memory_proposals(
        [_proposal()],
        current_checkpoint_version=4,
        current_checkpoint_hash=_HASH,
        trusted_evidence=_index(_EV_KEY),
    )
    assert len(admitted) == 1
    assert admitted[0].proposal_id == "p1"
    assert admitted[0].layer == "working_strategy"
    assert admitted[0].updates == {"priorities": {"ship_gate": True}}
    assert admitted[0].evidence_ids == ["e1"]


def test_stale_version_revision_link_is_rejected():
    with pytest.raises(WorkerProposalRejected, match="revision-link is stale"):
        admit_worker_memory_proposals(
            [_proposal()],
            current_checkpoint_version=5,  # advanced past the proposal's target 4
            current_checkpoint_hash=_HASH,
            trusted_evidence=_index(_EV_KEY),
        )


def test_stale_hash_revision_link_is_rejected():
    with pytest.raises(WorkerProposalRejected, match="revision-link is stale"):
        admit_worker_memory_proposals(
            [_proposal()],
            current_checkpoint_version=4,
            current_checkpoint_hash="b" * 64,  # checkpoint content changed
            trusted_evidence=_index(_EV_KEY),
        )


def test_untrusted_evidence_is_rejected():
    with pytest.raises(DecisiveEvidenceRequired, match="absent from the trusted"):
        admit_worker_memory_proposals(
            [_proposal()],
            current_checkpoint_version=4,
            current_checkpoint_hash=_HASH,
            trusted_evidence=_index(("pr", "x", "y", "z")),  # different evidence
        )


def test_evidence_free_proposal_is_rejected():
    with pytest.raises(DecisiveEvidenceRequired, match="must cite decisive evidence"):
        admit_worker_memory_proposals(
            [_proposal(evidence=())],
            current_checkpoint_version=4,
            current_checkpoint_hash=_HASH,
            trusted_evidence=_index(_EV_KEY),
        )


def test_duplicate_proposal_ids_are_rejected():
    with pytest.raises(WorkerProposalRejected, match="duplicate worker proposal id"):
        admit_worker_memory_proposals(
            [_proposal(), _proposal()],
            current_checkpoint_version=4,
            current_checkpoint_hash=_HASH,
            trusted_evidence=_index(_EV_KEY),
        )


def test_empty_updates_are_rejected_by_shape_validation():
    # Reuses ResidentMemoryProposal's validator, so malformed updates never reach
    # the apply path even though they pass the WorkerMemoryProposal shape.
    with pytest.raises(ValidationError):
        admit_worker_memory_proposals(
            [_proposal(updates={})],
            current_checkpoint_version=4,
            current_checkpoint_hash=_HASH,
            trusted_evidence=_index(_EV_KEY),
        )


def test_open_loops_in_updates_are_rejected():
    with pytest.raises(ValidationError):
        admit_worker_memory_proposals(
            [_proposal(updates={"open_loops": [{"loop_id": "x"}]})],
            current_checkpoint_version=4,
            current_checkpoint_hash=_HASH,
            trusted_evidence=_index(_EV_KEY),
        )


def test_one_bad_proposal_admits_nothing():
    # Fail-closed: a batch with a stale proposal after a valid one admits nothing.
    with pytest.raises(WorkerProposalRejected):
        admit_worker_memory_proposals(
            [
                _proposal(proposal_id="ok"),
                _proposal(proposal_id="stale", target_checkpoint_version=99),
            ],
            current_checkpoint_version=4,
            current_checkpoint_hash=_HASH,
            trusted_evidence=_index(_EV_KEY),
        )
