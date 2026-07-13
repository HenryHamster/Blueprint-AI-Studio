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

import asyncio
import logging
import os
import re
from typing import Any

from sqlalchemy.orm.attributes import flag_modified

from app.capability_registry import (
    annotate_capability_proof_status,
    compose_capability_markers,
    exempt_capability_names,
    normalize_capability_name,
)
from app.evidence_primitive import (
    EvidenceRef,  # noqa: F401 — imported for shape doc
    classify_evidence_tiered,
)

# Re-exported for back-compat: the annotation body lives in the IO-free
# capability_registry leaf so every spawn path shares ONE implementation.
__all__ = [
    "annotate_capability_proof_status",
    "carry_proof_records_for_consolidation",
    "commission_proof_earn_loop",
    "enrich_problem_model_capability_proofs",
    "record_capability_backing_path_proof",
    "record_capability_named_test_proof",
    "record_capability_proof",
    "record_capability_proof_from_trial",
    "record_gap_evidence",
    "should_record_capability_proof",
]

logger = logging.getLogger(__name__)

_PROOF_KIND_NAMED_TEST = "named_test"
_PROOF_KIND_DOWNSTREAM_ARTIFACT = "downstream_artifact"
_PROOF_KIND_TRIAL_ADOPTION = "trial_adoption"
_PROOF_KIND_BACKING_PATH = "backing_path"
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

    Each capability entry receives a ``proof_status`` field derived via a
    two-tier resolution:

    1. **evidence_refs** (tier-1): when the capability carries an ``evidence_refs``
       list, ``classify_evidence`` determines the status — ``"proven"`` /
       ``"path_proven"`` / ``"partial"`` / ``"unproven"`` / ``"unverified"``.

    2. **proof_records** (tier-2): when evidence_refs is absent, checks the
       durable proof-record kinds — ``"proven"`` when at least one record has a
       non-backing_path proof_kind, ``"path_proven"`` when records exist but ALL
       are backing_path, ``"unproven"`` otherwise.

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
        evidence_refs = entry2.get("evidence_refs")
        # Derive proof_status from evidence_refs (tier-1) when available,
        # falling back to proof_record kind checking (tier-2).
        # Empty list means no refs captured yet — fall through to proof_records
        # rather than treating as affirmatively cleared.
        if isinstance(evidence_refs, list) and evidence_refs:
            entry2["proof_status"] = classify_evidence_tiered(evidence_refs)
        elif entries:
            # Map proof records to EvidenceRef shape for tiered classification.
            # All stored proof records represent proven evidence (only recorded
            # when acceptance evidence is verified), so proven=True is correct.
            synthetic_refs = [
                {"kind": e.get("proof_kind"), "proven": True} for e in entries
            ]
            entry2["proof_status"] = classify_evidence_tiered(synthetic_refs)
        else:
            entry2["proof_status"] = "unproven"
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
        # Append an independent copy per capability. The shared ``entry`` is
        # built once above, but appending it by reference would alias the SAME
        # dict across every capability's list in a multi-capability task, so a
        # later mutation (or downstream enrichment) of one capability's entry
        # would silently bleed into the others. Copy so each entry is a distinct
        # object.
        entries.append(dict(entry))
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


def record_capability_backing_path_proof(project: Any, cap_name: str) -> bool:
    """Record a bounded proof entry when a working capability's declared
    ``backing_paths`` all resolve at HEAD (verified by the caller).

    Uses dedup key ``backing:<normalized_cap_name>`` — same prefix-based stable
    key pattern as ``record_capability_proof_from_trial`` — so a standing
    verification does not produce duplicate proof entries on re-evaluation.

    Non-raising: all errors are logged at WARNING level so they can never
    crash the caller (typically a reaper sweep).

    Returns ``True`` when a new entry was recorded, ``False`` when already
    present (dedup) or when the capability name cannot be normalized.
    """
    from app.time_utils import _utcnow

    norm = normalize_capability_name(cap_name)
    if not norm:
        logger.warning(
            "[CAPABILITY_PROOF] cannot record backing-path proof — "
            "capability name %r could not be normalized",
            cap_name,
        )
        return False

    # Stable dedup key: one proof record per capability backing-path verification.
    dedup_task_id = f"backing:{norm}"
    proved_at = _utcnow().isoformat()

    proof_entry: dict[str, Any] = {
        "task_id": dedup_task_id,
        "proved_at": proved_at,
        "proof_kind": _PROOF_KIND_BACKING_PATH,
        "proof_ref": norm,
        "commit_sha": "",
    }

    try:
        if not isinstance(getattr(project, "capability_proof_records", None), dict):
            project.capability_proof_records = {}
    except Exception:
        logger.warning(
            "[CAPABILITY_PROOF] failed to read capability_proof_records on project",
            exc_info=True,
        )
        return False

    entries: list[dict] = list(project.capability_proof_records.get(norm) or [])
    if any(e.get("task_id") == dedup_task_id for e in entries):
        return False

    entries.append(proof_entry)
    if len(entries) > _MAX_PROOF_ENTRIES_PER_CAPABILITY:
        entries = entries[-_MAX_PROOF_ENTRIES_PER_CAPABILITY:]
    project.capability_proof_records[norm] = entries
    try:
        flag_modified(project, "capability_proof_records")
    except Exception:
        logger.warning(
            "[CAPABILITY_PROOF] flag_modified failed for capability_proof_records",
            exc_info=True,
        )
        return False

    logger.info(
        "[CAPABILITY_PROOF] recorded backing-path proof for cap=%s",
        norm,
    )

    _append_evidence_refs_to_capabilities(
        project,
        [norm],
        {
            "kind": _PROOF_KIND_BACKING_PATH,
            "ref": norm,
            "proven": True,
            "source_id": dedup_task_id,
            "recorded_at": proved_at,
        },
    )
    return True


def record_capability_named_test_proof(
    project: Any,
    cap_name: str,
    test_refs: list[str],
    *,
    execution_verified: bool = False,
) -> bool:
    """Record a bounded ``named_test``-tier proof for a capability.

    The ``named_test`` tier is the ONLY one :func:`classify_evidence_tiered`
    grades as ``proven`` (clearing the enforce gate), so it must reflect a test
    that actually **ran green**, not merely a test file that exists. Per audit
    finding #2 ("named_test earn path mints proofs without execution"), this
    recorder now REQUIRES ``execution_verified=True`` — a caller's attestation
    that it holds real execution evidence (a recorded green run for the declared
    test) — before it will write anything. Called without it, the recorder logs
    and returns ``False`` (records nothing), so static file-existence /
    import-substring discovery can never mint a machine-verified proof.

    The genuine execution-verified path is the acceptance-verdict recorder
    (:func:`record_capability_proof`), whose ``named_test`` verdicts already
    passed :func:`app.acceptance_gate._evaluate_named_test` (test present in the
    diff AND ran green under a real merge-test verdict). The structural
    static-analysis earn loop instead earns at most the ``backing_path`` tier
    (:func:`record_capability_backing_path_proof`, graded ``path_proven``).

    This is NOT a parallel recording mechanism: it writes to the same durable
    ``project.capability_proof_records`` store and appends the same
    EvidenceRef shape (via :func:`_append_evidence_refs_to_capabilities`) as
    every other recorder in this module — only the ``proof_kind`` and dedup
    prefix differ.

    Uses dedup key ``named_test:<normalized_cap_name>`` — the same stable
    prefix-based pattern as the trial and backing-path recorders — so a standing
    verification does not produce duplicate proof entries on re-evaluation.

    Non-raising: all errors are logged at WARNING level so they can never crash
    the caller (typically a reaper sweep).

    Returns ``True`` when a new entry was recorded, ``False`` when already
    present (dedup), when the capability name cannot be normalized, when
    ``test_refs`` is empty, or when ``execution_verified`` is not ``True``.
    """
    from app.time_utils import _utcnow

    if execution_verified is not True:
        # No green-run evidence — refuse to mint the proven-tier proof. This is
        # the hard bar that closes the "unexecuted named_test proof" hole: a
        # test file existing at HEAD is not evidence it passed.
        logger.warning(
            "[CAPABILITY_PROOF] refusing named-test proof for cap=%r — no "
            "execution evidence (execution_verified is not True); a named_test "
            "proof requires a recorded green run, not just a present test file",
            cap_name,
        )
        return False

    norm = normalize_capability_name(cap_name)
    if not norm:
        logger.warning(
            "[CAPABILITY_PROOF] cannot record named-test proof — "
            "capability name %r could not be normalized",
            cap_name,
        )
        return False

    refs = [r for r in (test_refs or []) if isinstance(r, str) and r.strip()]
    if not refs:
        logger.warning(
            "[CAPABILITY_PROOF] cannot record named-test proof for cap=%s — "
            "no covering test refs supplied",
            norm,
        )
        return False

    # Stable dedup key: one named-test proof record per capability verification.
    dedup_task_id = f"named_test:{norm}"
    proved_at = _utcnow().isoformat()
    proof_ref = ",".join(refs)

    proof_entry: dict[str, Any] = {
        "task_id": dedup_task_id,
        "proved_at": proved_at,
        "proof_kind": _PROOF_KIND_NAMED_TEST,
        "proof_ref": proof_ref,
        "test_refs": refs,
        "commit_sha": "",
    }

    try:
        if not isinstance(getattr(project, "capability_proof_records", None), dict):
            project.capability_proof_records = {}
    except Exception:  # noqa: BLE001 — recording must degrade, never raise
        logger.warning(
            "[CAPABILITY_PROOF] failed to read capability_proof_records on project",
            exc_info=True,
        )
        return False

    entries: list[dict] = list(project.capability_proof_records.get(norm) or [])
    if any(e.get("task_id") == dedup_task_id for e in entries):
        return False

    entries.append(proof_entry)
    if len(entries) > _MAX_PROOF_ENTRIES_PER_CAPABILITY:
        entries = entries[-_MAX_PROOF_ENTRIES_PER_CAPABILITY:]
    project.capability_proof_records[norm] = entries
    try:
        flag_modified(project, "capability_proof_records")
    except Exception:  # noqa: BLE001 — recording must degrade, never raise
        logger.warning(
            "[CAPABILITY_PROOF] flag_modified failed for capability_proof_records",
            exc_info=True,
        )
        return False

    logger.info(
        "[CAPABILITY_PROOF] recorded named-test proof for cap=%s refs=%s",
        norm,
        refs,
    )

    _append_evidence_refs_to_capabilities(
        project,
        [norm],
        {
            "kind": _PROOF_KIND_NAMED_TEST,
            "ref": proof_ref,
            "proven": True,
            "source_id": dedup_task_id,
            "recorded_at": proved_at,
        },
    )
    return True


def record_gap_evidence(task: Any, project: Any) -> None:
    """Stamp EvidenceRef entries onto the Gap linked to this task's direction.

    Resolves the gap via task.context["direction_id"] → project.directions → gap_id
    → project.problem_model["gaps"].  Appends an EvidenceRef-shaped entry to the
    gap's ``evidence_refs`` list when the task carries a proven acceptance verdict.

    Non-raising: all errors are logged at WARNING level so this can never undo a
    real merge.  Idempotent: deduplicates by source_id.
    """
    from app.time_utils import _utcnow

    try:
        context = getattr(task, "context", None) or {}
        verdict = context.get(_CODE_ACCEPTANCE_VERDICT_KEY)
        if not isinstance(verdict, dict) or verdict.get("proven") is not True:
            return

        direction_id = context.get("direction_id")
        if not direction_id:
            return

        # Resolve direction → gap_id.
        directions = getattr(project, "directions", None) or []
        if not isinstance(directions, list):
            return
        gap_id: str | None = None
        for d in directions:
            if isinstance(d, dict) and d.get("id") == direction_id:
                gap_id = d.get("gap_id")
                break
        if not gap_id:
            return

        # Resolve gap in problem_model.
        pm = getattr(project, "problem_model", None)
        if not isinstance(pm, dict):
            return
        gaps = pm.get("gaps")
        if not isinstance(gaps, list):
            return
        target_gap: dict | None = None
        for g in gaps:
            if isinstance(g, dict) and g.get("id") == gap_id:
                target_gap = g
                break
        if target_gap is None:
            return

        task_id = str(getattr(task, "id", "") or "")
        proof_kind = _proof_kind_from_verdict(verdict)
        proof_ref = _extract_proof_ref(verdict) or ""
        recorded_at = _utcnow().isoformat()

        ref_entry: dict[str, Any] = {
            "kind": proof_kind,
            "ref": proof_ref,
            "proven": True,
            "source_id": task_id,
            "recorded_at": recorded_at,
        }

        refs: list = target_gap.setdefault("evidence_refs", [])
        # Dedup by source_id.
        if task_id and any(
            isinstance(r, dict) and r.get("source_id") == task_id for r in refs
        ):
            return

        refs.append(ref_entry)
        if len(refs) > _MAX_PROOF_ENTRIES_PER_CAPABILITY:
            target_gap["evidence_refs"] = refs[-_MAX_PROOF_ENTRIES_PER_CAPABILITY:]

        flag_modified(project, "problem_model")
        logger.info(
            "[GAP_EVIDENCE] recorded evidence for task %s gap_id=%s proof_kind=%s",
            task_id,
            gap_id,
            proof_kind,
        )
    except Exception:  # noqa: BLE001
        logger.warning(
            "[GAP_EVIDENCE] failed to record gap evidence for task %s",
            getattr(task, "id", None),
            exc_info=True,
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


# ── named_test coverage discovery ─────────────────────────────────────────────
# Convention-based mapping from a capability's backing source path to the test
# file(s) that would exercise it, plus a reference check confirming the test
# actually imports the backing module. This is the "identify the test file(s)
# that exercise a capability's backing paths, and reference them" step that lets
# the earn loop mint a machine-verified ``named_test`` proof (which
# ``classify_evidence_tiered`` grades ``proven``) instead of only the
# structural-only ``backing_path`` proof (graded ``path_proven``).

_TS_JS_EXTS: frozenset[str] = frozenset({"ts", "tsx", "js", "jsx"})


def _strip_path_suffix(path: str) -> str:
    """Drop a trailing ``:symbol`` / ``:line`` / ``#L`` suffix from a ref path."""
    for sep in ("#", ":"):
        if sep in path:
            path = path.split(sep, 1)[0]
    return path.strip()


def _path_stem(source_path: str) -> str | None:
    """Return the bare filename stem (no directory, no extension) of a path."""
    if not isinstance(source_path, str):
        return None
    p = _strip_path_suffix(source_path)
    filename = p.rsplit("/", 1)[-1]
    stem, dot, _ext = filename.rpartition(".")
    return stem if dot and stem else None


def _module_dotted_path(source_path: str) -> str | None:
    """Return the importable dotted module path for a ``backend/app`` source file.

    ``backend/app/capability_proof.py`` → ``app.capability_proof``;
    ``backend/app/routers/admin_compute_jobs.py`` → ``app.routers.admin_compute_jobs``.
    Returns ``None`` for non-Python paths or files outside ``backend/app/`` (whose
    import name is not derivable from the path alone).
    """
    if not isinstance(source_path, str):
        return None
    p = _strip_path_suffix(source_path)
    if not p.endswith(".py") or not p.startswith("backend/app/"):
        return None
    rel = p[len("backend/") : -len(".py")]  # app/routers/admin_compute_jobs
    return rel.replace("/", ".")


def _candidate_test_paths(source_path: str) -> list[str]:
    """Return convention-based candidate test file paths for a backing source path.

    Mirrors the repo's real layout conventions:
      * ``backend/**/<name>.py``   → ``backend/testing/test_<name>.py`` (flattened)
      * ``scripts/<name>.py``      → ``scripts/test_<name>.py``
      * ``<dir>/<name>.{ts,tsx,js,jsx}`` → ``<dir>/<name>.test.<ext>`` (sibling)

    Returns ``[]`` for paths whose test convention is not derivable.
    """
    if not isinstance(source_path, str) or not source_path.strip():
        return []
    p = _strip_path_suffix(source_path)
    if not p:
        return []
    dirname, _, filename = p.rpartition("/")
    stem, dot, ext = filename.rpartition(".")
    if not dot or not stem:
        return []
    if ext == "py":
        if p.startswith("backend/"):
            return [f"backend/testing/test_{stem}.py"]
        if p.startswith("scripts/"):
            return [f"scripts/test_{stem}.py"]
        return [f"{dirname}/test_{stem}.py" if dirname else f"test_{stem}.py"]
    if ext in _TS_JS_EXTS:
        base = f"{dirname}/" if dirname else ""
        return [f"{base}{stem}.test.{ext}"]
    return []


def _test_references_source(content: str, source_path: str) -> bool:
    """True when ``content`` (a test file's body) imports/references ``source_path``.

    Confirms the conventionally-named test actually exercises the backing module
    rather than being a same-named coincidence: for a ``backend/app`` module it
    looks for the importable dotted path (``app.routers.admin_compute_jobs``);
    otherwise it requires the bare module stem to appear on an ``import``/``from``
    line (e.g. ``import local_runner`` for a script, or a JS/TS
    ``import ... from './strategic-resident-lib'``).
    """
    if not isinstance(content, str) or not content:
        return False
    dotted = _module_dotted_path(source_path)
    if dotted and dotted in content:
        return True
    stem = _path_stem(source_path)
    if not stem:
        return False
    pattern = re.compile(
        rf"(?:^|\n)[^\n]*\b(?:import|from|require)\b[^\n]*\b{re.escape(stem)}\b"
    )
    return bool(pattern.search(content))


async def _read_file_at_head(path: str, repo_root: str = "/tmp/workspace") -> str | None:
    """Return the HEAD contents of ``path`` via ``git show``, or ``None``.

    Tri-state-ish: ``None`` on any indeterminacy (no checkout, git error/timeout,
    missing file) so the caller degrades to a lower proof tier rather than
    guessing. Never raises.
    """
    if not path or not isinstance(path, str) or not repo_root or not os.path.isdir(repo_root):
        return None
    try:
        proc = await asyncio.create_subprocess_exec(
            "git",
            "show",
            f"HEAD:{path}",
            cwd=repo_root,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=5)
        except asyncio.TimeoutError:
            proc.kill()
            logger.warning("[CAPABILITY_PROOF] git show timeout reading test %s", path)
            return None
        if proc.returncode != 0:
            return None
        return stdout.decode(errors="replace")
    except Exception:  # noqa: BLE001 — discovery must degrade, never raise
        logger.warning(
            "[CAPABILITY_PROOF] failed to read %s at HEAD", path, exc_info=True
        )
        return None


async def _named_tests_covering_paths(
    file_paths: list[str],
    *,
    verify_path,
    read_head=_read_file_at_head,
) -> list[str]:
    """Return the covering test refs when EVERY backing path is test-covered.

    For each backing source path, derives its conventional test file(s), confirms
    the test exists at HEAD (via ``verify_path``, the same tri-state existence
    checker the earn loop already uses), and confirms the test body references the
    backing module (:func:`_test_references_source`). A backing path is *covered*
    when at least one such test is found.

    Returns the deduplicated list of covering test refs ONLY when every path in
    ``file_paths`` is covered — an all-or-nothing bar so a ``named_test`` proof
    attests the whole declared surface is test-backed, not just part of it.
    Returns ``[]`` when any path lacks a discoverable covering test (the caller
    then falls back to the structural-only ``backing_path`` proof).
    """
    covering: list[str] = []
    seen: set[str] = set()
    for src in file_paths:
        path_covered = False
        for test_path in _candidate_test_paths(src):
            try:
                exists = await verify_path(test_path)
            except Exception:  # noqa: BLE001 — a verify failure is non-fatal
                exists = None
            if exists is not True:
                continue
            content = await read_head(test_path)
            if content is None:
                continue
            if _test_references_source(content, src):
                path_covered = True
                if test_path not in seen:
                    seen.add(test_path)
                    covering.append(test_path)
                break
        if not path_covered:
            # One uncovered backing path drops the whole capability to the
            # structural-only tier — do not over-claim named_test coverage.
            return []
    return covering


# --- Automated backing-path discovery (proposal path) ------------------------

# Search tokens shorter than this are dropped from capability-name matching:
# two-char fragments ("ci", "ui") match almost any file and produce noise.
_CAP_TOKEN_MIN_LEN = 3
# Hard cap on discovered candidates per capability — bounds both the named_test
# coverage probe and the suggestion list folded into a research obligation.
_MAX_DISCOVERED_CANDIDATES = 5


async def _list_head_files(repo_root: str = "/tmp/workspace") -> list[str] | None:
    """Return every tracked file path at HEAD via ``git ls-files``, or ``None``.

    Tri-state-ish, mirroring :func:`_read_file_at_head`: ``None`` on any
    indeterminacy (no checkout, git error/timeout) so discovery degrades to
    "found nothing" rather than guessing. Never raises.
    """
    if not repo_root or not os.path.isdir(repo_root):
        return None
    try:
        proc = await asyncio.create_subprocess_exec(
            "git",
            "ls-files",
            cwd=repo_root,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=10)
        except asyncio.TimeoutError:
            proc.kill()
            logger.warning("[CAPABILITY_PROOF] git ls-files timeout listing HEAD tree")
            return None
        if proc.returncode != 0:
            return None
        return [
            line for line in stdout.decode(errors="replace").splitlines() if line.strip()
        ]
    except Exception:  # noqa: BLE001 — discovery must degrade, never raise
        logger.warning(
            "[CAPABILITY_PROOF] failed to list HEAD files", exc_info=True
        )
        return None


def _capability_search_tokens(cap_name: str) -> list[str]:
    """Derive file-search tokens from a capability name.

    Normalizes to kebab-case (:func:`normalize_capability_name`) and splits into
    the alphanumeric tokens long enough to be discriminating (``>=
    _CAP_TOKEN_MIN_LEN``). Returns ``[]`` for an unnormalizable name or one whose
    tokens are all too short to search on.
    """
    norm = normalize_capability_name(cap_name)
    if not norm:
        return []
    return [t for t in norm.split("-") if len(t) >= _CAP_TOKEN_MIN_LEN]


async def _discover_candidate_backing_paths(
    cap_name: str,
    *,
    list_head_files=_list_head_files,
    verify_path=None,
    max_candidates: int = _MAX_DISCOVERED_CANDIDATES,
) -> list[str]:
    """Propose candidate backing-path files for a capability that declares none.

    Given a capability name, derive search tokens from its normalized kebab form
    (:func:`_capability_search_tokens`), scan the HEAD file tree
    (``list_head_files``), and keep the paths whose *basename* contains EVERY
    search token — a strict match so a multi-word capability lands on a
    specifically-named file rather than an incidental single-token hit. Each
    survivor must also pass :func:`_looks_like_file_path` AND verify present at
    HEAD via ``verify_path`` (defaults to
    :func:`app.source_ref_validation._path_exists_at_head`).

    IO is fully injectable (``list_head_files`` / ``verify_path``) so the helper
    is unit-testable without a git checkout, and — like the rest of the earn
    loop — it never raises: any discovery/verification failure degrades to a
    shorter (possibly empty) list.

    Returns a deduplicated, deterministically-ordered (most-specific first),
    bounded (``max_candidates``) list of candidate paths, or ``[]`` when nothing
    usable is found. This is advisory only: the caller decides whether the
    candidates clear the proof bar or merely seed a research obligation — this
    helper never records a proof or mutates the registry.
    """
    from app.source_ref_validation import _looks_like_file_path, _path_exists_at_head

    _verify = verify_path or _path_exists_at_head

    tokens = _capability_search_tokens(cap_name)
    if not tokens:
        return []

    try:
        tree = await list_head_files()
    except Exception:  # noqa: BLE001 — a listing failure is non-fatal
        logger.warning(
            "[CAPABILITY_PROOF] discovery: HEAD file listing failed for cap=%s",
            cap_name,
            exc_info=True,
        )
        return []
    if not tree:
        return []

    # Rank candidate paths by basename specificity (shortest basename first) so
    # the most on-the-nose match leads; ties break on the full path for stable,
    # deterministic ordering across runs.
    scored: list[tuple[int, str]] = []
    for path in tree:
        if not isinstance(path, str) or not _looks_like_file_path(path):
            continue
        base = path.rsplit("/", 1)[-1].lower()
        if not all(tok in base for tok in tokens):
            continue
        scored.append((len(base), path))
    if not scored:
        return []
    scored.sort(key=lambda sp: (sp[0], sp[1]))

    verified: list[str] = []
    seen: set[str] = set()
    for _, path in scored:
        if path in seen:
            continue
        try:
            exists = await _verify(path)
        except Exception:  # noqa: BLE001 — a verify failure is non-fatal
            exists = None
        if exists is True:
            seen.add(path)
            verified.append(path)
        if len(verified) >= max_candidates:
            break
    return verified


async def commission_proof_earn_loop(
    project: Any,
    demand_result: dict[str, Any],
    problem_model: dict[str, Any],
    *,
    verify_path=None,
    read_head=None,
) -> dict[str, Any]:
    """Earn proof for demanded unproven working capabilities.

    This is the core of the capability-proof EARN loop. It:

    1. Computes the intersection of **demanded capabilities** (from
       ``demand_result``) and **unproven working capabilities** (from
       ``problem_model``) via
       :func:`app.capability_demand.compute_unproven_demanded_capabilities`.

    2. For each demanded unproven capability that declares ``backing_paths``:
       verifies each path at HEAD via ``verify_path`` (defaults to
       :func:`app.source_ref_validation._path_exists_at_head`).  When ALL
       paths are definitely present, it records the structural-only
       ``backing_path`` proof (:func:`record_capability_backing_path_proof`,
       graded ``path_proven``).  It deliberately does NOT mint the
       machine-verified ``named_test`` tier: static file-existence /
       import-substring discovery is not execution evidence (audit finding #2),
       so it cannot prove a test ran green and must not clear the enforce gate.
       A capability reaches the ``proven`` tier only through the
       acceptance-verdict path (:func:`record_capability_proof`), whose verdict
       already passed the green-run gate in
       :func:`app.acceptance_gate._evaluate_named_test`.

    3. For each demanded unproven capability that has NO ``backing_paths``:
       no automated verification is possible — the gap is reported in the
       return summary so callers (reaper sweeps, advisory hint builders) can
       surface it.

    Parameters
    ----------
    project:
        The project ORM object (must carry ``problem_model`` and
        ``capability_proof_records``).
    demand_result:
        Output of :func:`collect_missing_capability_demand`.
    problem_model:
        The project's ``problem_model`` dict (the IO-free
        ``working_capability_proof_status`` reader is used on this).
    verify_path:
        Callable ``(path: str) -> bool | None`` — async, returns True when
        the path definitely exists at HEAD, False when definitely absent,
        None when indeterminate.  Defaults to
        :func:`app.source_ref_validation._path_exists_at_head`.  Injected
        for testability (an ``AsyncMock`` or ``lambda`` can be substituted).
    read_head:
        Callable ``(path: str) -> str | None`` — async, returns the HEAD
        contents of a test file (or ``None`` when indeterminate).  Retained for
        back-compat with injecting callers; INTENTIONALLY UNUSED since the loop no
        longer credits a ``named_test`` proof from static coverage (finding #2).

    Returns
    -------
    A summary dict:
    ``{"total_demanded_unproven": int, "earned": int, "no_backing_paths": list[str],
      "still_unproven": list[str], "path_proven_stuck": list[str],
      "named_test_earned": list[str]}``.
    Total entries found, count of newly-earned proofs (all ``backing_path`` tier),
    capability names with no backing_paths, remaining unproven names (failed path
    check), and already-recorded (stuck) names that were not newly recorded.
    ``named_test_earned`` is retained in the shape for back-compat but is now
    ALWAYS empty — the static earn loop never reaches the ``proven`` tier.
    """
    from app.capability_demand import compute_unproven_demanded_capabilities
    from app.source_ref_validation import (
        _looks_like_file_path,
        _path_exists_at_head,
    )

    _verify = verify_path or _path_exists_at_head
    # ``read_head`` is retained in the signature for back-compat with callers that
    # inject a test-content reader, but the loop no longer credits a ``named_test``
    # proof from static coverage (audit finding #2), so it is intentionally unused
    # here — structural presence earns only the ``backing_path`` tier.
    _ = read_head

    # Step 1: compute the intersection.
    demanded_unproven = compute_unproven_demanded_capabilities(
        demand_result, problem_model
    )
    if not demanded_unproven:
        return {
            "total_demanded_unproven": 0,
            "earned": 0,
            "no_backing_paths": [],
            "still_unproven": [],
            "path_proven_stuck": [],
            "named_test_earned": [],
        }

    # Step 1b: defensive filter — skip exempt capabilities (they classify as
    # "proven" via working_capability_proof_status, so they should never appear
    # in demanded_unproven, but this is a belt-and-suspenders guard).
    _exempt = exempt_capability_names(problem_model)
    if _exempt:
        demanded_unproven = [
            e
            for e in demanded_unproven
            if normalize_capability_name(e.get("ref", "")) not in _exempt
        ]
        if not demanded_unproven:
            return {
                "total_demanded_unproven": 0,
                "earned": 0,
                "no_backing_paths": [],
                "still_unproven": [],
                "path_proven_stuck": [],
                "named_test_earned": [],
            }

    # Build a lookup: canonical-name -> (raw_name, backing_paths).
    # We read from the raw capabilities so we can inspect backing_paths regardless
    # of proof_status (the intersection has already filtered by proof_status).
    caps_raw = problem_model.get("capabilities") or []
    cap_backing: dict[str, tuple[str, list[str]]] = {}
    for cap in caps_raw:
        if not isinstance(cap, dict):
            continue
        raw_name = cap.get("name")
        if not isinstance(raw_name, str):
            continue
        norm_name = normalize_capability_name(raw_name)
        if not norm_name:
            continue
        bp = cap.get("backing_paths")
        if isinstance(bp, list):
            cap_backing[norm_name] = (raw_name, bp)

    earned_names: list[str] = []
    named_test_earned: list[str] = []
    no_backing_paths: list[str] = []
    still_unproven: list[str] = []
    path_proven_stuck: list[str] = []

    for entry in demanded_unproven:
        ref = entry.get("ref", "")
        canonical = normalize_capability_name(ref)
        if not canonical:
            continue

        raw_name, backing_paths = cap_backing.get(canonical, (None, []))
        if not backing_paths:
            no_backing_paths.append(canonical)
            continue

        # Filter to file-like paths.
        file_paths = [
            p for p in backing_paths
            if isinstance(p, str) and _looks_like_file_path(p)
        ]
        if not file_paths:
            no_backing_paths.append(canonical)
            continue

        # Verify every path at HEAD.  A single None (indeterminate) or False
        # (definitely absent) prevents proof recording — only definite True
        # on ALL paths triggers record.
        all_present = True
        for p in file_paths:
            try:
                exists = await _verify(p)
            except Exception:  # noqa: BLE001 — verification failure is non-fatal
                exists = None
            if exists is not True:
                all_present = False
                break

        if all_present:
            # The declared backing paths exist at HEAD — but that is a STRUCTURAL
            # fact, not execution evidence. Per audit finding #2, static coverage
            # discovery (a conventionally-named test file that imports the module)
            # does NOT prove the test ran green, so the earn loop earns at most the
            # structural-only ``backing_path`` tier (graded ``path_proven``), which
            # does NOT clear the enforce gate on its own. The machine-verified
            # ``named_test`` tier (graded ``proven``) is minted only by the
            # acceptance-verdict path (:func:`record_capability_proof`), whose
            # verdict already passed the green-run gate in
            # :func:`app.acceptance_gate._evaluate_named_test`.
            recorded = record_capability_backing_path_proof(project, raw_name or canonical)
            if recorded:
                earned_names.append(canonical)
            else:
                # Dedup hit (recorded=False): already stamped in a prior sweep.
                # Do NOT count as earned — the sweep must not commit on no-op
                # re-runs, and the capability should not be reported as newly
                # earning proof. Surface as path_proven_stuck so callers can
                # distinguish genuinely-new from already-recorded.
                path_proven_stuck.append(canonical)
        else:
            still_unproven.append(canonical)

    logger.info(
        "[CAPABILITY_PROOF] commission_proof_earn_loop: "
        "total_demanded_unproven=%d earned=%d named_test_earned=%d "
        "no_backing_paths=%d still_unproven=%d",
        len(demanded_unproven),
        len(earned_names),
        len(named_test_earned),
        len(no_backing_paths),
        len(still_unproven),
    )

    return {
        "total_demanded_unproven": len(demanded_unproven),
        "earned": len(earned_names),
        "no_backing_paths": no_backing_paths,
        "still_unproven": still_unproven,
        "path_proven_stuck": path_proven_stuck,
        "named_test_earned": named_test_earned,
    }
