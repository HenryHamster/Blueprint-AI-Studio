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
    KNOWN_PROOF_KINDS,
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
    "enroll_capability_proof_trial",
    "record_capability_backing_path_proof",
    "record_capability_named_test_execution_proof",
    "record_capability_named_test_proof",
    "record_capability_proof",
    "record_capability_proof_from_trial",
    "record_gap_evidence",
    "resolve_no_backing_paths_gap",
    "revoke_stale_execution_proof",
    "should_record_capability_proof",
]

logger = logging.getLogger(__name__)

_PROOF_KIND_NAMED_TEST = "named_test"
#: Structural-only tier minted by the earn loop / fleet sweep from static
#: discovery (a convention-named test file exists at HEAD and imports the backing
#: module) WITHOUT ever executing the test. ``classify_evidence_tiered`` grades
#: this ``path_proven`` — NOT ``proven`` — so it never clears the enforce gate.
#: The genuine ``named_test`` tier is reserved for :func:`record_capability_proof`,
#: which stamps it only from a real green merge-test acceptance verdict.
_PROOF_KIND_NAMED_TEST_PRESENT = "named_test_present"
_PROOF_KIND_DOWNSTREAM_ARTIFACT = "downstream_artifact"
_PROOF_KIND_TRIAL_ADOPTION = "trial_adoption"
_PROOF_KIND_BACKING_PATH = "backing_path"
_MAX_PROOF_ENTRIES_PER_CAPABILITY = 10

#: Hard cap on how many covering test files one ``capability_proof_trial`` may
#: execute. Bounds the runner job so a capability that (mis)declares a huge
#: backing surface cannot fan out into an unbounded targeted test run.
MAX_CAPABILITY_PROOF_TRIAL_TEST_FILES = 10
#: Wall-clock timeout (seconds) carried in the trial launch contract. The agent
#: handler runs ONLY the targeted covering tests under this bound; a trial that
#: exceeds it is a no-op (records nothing), never a demotion.
CAPABILITY_PROOF_TRIAL_WALL_CLOCK_SECONDS = 900

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
    *,
    replace: bool = False,
) -> None:
    """Append an EvidenceRef-shaped entry to each matching Capability's evidence_refs.

    Fail-open: if problem_model is missing or malformed, logs a warning and
    returns without raising. The proof is still recorded in capability_proof_records.

    By default a ref whose ``source_id`` already exists on a capability is left
    untouched (dedup). When ``replace=True`` (used by the freshness-replacing
    executed-named_test recorder) a matching ref is instead OVERWRITTEN in place
    with the fresh ``ref_entry`` — carrying its new ``recorded_at`` / ``commit_sha``
    — so the ledger holds exactly one up-to-date executed ref per capability
    rather than a stale duplicate.
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
            existing_idx: int | None = None
            if source_id:
                for _i, _r in enumerate(refs):
                    if isinstance(_r, dict) and _r.get("source_id") == source_id:
                        existing_idx = _i
                        break
            if existing_idx is not None:
                # A ref with this source_id already exists. In freshness-replace
                # mode overwrite it in place with the fresh timestamps/commit_sha;
                # otherwise leave the prior ref untouched (dedup).
                if replace:
                    refs[existing_idx] = dict(ref_entry)
                    updated = True
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
    problem_model: Any, proof_records: Any, now: Any = None
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

    **Freshness (opt-in).** When ``now`` is supplied it is threaded into the
    tiered classifier so a ``proven=True`` ref (or a proof-record, whose
    ``proved_at`` is carried through as the synthetic ref's ``recorded_at``) older
    than :data:`~app.evidence_primitive.PROOF_FRESHNESS_TTL` demotes below
    ``proven`` — keeping the strategist-facing badge honest and consistent with
    the enforce gate. ``now`` defaults to ``None`` (no freshness evaluation) so
    this display path stays deterministic for callers that do not opt in.
    """
    if not isinstance(problem_model, dict):
        return problem_model
    capabilities = problem_model.get("capabilities")
    if not isinstance(capabilities, list):
        return dict(problem_model)

    # Canonical names of ``working`` capabilities carrying an AUTHORIZED (platform-
    # recorded) operator exemption — the only exemption the agent cannot forge (a
    # bare agent-writable ``proof_exempt`` flag does NOT authorize). Surface it on
    # each matching capability so the display path (coverage meter denominator,
    # Exempt badge, Revoke-exemption button) agrees with the ledger-aware auto-
    # graduation coverage rather than diverging after a page reload. Uses the same
    # alias-aware gate carve-out set as coverage (:func:`exempt_capability_names`),
    # so an exemption recorded against an absorbed name marks the survivor exempt.
    ledger_exempt = exempt_capability_names(problem_model, proof_records)

    annotated = dict(problem_model)
    annotated_caps: list[Any] = []
    for entry in capabilities:
        if not isinstance(entry, dict):
            annotated_caps.append(entry)
            continue
        entry2 = dict(entry)
        cap_name = entry2.get("name")
        # Drive proof_exempt AUTHORITATIVELY from the ledger, not the agent-writable
        # flag. Set it True for a ledger-backed exemption AND False otherwise, so an
        # unauthorized agent-written ``proof_exempt: true`` (no platform record) is
        # cleared rather than surfaced. Leaving a forged flag in place would diverge
        # the coverage-meter denominator (ProblemModelViewer.tsx:481, which excludes
        # proof_exempt caps) from the ledger-aware backend auto-graduation rule
        # (audit finding #9 ignores the agent flag) and render a dead-end Revoke-
        # exemption button whose revoke returns removed:0.
        if isinstance(cap_name, str):
            entry2["proof_exempt"] = normalize_capability_name(cap_name) in ledger_exempt
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
            entry2["proof_status"] = classify_evidence_tiered(evidence_refs, now=now)
        elif entries:
            # Map proof records to EvidenceRef shape for tiered classification.
            # All stored proof records represent proven evidence (only recorded
            # when acceptance evidence is verified), so proven=True is correct.
            # Carry proved_at through as recorded_at so an opt-in freshness caller
            # (now != None) can expire a stale write-once proof-record.
            synthetic_refs = [
                {
                    "kind": e.get("proof_kind"),
                    "proven": True,
                    "recorded_at": e.get("proved_at"),
                }
                for e in entries
            ]
            entry2["proof_status"] = classify_evidence_tiered(synthetic_refs, now=now)
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
    proven AND its ``kind`` is a recognized proof kind, stamps a bounded proof
    entry into ``project.capability_proof_records[cap_name]``. A verdict whose
    ``kind`` is unrecognized (e.g. a self-authored value on a task that declared
    no acceptance evidence, so the platform never overwrote it) mints nothing —
    it must never launder into a gate-clearing proof.

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
    # Refuse to mint a proof from an UNRECOGNIZED proof kind. ``verdict`` is read
    # from ``task.context`` — agent-writable JSONB that the platform overwrites
    # only when the task declared acceptance evidence (record_code_acceptance_
    # verdict no-ops otherwise). A task that declares none can therefore carry a
    # self-authored ``code_acceptance_verdict`` with ``proven=True`` and a forged
    # ``kind`` (e.g. "self_reflection"); ``classify_evidence_tiered`` treats any
    # non-machine-but-unrecognized kind as full ``proven`` (an unlabeled proof is
    # never silently demoted), so such a record would clear the enforce gate with
    # nothing verified behind it. Only a kind in the recognized proof vocabulary
    # may mint a durable proof record. A missing/blank kind is NOT rejected here:
    # ``_proof_kind_from_verdict`` already normalizes it to the ``named_test``
    # default (preserving the legacy unlabeled-proof behavior).
    if proof_kind not in KNOWN_PROOF_KINDS:
        logger.warning(
            "[CAPABILITY_PROOF] refusing proof for task %s: unrecognized "
            "proof_kind=%r (not in the recognized proof vocabulary)",
            task_id,
            proof_kind,
        )
        return []
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
) -> bool:
    """Record a bounded proof entry when a trial reaches an evidence-grounded ADOPT verdict.

    Called from persist_trial_verdicts_in_transaction after a successful adopt
    verdict is written. The dedup key ``trial:<normalized_cap_name>`` ensures a
    standing ADOPT does not produce duplicate proof entries on re-evaluation.

    Returns ``True`` when a new entry was recorded and ``False`` for a non-adopt
    verdict, an invalid capability name, or an already-recorded adoption.
    """
    from app.time_utils import _utcnow

    verdict = entry.get("verdict")
    if verdict not in ("adopt", "ADOPT"):
        return False

    norm = normalize_capability_name(cap_name)
    if not norm:
        return False

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
        return False

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
    return True


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
    project: Any, cap_name: str, test_refs: list[str]
) -> bool:
    """Record a bounded ``named_test_present``-tier proof for a capability.

    Called from :func:`commission_proof_earn_loop` when a working capability's
    declared ``backing_paths`` are not merely present at HEAD but a
    convention-named test file for them *exists at HEAD and imports the backing
    module* (identified by :func:`_named_tests_covering_paths`). Crucially, that
    discovery is purely STATIC — file existence plus an import regex — the test
    is never executed. This therefore records the structural-only
    ``named_test_present`` kind, which :func:`classify_evidence_tiered` grades
    ``path_proven`` (like ``backing_path``), NOT ``proven`` — so it does NOT
    clear the enforce gate. Only :func:`record_capability_proof`, driven by a real
    green merge-test acceptance verdict, mints the ``proven`` ``named_test`` tier.

    This is NOT a parallel recording mechanism: it writes to the same durable
    ``project.capability_proof_records`` store and appends the same
    EvidenceRef shape (via :func:`_append_evidence_refs_to_capabilities`) as
    every other recorder in this module — only the ``proof_kind`` and dedup
    prefix differ.

    Uses dedup key ``named_test_present:<normalized_cap_name>`` — the same stable
    prefix-based pattern as the trial and backing-path recorders — so a standing
    verification does not produce duplicate proof entries on re-evaluation.

    Non-raising: all errors are logged at WARNING level so they can never crash
    the caller (typically a reaper sweep).

    Returns ``True`` when a new entry was recorded, ``False`` when already
    present (dedup), when the capability name cannot be normalized, or when
    ``test_refs`` is empty.
    """
    from app.time_utils import _utcnow

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

    # Stable dedup key: one named-test-present proof record per capability
    # verification.
    dedup_task_id = f"named_test_present:{norm}"
    proved_at = _utcnow().isoformat()
    proof_ref = ",".join(refs)

    proof_entry: dict[str, Any] = {
        "task_id": dedup_task_id,
        "proved_at": proved_at,
        "proof_kind": _PROOF_KIND_NAMED_TEST_PRESENT,
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
        "[CAPABILITY_PROOF] recorded named-test-present (path_proven tier) "
        "proof for cap=%s refs=%s",
        norm,
        refs,
    )

    _append_evidence_refs_to_capabilities(
        project,
        [norm],
        {
            "kind": _PROOF_KIND_NAMED_TEST_PRESENT,
            "ref": proof_ref,
            "proven": True,
            "source_id": dedup_task_id,
            "recorded_at": proved_at,
        },
    )
    return True


def record_capability_named_test_execution_proof(
    project: Any,
    cap_name: str,
    test_refs: list[str],
    head_sha: str | None = None,
    *,
    suite_verifying: bool = True,
) -> bool:
    """Record a bounded GENUINE ``named_test``-tier (``proven``) proof from an
    actual test EXECUTION.

    Slice 1 of the machine-verified capability-proof earn path (design doc
    84021e2d). Unlike :func:`record_capability_named_test_proof` — which mints the
    structural-only ``named_test_present`` tier from STATIC file-existence
    discovery (a convention-named test exists at HEAD and imports the backing
    module, but is never run; graded ``path_proven``) — this recorder stamps the
    genuine ``named_test`` proof kind from an *executed* covering suite. Because
    ``named_test`` is NOT in :data:`~app.evidence_primitive.BACKING_ONLY_KINDS`,
    both :func:`classify_evidence_tiered` and
    :func:`~app.capability_registry._ledger_proof_status` grade the resulting
    ledger ``proven`` — so a fresh execution proof clears the enforce gate and
    :func:`~app.capability_registry.ledger_corroborated_proof_status`
    CORROBORATES (does not demote) it. This is the ONLY automated path that mints
    ``proven`` outside the merge gate (:func:`record_capability_proof`).

    Like every recorder in this module it writes to the SAME durable
    ``project.capability_proof_records`` ledger and appends the SAME EvidenceRef
    shape (via :func:`_append_evidence_refs_to_capabilities`).

    **Freshness re-verification** (the one behavioural difference vs. the other
    recorders): the dedup key ``named_test_exec:<normalized_cap_name>`` is used to
    REPLACE any prior entry with fresh ``proved_at`` / ``recorded_at`` and the
    current ``commit_sha=head_sha``, rather than returning ``False`` on a dedup
    hit. This lets :data:`~app.evidence_primitive.PROOF_FRESHNESS_TTL` (applied in
    :func:`~app.capability_registry.working_capability_proof_status`) age out a
    stale green so the capability drops below ``proven`` and is re-dispatched by a
    later trial. The appended EvidenceRef is replaced in place too
    (``replace=True``) so the ledger carries exactly one fresh executed ref.

    **Vacuous-green rejection**: a green run that verified nothing is not proof.
    The caller signals a skipped/empty suite via ``suite_verifying=False`` (cf.
    ``_suite_is_non_verifying`` in :mod:`app.ci_resolution`); the recorder itself
    additionally refuses empty ``test_refs``. In either case nothing is recorded.

    Non-raising: all errors are logged at WARNING level so they can never crash
    the caller.

    Returns ``True`` when a fresh proof was recorded (newly inserted OR replacing
    a prior stale entry with the same dedup key), ``False`` when the capability
    name cannot be normalized, ``test_refs`` is empty, or the executed suite did
    not actually verify anything (``suite_verifying=False``).
    """
    from app.time_utils import _utcnow

    norm = normalize_capability_name(cap_name)
    if not norm:
        logger.warning(
            "[CAPABILITY_PROOF] cannot record executed named-test proof — "
            "capability name %r could not be normalized",
            cap_name,
        )
        return False

    if not suite_verifying:
        logger.warning(
            "[CAPABILITY_PROOF] refusing vacuous executed named-test proof for "
            "cap=%s — the executed suite verified nothing (skipped/empty)",
            norm,
        )
        return False

    refs = [r for r in (test_refs or []) if isinstance(r, str) and r.strip()]
    if not refs:
        logger.warning(
            "[CAPABILITY_PROOF] cannot record executed named-test proof for "
            "cap=%s — no covering test refs supplied",
            norm,
        )
        return False

    # Stable dedup key: one executed named-test proof per capability. Distinct
    # prefix from the STATIC ``named_test_present:`` earn so the two coexist.
    dedup_task_id = f"named_test_exec:{norm}"
    proved_at = _utcnow().isoformat()
    proof_ref = ",".join(refs)
    commit_sha = head_sha if isinstance(head_sha, str) else ""

    proof_entry: dict[str, Any] = {
        "task_id": dedup_task_id,
        "proved_at": proved_at,
        "recorded_at": proved_at,
        "proof_kind": _PROOF_KIND_NAMED_TEST,
        "proof_ref": proof_ref,
        "test_refs": refs,
        "executed": True,
        "commit_sha": commit_sha,
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
    # Freshness re-verification: REPLACE any prior entry with this dedup key
    # (fresh timestamps + current commit_sha) instead of deduping it away, so a
    # stale green ages out under PROOF_FRESHNESS_TTL and gets re-dispatched.
    entries = [e for e in entries if e.get("task_id") != dedup_task_id]
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
        "[CAPABILITY_PROOF] recorded EXECUTED named-test (proven tier) proof for "
        "cap=%s refs=%s sha=%s",
        norm,
        refs,
        commit_sha,
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
            "executed": True,
            "commit_sha": commit_sha,
        },
        replace=True,
    )
    return True


def revoke_stale_execution_proof(project: Any, cap_name: str) -> bool:
    """Remove the ``named_test_exec:<cap>`` ledger entry (and its evidence_ref) for
    ``cap_name`` after a definitive RED trial proves the capability broken at HEAD.

    Called from :func:`app.task_completion._handle_capability_proof_trial_completion`
    when a trial ran to completion and failed (``ran=True``, ``passed=False``,
    ``could_not_run=False``). Without this revocation the old green proof (written at
    an earlier revision) keeps clearing the enforce gate until the 30-day TTL expires
    — even though the capability is demonstrably broken at the current HEAD.

    Non-raising: all errors are logged at WARNING level so a revocation failure can
    never strand the surrounding commit or prevent the contradiction from being
    recorded.

    Returns ``True`` when the ledger entry was present and removed, ``False`` when
    no entry existed (nothing to revoke) or on error.
    """
    norm = normalize_capability_name(cap_name)
    if not norm:
        return False

    try:
        if not isinstance(getattr(project, "capability_proof_records", None), dict):
            return False

        dedup_task_id = f"named_test_exec:{norm}"
        existing = list(project.capability_proof_records.get(norm) or [])
        pruned = [e for e in existing if not (isinstance(e, dict) and e.get("task_id") == dedup_task_id)]
        if len(pruned) == len(existing):
            # No executed-proof entry was present — nothing to revoke.
            return False

        project.capability_proof_records[norm] = pruned
        try:
            flag_modified(project, "capability_proof_records")
        except Exception:  # noqa: BLE001
            logger.warning(
                "[CAPABILITY_PROOF] revoke: flag_modified failed for capability_proof_records",
                exc_info=True,
            )
            return False

        # Mirror the revocation into problem_model evidence_refs so the
        # tier-1 proof_status no longer sees the executed-proof ref.
        try:
            pm = getattr(project, "problem_model", None)
            if isinstance(pm, dict):
                caps = pm.get("capabilities")
                if isinstance(caps, list):
                    ref_removed = False
                    for cap in caps:
                        if not isinstance(cap, dict):
                            continue
                        cap_nm = cap.get("name")
                        if not isinstance(cap_nm, str):
                            continue
                        if normalize_capability_name(cap_nm) != norm:
                            continue
                        refs = cap.get("evidence_refs")
                        if not isinstance(refs, list):
                            continue
                        new_refs = [
                            r for r in refs
                            if not (isinstance(r, dict) and r.get("source_id") == dedup_task_id)
                        ]
                        if len(new_refs) < len(refs):
                            cap["evidence_refs"] = new_refs
                            ref_removed = True
                    if ref_removed:
                        flag_modified(project, "problem_model")
        except Exception:  # noqa: BLE001
            logger.warning(
                "[CAPABILITY_PROOF] revoke: failed to remove evidence_ref from problem_model",
                exc_info=True,
            )

        logger.info(
            "[CAPABILITY_PROOF] revoked stale execution proof for cap=%s (definitive RED)",
            norm,
        )
        return True

    except Exception:  # noqa: BLE001 — revocation must degrade, never raise
        logger.warning(
            "[CAPABILITY_PROOF] revoke_stale_execution_proof failed for cap=%r",
            cap_name,
            exc_info=True,
        )
        return False


async def _default_read_head_sha() -> str | None:
    """Resolve the current verification-branch HEAD SHA (or ``None``).

    Thin default injectable for the earn-loop trial cadence guard; delegates to
    :func:`app.source_ref_validation.resolve_head_commit`, which returns ``None``
    when HEAD is indeterminate (no creds + no checkout, or any API/git error).
    """
    from app.source_ref_validation import resolve_head_commit

    return await resolve_head_commit()


def _fresh_executed_proof_covers_head(
    project: Any,
    cap_name: str,
    head_sha: str | None,
    now: Any = None,
) -> bool:
    """Whether a GREEN *executed* named_test proof for ``cap_name`` already covers
    the current HEAD.

    This is the idempotence / cadence guard for earn-loop trial enrollment
    (design §3.4): a fresh trial is NOT re-enrolled when the executed proof
    (dedup key ``named_test_exec:<cap>``) is still within
    :data:`~app.evidence_primitive.PROOF_FRESHNESS_TTL` **and** its stored
    ``commit_sha`` matches ``head_sha``. Returns ``False`` — the re-verify signal —
    when the proof aged past the TTL, its ``commit_sha`` differs from ``head_sha``,
    HEAD is indeterminate, or no executed proof exists.
    """
    from app.evidence_primitive import PROOF_FRESHNESS_TTL, _ref_is_expired
    from app.time_utils import _utcnow

    norm = normalize_capability_name(cap_name)
    if not norm:
        return False
    # HEAD indeterminate → we cannot assert coverage of the current tree; allow
    # re-enroll rather than silently suppress on an unknown SHA.
    if not isinstance(head_sha, str) or not head_sha:
        return False
    records = getattr(project, "capability_proof_records", None)
    if not isinstance(records, dict):
        return False
    exec_dedup_id = f"named_test_exec:{norm}"
    _now = now or _utcnow()
    for entry in records.get(norm) or []:
        if not isinstance(entry, dict):
            continue
        task_id = entry.get("task_id", "")
        proof_kind = entry.get("proof_kind", "")
        commit_sha_val = entry.get("commit_sha", "")
        # Accept a direct executed-proof entry OR a merge-gate named_test proof
        # with a non-empty commit_sha — the latter avoids unnecessary trial
        # enrollment when the capability was already proven at HEAD by the merge
        # gate (record_capability_proof) and its commit_sha matches head_sha.
        is_exec_entry = task_id == exec_dedup_id
        is_merge_gate_proof = (
            proof_kind == _PROOF_KIND_NAMED_TEST
            and isinstance(commit_sha_val, str)
            and bool(commit_sha_val)
        )
        if not (is_exec_entry or is_merge_gate_proof):
            continue
        if entry.get("commit_sha") != head_sha:
            # Proof exists but for a DIFFERENT tree — re-verify.
            continue
        if _ref_is_expired(entry, _now, PROOF_FRESHNESS_TTL):
            # Aged past the TTL — the genuine re-verification loop.
            continue
        return True
    return False


async def _maybe_enroll_proof_trial(
    session: Any,
    project: Any,
    raw_name: str,
    canonical: str,
    test_refs: list[str],
    *,
    read_head_sha: Any = None,
    now: Any = None,
) -> str | None:
    """Decide whether a discovered-covered capability enrolls a proof trial.

    Shared by both discovery points (:func:`commission_proof_earn_loop` and the
    ``check_capability_proof_gate_gap`` self-heal). Returns:

    * ``"enrolled"`` — a bounded ``capability_proof_trial`` Task was persisted;
      the caller counts the cap under ``named_test_executed`` (it is EARNING the
      real ``proven`` tier via execution, not the static ``named_test_present``).
    * ``"fresh"`` — a fresh green executed proof already covers HEAD
      (:func:`_fresh_executed_proof_covers_head`); nothing to re-verify this
      sweep. The caller does NOT downgrade to a static proof.
    * ``None`` — the execution-trials flag is disabled or enrollment
      degraded to a no-op; the caller falls back to the UNCHANGED static
      ``named_test_present`` / ``backing_path`` (``path_proven``) recording.

    Non-raising: any error degrades to ``None`` so the legacy static path always
    remains available.
    """
    try:
        from app.shared_state import runtime_config as _rc

        if not (_rc or {}).get("capability_proof_execution_trials_enabled", False):
            return None
        if session is None:
            return None

        _resolve = read_head_sha or _default_read_head_sha
        try:
            head_sha = await _resolve()
        except Exception:  # noqa: BLE001 — HEAD resolution failure is non-fatal
            head_sha = None

        if _fresh_executed_proof_covers_head(project, canonical, head_sha, now=now):
            return "fresh"

        task = await enroll_capability_proof_trial(
            session, project, raw_name or canonical, test_refs, head_sha=head_sha
        )
        return "enrolled" if task is not None else None
    except Exception:  # noqa: BLE001 — enrollment decision must degrade, never raise
        logger.warning(
            "[CAP_TRIAL] _maybe_enroll_proof_trial degraded for cap=%s",
            canonical,
            exc_info=True,
        )
        return None


async def enroll_capability_proof_trial(
    session: Any,
    project: Any,
    cap_name: str,
    test_refs: list[str],
    head_sha: str | None = None,
) -> Any:
    """Create + persist a ``capability_proof_trial`` Task for one capability.

    Slice 2 of the machine-verified capability-proof earn path (design 84021e2d).
    The trial is a bounded, targeted runner job: the agent handler runs ONLY the
    passed ``test_refs`` (the capability's declared convention-named covering
    tests), and on a green run the ingestion completion handler mints a genuine
    ``proven`` named_test proof (:func:`record_capability_named_test_execution_proof`).

    This is the ENROLL half, called by the proof-earn sweep and on-demand
    discovery. It is gated on the ``capability_proof_execution_trials_enabled``
    runtime flag (default True); with the operator kill switch off it is a no-op
    returning ``None``.

    Bounded launch contract (design §3.2 / §6.2): ``test_refs`` is normalized and
    capped to :data:`MAX_CAPABILITY_PROOF_TRIAL_TEST_FILES`, and a wall-clock
    timeout (:data:`CAPABILITY_PROOF_TRIAL_WALL_CLOCK_SECONDS`) is stamped into the
    payload. A trial that cannot be built cleanly (flag off, unnormalizable
    capability name, or no usable test refs) is a no-op returning ``None`` — never
    a raise, never a fabricated proof.

    Returns the persisted Task on success, or ``None`` on any no-op/degraded path.
    Non-raising: any error is logged at WARNING level and yields ``None`` so a
    caller (a future sweep) can never be crashed by enrollment.
    """
    try:
        from app.shared_state import runtime_config as _rc

        if not (_rc or {}).get("capability_proof_execution_trials_enabled", False):
            return None

        norm = normalize_capability_name(cap_name)
        if not norm:
            logger.warning(
                "[CAP_TRIAL] cannot enroll trial — capability name %r could not be "
                "normalized",
                cap_name,
            )
            return None

        refs = [r for r in (test_refs or []) if isinstance(r, str) and r.strip()]
        if not refs:
            logger.warning(
                "[CAP_TRIAL] cannot enroll trial for cap=%s — no usable test refs",
                norm,
            )
            return None
        refs = refs[:MAX_CAPABILITY_PROOF_TRIAL_TEST_FILES]

        from sqlalchemy import select

        from app.models import Task, TaskStatus

        # Dedup guard: if a trial for this capability is already QUEUED,
        # PROVISIONING, IN_PROGRESS, or EVALUATING, do not enroll another.
        # This ensures "revision B commissions exactly one re-verification" and
        # prevents concurrent earn sweeps from creating duplicate trials that race
        # to replace each other's proof.
        project_id = getattr(project, "id", None)
        in_flight_statuses = [
            TaskStatus.QUEUED,
            TaskStatus.PROVISIONING,
            TaskStatus.IN_PROGRESS,
            TaskStatus.EVALUATING,
        ]
        try:
            existing_q = await session.execute(
                select(Task.id).where(
                    Task.project_id == project_id,
                    Task.status.in_(in_flight_statuses),
                    Task.payloads.contains(
                        [{"type": "capability_proof_trial", "cap": norm}]
                    ),
                ).limit(1)
            )
            if existing_q.scalar_one_or_none() is not None:
                logger.info(
                    "[CAP_TRIAL] skipping duplicate enrollment for cap=%s — "
                    "a trial is already in-flight",
                    norm,
                )
                return None
        except Exception:  # noqa: BLE001 — dedup failure is non-fatal; enroll anyway
            logger.warning(
                "[CAP_TRIAL] dedup check failed for cap=%s — proceeding with enroll",
                norm,
                exc_info=True,
            )

        payload: dict[str, Any] = {
            "type": "capability_proof_trial",
            "cap": norm,
            "test_refs": refs,
            "head_sha": head_sha if isinstance(head_sha, str) else None,
            "max_test_files": MAX_CAPABILITY_PROOF_TRIAL_TEST_FILES,
            "wall_clock_seconds": CAPABILITY_PROOF_TRIAL_WALL_CLOCK_SECONDS,
        }
        task = Task(
            project_id=getattr(project, "id", None),
            title=f"Capability proof trial: {norm}",
            description=(
                f"Run the declared covering tests for capability '{norm}' as a "
                "targeted runner job; a green run mints a proven named_test proof."
            ),
            payloads=[payload],
            status=TaskStatus.QUEUED,
            created_by="ORCHESTRATOR",
        )
        session.add(task)
        await session.commit()
        await session.refresh(task)
        logger.info(
            "[CAP_TRIAL] enrolled trial task %s for cap=%s refs=%s sha=%s",
            task.id,
            norm,
            refs,
            head_sha,
        )
        return task
    except Exception:  # noqa: BLE001 — enrollment must degrade, never raise
        logger.warning(
            "[CAP_TRIAL] failed to enroll capability_proof_trial for cap=%r",
            cap_name,
            exc_info=True,
        )
        try:
            await session.rollback()
        except Exception:  # noqa: BLE001
            logger.debug(
                "[CAP_TRIAL] rollback after enroll failure also failed", exc_info=True
            )
        return None


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


def _absorption_forward_marker(
    survivor_norm: str, abs_norm: str, proved_at: str
) -> dict[str, Any]:
    """Build the durable, PLATFORM-authored forwarding marker left under an absorbed
    capability's ledger key after its real proof/exemption entries are moved onto
    the survivor's key by :func:`carry_proof_records_for_consolidation`.

    This marker is the trustworthy record of a GENUINE rename: it is written only
    from the model-write consolidation path (which required the absorbed capability
    to actually be deleted), never by an agent editing ``problem_model``. It carries
    :data:`~app.capability_registry.CAPABILITY_ABSORPTION_FORWARD_PROOF_KIND`, so every
    machine-proof and exemption pass filters it out — it never itself becomes proof or
    an exemption — and :func:`~app.capability_registry.platform_recorded_alias_map`
    reads it to reconstruct the absorbed→survivor relationship the authority seams use
    in place of the agent-writable ``absorbs`` field.
    """
    from app.capability_registry import CAPABILITY_ABSORPTION_FORWARD_PROOF_KIND

    return {
        "task_id": f"absorbed_into:{abs_norm}",
        "proof_kind": CAPABILITY_ABSORPTION_FORWARD_PROOF_KIND,
        "survivor": survivor_norm,
        "absorbed": abs_norm,
        "proof_ref": survivor_norm,
        "proved_at": proved_at,
        "commit_sha": "",
    }


def carry_proof_records_for_consolidation(
    capabilities_consolidated: list[dict[str, Any]],
    proof_records: dict[str, Any],
) -> dict[str, Any]:
    """Merge absorbed capability proof entries into the survivor's key.

    For each {survivor, absorbed[...]} consolidation event, all proof entries
    keyed under absorbed names are merged into the survivor's list, deduped by
    task_id, capped at _MAX_PROOF_ENTRIES_PER_CAPABILITY (oldest evicted). Each
    absorbed key is then REPLACED with a single platform-authored FORWARDING marker
    (:func:`_absorption_forward_marker`) recording the survivor it merged into —
    rather than being popped empty. Because this carry runs ONLY from the genuine,
    destructive model-write consolidation path (the ``capabilities_consolidated``
    diff, emitted only when a survivor's ``absorbs`` names a JUST-DELETED
    capability), the marker is a trustworthy record of a real rename. The authority
    seams read it via :func:`~app.capability_registry.platform_recorded_alias_map`
    instead of the agent-writable ``problem_model.absorbs`` list, so a survivor's
    proof/exemption forwards to the old name only after a genuine consolidation —
    never because an agent merely declares ``absorbs: [proven-or-exempt-name]``.

    A pre-existing forwarding marker under an absorbed key is NOT carried onto the
    survivor as proof (it is not evidence of anything); only real proof/exemption
    entries move.

    Returns a NEW dict — does not mutate the input.  Callers are responsible for
    flag_modified on the JSONB column after assigning the result.
    """
    if not capabilities_consolidated or not isinstance(proof_records, dict):
        return proof_records

    from app.capability_registry import _entry_is_absorption_forward
    from app.time_utils import _utcnow

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
        proved_at = _utcnow().isoformat()

        for abs_name in absorbed_names:
            abs_norm = normalize_capability_name(abs_name)
            # A self-referential absorb (survivor absorbs its own name) would corrupt
            # the survivor's own ledger — skip it and never mark the survivor as
            # forwarding to itself.
            if not abs_norm or abs_norm == survivor_norm:
                continue
            for entry in result.get(abs_norm) or []:
                if not isinstance(entry, dict):
                    continue
                # A prior consolidation's forwarding marker is a pointer, not proof;
                # do not launder it onto the survivor as an evidence entry.
                if _entry_is_absorption_forward(entry):
                    continue
                tid = entry.get("task_id", "")
                if tid not in seen_task_ids:
                    merged.append(entry)
                    seen_task_ids.add(tid)
            # Leave a durable forwarding marker under the absorbed key instead of
            # popping it, so the platform-recorded alias map can resolve a dependent
            # naming the old absorbed capability to the survivor's moved proof.
            result[abs_norm] = [
                _absorption_forward_marker(survivor_norm, abs_norm, proved_at)
            ]

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
# the earn loop mint a ``named_test_present`` proof. This discovery is STATIC
# (file exists + import regex; the test is never executed), so
# ``classify_evidence_tiered`` grades it ``path_proven`` — the same tier as a
# ``backing_path`` proof, NOT the executed ``proven`` ``named_test`` tier.

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


async def _read_file_on_branch(
    path: str,
    *,
    token: str,
    owner: str,
    repo: str,
    branch: str,
    project_id: str | None = None,
) -> str | None:
    """Return the contents of ``path`` on ``owner/repo@branch``, or ``None``.

    GitHub-backed twin of :func:`_read_file_at_head` for a backend with no local
    checkout (production runs under ``/app`` with no ``/tmp/workspace``). Reads
    the file text from the project's real integration-branch tree via the cached
    snapshot reader (:func:`app.github_repo_snapshot.file_text_on_branch`).

    ``None`` on any indeterminacy — snapshot unavailable, path absent, or a
    present-but-unreadable blob — so the caller degrades to a lower proof tier
    exactly as it does when a local checkout is missing. Never raises.
    """
    if not path or not isinstance(path, str):
        return None
    try:
        from app.github_repo_snapshot import file_text_on_branch

        exists, text = await file_text_on_branch(
            path,
            token=token,
            owner=owner,
            repo=repo,
            branch=branch,
            project_id=project_id,
        )
    except Exception:  # noqa: BLE001 — discovery must degrade, never raise
        logger.warning(
            "[CAPABILITY_PROOF] failed to read %s on branch %s", path, branch,
            exc_info=True,
        )
        return None
    # exists is tri-state (True/False/None); only a definitely-present file with
    # readable text yields content — absent/indeterminate/unreadable all degrade.
    if exists is not True:
        return None
    return text


async def _resolve_head_access(session: Any, project: Any):
    """Return ``(verify_path, read_head)`` bound to the project's real source.

    The capability-proof earn loop validates source references against HEAD. In
    production the backend image runs under ``/app`` with no ``/tmp/workspace``
    checkout, so the local-git helpers (:func:`_path_exists_at_head`,
    :func:`_read_file_at_head`) resolve indeterminate and every earn attempt
    stalls. When a ``session`` and ``project`` are available this resolves the
    project's GitHub integration-branch context (the SAME
    :func:`app.source_ref_validation.resolve_branch_verification_context` the
    design-doc grounding path uses) and returns reader callables backed by the
    cached branch-tree snapshot.

    Falls back to the local-checkout helpers when no session/project is supplied
    or the project's GitHub credentials do not resolve — preserving local
    validation whenever a real checkout is explicitly available (the agent VM).
    Both callables keep the tri-state ``True``/``False``/``None`` contract, so
    an unavailable snapshot degrades conservatively and never over-claims proof.
    """
    from app.source_ref_validation import _path_exists_at_head

    local = (_path_exists_at_head, _read_file_at_head)
    if session is None or project is None:
        return local

    try:
        from app.source_ref_validation import resolve_branch_verification_context

        ctx = await resolve_branch_verification_context(session, project)
    except Exception:  # noqa: BLE001 — advisory earn path must degrade, never raise
        logger.warning(
            "[CAPABILITY_PROOF] branch verification context resolution failed; "
            "falling back to local HEAD",
            exc_info=True,
        )
        return local

    token = ctx.get("token")
    owner = ctx.get("owner")
    repo = ctx.get("repo")
    branch = ctx.get("branch")
    project_id = ctx.get("project_id")
    if not (token and owner and repo and branch):
        # No authorized repo credentials — fall back to local git HEAD.
        return local

    from app.source_ref_validation import _file_exists_in_repo_branch

    async def _verify(path: str) -> bool | None:
        return await _file_exists_in_repo_branch(
            path,
            token=token,
            owner=owner,
            repo=repo,
            branch=branch,
            project_id=project_id,
        )

    async def _read(path: str) -> str | None:
        return await _read_file_on_branch(
            path,
            token=token,
            owner=owner,
            repo=repo,
            branch=branch,
            project_id=project_id,
        )

    return (_verify, _read)


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


async def resolve_no_backing_paths_gap(
    cap_name: str,
    *,
    raw_name: str | None = None,
    verify_path=None,
    read_head=None,
    session: Any = None,
    read_head_sha=None,
    now: Any = None,
    project: Any = None,
) -> dict[str, Any]:
    """Attempt to resolve a proof gap for a working capability with no backing_paths.

    Performs automated discovery of candidate backing paths by deriving search
    tokens from the capability name and scanning the HEAD file tree. When
    candidates are found:

    * If a convention-named test file for EVERY candidate exists at HEAD and
      imports it, a ``named_test_present`` proof is recorded (``path_proven``
      tier).  Under the execution-trials flag an actual trial is enrolled instead,
      earning the eventual ``proven`` tier.
    * Otherwise, if candidates exist at HEAD but lack test coverage, a
      ``backing_path`` proof is recorded (``path_proven`` tier).

    When no candidates are found at all, returns ``resolved=False`` so the caller
    can report the cap as needing manual backing-path assignment.

    All IO callables are injectable so the function is unit-testable without a
    git checkout. Never raises: any error degrades to ``{"resolved": False}``.

    Returns
    -------
    A dict with the following keys:
        resolved (bool): True when a proof was recorded (or trial enrolled).
        proof_kind (str | None): ``"named_test_present"``, ``"backing_path"``,
            or ``None`` when unresolved.
        trial_enrolled (bool): True when an execution trial was enrolled.
        named_test_earned (bool): True when a ``named_test_present`` proof was
            recorded (as opposed to the ``backing_path`` fallback).
        trial_fresh (bool): True when a fresh executed proof already covers HEAD.
        discovery_suggestions (list[str]): Candidate paths discovered, if any.
    """
    from app.source_ref_validation import _path_exists_at_head

    _verify = verify_path or _path_exists_at_head
    _read_head = read_head or _read_file_at_head

    try:
        candidates = await _discover_candidate_backing_paths(
            cap_name, verify_path=_verify,
        )
    except Exception:  # noqa: BLE001 — discovery must degrade, never raise
        logger.warning(
            "[CAPABILITY_PROOF] resolve_no_backing_paths_gap: discovery "
            "failed for cap=%s",
            cap_name,
            exc_info=True,
        )
        candidates = []

    if not candidates:
        return {
            "resolved": False,
            "proof_kind": None,
            "trial_enrolled": False,
            "named_test_earned": False,
            "trial_fresh": False,
            "discovery_suggestions": [],
        }

    try:
        test_refs = await _named_tests_covering_paths(
            candidates, verify_path=_verify, read_head=_read_head
        )
    except Exception:  # noqa: BLE001 — test discovery failure is non-fatal
        logger.warning(
            "[CAPABILITY_PROOF] resolve_no_backing_paths_gap: named-test "
            "discovery failed for cap=%s",
            cap_name,
            exc_info=True,
        )
        test_refs = []

    if test_refs:
        trial_outcome = await _maybe_enroll_proof_trial(
            session,
            project,
            raw_name or cap_name,
            cap_name,
            test_refs,
            read_head_sha=read_head_sha,
            now=now,
        )
        if trial_outcome == "enrolled":
            return {
                "resolved": True,
                "proof_kind": None,
                "trial_enrolled": True,
                "named_test_earned": False,
                "trial_fresh": False,
                "discovery_suggestions": candidates,
            }
        if trial_outcome == "fresh":
            return {
                "resolved": True,
                "proof_kind": None,
                "trial_enrolled": False,
                "named_test_earned": False,
                "trial_fresh": True,
                "discovery_suggestions": candidates,
            }

        recorded = record_capability_named_test_proof(
            project, raw_name or cap_name, test_refs
        )
        if recorded:
            return {
                "resolved": True,
                "proof_kind": _PROOF_KIND_NAMED_TEST_PRESENT,
                "trial_enrolled": False,
                "named_test_earned": True,
                "trial_fresh": False,
                "discovery_suggestions": candidates,
            }

    # Fallback: backing_path proof when candidates exist but no covering test.
    recorded = record_capability_backing_path_proof(
        project, raw_name or cap_name
    )
    if recorded:
        return {
            "resolved": True,
            "proof_kind": _PROOF_KIND_BACKING_PATH,
            "trial_enrolled": False,
            "named_test_earned": False,
            "trial_fresh": False,
            "discovery_suggestions": candidates,
        }

    # Dedup or recording failure — cap stays unresolved but suggestions exist.
    return {
        "resolved": False,
        "proof_kind": None,
        "trial_enrolled": False,
        "named_test_earned": False,
        "trial_fresh": False,
        "discovery_suggestions": candidates,
    }


async def commission_proof_earn_loop(
    project: Any,
    demand_result: dict[str, Any],
    problem_model: dict[str, Any],
    *,
    verify_path=None,
    read_head=None,
    session: Any = None,
    read_head_sha=None,
    now: Any = None,
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
       paths are definitely present, it then attempts the *higher* tier:
       :func:`_named_tests_covering_paths` looks for real test files that
       exercise (import) every backing path.  When every path has a
       convention-named test file present at HEAD that imports it, a
       ``named_test_present`` proof is recorded via
       :func:`record_capability_named_test_proof`.  That discovery is STATIC —
       the test is never executed — so :func:`classify_evidence_tiered` grades it
       ``path_proven`` (the SAME tier as the ``backing_path`` fallback), NOT
       ``proven``: it does NOT clear the enforce gate.  Only a real green
       merge-test acceptance verdict (:func:`record_capability_proof`) mints the
       ``proven`` ``named_test`` tier.  The fallback when no covering test is
       discoverable is the ``backing_path`` proof
       (:func:`record_capability_backing_path_proof`, also ``path_proven``).

    3. For each demanded unproven capability that has NO ``backing_paths``:
       automated discovery is attempted via :func:`resolve_no_backing_paths_gap`:
       search tokens derived from the capability name locate candidate files at
       HEAD; when candidates are found and a covering test exists, a
       ``named_test_present`` (path_proven) proof is recorded (or an execution
       trial enrolled); when candidates exist but lack test coverage, a
       ``backing_path`` (path_proven) proof is recorded.  Caps that cannot be
       auto-resolved (no discoverable candidates) are reported in the return
       summary so callers can surface the gap.

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
        contents of a test file (or ``None`` when indeterminate).  Defaults to
        :func:`_read_file_at_head`.  Injected for testability so the
        named_test-present branch can be exercised without a git checkout.
    session:
        Async DB session.  When supplied AND the
        ``capability_proof_execution_trials_enabled`` runtime flag is ON, a
        capability whose covering tests are discovered ENROLLS a bounded
        ``capability_proof_trial`` (executed → genuine ``proven`` tier) instead of
        recording the static ``named_test_present`` proof.  When ``None`` (or the
        flag is off), behaviour is byte-for-byte the legacy ``path_proven`` path.
    read_head_sha:
        Async callable ``() -> str | None`` returning the current HEAD SHA, used
        for the trial cadence/idempotence guard (re-enroll only when HEAD moved or
        the executed proof aged out).  Defaults to :func:`_default_read_head_sha`;
        injected for testability.
    now:
        Reference time for the executed-proof freshness check; defaults to
        :func:`app.time_utils._utcnow`.

    Returns
    -------
    A summary dict:
    ``{"total_demanded_unproven": int, "earned": int, "no_backing_paths": list[str],
      "still_unproven": list[str], "path_proven_stuck": list[str],
      "named_test_earned": list[str], "named_test_executed": list[str]}``.
    ``named_test_executed`` is the subset of caps that enrolled a real execution
    trial this sweep (flag on) — the path that earns the ``proven`` tier.
    Total entries found, count of newly-recorded proofs (named_test_present +
    backing_path), capability names with no backing_paths, remaining unproven
    names (failed path check), already-recorded (stuck) names that were not newly
    recorded, and the subset of newly-recorded names that got a
    ``named_test_present`` proof (a convention test file is present at HEAD).
    NOTE: both recorded tiers are graded ``path_proven`` — neither clears the
    enforce gate; a capability reaches ``proven`` only via a real green
    merge-test acceptance verdict (:func:`record_capability_proof`).
    """
    from app.capability_demand import compute_unproven_demanded_capabilities
    from app.source_ref_validation import (
        _looks_like_file_path,
        _path_exists_at_head,
    )

    # Resolve the HEAD readers. When the caller injects neither hook (the
    # production path), bind them to the project's real source: GitHub
    # integration branch when credentials resolve, else the local checkout.
    # This lets a backend with no /tmp/workspace checkout (prod runs under
    # /app) still validate source references. An explicitly-injected hook (unit
    # tests) is always honoured verbatim and never triggers credential
    # resolution.
    if verify_path is None and read_head is None:
        _verify, _read_head = await _resolve_head_access(session, project)
    else:
        _verify = verify_path or _path_exists_at_head
        _read_head = read_head or _read_file_at_head

    # Step 1: compute the intersection.
    demanded_unproven = compute_unproven_demanded_capabilities(
        demand_result,
        problem_model,
        getattr(project, "capability_proof_records", None),
        now=now,
    )
    if not demanded_unproven:
        return {
            "total_demanded_unproven": 0,
            "earned": 0,
            "no_backing_paths": [],
            "still_unproven": [],
            "path_proven_stuck": [],
            "named_test_earned": [],
            "named_test_executed": [],
        }

    # Step 1b: defensive filter — skip platform-exempt capabilities (the
    # authoritative grade already clears them, so they should never appear in
    # demanded_unproven, but this is a belt-and-suspenders guard).
    _exempt = exempt_capability_names(
        problem_model, getattr(project, "capability_proof_records", None)
    )
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
    named_test_executed: list[str] = []
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
            # Try automated discovery + auto-resolve for caps without backing_paths.
            gap_result = await resolve_no_backing_paths_gap(
                canonical,
                raw_name=raw_name or canonical,
                verify_path=_verify,
                read_head=_read_head,
                session=session,
                read_head_sha=read_head_sha,
                now=now,
                project=project,
            )
            if gap_result.get("resolved"):
                earned_names.append(canonical)
                if gap_result.get("named_test_earned"):
                    named_test_earned.append(canonical)
                if gap_result.get("trial_enrolled"):
                    named_test_executed.append(canonical)
            else:
                no_backing_paths.append(canonical)
            continue

        # Filter to file-like paths.
        file_paths = [
            p for p in backing_paths
            if isinstance(p, str) and _looks_like_file_path(p)
        ]
        if not file_paths:
            # Same discovery + auto-resolve for caps whose declared paths are
            # not file-like (no automated verification possible from them).
            gap_result = await resolve_no_backing_paths_gap(
                canonical,
                raw_name=raw_name or canonical,
                verify_path=_verify,
                read_head=_read_head,
                session=session,
                read_head_sha=read_head_sha,
                now=now,
                project=project,
            )
            if gap_result.get("resolved"):
                earned_names.append(canonical)
                if gap_result.get("named_test_earned"):
                    named_test_earned.append(canonical)
                if gap_result.get("trial_enrolled"):
                    named_test_executed.append(canonical)
            else:
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
            # If a convention-named test file for every backing path exists at
            # HEAD and imports the module, record a ``named_test_present`` proof.
            # This is a STATIC signal (no test is executed), so it is graded
            # ``path_proven`` — richer metadata than a bare ``backing_path`` proof
            # but the SAME tier: it does NOT clear the enforce gate. Only a real
            # green merge-test verdict (record_capability_proof) reaches ``proven``.
            test_refs = await _named_tests_covering_paths(
                file_paths, verify_path=_verify, read_head=_read_head
            )
            if test_refs:
                # Slice 3 (design 84021e2d): under the execution-trials flag, a
                # discovered-covered cap ENROLLS a bounded capability_proof_trial
                # that actually RUNS the covering tests — a green run mints the
                # real ``proven`` tier. Cadence-guarded: no fresh trial when a
                # green executed proof already covers HEAD; re-enroll when it aged
                # out or HEAD moved. Flag-off / degraded / no-session falls through
                # to the UNCHANGED static ``named_test_present`` recording below.
                trial_outcome = await _maybe_enroll_proof_trial(
                    session,
                    project,
                    raw_name or canonical,
                    canonical,
                    test_refs,
                    read_head_sha=read_head_sha,
                    now=now,
                )
                if trial_outcome == "enrolled":
                    named_test_executed.append(canonical)
                    continue
                if trial_outcome == "fresh":
                    # A fresh green executed proof already covers HEAD — nothing
                    # to re-verify. Do NOT downgrade to the static proof.
                    continue

                recorded = record_capability_named_test_proof(
                    project, raw_name or canonical, test_refs
                )
                if recorded:
                    earned_names.append(canonical)
                    named_test_earned.append(canonical)
                else:
                    # Dedup hit: a named_test_present proof was already stamped
                    # in a prior sweep. Not newly recorded.
                    path_proven_stuck.append(canonical)
                continue

            # Fallback: paths present but no discoverable test coverage — record
            # the structural-only ``backing_path`` proof (graded ``path_proven``).
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
        "named_test_executed=%d no_backing_paths=%d still_unproven=%d",
        len(demanded_unproven),
        len(earned_names),
        len(named_test_earned),
        len(named_test_executed),
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
        "named_test_executed": named_test_executed,
    }
