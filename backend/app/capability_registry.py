"""Shared capability vocabulary helpers.

This module centralizes the capability-name primitives shared across the
spawn-time resolver, blocked-need matcher, and future registry-aware planner
surfaces. It is intentionally small and IO-free.
"""

from __future__ import annotations

import os
import re
from collections.abc import Iterable, Mapping
from typing import TYPE_CHECKING

from app.evidence_primitive import classify_evidence

if TYPE_CHECKING:
    from app.evidence_primitive import EvidenceStatus
    from app.models import Task

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
            entry2["proof_status"] = "proven"
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
    """Yield ``(raw_name, status, absorbs, evidence_refs)`` quadruples.

    The evidence-aware sibling of :func:`_iter_capability_entries`: it draws from
    the exact same ``capabilities`` source (accepting either the pydantic
    ``ProblemModel`` instance or a dict-like payload) but additionally surfaces
    each capability's ``evidence_refs`` so the proof-aware resolvers can classify
    the backing evidence WITHOUT widening the shared 3-tuple contract three other
    call sites unpack. ``evidence_refs`` is whatever the payload carries (a list,
    ``None`` on older/dict payloads that omit it, or junk) — callers coerce it
    before handing it to :func:`classify_evidence`.
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
            )
        else:
            yield (
                getattr(cap, "name", None),
                getattr(cap, "status", None),
                getattr(cap, "absorbs", None),
                getattr(cap, "evidence_refs", None),
            )


def working_capability_proof_status(
    problem_model: object,
) -> dict[str, EvidenceStatus]:
    """Map every ``working`` capability's canonical name to its evidence status.

    The proof-aware companion to :func:`working_capability_names`: it selects the
    exact same set of names (only ``working`` capabilities, alias-aware over each
    entry's ``absorbs`` list) but pairs each name with the :data:`EvidenceStatus`
    that :func:`classify_evidence` derives from the capability's
    ``evidence_refs`` — ``proven`` / ``partial`` / ``unproven`` / ``unverified``.
    A ``working`` capability with an empty or missing ``evidence_refs`` classifies
    as ``unproven`` (the "declared working with zero proof" case this gate exists
    to surface); one carrying a ``proven=True`` ref classifies as ``proven``.

    IO-free and additive — the observe-mode wiring in both the spawn-time
    resolver and the blocked-resume sweep builds this map once per project
    (mirroring how they cache :func:`working_capability_names`) and looks needs up
    against it via :func:`capability_need_proof_status`. First-seen wins when two
    entries normalize to the same name, matching the name reader's dedupe order.
    """
    out: dict[str, EvidenceStatus] = {}
    for raw_name, status, absorbs, evidence_refs in _iter_capability_evidence_entries(
        problem_model
    ):
        if not (isinstance(status, str) and status.strip().lower() == "working"):
            continue
        # Coerce a missing/None evidence_refs to the empty list so a capability
        # that simply omits the field classifies as ``unproven`` (definite absence
        # of proof), matching the pydantic ``Capability`` default of ``[]``.
        refs = evidence_refs if isinstance(evidence_refs, list) else []
        proof_status = classify_evidence(refs)
        for name in (
            normalize_capability_name(raw_name),
            *_normalized_absorbed_names(absorbs),
        ):
            if name and name not in out:
                out[name] = proof_status
    return out


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


def capability_proof_enforce_enabled() -> bool:
    """True when proof-gate ENFORCE mode is active for capability resolution.

    Reads the ``CAPABILITY_PROOF_ENFORCE`` env var *at call time* — never cached
    at import — so the gate can be toggled at runtime without a restart. Default
    is observe mode: unset or ``"0"`` → ``False`` (an unproven ``working``
    capability still satisfies a prerequisite; the observe-mode wiring only logs
    a warning). ``"1"`` → ``True`` (enforce: an unproven/partial ``working``
    capability does NOT satisfy the prerequisite, so the dependent stays gated).
    Any other value is treated as observe mode, keeping the fail-safe default of
    "do not re-block runnable work" unless enforcement is explicitly requested.
    """
    return os.getenv("CAPABILITY_PROOF_ENFORCE", "0").strip() == "1"


def capability_need_blocked_by_proof_gate(
    need: Mapping,
    proof_status_by_name: Mapping[str, EvidenceStatus],
    *,
    enforce: bool,
) -> bool:
    """True iff enforce mode should BLOCK this capability need on proof status.

    The single shared gate predicate both consumers (the spawn-time resolver and
    the blocked-resume sweep) call before their observe-mode warning branch, so
    the "not proven ⇒ blocked when enforcing" decision lives in exactly one
    place. Returns ``True`` only when ALL of the following hold:

    * ``enforce`` mode is active,
    * ``need`` matches a ``working`` registry capability (via
      :func:`capability_need_proof_status`), and
    * that capability's proof status is anything other than ``"proven"`` — i.e.
      ``unproven`` / ``partial`` / ``unverified`` / ``contradicted`` — mirroring
      the same "not proven" threshold the observe-mode warning uses.

    Returns ``False`` in observe mode, for a no-match need (not registry-
    satisfied at all, so normal resolution applies), and for a proven capability.
    In every ``False`` case the caller treats the need exactly as observe mode
    does, so enabling enforce only changes the unproven-match path.
    """
    if not enforce:
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
