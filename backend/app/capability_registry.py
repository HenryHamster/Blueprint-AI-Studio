"""Shared capability vocabulary helpers.

This module centralizes the capability-name primitives shared across the
spawn-time resolver, blocked-need matcher, and future registry-aware planner
surfaces. It is intentionally small and IO-free.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from typing import TYPE_CHECKING

from app.evidence_primitive import classify_evidence, classify_evidence_tiered

if TYPE_CHECKING:
    from typing import Literal

    from app.evidence_primitive import EvidenceStatus
    from app.models import Task

    #: Valid proof gate modes — narrowing the parameter type so callers and
    #: static analysis agree on the three-way mode knob.
    _GateMode = Literal["off", "observe", "enforce"]
else:
    _GateMode = str

_NORMALIZE_NON_ALNUM_RE = re.compile(r"[^a-z0-9]+")
_DERIVED_PROVIDER_PREFIX = "task-provider:"


def normalize_capability_name(raw: object) -> str | None:
    """Return a canonical kebab-case capability name, or ``None``.

    This is the human capability vocabulary normalizer: it lowercases, trims,
    collapses any run of non-alphanumeric characters to ``-``, and strips
    leading/trailing separators.
    """
    if not isinstance(raw, str):
        return None
    collapsed = _NORMALIZE_NON_ALNUM_RE.sub("-", raw.strip().lower()).strip("-")
    return collapsed or None


def _iter_capability_entries(problem_model: object):
    """Yield ``(raw_name, status, absorbs)`` triples from a problem_model's
    capabilities.

    Accepts either the pydantic ``ProblemModel`` instance or a dict-like payload
    carrying a ``capabilities`` collection. Shared by the name reader, the richer
    vocabulary reader, and the working-set reader so all three draw from the exact
    same source. ``absorbs`` is the list of old capability names the entry merged
    in during a consolidation (empty list when absent / older payload) — the
    alias-aware readers fold these into their output while the vocabulary reader
    ignores them so the rendered vocabulary stays survivor-only.
    """
    if isinstance(problem_model, Mapping):
        raw_caps = problem_model.get("capabilities")
    else:
        raw_caps = getattr(problem_model, "capabilities", None)
    if not isinstance(raw_caps, Iterable):
        return
    for cap in raw_caps:
        if isinstance(cap, Mapping):
            absorbs = cap.get("absorbs")
            yield cap.get("name"), cap.get("status"), absorbs
        else:
            absorbs = getattr(cap, "absorbs", None)
            yield getattr(cap, "name", None), getattr(cap, "status", None), absorbs


def _normalized_absorbed_names(absorbs: object) -> list[str]:
    """Canonicalize the ``absorbs`` list of one capability, dropping non-strings
    and unnormalizable / blank entries. Tolerates a non-list ``absorbs`` (returns
    ``[]``) so older payloads and junk shapes pass through harmlessly."""
    if not isinstance(absorbs, Iterable) or isinstance(absorbs, (str, bytes)):
        return []
    out: list[str] = []
    for name in absorbs:
        normalized = normalize_capability_name(name)
        if normalized:
            out.append(normalized)
    return out


def registry_capability_names(problem_model: object) -> list[str]:
    """Return the advisory known-set of canonical capability names.

    Accepts either the pydantic ``ProblemModel`` instance or a dict-like payload
    carrying a ``capabilities`` collection. Output preserves first-seen order
    while dropping blanks and duplicates.

    Alias-aware: each capability contributes its own canonical name AND the
    canonical form of every name in its ``absorbs`` list. This is the advisory
    known-set fed to :func:`off_registry_capability_refs` at every call site, so
    an old name absorbed during a consolidation is still treated as KNOWN (never
    flagged off-registry). It deliberately diverges from
    :func:`registry_capability_vocabulary`, which renders survivor names only so
    the consolidation actually shrinks the agent-facing vocabulary.
    """
    seen: set[str] = set()
    names: list[str] = []
    for raw_name, _status, absorbs in _iter_capability_entries(problem_model):
        for normalized in (
            normalize_capability_name(raw_name),
            *_normalized_absorbed_names(absorbs),
        ):
            if normalized and normalized not in seen:
                seen.add(normalized)
                names.append(normalized)
    return names


def registry_capability_vocabulary(problem_model: object) -> list[dict]:
    """Return the agent-facing capability vocabulary: canonical name + status.

    Built from the same registry source as :func:`registry_capability_names`,
    pairing each canonical kebab-case name with its declared status
    (``working`` / ``partial`` / ``planned``) for prompt rendering. Output
    preserves first-seen order, drops blanks and duplicates, and omits ``status``
    when it is absent/blank. Returns ``[]`` for a model with no capabilities, so
    callers can omit the additive ``available_capabilities`` payload field on the
    empty path.

    Survivor-only by design: unlike :func:`registry_capability_names` (the
    advisory known-set, which folds in each capability's ``absorbs`` aliases),
    this rendered vocabulary deliberately does NOT re-add absorbed old names. A
    consolidation removes the old names from what the agent sees here — that
    shrinkage is the whole point — while the old names keep RESOLVING through the
    alias-aware known-set and :func:`working_capability_names`.
    """
    seen: set[str] = set()
    vocab: list[dict] = []
    for raw_name, status, _absorbs in _iter_capability_entries(problem_model):
        normalized = normalize_capability_name(raw_name)
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        entry: dict = {"name": normalized}
        if isinstance(status, str) and status.strip():
            entry["status"] = status.strip()
        vocab.append(entry)
    return vocab


def registry_capability_evidence_refs_map(problem_model: object) -> dict[str, list]:
    """Build ``{canonical_cap_name: [EvidenceRef, ...]}`` from a problem_model.

    Returns a map of every capability's ``evidence_refs`` indexed by its
    canonical (normalized) name, matching the ``evidence_refs_map`` contract
    that :func:`annotate_capability_proof_status` consumes as its tier-1
    source of truth.  Capabilities with no ``evidence_refs`` (missing,
    ``None``, or empty) are omitted so the annotator falls back to
    proof-records for those, preserving backward compatibility.

    IO-free — reads only from the in-memory model.
    """
    refs_map: dict[str, list] = {}
    for raw_name, _status, _absorbs, evidence_refs, _backing_paths, _proof_exempt in _iter_capability_evidence_entries(
        problem_model
    ):
        normalized = normalize_capability_name(raw_name)
        if normalized and isinstance(evidence_refs, list) and evidence_refs:
            refs_map[normalized] = evidence_refs
    return refs_map


def annotate_capability_proof_status(
    vocab: list[dict],
    proof_records: dict | None,
    evidence_refs_map: dict | None = None,
) -> list[dict]:
    """Annotate each capability vocab entry with its durable proof status.

    Adds a ``proof_status`` field to each entry using a two-tier resolution:

    1. **Re-verified evidence refs** (``evidence_refs_map``): when the caller
       provides a ``{cap_name: [EvidenceRef, ...]}`` map, ``classify_evidence``
       determines the status — ``"proven"`` / ``"partial"`` / ``"unproven"`` /
       ``"contradicted"`` / ``"unverified"``.  This reflects ground-truth
       re-verification (e.g. a backing path removed at HEAD) rather than the
       write-once proof-record existence check, so the badge stays honest when
       old evidence is later disproved.

    2. **Legacy proof records** (``proof_records``): when ``evidence_refs_map``
       has no entry for a capability, falls back to the durable proof-record
       existence check — ``"proven"`` when at least one record exists, else
       ``"unproven"``.  Backward-compatible: callers that omit
       ``evidence_refs_map`` see unchanged behaviour.

    Input ``vocab`` is not mutated — a shallow copy of each entry is returned.
    Fail-open: unexpected shapes pass through unchanged. Shared by every spawn
    path (strategic, design, research, plan/finding) so all four prompt
    surfaces ship the SAME annotation alongside ``status``.
    """
    if not isinstance(vocab, list):
        return vocab
    records = proof_records if isinstance(proof_records, dict) else {}
    refs_map = evidence_refs_map if isinstance(evidence_refs_map, dict) else {}
    annotated: list[dict] = []
    for entry in vocab:
        if not isinstance(entry, dict):
            annotated.append(entry)
            continue
        entry2 = dict(entry)
        cap_name = entry2.get("name")
        ev_refs = refs_map.get(cap_name) if isinstance(cap_name, str) else None
        if isinstance(ev_refs, list) and ev_refs:
            entry2["proof_status"] = classify_evidence(ev_refs)
        elif isinstance(cap_name, str) and records.get(cap_name):
            # Use tier-aware classification so backing_path-only proof_records
            # return "path_proven" (not "proven"), matching enrich_problem_model_capability_proofs.
            synthetic_refs = [
                {"kind": e.get("proof_kind"), "proven": True}
                for e in records[cap_name]
                if isinstance(e, dict)
            ]
            entry2["proof_status"] = classify_evidence_tiered(synthetic_refs) if synthetic_refs else "unproven"
        else:
            entry2["proof_status"] = "unproven"
        annotated.append(entry2)
    return annotated


def working_capability_names(problem_model: object) -> set[str]:
    """Return canonical names of registry capabilities declared ``working``.

    A ``working`` capability already exists in the codebase per the
    strategist-curated registry, so a prerequisite naming it is satisfied by
    the repo itself — no task has to provide it. Only the exact ``working``
    status qualifies: ``partial`` / ``planned`` capabilities still gate, since
    the declared foundation genuinely does not (fully) exist yet.

    Alias-aware: a ``working`` capability contributes its own canonical name AND
    the canonical form of every name in its ``absorbs`` list. So after a
    consolidation merges several old capabilities into one working survivor, a
    prerequisite that named any absorbed old capability still resolves as
    satisfied-by-the-codebase — exactly as it did before consolidation. Both the
    spawn-time resolver (``research_completion``) and the blocked-resume matcher
    (``reaper_sweeps``) consume this set via
    :func:`registry_satisfies_capability_need`, so neither needs its own edit.
    """
    working: set[str] = set()
    for raw_name, status, absorbs in _iter_capability_entries(problem_model):
        if not (isinstance(status, str) and status.strip().lower() == "working"):
            continue
        normalized = normalize_capability_name(raw_name)
        if normalized:
            working.add(normalized)
        working.update(_normalized_absorbed_names(absorbs))
    return working


def registry_satisfies_capability_need(
    need: Mapping, working_names: Iterable[str]
) -> bool:
    """True iff ``need`` is a capability need whose ref the registry marks working.

    Shared by the spawn-time prerequisite resolver and the blocked-resume sweep
    so both layers agree that an already-``working`` capability never blocks a
    dependent. Non-capability needs and unnormalizable refs never match.
    """
    if not isinstance(need, Mapping) or need.get("kind") != "capability":
        return False
    normalized = normalize_capability_name(need.get("ref"))
    return normalized is not None and normalized in set(working_names)


def _iter_capability_evidence_entries(problem_model: object):
    """Yield ``(raw_name, status, absorbs, evidence_refs, backing_paths, proof_exempt)`` sextuples.

    The evidence-aware sibling of :func:`_iter_capability_entries`: it draws from
    the exact same ``capabilities`` source (accepting either the pydantic
    ``ProblemModel`` instance or a dict-like payload) but additionally surfaces
    each capability's ``evidence_refs``, ``backing_paths``, and ``proof_exempt``
    so the proof-aware resolvers can classify the backing evidence and apply
    exemption semantics WITHOUT widening the shared 3-tuple contract three other
    call sites unpack. ``evidence_refs`` / ``backing_paths`` are whatever the
    payload carries (a list, ``None`` on older/dict payloads that omit it, or
    junk) — callers coerce them before handing them to
    :func:`classify_evidence`. ``proof_exempt`` defaults to ``False`` for
    backward-compatibility with payloads that omit the field.
    """
    if isinstance(problem_model, Mapping):
        raw_caps = problem_model.get("capabilities")
    else:
        raw_caps = getattr(problem_model, "capabilities", None)
    if not isinstance(raw_caps, Iterable):
        return
    for cap in raw_caps:
        if isinstance(cap, Mapping):
            yield (
                cap.get("name"),
                cap.get("status"),
                cap.get("absorbs"),
                cap.get("evidence_refs"),
                cap.get("backing_paths"),
                cap.get("proof_exempt", False),
            )
        else:
            yield (
                getattr(cap, "name", None),
                getattr(cap, "status", None),
                getattr(cap, "absorbs", None),
                getattr(cap, "evidence_refs", None),
                getattr(cap, "backing_paths", None),
                getattr(cap, "proof_exempt", False),
            )


def working_capability_proof_status(
    problem_model: object,
) -> dict[str, EvidenceStatus]:
    """Map every ``working`` capability's canonical name to its evidence status.

    The proof-aware companion to :func:`working_capability_names`: it selects the
    exact same set of names (only ``working`` capabilities, alias-aware over each
    entry's ``absorbs`` list) but pairs each name with the :data:`EvidenceStatus`
    that :func:`classify_evidence_tiered` derives from the capability's
    ``evidence_refs`` — ``proven`` / ``partial`` / ``unproven`` / ``unverified``.
    A ``working`` capability with an empty or missing ``evidence_refs`` classifies
    as ``unproven`` (the "declared working with zero proof" case this gate exists
    to surface); one carrying a machine-verified ``proven=True`` ref classifies as
    ``proven``.

    Because this map feeds the enforce-gate path, it uses the *tier-aware*
    classifier: a capability whose ONLY ``proven=True`` ref is a
    ``backing_path`` (declared files merely exist at HEAD — the same self-report
    vector the gate exists to close) classifies as ``partial``, not ``proven``,
    and therefore does NOT clear the enforce gate on its own. A ``backing_path``
    proof corroborated by a machine-verified ``named_test`` still reads
    ``proven``. The badge/display path
    (:func:`annotate_capability_proof_status`) deliberately keeps the plain
    :func:`classify_evidence` so existing evidence still surfaces on the UI.

    When ``evidence_refs`` is empty or missing but the capability declares a
    non-empty ``backing_paths`` list, the status is upgraded to ``partial`` —
    the capability at least declares *where* its code is in the repo, even if no
    machine-checked verification has been recorded yet.  This prevents every
    registry capability from starting at ``unproven`` and distinguishes genuinely
    evidence-less claims from structurally-backed ones that simply haven't been
    through the earning loop.

    **Exemption**: a ``working`` capability with ``proof_exempt=True`` is
    classified as ``"proven"`` unconditionally — it satisfies the proof gate
    without machine-verified evidence, is excluded from the unproven count in
    the release qualifier, and does not inflate the metrics-sweep delta.
    Exemption is for capabilities that genuinely cannot have machine-verifiable
    proof (e.g. purely human processes, external integrations without test
    hooks) and must be explicitly set by an operator or strategic agent.

    IO-free and additive — the observe-mode wiring in both the spawn-time
    resolver and the blocked-resume sweep builds this map once per project
    (mirroring how they cache :func:`working_capability_names`) and looks needs up
    against it via :func:`capability_need_proof_status`. First-seen wins when two
    entries normalize to the same name, matching the name reader's dedupe order.
    """
    out: dict[str, EvidenceStatus] = {}
    for raw_name, status, absorbs, evidence_refs, backing_paths, proof_exempt in _iter_capability_evidence_entries(
        problem_model
    ):
        if not (isinstance(status, str) and status.strip().lower() == "working"):
            continue
        # Exempt working capabilities are unconditionally "proven" — they
        # satisfy the proof gate without machine-verified evidence.
        if proof_exempt:
            proof_status: EvidenceStatus = "proven"
        else:
            # Coerce a missing/None evidence_refs to the empty list so a capability
            # that simply omits the field classifies as ``unproven`` (definite absence
            # of proof), matching the pydantic ``Capability`` default of ``[]``.
            refs = evidence_refs if isinstance(evidence_refs, list) else []
            # Tier-aware classification for the ENFORCE-gate path: a proven set whose
            # only proof is ``backing_path`` (declared files exist at HEAD, nothing
            # machine-verified) is demoted to ``partial`` so it does NOT clear the
            # gate as strongly as a machine-verified ``named_test``. The badge/display
            # path (annotate_capability_proof_status) keeps the plain classifier.
            proof_status = classify_evidence_tiered(refs)
            # When evidence_refs is empty/missing but the capability declares
            # backing_paths, upgrade to ``partial`` — the claim has structural
            # evidence (it declares where its code lives) even if no formal
            # machine-checked proof has been recorded yet.  This prevents ALL
            # registry capabilities from starting at ``unproven`` and keeps the
            # proof-gate readiness gap honest: only capabilities with ZERO evidence
            # (no refs AND no backing_paths) are truly unproven.
            if proof_status == "unproven" and isinstance(backing_paths, list) and backing_paths:
                proof_status = "partial"
        for name in (
            normalize_capability_name(raw_name),
            *_normalized_absorbed_names(absorbs),
        ):
            if name and name not in out:
                out[name] = proof_status
    return out


def exempt_capability_names(problem_model: object) -> set[str]:
    """Return the canonical names of ``working`` capabilities marked ``proof_exempt``.

    These are capabilities the operator or strategic agent has declared genuinely
    cannot have machine-verifiable proof. They are excluded from the proof-gate
    delta, the release-qualifier unproven count, and the earn-loop targets.

    Alias-aware: same as :func:`working_capability_names`, an exempt entry
    contributes its own canonical name AND every name in its ``absorbs`` list so
    an absorbed old name still resolves as exempt.
    """
    exempt: set[str] = set()
    for (
        raw_name,
        status,
        absorbs,
        _evidence_refs,
        _backing_paths,
        proof_exempt,
    ) in _iter_capability_evidence_entries(problem_model):
        if not proof_exempt:
            continue
        if not (isinstance(status, str) and status.strip().lower() == "working"):
            continue
        normalized = normalize_capability_name(raw_name)
        if normalized:
            exempt.add(normalized)
        exempt.update(_normalized_absorbed_names(absorbs))
    return exempt


def capability_need_proof_status(
    need: Mapping, proof_status_by_name: Mapping[str, EvidenceStatus]
) -> EvidenceStatus | None:
    """Proof status of the ``working`` capability that satisfies ``need``, or ``None``.

    The proof-aware companion to :func:`registry_satisfies_capability_need`,
    sharing its match semantics exactly: a non-capability need, an unnormalizable
    ref, or a ref that names no ``working`` capability all return ``None``
    (no-match). When the ref DOES match a ``working`` capability, this returns
    that capability's :data:`EvidenceStatus` (from ``proof_status_by_name``, built
    by :func:`working_capability_proof_status`) so a consumer can distinguish a
    proof-backed resolution from an unproven "declared working with zero
    evidence" one. Non-``working`` capabilities are absent from the map by
    construction, so they read as no-match — only ``working`` capabilities
    currently satisfy a prerequisite. This is a pure lookup; the observe-mode
    consumers log on the unproven branch without changing resolution behavior.
    """
    if not isinstance(need, Mapping) or need.get("kind") != "capability":
        return None
    normalized = normalize_capability_name(need.get("ref"))
    if normalized is None:
        return None
    return proof_status_by_name.get(normalized)


def capability_proof_gate_mode(override: str | None = None) -> str:
    """Return the current proof gate mode: ``"off"``, ``"observe"``, or ``"enforce"``.

    When ``override`` is provided, validates and returns it (callers inject the
    value from ``runtime_config`` or wherever the canonical source lives).
    When ``override`` is ``None`` (or omitted), returns ``"off"`` — the safest
    default: the gate never blocks.

    ``"off"`` — the proof gate is disabled; unproven working capabilities
    unconditionally satisfy prerequisites (identical to the pre-gate behaviour).

    ``"observe"`` — the proof gate logs warnings for unproven resolutions but
    does NOT change resolution behaviour (useful for evaluating impact before
    flipping to enforce).

    ``"enforce"`` — an unproven/partial/contradicted ``working`` capability does
    NOT satisfy the prerequisite; the dependent stays gated.
    """
    if override is not None:
        return override if override in ("off", "observe", "enforce") else "off"
    return "off"


def capability_need_blocked_by_proof_gate(
    need: Mapping,
    proof_status_by_name: Mapping[str, EvidenceStatus],
    *,
    mode: _GateMode = "off",
) -> bool:
    """True iff ``mode`` is ``"enforce"`` and proof status blocks this capability need.

    The single shared gate predicate both consumers (the spawn-time resolver and
    the blocked-resume sweep) call before their observe-mode warning branch, so
    the "not proven ⇒ blocked when enforcing" decision lives in exactly one
    place. Returns ``True`` only when ALL of the following hold:

    * ``mode`` is ``"enforce"``,
    * ``need`` matches a ``working`` registry capability (via
      :func:`capability_need_proof_status`), and
    * that capability's proof status is anything other than ``"proven"`` — i.e.
      ``unproven`` / ``partial`` / ``unverified`` / ``contradicted`` — mirroring
      the same "not proven" threshold the observe-mode warning uses.

    Returns ``False`` in ``"off"`` or ``"observe"`` mode, for a no-match need
    (not registry-satisfied at all, so normal resolution applies), and for a
    proven capability. In every ``False`` case the caller treats the need exactly
    as observe mode does, so switching to enforce only changes the unproven-match
    path.

    Full-cycle contract: a capability transitions from blocked-under-enforce
    (``True``, unproven) to allowed (``False``, proven) when the project's
    ``evidence_refs`` for that capability gain at least one ``proven=True`` entry
    (recorded by ``capability_proof.record_capability_proof``). The
    blocked-resume sweep's ``_classify_blocker_needs`` re-evaluates against the
    now-proven proof map, routing the need from residual to completed_needs.
    """
    if mode != "enforce":
        return False
    proof_status = capability_need_proof_status(need, proof_status_by_name)
    if proof_status is None:
        return False
    return proof_status != "proven"


def off_registry_capability_refs(
    refs: Iterable[object], registry_names: Iterable[str]
) -> list[str]:
    """Return the capability refs whose normalized form is NOT a registry name.

    The soft, fail-open advisory primitive: a declared ``provides_capability`` /
    ``prerequisite`` capability ref that does not correspond to any
    ``problem_model.capabilities`` entry is flagged here so the strategist can
    fold it into the registry — it is NEVER rejected or dropped, and the ref still
    resolves EXACTLY against any provider that advertises it (matchers stay exact).

    Conservative and permissive: a ref that does not normalize (non-string/blank)
    is skipped (it cannot be judged), input order is preserved, and duplicates are
    collapsed. Refs are returned verbatim (stripped) so the marker names exactly
    what the agent wrote.
    """
    known = {n for n in registry_names if isinstance(n, str)}
    off: list[str] = []
    seen: set[str] = set()
    for ref in refs:
        if not isinstance(ref, str):
            continue
        stripped = ref.strip()
        if not stripped or stripped in seen:
            continue
        normalized = normalize_capability_name(stripped)
        if normalized is None or normalized in known:
            continue
        seen.add(stripped)
        off.append(stripped)
    return off


def derive_provider_marker(task: Task | object) -> str | None:
    """Return the stable namespaced floor marker for ``task``, or ``None``.

    The derived marker is intentionally outside the human capability namespace,
    so it cannot satisfy a real registry capability by accident. Task ids are
    assigned at construction time, making the marker stable across flushes.
    """
    task_id = getattr(task, "id", None)
    if task_id is None:
        return None
    marker = str(task_id).strip().lower()
    if not marker:
        return None
    return f"{_DERIVED_PROVIDER_PREFIX}{marker}"


def compose_capability_markers(task: Task | object) -> set[str]:
    """Return the exact capability markers the matchers should treat as provided.

    Explicit markers remain authoritative and are preserved exactly (modulo the
    existing case-insensitive compare). The derived floor marker is added
    alongside them so every task advertises at least one non-human provider id.
    """
    markers: set[str] = set()
    context = getattr(task, "context", None)
    ctx = context if isinstance(context, Mapping) else {}
    for key in ("provides_capability", "capability"):
        value = ctx.get(key)
        if isinstance(value, str):
            normalized = normalize_capability_name(value)
            if normalized:
                markers.add(normalized)
        elif isinstance(value, (list, tuple, set)):
            for item in value:
                if isinstance(item, str):
                    normalized = normalize_capability_name(item)
                    if normalized:
                        markers.add(normalized)
    derived = derive_provider_marker(task)
    if derived:
        markers.add(derived.lower())
    return markers
