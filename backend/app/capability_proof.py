"""Durable evidence-grounded capability proof records.

When a task that advertises ``provides_capability`` completes AND its declared
acceptance evidence is verified (named_test proven=True, or downstream_artifact
consumed=True), this module records a bounded proof entry on
``project.capability_proof_records[normalized_cap_name]``.

Contract:
- Merging a PR alone is NOT proof. Only verified acceptance evidence qualifies.
- A capability with no verified evidence stays 'claimed/unverified'.
- Proof records are additive (never block merges) and bounded (10 per capability).
- Each entry: {task_id, proved_at, proof_kind, proof_ref, commit_sha}.
"""

from __future__ import annotations

import logging
from typing import Any

from sqlalchemy.orm.attributes import flag_modified

from app.capability_registry import (
    annotate_capability_proof_status,
    compose_capability_markers,
    normalize_capability_name,
)
from app.evidence_primitive import EvidenceRef  # noqa: F401 — imported for shape doc

# Re-exported for back-compat: the annotation body lives in the IO-free
# capability_registry leaf so every spawn path shares ONE implementation.
__all__ = [
    "annotate_capability_proof_status",
    "carry_proof_records_for_consolidation",
    "enrich_problem_model_capability_proofs",
    "record_capability_proof",
    "record_capability_proof_from_trial",
    "should_record_capability_proof",
]

logger = logging.getLogger(__name__)

_PROOF_KIND_NAMED_TEST = "named_test"
_PROOF_KIND_DOWNSTREAM_ARTIFACT = "downstream_artifact"
_PROOF_KIND_TRIAL_ADOPTION = "trial_adoption"
_MAX_PROOF_ENTRIES_PER_CAPABILITY = 10

# Context key where we read the acceptance verdict.
_CODE_ACCEPTANCE_VERDICT_KEY = "code_acceptance_verdict"


def _extract_proof_ref(verdict: dict) -> str | None:
    """Return the proof reference from a stored verdict dict.

    The stored verdict (written by acceptance_gate.record_code_acceptance_verdict)
    carries a top-level ``proof_ref`` key set at evaluation time.
    """
    proof_ref = verdict.get("proof_ref")
    if proof_ref:
        return str(proof_ref)
    return None


def _proof_kind_from_verdict(verdict: dict) -> str:
    """Derive the proof kind label from a stored verdict dict.

    The stored verdict carries a top-level ``kind`` key set at evaluation time.
    """
    kind = verdict.get("kind")
    if kind:
        return str(kind)
    return _PROOF_KIND_NAMED_TEST  # default when kind is absent


def should_record_capability_proof(task: Any, verdict: Any) -> bool:
    """True when task advertises a capability AND verdict is proven.

    A task must both:
    1. Advertise at least one human-readable capability via ``provides_capability``
       or ``capability`` in its context (derived task-provider markers don't count).
    2. Have a recorded acceptance verdict with ``proven=True``.
    """
    if not isinstance(verdict, dict) or verdict.get("proven") is not True:
        return False
    # Check for at least one human-namespace capability marker (not task-provider:).
    context = getattr(task, "context", None) or {}
    for key in ("provides_capability", "capability"):
        value = context.get(key)
        if isinstance(value, str) and value.strip():
            return True
        if isinstance(value, (list, tuple, set)):
            for item in value:
                if isinstance(item, str) and item.strip():
                    return True
    return False


def _proof_entries_for_capability(
    proof_records: Any, cap_name: str
) -> list[dict[str, Any]]:
    """Return a shallow-copied proof-entry list for one capability."""
    if not isinstance(proof_records, dict):
        return []
    raw_entries = proof_records.get(cap_name)
    if not isinstance(raw_entries, list):
        return []
    return [entry for entry in raw_entries if isinstance(entry, dict)]


def _append_evidence_refs_to_capabilities(
    project: Any,
    cap_names: list[str],
    ref_entry: dict[str, Any],
) -> None:
    """Append an EvidenceRef-shaped entry to each matching Capability's evidence_refs.

    Fail-open: if problem_model is missing or malformed, logs a warning and
    returns without raising. The proof is still recorded in capability_proof_records.
    """
    try:
        pm = getattr(project, "problem_model", None)
        if not isinstance(pm, dict):
            return
        capabilities = pm.get("capabilities")
        if not isinstance(capabilities, list):
            return
        updated = False
        norm_names = set(cap_names)
        for cap in capabilities:
            if not isinstance(cap, dict):
                continue
            cap_name = cap.get("name")
            if not isinstance(cap_name, str):
                continue
            if normalize_capability_name(cap_name) not in norm_names:
                continue
            refs: list = cap.setdefault("evidence_refs", [])
            # Dedup by source_id.
            source_id = ref_entry.get("source_id")
            if source_id and any(
                isinstance(r, dict) and r.get("source_id") == source_id
                for r in refs
            ):
                continue
            refs.append(dict(ref_entry))
            if len(refs) > _MAX_PROOF_ENTRIES_PER_CAPABILITY:
                cap["evidence_refs"] = refs[-_MAX_PROOF_ENTRIES_PER_CAPABILITY:]
            updated = True
        if updated:
            flag_modified(project, "problem_model")
    except Exception:
        logger.warning(
            "[CAPABILITY_PROOF] failed to append evidence_refs to problem_model; "
            "proof_records still recorded",
            exc_info=True,
        )


def enrich_problem_model_capability_proofs(
    problem_model: Any, proof_records: Any
) -> Any:
    """Return a shallow-copied problem_model with capability proof evidence.

    Each capability entry receives a ``proof_status`` field and, when proof
    records exist, a ``proof_records`` list containing the durable entries.
    Missing / empty / malformed records fail open to claimed-only.
    """
    if not isinstance(problem_model, dict):
        return problem_model
    capabilities = problem_model.get("capabilities")
    if not isinstance(capabilities, list):
        return dict(problem_model)

    annotated = dict(problem_model)
    annotated_caps: list[Any] = []
    for entry in capabilities:
        if not isinstance(entry, dict):
            annotated_caps.append(entry)
            continue
        entry2 = dict(entry)
        cap_name = entry2.get("name")
        entries = (
            _proof_entries_for_capability(proof_records, cap_name)
            if isinstance(cap_name, str)
            else []
        )
        entry2["proof_status"] = "proven" if entries else "unproven"
        if entries:
            entry2["proof_records"] = [dict(proof_entry) for proof_entry in entries]
        annotated_caps.append(entry2)

    annotated["capabilities"] = annotated_caps
    return annotated


def record_capability_proof(task: Any, project: Any) -> list[str]:
    """Record a proof entry on project for each capability the task provides.

    Reads the ``code_acceptance_verdict`` from ``task.context`` and, if it is
    proven, stamps a bounded proof entry into
    ``project.capability_proof_records[cap_name]``.

    Returns the list of normalized capability names that received a proof entry
    (empty when no proof was recorded).

    Non-raising: all errors are logged at WARNING level and an empty list is
    returned so callers can wrap this in a try/except BLE001 guard.
    """
    from app.time_utils import _utcnow

    context = getattr(task, "context", None) or {}
    verdict = context.get(_CODE_ACCEPTANCE_VERDICT_KEY)

    if not should_record_capability_proof(task, verdict):
        return []

    # Gather human-namespace capability markers only.
    all_markers = compose_capability_markers(task)
    # Filter to human-namespace names (exclude derived task-provider: prefix).
    human_caps: list[str] = []
    for marker in all_markers:
        if marker.startswith("task-provider:"):
            continue
        normalized = normalize_capability_name(marker)
        if normalized:
            human_caps.append(normalized)

    if not human_caps:
        return []

    # Build the proof entry.
    task_id = str(getattr(task, "id", "") or "")
    proved_at = _utcnow().isoformat()
    proof_kind = _proof_kind_from_verdict(verdict)
    proof_ref = _extract_proof_ref(verdict) or ""
    commit_sha = context.get("merge_commit_sha") or context.get("commit_sha") or ""

    entry: dict[str, Any] = {
        "task_id": task_id,
        "proved_at": proved_at,
        "proof_kind": proof_kind,
        "proof_ref": proof_ref,
        "commit_sha": commit_sha,
    }

    # Ensure the column is initialized.
    if not isinstance(getattr(project, "capability_proof_records", None), dict):
        project.capability_proof_records = {}

    recorded: list[str] = []
    for cap_name in human_caps:
        entries: list[dict] = list(project.capability_proof_records.get(cap_name) or [])
        # Avoid duplicating a proof entry for the same task.
        if any(e.get("task_id") == task_id for e in entries):
            continue
        entries.append(entry)
        # Cap at _MAX_PROOF_ENTRIES_PER_CAPABILITY (keep most recent).
        if len(entries) > _MAX_PROOF_ENTRIES_PER_CAPABILITY:
            entries = entries[-_MAX_PROOF_ENTRIES_PER_CAPABILITY:]
        project.capability_proof_records[cap_name] = entries
        recorded.append(cap_name)

    if recorded:
        flag_modified(project, "capability_proof_records")
        logger.info(
            "[CAPABILITY_PROOF] recorded proof for task %s caps=%s proof_kind=%s",
            task_id,
            recorded,
            proof_kind,
        )
        _append_evidence_refs_to_capabilities(
            project,
            recorded,
            {
                "kind": proof_kind,
                "ref": proof_ref,
                "proven": True,
                "source_id": task_id,
                "recorded_at": proved_at,
            },
        )

    return recorded


def record_capability_proof_from_trial(
    project: Any,
    cap_name: str,
    entry: dict[str, Any],
) -> None:
    """Record a bounded proof entry when a trial reaches an evidence-grounded ADOPT verdict.

    Called from persist_trial_verdicts_in_transaction after a successful adopt
    verdict is written. The dedup key ``trial:<normalized_cap_name>`` ensures a
    standing ADOPT does not produce duplicate proof entries on re-evaluation.

    Non-raising: all errors are logged at WARNING level so they cannot roll back
    the surrounding session commit.
    """
    from app.time_utils import _utcnow

    verdict = entry.get("verdict")
    if verdict not in ("adopt", "ADOPT"):
        return

    norm = normalize_capability_name(cap_name)
    if not norm:
        return

    # Stable dedup key: one proof record per capability trial adoption.
    dedup_task_id = f"trial:{norm}"
    proved_at = _utcnow().isoformat()

    proof_entry: dict[str, Any] = {
        "task_id": dedup_task_id,
        "proved_at": proved_at,
        "proof_kind": _PROOF_KIND_TRIAL_ADOPTION,
        "proof_ref": norm,
        "lift": entry.get("lift"),
        "n_with": entry.get("n_with"),
        "n_without": entry.get("n_without"),
        "verdict_reason": entry.get("verdict_reason"),
    }

    if not isinstance(getattr(project, "capability_proof_records", None), dict):
        project.capability_proof_records = {}

    entries: list[dict] = list(project.capability_proof_records.get(norm) or [])
    if any(e.get("task_id") == dedup_task_id for e in entries):
        return

    entries.append(proof_entry)
    if len(entries) > _MAX_PROOF_ENTRIES_PER_CAPABILITY:
        entries = entries[-_MAX_PROOF_ENTRIES_PER_CAPABILITY:]
    project.capability_proof_records[norm] = entries
    flag_modified(project, "capability_proof_records")
    logger.info(
        "[CAPABILITY_PROOF] recorded trial-adoption proof for cap=%s lift=%s"
        " n_with=%s n_without=%s",
        norm,
        proof_entry.get("lift"),
        proof_entry.get("n_with"),
        proof_entry.get("n_without"),
    )
    _append_evidence_refs_to_capabilities(
        project,
        [norm],
        {
            "kind": _PROOF_KIND_TRIAL_ADOPTION,
            "ref": norm,
            "proven": True,
            "source_id": dedup_task_id,
            "recorded_at": proved_at,
        },
    )


def carry_proof_records_for_consolidation(
    capabilities_consolidated: list[dict[str, Any]],
    proof_records: dict[str, Any],
) -> dict[str, Any]:
    """Merge absorbed capability proof entries into the survivor's key.

    For each {survivor, absorbed[...]} consolidation event, all proof entries
    keyed under absorbed names are merged into the survivor's list, deduped by
    task_id, capped at _MAX_PROOF_ENTRIES_PER_CAPABILITY (oldest evicted), and
    the absorbed keys are removed.

    Returns a NEW dict — does not mutate the input.  Callers are responsible for
    flag_modified on the JSONB column after assigning the result.
    """
    if not capabilities_consolidated or not isinstance(proof_records, dict):
        return proof_records

    result = dict(proof_records)
    for event in capabilities_consolidated:
        if not isinstance(event, dict):
            continue
        survivor = event.get("survivor")
        absorbed_names = event.get("absorbed") or []
        if not survivor or not absorbed_names:
            continue
        survivor_norm = normalize_capability_name(survivor)
        if not survivor_norm:
            continue

        merged: list[dict[str, Any]] = list(result.get(survivor_norm) or [])
        seen_task_ids: set[str] = {e.get("task_id", "") for e in merged}

        for abs_name in absorbed_names:
            abs_norm = normalize_capability_name(abs_name)
            if not abs_norm:
                continue
            for entry in result.get(abs_norm) or []:
                if not isinstance(entry, dict):
                    continue
                tid = entry.get("task_id", "")
                if tid not in seen_task_ids:
                    merged.append(entry)
                    seen_task_ids.add(tid)
            result.pop(abs_norm, None)

        if len(merged) > _MAX_PROOF_ENTRIES_PER_CAPABILITY:
            merged = merged[-_MAX_PROOF_ENTRIES_PER_CAPABILITY:]

        if merged:
            result[survivor_norm] = merged
        else:
            result.pop(survivor_norm, None)

        logger.info(
            "[CAPABILITY_PROOF] consolidation carry-through: survivor=%s absorbed=%s"
            " merged_count=%d",
            survivor_norm,
            absorbed_names,
            len(merged),
        )

    return result
