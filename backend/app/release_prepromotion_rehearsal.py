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


def prepromotion_rehearsal_ref(version_number: int) -> str:
    """Return the candidate-pinned release branch ref used by the build."""
    if isinstance(version_number, bool) or not isinstance(version_number, int):
        raise ValueError("release version number must be an integer")
    if version_number < 1:
        raise ValueError("release version number must be positive")
    return f"release-v{version_number}"


async def dispatch_prepromotion_rehearsal(
    token: str,
    owner: str,
    repo: str,
    version_number: int,
    *,
    project_id: str | None = None,
) -> bool:
    """Build the candidate image without granting deploy workflow side effects."""
    return await github.dispatch_workflow_run(
        token,
        owner,
        repo,
        PREPROMOTION_REHEARSAL_WORKFLOW,
        prepromotion_rehearsal_ref(version_number),
        project_id=project_id,
        inputs=PREPROMOTION_REHEARSAL_INPUTS,
    )


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
    text = str(category.get("text") or "").lower()
    return (
        "rehears" in text
        and ("bootstrap" in text or "release" in text)
        and ("pre-promot" in text or "prepromot" in text or "candidate" in text)
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
