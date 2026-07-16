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
    from datetime import datetime
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
    for raw_name, _status, _absorbs, evidence_refs, _backing_paths, _proof_exempt, _source in _iter_capability_evidence_entries(
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
       store, grading its entries with the SAME classifier (so a
       ``backing_path``-only ledger reads ``"path_proven"``, not ``"proven"``);
       a capability with no records at all reads ``"unproven"``.
       Backward-compatible: callers that omit ``evidence_refs_map`` see unchanged
       behaviour.

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
            # backing_path-only proof_records return "path_proven" (not "proven"),
            # matching enrich_problem_model_capability_proofs. (The classifier is
            # the same one used on the ev_refs branch above — classify_evidence
            # and classify_evidence_tiered are one function; see finding #8.)
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


#: Platform capabilities fully implemented at runtime.  Registered here so
#: prerequisite refs to these names resolve in the blocked-resume sweep and
#: missing-foundation demand surface without requiring a per-project vocabulary
#: entry in ``problem_model.capabilities``.
PLATFORM_WORKING_CAPABILITIES: frozenset[str] = frozenset(
    {
        "project-custom-tool-registry",
        "qualification-dimension-ledger",
        "multi-project-runner",
    }
)


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


#: The ``proof_kind`` that marks a PLATFORM-RECORDED capability exemption inside
#: ``project.capability_proof_records``.
#:
#: THIS IS THE ONLY THING THAT AUTHORIZES AN EXEMPTION. ``capability_proof_records``
#: is the durable, platform-only ledger: no agent-facing endpoint writes it (agents
#: author ``problem_model``; the ledger is written only by the recorders in
#: :mod:`app.capability_proof` and by an operator/admin path). An exemption record
#: is NOT a machine proof — it is an operator's standing authorization to clear the
#: proof gate WITHOUT one — so it is filtered out of every machine-proof grading
#: pass (:func:`_ledger_machine_proof_entries`) and takes effect solely through the
#: exemption set (:func:`platform_exempt_capability_names`).
CAPABILITY_EXEMPTION_PROOF_KIND = "operator_exemption"


#: Source ``type`` values that represent a deliberate operator or strategic-agent
#: authoring decision.
#:
#: ⚠️ THIS IS NOT AN AUTHORIZATION AND MUST NEVER BE USED AS ONE (audit finding
#: #9). ``source`` lives in ``problem_model.capabilities[].source`` — the same
#: AGENT-WRITABLE JSONB the whole ledger-corroboration pass exists to distrust — so
#: an agent can trivially write ``proof_exempt: true`` plus
#: ``source: {"type": "self_reflection"}`` and forge any provenance in this set.
#: Using an agent-writable field to authorize a bypass of an agent-writable field
#: is not a trust boundary at all.
#:
#: It survives ONLY as a DECLARATION filter on the tier-1 (problem_model-derived)
#: proof map, whose consumers are the earn loop and the metrics sweeps — surfaces
#: that decide what to spend effort proving, never what clears a gate. A capability
#: the strategist declared exempt with an attributed source is skipped by the earn
#: loop (there is nothing to machine-prove) exactly as before. Every ENFORCE seam
#: re-grades the tier-1 map through :func:`ledger_corroborated_proof_status`, which
#: carves out ONLY :func:`platform_exempt_capability_names` — so a declared-but-not-
#: platform-recorded exemption is demoted to ``unverified`` and BLOCKS.
_EXEMPTION_DECLARATION_TYPES: frozenset[str] = frozenset(
    {
        "user_message",       # operator explicitly authored the exemption
        "self_reflection",    # strategic agent authored it during reflection
        "direction_outcome",  # established by a strategic direction's outcome
        "agent_observation",  # strategic agent observation
    }
)


def _exemption_source_type(source: object) -> str | None:
    """Return the non-empty string ``type`` of a capability ``source``, else ``None``.

    Tolerates the pydantic ``Source`` instance, a dict-like payload, and any
    missing / junk value (all fall through to ``None``).
    """
    if isinstance(source, Mapping):
        raw = source.get("type")
    else:
        raw = getattr(source, "type", None)
    return raw if isinstance(raw, str) and raw.strip() else None


def _exemption_is_declared(source: object) -> bool:
    """True iff ``source`` names an attributed authoring channel for a ``proof_exempt``.

    A DECLARATION check, NOT an authorization check — see the warning on
    :data:`_EXEMPTION_DECLARATION_TYPES`. ``source`` is agent-writable, so this can
    be forged; it is used only to keep an unattributed boolean flag from suppressing
    the earn loop on the tier-1 map. No gate consults it: gates consult
    :func:`platform_exempt_capability_names` (the platform-only ledger).
    """
    return _exemption_source_type(source) in _EXEMPTION_DECLARATION_TYPES


def _ledger_entries(proof_records: object, cap_name: object) -> list[dict]:
    """Return the ledger entry dicts recorded for ``cap_name``, or ``[]``.

    Tolerates a missing / non-dict ledger, a missing key, a non-list value, and
    junk entries (all filtered out), so every reader below can assume a clean list.
    """
    if not isinstance(proof_records, dict) or not isinstance(cap_name, str):
        return []
    entries = proof_records.get(cap_name)
    if not isinstance(entries, list):
        return []
    return [e for e in entries if isinstance(e, dict)]


def _entry_is_exemption(entry: Mapping) -> bool:
    """True iff a ledger entry is a platform-recorded operator exemption."""
    kind = entry.get("proof_kind")
    return (
        isinstance(kind, str)
        and kind.strip().lower() == CAPABILITY_EXEMPTION_PROOF_KIND
    )


def _ledger_machine_proof_entries(proof_records: object, cap_name: object) -> list[dict]:
    """Ledger entries for ``cap_name`` EXCLUDING operator-exemption records.

    An exemption record authorizes clearing the gate *without* machine proof; it is
    not itself evidence of anything being verified. Filtering it here keeps the two
    routes strictly separate — a capability is cleared either because the platform
    ledger holds a real machine proof (``corroborated``) or because the operator
    recorded an exemption (``exempt_cleared``), never by laundering the latter into
    the former on a badge, an earn-loop decision, or a corroboration pass.
    """
    return [
        e for e in _ledger_entries(proof_records, cap_name) if not _entry_is_exemption(e)
    ]


def platform_exempt_capability_names(proof_records: object) -> set[str]:
    """Canonical capability names carrying a PLATFORM-RECORDED operator exemption.

    THE EXEMPTION TRUST BOUNDARY, in one place. Reads
    ``project.capability_proof_records`` — the durable, platform-only ledger the
    agent cannot write — and returns the canonical names holding at least one entry
    whose ``proof_kind`` is :data:`CAPABILITY_EXEMPTION_PROOF_KIND`.

    This REPLACES the previous authorization rule, which read the exemption's
    ``source.type`` out of ``problem_model`` (audit finding #9): that field is
    agent-writable, so ``proof_exempt: true`` + ``source: {"type": "self_reflection"}``
    cleared the enforce gate with nothing behind it. An exemption is now authorized
    only by something the agent cannot write.

    A missing / ``None`` / malformed ledger yields the empty set — no ledger means
    no authorized exemptions, which fails CLOSED (the capability must then earn a
    real machine proof to clear any gate).

    Grant an exemption by appending :func:`build_operator_exemption_record` to the
    project's ``capability_proof_records[cap_name]`` list through an operator/admin
    path; nothing an agent can reach writes that column.
    """
    if not isinstance(proof_records, dict):
        return set()
    exempt: set[str] = set()
    for raw_name, entries in proof_records.items():
        normalized = normalize_capability_name(raw_name)
        if not normalized or not isinstance(entries, list):
            continue
        if any(isinstance(e, Mapping) and _entry_is_exemption(e) for e in entries):
            exempt.add(normalized)
    return exempt


def build_operator_exemption_record(
    cap_name: str,
    authorized_by: str,
    reason: str | None = None,
    proved_at: str | None = None,
) -> dict:
    """Build the platform ledger entry that AUTHORIZES a capability's proof exemption.

    The canonical shape an operator/admin path appends to
    ``project.capability_proof_records[normalize_capability_name(cap_name)]`` to
    grant an exemption. It mirrors the entry shape the machine-proof recorders in
    :mod:`app.capability_proof` write (``task_id`` / ``proved_at`` / ``proof_kind`` /
    ``proof_ref`` / ``commit_sha``) so every existing ledger reader tolerates it,
    but carries the distinguishing :data:`CAPABILITY_EXEMPTION_PROOF_KIND` so the
    machine-proof passes skip it (:func:`_ledger_machine_proof_entries`) and only
    the exemption set honors it.

    ``authorized_by`` records WHO granted it (an operator id / email) and ``reason``
    WHY — the audit trail the agent-writable ``source`` field could only pretend to
    be.
    """
    if proved_at is None:
        from app.time_utils import _utcnow

        proved_at = _utcnow().isoformat()
    normalized = normalize_capability_name(cap_name) or str(cap_name)
    return {
        "task_id": f"exemption:{normalized}",
        "proof_kind": CAPABILITY_EXEMPTION_PROOF_KIND,
        "proof_ref": reason or "operator-authorized proof exemption",
        "proved_at": proved_at,
        "commit_sha": "",
        "authorized_by": authorized_by,
    }


def _iter_capability_evidence_entries(problem_model: object):
    """Yield ``(raw_name, status, absorbs, evidence_refs, backing_paths, proof_exempt, source)`` septuples.

    The evidence-aware sibling of :func:`_iter_capability_entries`: it draws from
    the exact same ``capabilities`` source (accepting either the pydantic
    ``ProblemModel`` instance or a dict-like payload) but additionally surfaces
    each capability's ``evidence_refs``, ``backing_paths``, ``proof_exempt``, and
    ``source`` so the proof-aware resolvers can classify the backing evidence and
    apply exemption semantics WITHOUT widening the shared 3-tuple contract three
    other call sites unpack. ``evidence_refs`` / ``backing_paths`` are whatever
    the payload carries (a list, ``None`` on older/dict payloads that omit it, or
    junk) — callers coerce them before handing them to
    :func:`classify_evidence`. ``proof_exempt`` defaults to ``False`` and
    ``source`` to ``None`` for backward-compatibility with payloads that omit
    those fields. ``source`` is the *declared* provenance :func:`_exemption_is_declared`
    reads — an agent-writable field that NO gate trusts (finding #9); the authorizing
    exemption lives in the platform ledger (:func:`platform_exempt_capability_names`).
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
                cap.get("source"),
            )
        else:
            yield (
                getattr(cap, "name", None),
                getattr(cap, "status", None),
                getattr(cap, "absorbs", None),
                getattr(cap, "evidence_refs", None),
                getattr(cap, "backing_paths", None),
                getattr(cap, "proof_exempt", False),
                getattr(cap, "source", None),
            )


def working_capability_proof_status(
    problem_model: object,
    now: "datetime | None" = None,
    proof_records: object = None,
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

    This map feeds the enforce-gate path, so the tiering matters here: a
    capability whose ONLY ``proven=True`` ref is a ``backing_path`` (declared
    files merely exist at HEAD — the same self-report vector the gate exists to
    close) classifies as ``path_proven``, not ``proven``, and therefore does NOT
    clear the enforce gate on its own. A ``backing_path`` proof corroborated by a
    machine-verified ``named_test`` still reads ``proven``.

    NOTE: there is only ONE classifier — :func:`classify_evidence_tiered` is an
    alias of :func:`classify_evidence` (audit finding #8). The tiering above is
    not a property of *which* function is called; every surface, badge/display
    included, grades a given ref set identically. The gate's extra strictness
    comes from what it does with the tier (only ``proven`` clears), not from a
    second classifier.

    When ``evidence_refs`` is empty or missing but the capability declares a
    non-empty ``backing_paths`` list, the status is upgraded to ``partial`` —
    the capability at least declares *where* its code is in the repo, even if no
    machine-checked verification has been recorded yet.  This prevents every
    registry capability from starting at ``unproven`` and distinguishes genuinely
    evidence-less claims from structurally-backed ones that simply haven't been
    through the earning loop.

    **Exemption (audit finding #9 — read this before trusting the map).** THIS IS
    THE TIER-1, AGENT-WRITABLE VIEW. Two different things can mark a capability
    exempt here, and only one of them is an authorization:

    * **PLATFORM-RECORDED (authorized).** ``proof_records`` — the platform-only
      ``project.capability_proof_records`` ledger — holds an operator-exemption
      entry for the capability (:func:`platform_exempt_capability_names`). This is
      the ONLY exemption any enforce gate honors, because it is the only one an
      agent cannot write. It grades ``"proven"`` here regardless of what the
      problem_model says, so an operator's grant cannot be nullified by an agent
      rewriting (or dropping) the ``proof_exempt`` flag.
    * **DECLARED (agent-writable, NOT an authorization).** The capability carries
      ``proof_exempt=True`` plus an attributed ``source`` (:func:`_exemption_is_declared`).
      It also grades ``"proven"`` HERE — but only so the earn loop and the metrics
      sweeps (the tier-1 consumers) do not burn cycles trying to machine-prove a
      capability the strategist says cannot be machine-proven. It CLEARS NO GATE:
      every enforce seam re-grades this map through
      :func:`ledger_corroborated_proof_status`, which demotes any ``proven`` the
      platform ledger neither corroborates nor exempts to ``"unverified"``. A
      forged ``source: {"type": "self_reflection"}`` therefore BLOCKS — that forgery
      is exactly what finding #9 reported.

    A bare ``proof_exempt=True`` with NO cited ``source`` is not honored even at
    tier-1: it falls through to evidence-based classification and blocks
    everywhere. Exemption is for capabilities that genuinely cannot have
    machine-verifiable proof (purely human processes, external integrations without
    test hooks) and is granted by an operator recording
    :func:`build_operator_exemption_record` into the platform ledger.

    **Freshness re-verification**: capability proofs are written once (each
    ``evidence_ref`` stamps a ``recorded_at``) but nothing re-runs them, so a
    proof whose backing test/file was since deleted or materially changed still
    lingers as ``proven``. Because this map feeds the ENFORCE gate, it evaluates
    proof freshness: a ``proven=True`` ref older than
    :data:`~app.evidence_primitive.PROOF_FRESHNESS_TTL` is dropped before tiering,
    so a stale proof demotes to a non-``proven`` tier and no longer clears the
    gate on its own — the standing earn loop then re-verifies it against HEAD and
    re-stamps a fresh proof. ``now`` defaults to the current UTC time (evaluated
    lazily so the module stays import-time side-effect-free); a caller may pass an
    explicit ``now`` for deterministic evaluation. A ref with a missing/malformed
    ``recorded_at`` is treated as indeterminate and never expires, so every legacy
    proof that predates timestamped evidence classifies exactly as before.

    IO-free and additive — the observe-mode wiring in both the spawn-time
    resolver and the blocked-resume sweep builds this map once per project
    (mirroring how they cache :func:`working_capability_names`) and looks needs up
    against it via :func:`capability_need_proof_status`. First-seen wins when two
    entries normalize to the same name, matching the name reader's dedupe order.
    """
    if now is None:
        from app.time_utils import _utcnow

        now = _utcnow()
    ledger_exempt = platform_exempt_capability_names(proof_records)
    out: dict[str, EvidenceStatus] = {}
    for raw_name, status, absorbs, evidence_refs, backing_paths, proof_exempt, source in _iter_capability_evidence_entries(
        problem_model
    ):
        if not (isinstance(status, str) and status.strip().lower() == "working"):
            continue
        names = [
            n
            for n in (
                normalize_capability_name(raw_name),
                *_normalized_absorbed_names(absorbs),
            )
            if n
        ]
        # AUTHORIZED exemption: the platform-only ledger records an operator
        # exemption for this capability (or for a name it absorbed). Honored
        # regardless of the agent-writable ``proof_exempt`` flag — the operator's
        # grant is not the agent's to revoke — and it is the ONLY exemption that
        # survives ledger_corroborated_proof_status into an enforce gate.
        if any(n in ledger_exempt for n in names):
            proof_status: EvidenceStatus = "proven"
        # DECLARED exemption (agent-writable, NOT an authorization — finding #9):
        # graded ``proven`` on this tier-1 map ONLY so the earn loop and metrics
        # sweeps skip a capability the strategist says cannot be machine-proven.
        # Every enforce seam re-grades through ledger_corroborated_proof_status,
        # which does NOT carve this out, so it is demoted to ``unverified`` and
        # BLOCKS. A bare flag with no attributed source is not honored even here.
        elif proof_exempt and _exemption_is_declared(source):
            proof_status = "proven"
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
            proof_status = classify_evidence_tiered(refs, now=now)
            # When evidence_refs is empty/missing but the capability declares
            # backing_paths, upgrade to ``partial`` — the claim has structural
            # evidence (it declares where its code lives) even if no formal
            # machine-checked proof has been recorded yet.  This prevents ALL
            # registry capabilities from starting at ``unproven`` and keeps the
            # proof-gate readiness gap honest: only capabilities with ZERO evidence
            # (no refs AND no backing_paths) are truly unproven.
            if proof_status == "unproven" and isinstance(backing_paths, list) and backing_paths:
                proof_status = "partial"
        for name in names:
            if name not in out:
                out[name] = proof_status
    return out


def exempt_capability_names(
    problem_model: object, proof_records: object = None
) -> set[str]:
    """Canonical names of ``working`` capabilities with an AUTHORIZED proof exemption.

    THE GATE CARVE-OUT SET — the one set of capabilities allowed to clear an
    enforce gate with no machine-verified proof behind them. Membership therefore
    requires an authorization the agent cannot forge:

    1. the capability is declared ``working`` in the registry, AND
    2. the PLATFORM-ONLY ledger (``proof_records`` =
       ``project.capability_proof_records``) holds an operator-exemption record for
       its canonical name — or for a name it absorbed
       (:func:`platform_exempt_capability_names`).

    The agent-writable ``proof_exempt`` flag and its ``source`` are NOT consulted
    (audit finding #9). They used to be: an exemption was honored whenever
    ``source.type`` named an "operator or strategic" channel — a field living in the
    same ``problem_model`` JSONB the ledger-corroboration pass exists to distrust,
    so any agent could write ``proof_exempt: true`` +
    ``source: {"type": "self_reflection"}`` and clear the gate. An agent-writable
    field can never authorize a bypass of an agent-writable field.

    Conversely, an operator's grant is not the agent's to revoke: a ledger-recorded
    exemption holds even if the agent never sets (or later deletes) ``proof_exempt``
    on the problem_model.

    ``proof_records`` defaults to ``None`` = EMPTY ledger = NO exemptions, so a
    caller that does not supply the ledger fails CLOSED. The two enforce seams
    (:func:`consumer_gate_proof_status`) and the release seam
    (:func:`capability_proof_gate_release_status`) always supply the project's
    column.
    """
    ledger_exempt = platform_exempt_capability_names(proof_records)
    if not ledger_exempt:
        return set()
    exempt: set[str] = set()
    for (
        raw_name,
        status,
        absorbs,
        _evidence_refs,
        _backing_paths,
        _proof_exempt,
        _source,
    ) in _iter_capability_evidence_entries(problem_model):
        if not (isinstance(status, str) and status.strip().lower() == "working"):
            continue
        names = [
            n
            for n in (
                normalize_capability_name(raw_name),
                *_normalized_absorbed_names(absorbs),
            )
            if n
        ]
        # Alias-aware, exactly as working_capability_names: an exemption recorded
        # against an absorbed old name still exempts the surviving capability, and
        # the survivor's aliases all resolve as exempt.
        if any(n in ledger_exempt for n in names):
            exempt.update(names)
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


def task_backed_capability_need_blocked_by_proof_gate(
    need: Mapping,
    proof_records: object,
    problem_model: object,
    *,
    mode: _GateMode = "off",
) -> bool:
    """Gate an exact task-backed capability provider on platform proof only."""
    if (
        mode != "enforce"
        or not isinstance(need, Mapping)
        or need.get("kind") != "capability"
    ):
        return False
    name = normalize_capability_name(need.get("ref"))
    if not name:
        return False
    names = _capability_alias_cluster(problem_model, name)
    if names & platform_exempt_capability_names(proof_records):
        return False
    from app.time_utils import _utcnow

    return _ledger_proof_status_for_names(proof_records, names, now=_utcnow()) != "proven"


def _capability_alias_cluster(problem_model: object, name: str) -> set[str]:
    """Return a capability and all canonical/absorbed aliases connected to it."""
    cluster = {name}
    entries: list[set[str]] = []
    for raw_name, _status, absorbs in _iter_capability_entries(problem_model):
        aliases = {
            normalized
            for normalized in (
                normalize_capability_name(raw_name),
                *_normalized_absorbed_names(absorbs),
            )
            if normalized
        }
        if aliases:
            entries.append(aliases)
    changed = True
    while changed:
        changed = False
        for aliases in entries:
            if cluster & aliases and not aliases <= cluster:
                cluster.update(aliases)
                changed = True
    return cluster


def _ledger_proof_status_for_names(
    proof_records: object, names: Iterable[str], *, now: datetime | None = None
) -> EvidenceStatus:
    """Grade machine proofs across one canonical/absorbed namespace."""
    synthetic = [
        {
            "kind": entry.get("proof_kind"),
            "proven": True,
            "recorded_at": entry.get("proved_at"),
        }
        for name in names
        for entry in _ledger_machine_proof_entries(proof_records, name)
    ]
    if not synthetic:
        return "unproven"
    return classify_evidence_tiered(synthetic, now=now)


def _alias_to_survivor_map(problem_model: object) -> dict[str, str]:
    """Build ``{absorbed_name: survivor_name}`` from the problem_model's capabilities.

    Reads each capability's ``absorbs`` list and maps every absorbed canonical
    name to its survivor. Used by the alias-aware ledger corroboration pass so a
    blocked dependent that names an absorbed alias is verified against the
    survivor's platform proof record — not a missing ledger key for the alias
    itself — and so a self-declared ``proven`` claim on a non-absorbed name
    is never accidentally corroborated by a coincidentally-named survivor entry.

    Returns ``{}`` for a missing / empty / malformed model — alias-aware callers
    fall through to direct ledger lookups, preserving the legacy behaviour.
    """
    alias_map: dict[str, str] = {}
    for raw_name, _status, absorbs in _iter_capability_entries(problem_model):
        survivor = normalize_capability_name(raw_name)
        if not survivor:
            continue
        for absorbed in _normalized_absorbed_names(absorbs):
            if absorbed not in alias_map:
                alias_map[absorbed] = survivor
    return alias_map


def _ledger_proof_status(
    proof_records: object, cap_name: str,
    now: datetime | None = None,
) -> EvidenceStatus:
    """Classify a capability's PLATFORM-recorded proof ledger into an EvidenceStatus.

    Reads ``proof_records[cap_name]`` — the durable
    ``project.capability_proof_records`` store written ONLY by the platform
    recorders in :mod:`app.capability_proof` (never by an agent editing the
    JSONB ``problem_model``) — and grades it with the tier-aware classifier.
    A named_test/trial_adoption ledger entry grades ``proven``; a backing_path-only
    ledger grades ``path_proven``; a missing/empty ledger grades ``unproven``.

    This is the trust boundary the enforce gate uses to distinguish
    platform-verified proof from an agent's self-reported ``evidence_refs`` /
    ``proof_exempt`` claim on the problem_model.

    **Freshness.** When ``now`` is supplied, each ledger entry's ``proved_at``
    is carried into the synthetic ref as ``recorded_at`` so ``classify_evidence_tiered``
    drops stale proofs before grading. A ref with a missing/unparseable
    ``proved_at`` is treated as indeterminate and never expires, so every legacy
    ledger entry that predates timestamped proof records classifies as it always
    did.

    Operator-exemption records are EXCLUDED (:func:`_ledger_machine_proof_entries`):
    an exemption authorizes clearing the gate *without* machine proof and must never
    be laundered into machine proof. A capability whose ledger holds only an
    exemption grades ``unproven`` here and is cleared solely through the exemption
    carve-out in :func:`ledger_corroborated_proof_status`, so the ``corroborated`` /
    ``exempt_cleared`` audit buckets stay honest about WHY each capability cleared.
    """
    synthetic = [
        {
            "kind": e.get("proof_kind"),
            "proven": True,
            "recorded_at": e.get("proved_at"),
        }
        for e in _ledger_machine_proof_entries(proof_records, cap_name)
    ]
    if not synthetic:
        return "unproven"
    return classify_evidence_tiered(synthetic, now=now)


def ledger_corroborated_proof_status(
    proof_status_by_name: Mapping[str, EvidenceStatus] | dict,
    proof_records: object,
    exempt_names: Iterable[str] = (),
    now: datetime | None = None,
    alias_map: dict[str, str] | None = None,
) -> dict[str, EvidenceStatus]:
    """Demote every ``proven`` capability the PLATFORM ledger does not corroborate.

    THE TRUST BOUNDARY, in one place. ``working_capability_proof_status`` grades a
    capability from ``problem_model.capabilities[].evidence_refs`` and
    ``proof_exempt`` — both AGENT-WRITABLE JSONB. An agent can therefore write
    ``{"kind": "named_test", "proven": true}`` onto its own capability and read
    back ``proven`` with nothing machine-verified behind it. This pass re-grades
    such a map against ``project.capability_proof_records`` — the durable ledger
    written ONLY by the platform recorders in :mod:`app.capability_proof` — so a
    self-declared proof cannot clear an enforce gate:

    * ``status != "proven"``  → passed through unchanged (already not clearing).
    * name in ``exempt_names`` → passed through unchanged. An exemption is cleared BY
      ITS AUTHORIZATION, not by machine proof: the earn loop deliberately never mints
      a ledger *proof* entry for an exempt capability, so demanding corroboration from
      it would nullify the exemption the operator granted. ``exempt_names`` MUST come
      from :func:`exempt_capability_names` / :func:`platform_exempt_capability_names`,
      i.e. from an operator-exemption record in this same platform-only ledger —
      NEVER from ``problem_model.proof_exempt`` / ``source``, which are agent-writable
      and were the finding-#9 bypass. Anything an agent can write must arrive here as
      a plain ``proven`` claim and take the corroboration path below.
    * ledger grades ``proven`` (:func:`_ledger_proof_status`) → corroborated, kept.
    * otherwise → demoted to ``"unverified"``: the capability CLAIMS proof that no
      platform record backs. ``unverified`` (not ``unproven``) names the situation
      precisely — evidence was asserted, but nothing corroborates it — and, being
      any tier other than ``proven``, it blocks
      :func:`capability_need_blocked_by_proof_gate` exactly as an unproven claim does.

    **Freshness.** ``now`` is threaded to :func:`_ledger_proof_status` so a stale
    ledger proof older than :data:`~app.evidence_primitive.PROOF_FRESHNESS_TTL` no
    longer corroborates a self-declared ``proven`` claim at the enforce gate. A
    missing ``now`` means no freshness evaluation — the stored-flag-only behaviour
    unchanged from the legacy corroboration pass.

    **Alias-aware.** When ``alias_map`` is provided and a name fails direct ledger
    corroboration, the pass checks whether the name was absorbed by a surviving
    capability and retries the survivor's ledger key. This prevents a dependent
    blocked on an absorbed old name from being stranded by a ledger that stores
    proof only under the survivor's canonical name, while a self-declared
    ``proven`` claim on a non-absorbed name is never accidentally corroborated
    by a coincidentally-named ledger entry.

    Shared verbatim by all THREE gate seams (the release qualifier via
    :func:`capability_proof_gate_release_status`, the spawn-time prerequisite
    resolver in :mod:`app.research_completion`, and the blocked-resume sweep in
    :mod:`app.blocked_task_unblock_sweeps`) so the "who cleared this capability"
    rule cannot drift between them — the drift IS the vulnerability (audit finding
    #4: the two consumer seams graded from ``evidence_refs`` alone while the
    release seam already corroborated).

    IO-free; the input map is not mutated.
    """
    if not isinstance(proof_status_by_name, Mapping):
        return {}
    exempt = {n for n in exempt_names if isinstance(n, str)}
    # Reverse alias map: survivor → [absorbed, ...] so a survivor name whose proof
    # lives only under an absorbed alias key in the ledger (pre-rename record) is
    # still corroborated. Built once per call, empty when alias_map is absent.
    reverse_map: dict[str, list[str]] = {}
    if alias_map:
        for absorbed, survivor in alias_map.items():
            reverse_map.setdefault(survivor, []).append(absorbed)
    out: dict[str, EvidenceStatus] = {}
    for name, status in proof_status_by_name.items():
        if (
            status != "proven"
            or name in exempt
            or _ledger_proof_status(proof_records, name, now=now) == "proven"
        ):
            out[name] = status
            continue
        # Forward alias retry: if this name was absorbed by a surviving capability,
        # check the survivor's ledger key instead of the alias's own (which
        # may not exist in the platform ledger).
        if alias_map and name in alias_map:
            survivor = alias_map[name]
            if _ledger_proof_status(proof_records, survivor, now=now) == "proven":
                out[name] = status
                continue
        # Reverse alias retry: if this name IS a survivor, also check its absorbed
        # alias keys — proof recorded before a rename lives under the old name.
        if reverse_map and name in reverse_map and any(
            _ledger_proof_status(proof_records, absorbed, now=now) == "proven"
            for absorbed in reverse_map[name]
        ):
            out[name] = status
            continue
        out[name] = "unverified"
    return out


def fold_ledger_proof_upgrades(
    proof_status_by_name: Mapping[str, EvidenceStatus] | dict,
    proof_records: object,
    now: datetime | None = None,
    alias_map: dict[str, str] | None = None,
) -> dict[str, EvidenceStatus]:
    """Upgrade an ``evidence_refs``-derived proof map with the durable proof ledger.

    ``record_capability_proof`` stamps a landed provider's proof into BOTH the
    capability's ``evidence_refs`` (tier-1, read by
    :func:`working_capability_proof_status`) AND the project-level
    ``capability_proof_records`` column (tier-2). The evidence_refs append is
    fail-open — if the problem_model shape blocks it, the proof survives ONLY in
    the ledger. Without this fold, a genuinely platform-verified capability whose
    tier-1 append was lost would be held BLOCKED by the enforce gate.

    Only capabilities already present in ``proof_status_by_name`` are considered
    (the tier-1 map is authoritative on WHICH capabilities exist and are
    ``working``), and a tier-2 status is applied only when it is strictly stronger
    than the tier-1 status, so a ledger proof never REGRESSES a capability the
    evidence_refs classify more highly.

    **Alias-aware.** When ``alias_map`` is provided, an absorbed alias name whose
    own ledger key has no proof entries is retried against the survivor's ledger
    key. This keeps the fold pass in sync with the alias-aware corroboration pass
    in :func:`ledger_corroborated_proof_status`, so a ledger-only proof (tier-1
    append was lost) on the survivor also upgrades the alias — preventing a
    dependent blocked on the old name from staying gated.

    Freshness parity with tier-1: each record's ``proved_at`` is carried into the
    synthetic ref as ``recorded_at`` and ``now`` is threaded into the tiered
    classifier, so a ledger record older than
    :data:`~app.evidence_primitive.PROOF_FRESHNESS_TTL` can no longer resurrect an
    expired capability at the gate. ``now`` defaults to the current UTC time.

    The mirror image of :func:`ledger_corroborated_proof_status`: this pass lets
    the platform ledger RAISE a claim, that one lets it REFUSE one. Both consumer
    seams run them in that order (see :func:`consumer_gate_proof_status`).

    IO-free; the input map is not mutated.
    """
    if not isinstance(proof_status_by_name, Mapping):
        return {}
    out: dict[str, EvidenceStatus] = dict(proof_status_by_name)
    if not isinstance(proof_records, dict) or not out:
        return out
    from app.evidence_primitive import EVIDENCE_STATUS_RANK, evidence_status_rank

    if now is None:
        from app.time_utils import _utcnow

        now = _utcnow()

    for name, tier1 in list(out.items()):
        if evidence_status_rank(tier1) >= EVIDENCE_STATUS_RANK["proven"]:
            continue
        # Operator-exemption records are not machine proof and must not upgrade a
        # capability here (an exemption clears only via the exemption carve-out).
        # Determine the ledger key to check: for an absorbed alias, use the
        # survivor's ledger key so a ledger-only proof on the survivor also
        # upgrades the alias.
        ledger_key = alias_map.get(name, name) if alias_map else name
        synthetic = [
            {
                "kind": e.get("proof_kind"),
                "proven": True,
                "recorded_at": e.get("proved_at"),
            }
            for e in _ledger_machine_proof_entries(proof_records, ledger_key)
        ]
        if not synthetic:
            continue
        tier2 = classify_evidence_tiered(synthetic, now=now)
        if evidence_status_rank(tier2) > evidence_status_rank(tier1):
            out[name] = tier2
    return out


def consumer_gate_proof_status(
    problem_model: object,
    proof_records: object,
    now: datetime | None = None,
) -> dict[str, EvidenceStatus]:
    """The proof map BOTH consumer enforce-gate seams grade a capability need against.

    One function so the spawn-time prerequisite resolver
    (:mod:`app.research_completion`) and the blocked-resume sweep
    (:mod:`app.blocked_task_unblock_sweeps`) cannot disagree about whether a
    capability is proven — and so neither can drift away from the release seam,
    which applies the same corroboration rule
    (:func:`capability_proof_gate_release_status`).

    Three composed steps:

    1. :func:`working_capability_proof_status` — the tier-1, problem_model-derived
       (AGENT-WRITABLE) grade, with evidence-freshness already applied. The platform
       ledger is threaded in so an operator-recorded exemption grades ``proven`` here
       even when the agent never wrote ``proof_exempt``.
    2. :func:`fold_ledger_proof_upgrades` — the platform ledger RAISES a capability
       whose tier-1 ``evidence_refs`` append was lost to the fail-open path.
    3. :func:`ledger_corroborated_proof_status` — the platform ledger REFUSES a
       ``proven`` claim it does not corroborate, demoting a self-declared proof to
       ``unverified`` so it cannot clear the enforce gate. Only PLATFORM-RECORDED
       exemptions are carved out (:func:`exempt_capability_names`) — they are cleared
       by authorization, never by a ledger proof entry, which the earn loop never
       mints for them.

    Net contract — a capability need clears the enforce gate at a consumer seam iff
    the capability is ``working`` AND (the platform ledger holds an operator-recorded
    EXEMPTION for it OR the platform ledger holds a machine-verified PROOF for it).
    Nothing the agent writes to ``problem_model`` — a ``proven=true`` ``evidence_ref``,
    a ``proof_exempt`` flag, a fabricated ``source.type`` (finding #9) — clears it.

    ``proof_records`` is the project's ``capability_proof_records`` column; ``None``
    / a missing column is treated as an EMPTY ledger (nothing corroborates), which
    fails CLOSED. This deliberately differs from
    :func:`capability_proof_gate_release_status`, where ``proof_records=None`` means
    "the caller did not supply the ledger" and preserves the legacy
    problem_model-only derivation; here the caller ALWAYS supplies the project's
    column, so ``None`` genuinely means "this project has no recorded proofs".
    """
    if now is None:
        from app.time_utils import _utcnow

        now = _utcnow()
    proof_map = working_capability_proof_status(
        problem_model, now=now, proof_records=proof_records
    )
    alias_map = _alias_to_survivor_map(problem_model)
    proof_map = fold_ledger_proof_upgrades(proof_map, proof_records, now=now, alias_map=alias_map)
    return ledger_corroborated_proof_status(
        proof_map,
        proof_records,
        exempt_capability_names(problem_model, proof_records),
        now=now,
        alias_map=alias_map,
    )


def capability_proof_gate_release_status(
    problem_model: object,
    mode: str | None,
    proof_records: object = None,
    now: datetime | None = None,
) -> tuple[bool, dict]:
    """Derive the release-qualifier capability-proof dimension: ``(blocked, detail)``.

    This is the release-qualification enforcement point of the capability proof
    gate — the third seam alongside the spawn-time resolver and the
    blocked-resume sweep. Extracted here as a single IO-free primitive so the
    qualifier's promotion decision derives from the SAME tiered proof map
    (:func:`working_capability_proof_status`) and exemption set
    (:func:`exempt_capability_names`) the other two seams use, rather than
    re-implementing the "unproven working capability blocks promotion" rule in
    the 5k-line qualifier where it can silently drift.

    Behaviour:

    * Returns ``blocked=False`` in ``"off"`` / ``"observe"`` mode (and for any
      unrecognized mode) — the dimension is recorded but never blocks promotion.
    * In ``"enforce"`` mode with a truthy ``problem_model``, ``blocked`` is
      ``True`` iff at least one ``working`` capability is not cleared.
    * In ``"enforce"`` mode with no ``problem_model``, returns
      ``blocked=False`` with ``detail["error"] == "no problem_model"``.

    **Platform corroboration (audit finding #1).** The ``problem_model`` fields
    the base proof map reads — a capability's ``evidence_refs`` (a ``proven=True``
    entry) and its ``proof_exempt`` flag — are AGENT-WRITABLE: an agent editing
    the JSONB problem_model can self-declare a capability proven or exempt and
    clear the enforce gate without any platform verification. To close that hole,
    the enforce gate requires that every capability the base map treats as cleared
    be CORROBORATED by a machine-verified entry in ``proof_records`` (the durable,
    platform-only ``project.capability_proof_records`` ledger —
    :func:`_ledger_proof_status` grades it ``"proven"``). A capability that reads
    ``proven`` from a self-reported ``evidence_ref`` but carries no corroborating
    platform proof is NOT cleared: it is added to ``unproven`` (and surfaced under
    ``uncorroborated``) so promotion blocks.

    ``proof_records=None`` means an EMPTY ledger — nothing corroborates, so nothing
    self-declared clears — matching :func:`consumer_gate_proof_status`. It does NOT
    mean "skip corroboration". It used to (the "legacy problem_model-only
    derivation"), and that was a hole big enough to swallow the whole gate: the
    column is nullable with no server default (migration 0208), so a project that
    has never earned a proof reads ``None`` — precisely the project whose agent has
    the most to gain from self-declaring one. The two consumer seams already treated
    ``None`` as an empty ledger; the release seam now agrees, and the drift IS the
    vulnerability.

    **Authorized exemptions bypass the machine-proof requirement (findings #2 + #9).**
    A capability the OPERATOR exempted — an ``operator_exemption`` record in the
    platform-only ledger (:func:`exempt_capability_names`) — is cleared BY THAT
    AUTHORIZATION, not by machine proof. The earn loop never mints a *proof* entry
    for an exempt capability BY DESIGN, so demanding one from it would nullify the
    exemption the operator granted (finding #2). Those capabilities are EXCLUDED
    from the corroboration requirement and reported under ``exempt_cleared``.

    This does not reopen the self-report hole (finding #9): the exemption must live
    in the platform ledger, which no agent-facing endpoint writes. A ``proof_exempt``
    flag on the problem_model — bare, or dressed in a fabricated
    ``source: {"type": "self_reflection"}`` — is NOT an exemption here. It may grade
    ``proven`` on the agent-writable base map, but it is not in ``exempt_set``, so
    the corroboration pass demands a ledger proof it does not have, demotes it, and
    it lands in ``uncorroborated`` → ``unproven`` → promotion BLOCKS.

    ``detail`` always carries ``mode`` and, when the enforce branch runs against
    a problem model, the ``total_working`` / ``unproven_count`` / ``unproven`` /
    ``exempt_count`` / ``exempt_names`` audit fields plus ``corroborated`` /
    ``uncorroborated`` / ``exempt_cleared`` — the last three answering, for every
    cleared capability, WHY it cleared and on whose authority.
    """
    mode_norm = str(mode or "off").strip().lower()
    detail: dict = {"mode": mode_norm}
    if mode_norm != "enforce":
        return False, detail
    if not problem_model:
        detail["error"] = "no problem_model"
        return False, detail
    if now is None:
        from app.time_utils import _utcnow

        now = _utcnow()
    proof_map = working_capability_proof_status(
        problem_model, now=now, proof_records=proof_records
    )
    # The carve-out set: PLATFORM-recorded operator exemptions only (finding #9).
    # An agent-declared proof_exempt/source never lands here, so it cannot skip the
    # corroboration pass below.
    exempt_set = exempt_capability_names(problem_model, proof_records)
    # Base (problem_model-derived) unproven set: a capability the agent-authored view
    # already reports as not-proven.
    unproven = [name for name, status in proof_map.items() if status != "proven"]

    # Corroboration pass (finding #1): every capability the base map treats as
    # cleared ("proven") must be backed by a machine-verified PLATFORM ledger
    # entry — not just a self-reported evidence_ref / proof_exempt flag on the
    # problem_model. Uncorroborated "proven" caps are demoted to blocked. Runs
    # UNCONDITIONALLY: proof_records=None is an empty ledger (nothing corroborates),
    # never a licence to trust the problem_model on its own.
    #
    # EXCEPT a platform-recorded exemption (finding #2): an operator-granted
    # exemption clears BY ITS AUTHORIZATION, not by machine proof, and the earn loop
    # deliberately never mints a ledger proof entry for an exempt capability.
    # Requiring one would demote every exemption to unproven and block promotion —
    # nullifying the grant. Such a capability is skipped here (neither corroborated
    # nor uncorroborated; it is audited under ``exempt_cleared`` / ``exempt_names``).
    #
    # The corroboration rule itself lives in ledger_corroborated_proof_status, which
    # the two CONSUMER seams also call (via consumer_gate_proof_status) — one
    # implementation, so this seam and they cannot drift apart. A "proven" capability
    # the ledger refuses comes back demoted; that demotion IS the uncorroborated set.
    alias_map = _alias_to_survivor_map(problem_model)
    corroborated_map = ledger_corroborated_proof_status(
        proof_map, proof_records, exempt_set, now=now, alias_map=alias_map
    )
    uncorroborated: list[str] = [
        name
        for name, status in proof_map.items()
        if status == "proven" and corroborated_map.get(name) != "proven"
    ]
    if uncorroborated:
        unproven = sorted(set(unproven) | set(uncorroborated))
    uncorroborated_set = set(uncorroborated)
    corroborated = [
        name
        for name, status in proof_map.items()
        if status == "proven"
        and name not in uncorroborated_set
        and name not in exempt_set
    ]
    detail["corroborated"] = sorted(corroborated)
    detail["uncorroborated"] = sorted(uncorroborated)
    # Cleared by authorization rather than by ledger proof — surfaced separately so
    # an auditor can see exactly which capabilities bypassed the machine-proof
    # requirement and on whose authority.
    detail["exempt_cleared"] = sorted(
        name
        for name, status in proof_map.items()
        if status == "proven" and name in exempt_set
    )

    detail["total_working"] = len(proof_map)
    detail["unproven_count"] = len(unproven)
    detail["unproven"] = unproven
    detail["exempt_count"] = len(exempt_set)
    detail["exempt_names"] = sorted(exempt_set)
    return bool(unproven), detail


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
