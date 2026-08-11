"""Validation and scoring helpers for the release bootstrap rehearsal receipt.

The host already owns the privileged rehearsal.  This module keeps the backend
side deliberately small: accept only a successful, structurally complete
receipt whose revision is the candidate being qualified, and expose that fact
to the qualification scorer without recomputing host checks on read.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

from app import github

PREPROMOTION_REHEARSAL_KIND = "blueprint.local_app_release_rehearsal.v1"
PREPROMOTION_REHEARSAL_CRITERION_KEY = "c-cafe3815"
# Keep this tuple aligned with scripts.release_preconditions.PRE_STOP_PRECONDITIONS.
# The host emits these labels in RehearsalReport.checked; receipt validation must
# verify the content of that report, not merely its JSON shape.
PREPROMOTION_REHEARSAL_PRECONDITIONS = (
    "live systemd privilege boundary",
    "worker secret and static migration inputs",
    "candidate migration reachability from live schema",
)
PREPROMOTION_REHEARSAL_WORKFLOW = "deploy.yml"
PREPROMOTION_REHEARSAL_INPUTS: dict[str, str] = {
    # Building the backend image is needed so the host can pull the candidate.
    # deploy-backend is independently gated by recovery_data_reconciled in the
    # workflow; these explicit false values fence every other side effect.
    "deploy_backend": "true",
    "deploy_frontend": "false",
    "deploy_agent": "false",
    "run_migrations": "false",
    "recovery_data_reconciled": "false",
}
_SHA40_RE = re.compile(r"^[0-9a-f]{40}$")

# Evidence producers are ordered before the promotion gate.  This registry is
# intentionally small and declarative: a new promotion criterion must name a
# producer phase that is earlier than the gate, or the qualifier refuses to
# grade the attempt instead of discovering the cycle during a release.
_PREPROMOTION_GRADEABILITY_PHASES = {
    "qualification_setup": 10,
    "qualification_evidence": 20,
    "promotion_gate": 30,
}
PREPROMOTION_GRADEABILITY_CONTRACTS: dict[str, dict[str, Any]] = {
    PREPROMOTION_REHEARSAL_CRITERION_KEY: {
        "producer": "prepromotion_rehearsal_receipt",
        "producer_phase": "qualification_setup",
        "gate_phase": "promotion_gate",
        "prerequisites": ("candidate_ref_resolves_before_dispatch",),
    }
}


def validate_prepromotion_gradeability(
    active_categories: list[Mapping[str, Any]],
    *,
    contracts: Mapping[str, Mapping[str, Any]] | None = None,
) -> None:
    """Reject promotion criteria whose evidence producer is not pre-gradeable.

    Every active category can force ``not_met`` during qualification.  Ordinary
    categories are produced by the blind merge-grade in the qualification
    evidence phase; special machine-backed categories must be listed in
    ``PREPROMOTION_GRADEABILITY_CONTRACTS``.  A producer at or after the gate,
    or one with an unknown phase, is a circular/deadlocked contract and fails
    closed before promotion work begins.
    """
    declared = contracts or PREPROMOTION_GRADEABILITY_CONTRACTS
    for category in active_categories:
        key = str(
            category.get("category_key") or category.get("criterion_key") or ""
        ).strip()
        if not key:
            raise ValueError("active promotion criterion has no stable key")
        contract = declared.get(key) or {
            "producer": "blind_merge_grade",
            "producer_phase": "qualification_evidence",
            "gate_phase": "promotion_gate",
        }
        producer = str(contract.get("producer") or "").strip()
        producer_phase = str(contract.get("producer_phase") or "").strip()
        gate_phase = str(contract.get("gate_phase") or "").strip()
        if not producer:
            raise ValueError(f"criterion {key} has no evidence producer")
        if (
            producer_phase not in _PREPROMOTION_GRADEABILITY_PHASES
            or gate_phase not in _PREPROMOTION_GRADEABILITY_PHASES
        ):
            raise ValueError(
                f"criterion {key} has an unknown gradeability phase "
                f"({producer_phase!r} -> {gate_phase!r})"
            )
        if (
            _PREPROMOTION_GRADEABILITY_PHASES[producer_phase]
            >= _PREPROMOTION_GRADEABILITY_PHASES[gate_phase]
        ):
            raise ValueError(
                f"criterion {key} evidence producer {producer!r} runs at or "
                f"after its promotion gate"
            )
        if key == PREPROMOTION_REHEARSAL_CRITERION_KEY and (
            "candidate_ref_resolves_before_dispatch"
            not in contract.get("prerequisites", ())
        ):
            raise ValueError(
                f"criterion {key} is missing the candidate ref resolution "
                "precondition"
            )


def prepromotion_rehearsal_ref(version_number: int) -> str:
    """Return the candidate-pinned release branch ref used by the build."""
    if isinstance(version_number, bool) or not isinstance(version_number, int):
        raise ValueError("release version number must be an integer")
    if version_number < 1:
        raise ValueError("release version number must be positive")
    return f"release-v{version_number}"


async def ensure_prepromotion_rehearsal_ref(
    token: str,
    owner: str,
    repo: str,
    version_number: int,
    candidate_sha: str,
) -> str:
    """Create or recut the rehearsal ref and prove its exact candidate SHA.

    Qualification creates this ref before dispatching the build workflow.  A
    pre-existing ref is safe only when it already resolves to the candidate;
    otherwise it is deleted and recreated, mirroring the promotion path's
    candidate-pinning contract.  Any ambiguous result fails closed before a
    workflow can build a different tree.
    """
    expected_sha = str(candidate_sha or "").strip()
    if not expected_sha:
        raise ValueError("candidate SHA must be non-empty")

    release_ref = prepromotion_rehearsal_ref(version_number)

    async def _create_and_observe() -> str | None:
        created = await github.create_branch(
            token, owner, repo, release_ref, expected_sha
        )
        observed = (created or {}).get("object", {}).get("sha")
        return observed if isinstance(observed, str) else None

    observed_sha = await _create_and_observe()
    if observed_sha != expected_sha:
        await github.delete_branch(token, owner, repo, release_ref)
        observed_sha = await _create_and_observe()

    if observed_sha != expected_sha:
        raise RuntimeError(
            f"rehearsal ref {release_ref} does not resolve to candidate "
            f"{expected_sha} (observed {observed_sha or 'unknown'})"
        )
    return release_ref


async def dispatch_prepromotion_rehearsal(
    token: str,
    owner: str,
    repo: str,
    version_number: int,
    candidate_sha: str,
    *,
    project_id: str | None = None,
) -> bool:
    """Build the candidate image without granting deploy workflow side effects.

    The ref is part of this dispatch contract, rather than a caller-owned
    precondition.  Every caller must supply the candidate SHA; this function
    proves the exact ``release-vN`` ref resolves to it immediately before the
    workflow dispatch.  Keeping the proof here prevents a new caller (or a
    manual retry path) from recreating the old gate deadlock by dispatching an
    unresolved ref.
    """
    await ensure_prepromotion_rehearsal_ref(
        token,
        owner,
        repo,
        version_number,
        candidate_sha,
    )
    dispatched = await github.dispatch_workflow_run(
        token,
        owner,
        repo,
        PREPROMOTION_REHEARSAL_WORKFLOW,
        prepromotion_rehearsal_ref(version_number),
        project_id=project_id,
        inputs=PREPROMOTION_REHEARSAL_INPUTS,
    )
    return bool(dispatched)


def validate_prepromotion_rehearsal_receipt(
    receipt: Any,
    candidate_sha: str | None,
) -> dict[str, Any] | None:
    """Return a normalized receipt only when it proves *candidate_sha*.

    Missing, malformed, failed, and differently-bound receipts all return
    ``None``.  The scorer consequently treats every unusable receipt as not
    met; it never substitutes the current branch tip or a receipt for another
    candidate.
    """
    expected_sha = str(candidate_sha or "").strip()
    if not _SHA40_RE.fullmatch(expected_sha) or not isinstance(receipt, Mapping):
        return None
    if receipt.get("kind") != PREPROMOTION_REHEARSAL_KIND:
        return None
    if receipt.get("revision") != expected_sha:
        return None
    if receipt.get("units_stopped") is not False:
        return None
    checked = receipt.get("checked")
    deferred = receipt.get("deferred")
    if (
        not isinstance(checked, list)
        or not checked
        or len(checked) > 32
        or not all(isinstance(item, str) and item.strip() for item in checked)
        or not isinstance(deferred, list)
        or len(deferred) > 32
        or not all(isinstance(item, str) and item.strip() for item in deferred)
        or any(len(item) > 200 for item in [*checked, *deferred])
    ):
        return None
    if not set(PREPROMOTION_REHEARSAL_PRECONDITIONS).issubset(checked):
        return None
    candidate = receipt.get("candidate")
    if not isinstance(candidate, str) or not candidate.strip():
        return None
    return {
        "kind": PREPROMOTION_REHEARSAL_KIND,
        "candidate": candidate.strip()[:500],
        "revision": expected_sha,
        "checked": list(checked),
        "deferred": list(deferred),
        "units_stopped": False,
    }


def persisted_rehearsal_for_candidate(
    receipt: Any,
    candidate_sha: str | None,
) -> dict[str, Any] | None:
    """Read the persisted host receipt as a gradeable, candidate-bound fact."""
    return validate_prepromotion_rehearsal_receipt(receipt, candidate_sha)


def is_rehearsal_acceptance_category(category: Mapping[str, Any]) -> bool:
    """Identify the rubric category whose contract is the release rehearsal."""
    return str(category.get("category_key") or "").strip() == (
        PREPROMOTION_REHEARSAL_CRITERION_KEY
    )


def apply_rehearsal_evidence(
    category_verdicts: list[dict[str, Any]] | None,
    active_categories: list[Mapping[str, Any]],
    receipt: Any,
    candidate_sha: str | None,
) -> list[dict[str, Any]] | None:
    """Upgrade the matching acceptance category from the persisted receipt.

    The host receipt is machine evidence for this one contract.  Other
    categories retain the grader's result unchanged.  A missing or mismatched
    receipt explicitly forces the rehearsal category to ``not_met`` so the
    qualifier fails closed.
    """
    if category_verdicts is None:
        return None
    valid = persisted_rehearsal_for_candidate(receipt, candidate_sha)
    rehearsal_keys = {
        str(category.get("category_key") or "")
        for category in active_categories
        if is_rehearsal_acceptance_category(category)
    }
    if not rehearsal_keys:
        return category_verdicts
    if valid is None:
        return [
            {
                **verdict,
                "status": "not_met",
                "tier": "machine",
                "gap": "candidate-bound pre-promotion rehearsal receipt is absent",
                "citations": [],
            }
            if str((verdict or {}).get("category_key") or "") in rehearsal_keys
            else verdict
            for verdict in category_verdicts
        ]
    citation = {"kind": "test", "ref": "release_prepromotion_rehearsal"}
    return [
        {
            **verdict,
            "status": "met",
            "tier": "machine",
            "gap": None,
            "citations": [citation],
        }
        if str((verdict or {}).get("category_key") or "") in rehearsal_keys
        else verdict
        for verdict in category_verdicts
    ]
