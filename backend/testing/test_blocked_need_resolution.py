"""Unit tests for the pure cross-task NEED resolver.

These exercise ``blocked_need_resolution.resolve_blocked_need`` as a pure matcher
over in-memory ``Task`` objects (no DB / no session) — the sweep integration is
covered separately in ``test_reaper.py``.
"""

import asyncio
import uuid
from datetime import datetime, timedelta

from app.blocked_need_resolution import (
    completed_foundation_for_need,
    need_is_structurally_unobservable,
    need_matched_dead_foundation,
    need_satisfied_by_completed,
    resolve_blocked_need,
)
from app.capability_registry import derive_provider_marker, registry_capability_names
from app.models import Task, TaskStatus
from app.problem_model import Capability, ProblemModel


def _task(*, status=TaskStatus.IN_PROGRESS, title="cand", payloads=None,
          context=None, depends_on=None, tid=None, parent_task_id=None,
          created_at=None) -> Task:
    return Task(
        id=tid or uuid.uuid4(),
        project_id=uuid.uuid4(),
        title=title,
        description="x",
        status=status,
        created_by="u",
        requires_approval=False,
        payloads=payloads if payloads is not None else [{"type": "create"}],
        context=context or {},
        depends_on=depends_on or [],
        parent_task_id=parent_task_id,
        created_at=created_at,
        status_log=[],
    )


def _shipped(**kwargs) -> Task:
    """A COMPLETED foundation that carries the merge-unblock signal (shipped)."""
    ctx = {"pr_merged": True, **(kwargs.pop("context", None) or {})}
    return _task(status=TaskStatus.COMPLETED, context=ctx, **kwargs)


# ── kind='task' ──────────────────────────────────────────────────────────────

def test_task_need_resolves_by_id():
    foundation = _task()
    blocked = _task(status=TaskStatus.BLOCKED)
    need = {"kind": "task", "ref": str(foundation.id)}
    assert resolve_blocked_need(blocked, need, [foundation]) is foundation


def test_task_need_no_matching_id_returns_none():
    foundation = _task()
    blocked = _task(status=TaskStatus.BLOCKED)
    need = {"kind": "task", "ref": str(uuid.uuid4())}
    assert resolve_blocked_need(blocked, need, [foundation]) is None


def test_task_need_resolves_by_finding_hash():
    # A non-UUID finding-hash ref resolves onto the candidate that stamps it —
    # this is the canonical signal the producer records for sibling prereqs.
    foundation = _task(context={"finding_hash": "deadbeefcafe0001"})
    blocked = _task(status=TaskStatus.BLOCKED)
    need = {"kind": "task", "ref": "deadbeefcafe0001"}
    assert resolve_blocked_need(blocked, need, [foundation]) is foundation


def test_task_need_resolves_uppercase_finding_hash_and_uuid():
    foundation = _task(context={"finding_hash": "deadbeefcafe0001"})
    blocked = _task(status=TaskStatus.BLOCKED)
    need = {"kind": "task", "ref": "DEADBEEFCAFE0001"}
    assert resolve_blocked_need(blocked, need, [foundation]) is foundation

    uuid_foundation = _task()
    uuid_need = {"kind": "task", "ref": str(uuid_foundation.id).upper()}
    assert resolve_blocked_need(blocked, uuid_need, [uuid_foundation]) is uuid_foundation


def test_task_need_unmatched_ref_returns_none():
    # A title-like ref ("not-a-known-ref") is not an exact task ref (UUID /
    # finding hash), so the resolver short-circuits to None under the shared
    # exact-task-ref policy — never a fuzzy match against the candidate's title.
    blocked = _task(status=TaskStatus.BLOCKED)
    need = {"kind": "task", "ref": "not-a-known-ref"}
    assert resolve_blocked_need(blocked, need, [_task()]) is None


def test_task_need_title_like_ref_never_matches_titled_candidate():
    # Even when a candidate's title equals the title-like task ref, a kind=task
    # need must NOT fuzzy-match it: task resolution is by id / finding hash only.
    candidate = _task(title="Build the auth subsystem")
    blocked = _task(status=TaskStatus.BLOCKED)
    need = {"kind": "task", "ref": "Build the auth subsystem"}
    assert resolve_blocked_need(blocked, need, [candidate]) is None


# ── lineage-aware resume matching (identity drift / retitled-split provider) ──

def test_task_need_resolves_to_retitled_provider_via_lineage():
    """Audit case 2: a need keyed by the ORIGINAL finding hash resolves onto a
    retitled / split provider whose recomputed ``finding_hash`` has drifted,
    because the provider records the original hash in its ``finding_lineage``.
    Hash-only matching could never bind the renamed provider — lineage does."""
    original = "aaaabbbbccccdddd"
    provider = _task(
        title="feat: agent before/after viewport screenshot pilot (Phase 0)",
        context={"finding_hash": "1111222233334444", "finding_lineage": [original]},
    )
    blocked = _task(status=TaskStatus.BLOCKED)
    need = {"kind": "task", "ref": original}
    # Sanity: the provider's recomputed hash is NOT the need ref (genuine drift).
    assert provider.context["finding_hash"] != original
    assert resolve_blocked_need(blocked, need, [provider]) is provider


def test_completed_retitled_provider_satisfies_need_via_lineage():
    """The completed / landed resume path is lineage-aware too: a shipped,
    retitled provider satisfies a need keyed by the ORIGINAL finding hash."""
    original = "feeddeadbeef0001"
    provider = _shipped(
        title="feat: agent before/after viewport screenshot capture loop",
        context={"finding_hash": "0000111122223333", "finding_lineage": [original]},
    )
    blocked = _task(status=TaskStatus.BLOCKED)
    need = {"kind": "task", "ref": original}
    assert need_satisfied_by_completed(blocked, need, [provider]) is True
    assert completed_foundation_for_need(blocked, need, [provider]) is provider


def test_task_need_lineage_degrades_to_exact_hash_when_absent():
    """Graceful degradation: a provider carrying NO ``finding_lineage`` (older
    task) still resolves by its exact ``finding_hash``, and a need by a hash it
    neither stamps nor lists in lineage still misses."""
    provider = _task(context={"finding_hash": "abcabcabcabc0001"})
    blocked = _task(status=TaskStatus.BLOCKED)
    assert resolve_blocked_need(
        blocked, {"kind": "task", "ref": "abcabcabcabc0001"}, [provider]
    ) is provider
    assert resolve_blocked_need(
        blocked, {"kind": "task", "ref": "abcabcabcabc9999"}, [provider]
    ) is None


# ── shared exact-task-ref policy ─────────────────────────────────────────────

def test_is_exact_task_ref_accepts_uuid_and_finding_hash():
    from app.task_blocked import is_exact_task_ref

    assert is_exact_task_ref(str(uuid.uuid4())) is True
    assert is_exact_task_ref("deadbeefcafe0001") is True  # 16-char finding hash
    assert is_exact_task_ref("DEADBEEFCAFE0001") is True  # case-insensitive hex


def test_is_exact_task_ref_rejects_title_like_and_malformed():
    from app.task_blocked import is_exact_task_ref

    assert is_exact_task_ref("Build the auth subsystem") is False
    assert is_exact_task_ref("auth-subsystem") is False
    assert is_exact_task_ref("abc123") is False  # too short
    assert is_exact_task_ref("deadbeefcafe000") is False  # 15 chars, not a hash
    assert is_exact_task_ref("") is False
    assert is_exact_task_ref(None) is False
    assert is_exact_task_ref(42) is False


def test_is_foundation_demand_need_policy():
    from app.task_blocked import is_foundation_demand_need

    # Capability needs are foundation demand regardless of ref shape.
    assert is_foundation_demand_need({"kind": "capability", "ref": "payment-core"}) is True
    # Exact task refs are foundation demand; title-like task refs are NOT.
    assert is_foundation_demand_need({"kind": "task", "ref": str(uuid.uuid4())}) is True
    assert is_foundation_demand_need({"kind": "task", "ref": "deadbeefcafe0001"}) is True
    assert is_foundation_demand_need({"kind": "task", "ref": "Build the harness"}) is False
    # pr needs and malformed input are never foundation demand.
    assert is_foundation_demand_need({"kind": "pr", "ref": "42"}) is False
    assert is_foundation_demand_need({"kind": "capability", "ref": ""}) is False
    assert is_foundation_demand_need(None) is False


# ── kind='pr' ────────────────────────────────────────────────────────────────

def test_pr_need_resolves_to_owning_task():
    foundation = _task(payloads=[{"type": "fix", "pr_number": 7919}])
    blocked = _task(status=TaskStatus.BLOCKED)
    need = {"kind": "pr", "ref": "7919"}
    assert resolve_blocked_need(blocked, need, [foundation]) is foundation


def test_pr_need_handles_hash_prefix():
    foundation = _task(payloads=[{"type": "fix", "pr_number": 42}])
    blocked = _task(status=TaskStatus.BLOCKED)
    need = {"kind": "pr", "ref": "#42"}
    assert resolve_blocked_need(blocked, need, [foundation]) is foundation


def test_pr_need_no_match_returns_none():
    foundation = _task(payloads=[{"type": "fix", "pr_number": 1}])
    blocked = _task(status=TaskStatus.BLOCKED)
    need = {"kind": "pr", "ref": "999"}
    assert resolve_blocked_need(blocked, need, [foundation]) is None


def test_pr_need_ambiguous_resolves_to_canonical():
    # Two candidates own PR #7 (genuine duplicate foundations). The resume-time
    # matcher deterministically picks the canonical (earliest-created) one
    # rather than refusing to resolve.
    base = datetime(2026, 5, 1, 0, 0, 0)
    earliest = _task(
        payloads=[{"type": "fix", "pr_number": 7}], created_at=base
    )
    later = _task(
        payloads=[{"type": "create", "pr_number": 7}],
        created_at=base + timedelta(hours=1),
    )
    blocked = _task(status=TaskStatus.BLOCKED)
    need = {"kind": "pr", "ref": "7"}
    assert resolve_blocked_need(blocked, need, [later, earliest]) is earliest
    assert resolve_blocked_need(blocked, need, [earliest, later]) is earliest


# ── kind='capability' ────────────────────────────────────────────────────────

def test_capability_resolves_on_exact_payload_type():
    foundation = _task(payloads=[{"type": "design"}])
    blocked = _task(status=TaskStatus.BLOCKED)
    need = {"kind": "capability", "ref": "design"}
    assert resolve_blocked_need(blocked, need, [foundation]) is foundation


def test_capability_resolves_on_exact_title():
    foundation = _task(title="Build the auth foundation")
    blocked = _task(status=TaskStatus.BLOCKED)
    need = {"kind": "capability", "ref": "build the auth foundation"}
    assert resolve_blocked_need(blocked, need, [foundation]) is foundation


def test_capability_resolves_on_exact_related_file():
    foundation = _task(context={"related_files": ["backend/app/auth.py"]})
    blocked = _task(status=TaskStatus.BLOCKED)
    need = {"kind": "capability", "ref": "backend/app/auth.py"}
    assert resolve_blocked_need(blocked, need, [foundation]) is foundation


def test_capability_no_conservative_match_returns_none():
    # A fuzzy/partial description must NOT match — only exact signals do.
    foundation = _task(title="Build the auth foundation",
                       context={"related_files": ["backend/app/auth.py"]})
    blocked = _task(status=TaskStatus.BLOCKED)
    need = {"kind": "capability", "ref": "auth"}
    assert resolve_blocked_need(blocked, need, [foundation]) is None


def test_capability_resolves_on_advertised_marker():
    # The producer-stamped explicit capability marker resolves, even when no
    # payload-type / title / related-file signal would.
    foundation = _task(
        title="unrelated title",
        context={"provides_capability": "auth-tokens"},
    )
    blocked = _task(status=TaskStatus.BLOCKED)
    need = {"kind": "capability", "ref": "auth-tokens"}
    assert resolve_blocked_need(blocked, need, [foundation]) is foundation


def test_capability_resolves_on_marker_in_list():
    foundation = _task(context={"capability": ["billing", "Auth-Tokens"]})
    blocked = _task(status=TaskStatus.BLOCKED)
    need = {"kind": "capability", "ref": "auth-tokens"}  # case-insensitive
    assert resolve_blocked_need(blocked, need, [foundation]) is foundation


def test_floor_stamped_provider_resolves_without_false_match():
    foundation = _task(
        title="Build seeded evaluation harness",
        payloads=[{"type": "plan"}],
    )
    blocked = _task(status=TaskStatus.BLOCKED)
    floor_marker = derive_provider_marker(foundation)

    assert floor_marker is not None
    assert resolve_blocked_need(
        blocked, {"kind": "capability", "ref": floor_marker}, [foundation]
    ) is foundation
    assert resolve_blocked_need(
        blocked,
        {"kind": "capability", "ref": "seeded-evaluation-harness"},
        [foundation],
    ) is None


def test_registry_capability_names_canonicalize_problem_model_names():
    model = ProblemModel(
        capabilities=[
            Capability(name="Seeded evaluation harness", status="working"),
            Capability(name="seeded-evaluation-harness", status="planned"),
            Capability(name=" PR Promotion ", status="partial"),
        ]
    )

    assert registry_capability_names(model) == [
        "seeded-evaluation-harness",
        "pr-promotion",
    ]


def test_working_capability_names_only_working_status():
    from app.capability_registry import working_capability_names

    model = ProblemModel(
        capabilities=[
            Capability(name="Seeded evaluation harness", status="working"),
            Capability(name=" PR Promotion ", status="partial"),
            Capability(name="future-thing", status="planned"),
        ]
    )

    assert working_capability_names(model) == {"seeded-evaluation-harness"}
    assert working_capability_names(None) == set()


def test_registry_satisfies_capability_need_matches_working_only():
    from app.capability_registry import registry_satisfies_capability_need

    working = {"seeded-evaluation-harness"}
    # Refs normalize before matching, so the agent's raw phrasing still matches.
    assert registry_satisfies_capability_need(
        {"kind": "capability", "ref": "Seeded Evaluation Harness"}, working
    )
    assert registry_satisfies_capability_need(
        {"kind": "capability", "ref": "seeded-evaluation-harness"}, working
    )
    # Non-working capability, task-kind need, and junk refs never match.
    assert not registry_satisfies_capability_need(
        {"kind": "capability", "ref": "pr-promotion"}, working
    )
    assert not registry_satisfies_capability_need(
        {"kind": "task", "ref": "seeded-evaluation-harness"}, working
    )
    assert not registry_satisfies_capability_need(
        {"kind": "capability", "ref": "   "}, working
    )


# ---------------------------------------------------------------------------
# Proof-aware capability resolver (observe-mode wiring, non-gating)
# ---------------------------------------------------------------------------


def _proven_ref() -> dict:
    """One machine-checked evidence ref (``proven=True``)."""
    return {"kind": "named_test", "ref": "testing/test_x.py::test_x", "proven": True}


def test_working_capability_proof_status_maps_names_to_status():
    from app.capability_registry import working_capability_proof_status

    model = ProblemModel(
        capabilities=[
            Capability(
                name="Proven cap", status="working", evidence_refs=[_proven_ref()]
            ),
            Capability(name="Unproven cap", status="working"),
            Capability(
                name="Partial cap",
                status="working",
                evidence_refs=[{"kind": "named_test", "ref": "t", "proven": False}],
            ),
            # Non-working capabilities are excluded from the map entirely.
            Capability(name="Planned cap", status="planned", evidence_refs=[_proven_ref()]),
        ]
    )

    assert working_capability_proof_status(model) == {
        "proven-cap": "proven",
        "unproven-cap": "unproven",
        "partial-cap": "partial",
    }
    assert working_capability_proof_status(None) == {}


def test_proof_aware_resolver_returns_proven_for_proven_capability():
    from app.capability_registry import (
        capability_need_proof_status,
        working_capability_proof_status,
    )

    model = ProblemModel(
        capabilities=[
            Capability(
                name="Seeded evaluation harness",
                status="working",
                evidence_refs=[_proven_ref()],
            )
        ]
    )
    proof = working_capability_proof_status(model)

    # The agent's raw phrasing normalizes before matching, exactly like the
    # boolean registry-satisfies predicate.
    assert (
        capability_need_proof_status(
            {"kind": "capability", "ref": "Seeded Evaluation Harness"}, proof
        )
        == "proven"
    )


def test_proof_aware_resolver_returns_unproven_for_unproven_capability():
    """A capability declared 'working' with zero evidence resolves as ``unproven``.

    This is the exact seam the observe-mode gate exists to surface: a self-declared
    working capability with no ``evidence_refs`` satisfies a prerequisite today, and
    the proof-aware resolver now names that resolution ``unproven`` (distinct from a
    proof-backed ``proven`` one) without yet changing whether the need resolves.
    """
    from app.capability_registry import (
        capability_need_proof_status,
        registry_satisfies_capability_need,
        working_capability_names,
        working_capability_proof_status,
    )

    model = ProblemModel(
        capabilities=[
            # 'working' but with an empty evidence_refs list → zero proof.
            Capability(name="Seeded evaluation harness", status="working"),
        ]
    )
    proof = working_capability_proof_status(model)
    need = {"kind": "capability", "ref": "seeded-evaluation-harness"}

    # The proof-aware resolver classifies the resolution as unproven ...
    assert capability_need_proof_status(need, proof) == "unproven"
    # ... while observe mode leaves the boolean resolution unchanged: the unproven
    # working capability STILL satisfies the need (non-gating).
    assert registry_satisfies_capability_need(
        need, working_capability_names(model)
    )
    # The map keys are exactly the working-capability name set the boolean check
    # consumes — the invariant the sweep relies on to fetch once and serve both.
    assert set(proof) == working_capability_names(model)


def test_proof_aware_resolver_no_match_cases_return_none():
    from app.capability_registry import capability_need_proof_status

    proof = {"seeded-evaluation-harness": "unproven"}
    # Non-capability need kind never matches.
    assert (
        capability_need_proof_status(
            {"kind": "task", "ref": "seeded-evaluation-harness"}, proof
        )
        is None
    )
    # Capability ref that names no working capability (absent from the map).
    assert (
        capability_need_proof_status(
            {"kind": "capability", "ref": "pr-promotion"}, proof
        )
        is None
    )
    # Blank / unnormalizable ref.
    assert (
        capability_need_proof_status({"kind": "capability", "ref": "   "}, proof)
        is None
    )
    # Non-mapping need.
    assert capability_need_proof_status(None, proof) is None


def test_proof_aware_resolver_alias_absorbs_returns_survivor_status():
    """An absorbed old capability name resolves to the surviving cap's proof status."""
    from app.capability_registry import (
        capability_need_proof_status,
        working_capability_proof_status,
    )

    model = ProblemModel(
        capabilities=[
            Capability(
                name="Unified harness",
                status="working",
                absorbs=["Old harness name"],
                evidence_refs=[_proven_ref()],
            )
        ]
    )
    proof = working_capability_proof_status(model)

    # Both the survivor name and the absorbed alias carry the survivor's status.
    assert (
        capability_need_proof_status(
            {"kind": "capability", "ref": "unified-harness"}, proof
        )
        == "proven"
    )
    assert (
        capability_need_proof_status(
            {"kind": "capability", "ref": "old-harness-name"}, proof
        )
        == "proven"
    )


def test_working_capability_proof_status_accepts_dict_form_problem_model():
    """Dict-form payloads classify identically; a missing evidence_refs → unproven."""
    from app.capability_registry import working_capability_proof_status

    payload = {
        "capabilities": [
            {"name": "Dict cap", "status": "working"},  # evidence_refs key absent
            {
                "name": "Dict proven",
                "status": "working",
                "evidence_refs": [_proven_ref()],
            },
        ]
    }

    assert working_capability_proof_status(payload) == {
        "dict-cap": "unproven",
        "dict-proven": "proven",
    }


def test_working_capability_proof_status_backing_paths_are_partial():
    """A working capability with backing_paths but no evidence_refs → partial.

    The presence of backing_paths distinguishes a structurally-backed claim
    (the capability declares where its code lives) from a genuinely
    evidence-less one.  This prevents every registry capability from starting
    at 'unproven' — only capabilities with ZERO evidence (no evidence_refs
    AND no backing_paths) are truly unproven.
    """
    from app.capability_registry import working_capability_proof_status

    model = ProblemModel(
        capabilities=[
            # No evidence_refs but has backing_paths → partial.
            Capability(
                name="Has paths",
                status="working",
                backing_paths=["backend/app/some_module.py"],
            ),
            # No evidence_refs and no backing_paths → unproven.
            Capability(name="No paths", status="working"),
            # Has both: evidence_refs wins the classification.
            Capability(
                name="Both",
                status="working",
                evidence_refs=[_proven_ref()],
                backing_paths=["backend/app/other.py"],
            ),
            # Non-working with backing_paths is excluded (not 'working').
            Capability(
                name="Planned with paths",
                status="planned",
                backing_paths=["backend/app/planned.py"],
            ),
        ]
    )

    proof = working_capability_proof_status(model)
    assert proof["has-paths"] == "partial", (
        "backing_paths present but no evidence_refs → partial"
    )
    assert proof["no-paths"] == "unproven", (
        "no evidence_refs and no backing_paths → unproven"
    )
    assert proof["both"] == "proven", (
        "evidence_refs proven → proven, even with backing_paths"
    )
    assert "planned-with-paths" not in proof, (
        "non-working capabilities are excluded regardless of backing_paths"
    )
    assert set(proof) == {"has-paths", "no-paths", "both"}, (
        "only working capabilities appear in the map"
    )


def test_working_capability_proof_status_backing_path_evidence_ref_is_partial():
    """A working cap whose ONLY evidence_ref is a proven ``backing_path`` reads
    ``partial`` (not ``proven``): a file-existence proof is not machine-verified
    corroboration, so it must NOT clear the enforce gate on its own."""
    from app.capability_registry import (
        capability_need_blocked_by_proof_gate,
        working_capability_proof_status,
    )

    model = ProblemModel(
        capabilities=[
            Capability(
                name="Backing only",
                status="working",
                evidence_refs=[
                    {"kind": "backing_path", "ref": "backend/app/foo.py", "proven": True}
                ],
            ),
            # A named_test proven ref alongside corroborates → proven.
            Capability(
                name="Corroborated",
                status="working",
                evidence_refs=[
                    {"kind": "backing_path", "ref": "backend/app/bar.py", "proven": True},
                    _proven_ref(),
                ],
            ),
        ]
    )
    proof = working_capability_proof_status(model)
    assert proof["backing-only"] == "partial"
    assert proof["corroborated"] == "proven"

    # Enforce mode: the backing-only cap stays gated; the corroborated one clears.
    assert capability_need_blocked_by_proof_gate(
        {"kind": "capability", "ref": "backing-only"}, proof, mode="enforce"
    ) is True
    assert capability_need_blocked_by_proof_gate(
        {"kind": "capability", "ref": "corroborated"}, proof, mode="enforce"
    ) is False


def test_working_capability_proof_status_backing_paths_dict_form():
    """Dict-form payloads also respect backing_paths → partial upgrade."""
    from app.capability_registry import working_capability_proof_status

    payload = {
        "capabilities": [
            {"name": "Backed cap", "status": "working",
             "backing_paths": ["backend/app/foo.py"]},
            {"name": "Bare cap", "status": "working"},
        ]
    }

    proof = working_capability_proof_status(payload)
    assert proof["backed-cap"] == "partial"
    assert proof["bare-cap"] == "unproven"


# ---------------------------------------------------------------------------
# Proof-gated capability resolution (enforce-mode toggle)
# ---------------------------------------------------------------------------


def test_capability_proof_gate_mode_defaults_to_off():
    """No override → "off" (safest default)."""
    from app.capability_registry import capability_proof_gate_mode

    assert capability_proof_gate_mode() == "off"
    assert capability_proof_gate_mode(None) == "off"


def test_capability_proof_gate_mode_no_env_fallback():
    """The env var path was removed — all consumers read from runtime_config;
    the function is a pure validator that defaults to 'off'."""
    from app.capability_registry import capability_proof_gate_mode

    assert capability_proof_gate_mode() == "off"
    # Override still works as the canonical path.
    assert capability_proof_gate_mode(override="observe") == "observe"
    assert capability_proof_gate_mode(override="enforce") == "enforce"


def test_capability_proof_gate_mode_override_takes_precedence():
    """The override kwarg takes precedence over the env var."""
    from app.capability_registry import capability_proof_gate_mode

    assert capability_proof_gate_mode(override="off") == "off"
    assert capability_proof_gate_mode(override="observe") == "observe"
    assert capability_proof_gate_mode(override="enforce") == "enforce"
    assert capability_proof_gate_mode(override="invalid") == "off"  # fail-safe


def test_proof_gate_blocks_unproven_only_under_enforce():
    """The shared gate predicate: enforce + not-proven ⇒ blocked; every other
    combination (off/observe mode, proven cap, no-match need) ⇒ not blocked."""
    from app.capability_registry import capability_need_blocked_by_proof_gate

    proof = {
        "unproven-cap": "unproven",
        "partial-cap": "partial",
        "proven-cap": "proven",
    }
    unproven_need = {"kind": "capability", "ref": "unproven-cap"}
    partial_need = {"kind": "capability", "ref": "partial-cap"}
    proven_need = {"kind": "capability", "ref": "proven-cap"}
    nomatch_need = {"kind": "capability", "ref": "not-in-registry"}

    # Enforce: unproven AND partial are gated; proven and no-match are not.
    assert capability_need_blocked_by_proof_gate(
        unproven_need, proof, mode="enforce"
    ) is True
    assert capability_need_blocked_by_proof_gate(
        partial_need, proof, mode="enforce"
    ) is True
    assert capability_need_blocked_by_proof_gate(
        proven_need, proof, mode="enforce"
    ) is False
    assert capability_need_blocked_by_proof_gate(
        nomatch_need, proof, mode="enforce"
    ) is False
    # Off mode: nothing is gated.
    assert capability_need_blocked_by_proof_gate(
        unproven_need, proof, mode="off"
    ) is False
    # Observe mode: nothing is gated either.
    assert capability_need_blocked_by_proof_gate(
        unproven_need, proof, mode="observe"
    ) is False


def _capability_blocker_task(ref: str, project_id=None):
    """A BLOCKED task waiting on a single capability need ``ref``."""
    task = _task(
        status=TaskStatus.BLOCKED,
        context={"blocker": {"needs": [{"kind": "capability", "ref": ref}]}},
    )
    if project_id is not None:
        task.project_id = project_id
    return task


def _classify_capability_need(ref: str, proof_map: dict, *, proof_gate_mode: str = "off") -> dict:
    """Run ``_classify_blocker_needs`` with every candidate cache pre-populated
    (so no DB/session is touched) and the registry proof map injected — returns
    the disposition dict, isolating the registry/proof-gate branch."""
    from app import blocked_task_unblock_sweeps as btu

    task = _capability_blocker_task(ref)
    pid = task.project_id
    return asyncio.run(
        btu._classify_blocker_needs(
            task,
            None,
            candidate_cache={pid: []},
            completed_cache={pid: []},
            dead_cache={pid: []},
            knowledge_cache={pid: []},
            registry_cache={pid: proof_map},
            proof_gate_mode=proof_gate_mode,
        )
    )


def test_unproven_working_capability_does_not_satisfy_need_in_enforce_mode():
    """ACCEPTANCE EVIDENCE. Enforce mode + a need whose ref matches a ``working``
    registry capability with ``proof_status=unproven`` must NOT satisfy the
    prerequisite — the blocked-resume sweep leaves the need in ``residual`` (the
    dependent stays BLOCKED) instead of routing it to ``completed_needs``."""
    proof = {"seeded-evaluation-harness": "unproven"}
    disp = _classify_capability_need(
        "seeded-evaluation-harness", proof, proof_gate_mode="enforce"
    )

    # The unproven working capability does NOT satisfy the need under enforce:
    # it stays residual (task remains BLOCKED) and is NOT marked completed.
    assert disp["residual"] == [
        {"kind": "capability", "ref": "seeded-evaluation-harness"}
    ]
    assert disp["completed_needs"] == []
    assert disp["edges"] == []
    assert disp["dead"] is False


def test_proven_working_capability_satisfies_need_in_enforce_mode():
    """Enforce mode does NOT gate a PROVEN working capability: the need is still
    satisfied by the codebase (routed to ``completed_needs``, not residual)."""
    proof = {"seeded-evaluation-harness": "proven"}
    disp = _classify_capability_need(
        "seeded-evaluation-harness", proof, proof_gate_mode="enforce"
    )

    assert disp["completed_needs"] == [
        {"kind": "capability", "ref": "seeded-evaluation-harness"}
    ]
    assert disp["residual"] == []


def test_observe_mode_still_satisfies_unproven():
    """Observe mode still treats an unproven working capability as
    satisfied — the need routes to ``completed_needs`` exactly as before."""
    proof = {"seeded-evaluation-harness": "unproven"}
    disp = _classify_capability_need(
        "seeded-evaluation-harness", proof, proof_gate_mode="observe"
    )

    assert disp["completed_needs"] == [
        {"kind": "capability", "ref": "seeded-evaluation-harness"}
    ]
    assert disp["residual"] == []


def test_off_mode_still_satisfies_unproven():
    """Off mode (the default) still treats an unproven working capability as
    satisfied — the need routes to ``completed_needs`` exactly as before."""
    proof = {"seeded-evaluation-harness": "unproven"}
    disp = _classify_capability_need(
        "seeded-evaluation-harness", proof, proof_gate_mode="off"
    )

    assert disp["completed_needs"] == [
        {"kind": "capability", "ref": "seeded-evaluation-harness"}
    ]
    assert disp["residual"] == []


def test_blocked_resume_sweep_routes_unproven_by_mode():
    """The blocked-resume sweep routes an unproven working-capability need to
    ``residual`` under enforce and to ``completed_needs`` under observe/off — the
    three modes diverge only on the unproven-match path (partial gates the same way)."""
    proof = {"seeded-evaluation-harness": "partial"}

    enforced = _classify_capability_need(
        "seeded-evaluation-harness", proof, proof_gate_mode="enforce"
    )
    assert enforced["residual"] and not enforced["completed_needs"]

    observed = _classify_capability_need(
        "seeded-evaluation-harness", proof, proof_gate_mode="observe"
    )
    assert observed["completed_needs"] and not observed["residual"]

    off = _classify_capability_need(
        "seeded-evaluation-harness", proof, proof_gate_mode="off"
    )
    assert off["completed_needs"] and not off["residual"]


def test_capability_proof_gate_mode_three_way():
    """ACCEPTANCE EVIDENCE. The three-way mode knob (off/observe/enforce) controls
    whether an unproven 'working' capability satisfies a blocked-task need:

    - ``off`` (default, safest) — the gate is entirely disabled; an unproven
      working capability resolves the need exactly as before the gate existed.
    - ``observe`` — same resolution behavior as off, but serves as the visual
      readiness check: the unproven resolution is visible in logs/heat so the
      operator can assess impact before flipping to enforce.
    - ``enforce`` — an unproven working capability does NOT satisfy the need;
      the dependent stays BLOCKED (need routed to residual, not completed_needs).
    """
    from app.capability_registry import (
        capability_need_blocked_by_proof_gate,
        capability_proof_gate_mode,
    )

    proof = {"seeded-evaluation-harness": "unproven"}
    need = {"kind": "capability", "ref": "seeded-evaluation-harness"}

    # ---- off mode ----
    assert capability_proof_gate_mode(override="off") == "off"
    assert (
        capability_need_blocked_by_proof_gate(need, proof, mode="off") is False
    )

    # ---- observe mode ----
    assert capability_proof_gate_mode(override="observe") == "observe"
    assert (
        capability_need_blocked_by_proof_gate(need, proof, mode="observe") is False
    )

    # ---- enforce mode ----
    assert capability_proof_gate_mode(override="enforce") == "enforce"
    assert (
        capability_need_blocked_by_proof_gate(need, proof, mode="enforce") is True
    )


def test_capability_ambiguous_resolves_to_canonical():
    # Two candidates qualify for capability 'design' by different exact signals
    # (payload type vs. exact title). The canonical (earliest-created) one wins
    # deterministically, regardless of caller order.
    base = datetime(2026, 6, 1, 0, 0, 0)
    earliest = _task(payloads=[{"type": "design"}], created_at=base)
    later = _task(title="design", created_at=base + timedelta(minutes=10))
    blocked = _task(status=TaskStatus.BLOCKED)
    need = {"kind": "capability", "ref": "design"}
    assert resolve_blocked_need(blocked, need, [later, earliest]) is earliest
    assert resolve_blocked_need(blocked, need, [earliest, later]) is earliest


def test_capability_ambiguous_marker_resolves_to_canonical():
    # Multiple candidates advertise the SAME explicit capability marker.
    base = datetime(2026, 6, 2, 0, 0, 0)
    earliest = _task(
        context={"provides_capability": "search-index"}, created_at=base
    )
    later = _task(
        context={"capability": ["search-index"]},
        created_at=base + timedelta(hours=3),
    )
    blocked = _task(status=TaskStatus.BLOCKED)
    need = {"kind": "capability", "ref": "search-index"}
    assert resolve_blocked_need(blocked, need, [later, earliest]) is earliest


def test_capability_ambiguous_created_at_tie_breaks_on_id():
    # Same created_at across duplicate capability providers → str(id) tiebreak,
    # matching compute_foundation_convergence_actions' canonical ordering.
    base = datetime(2026, 6, 3, 0, 0, 0)
    a = _task(context={"provides_capability": "cache"}, created_at=base)
    b = _task(context={"provides_capability": "cache"}, created_at=base)
    blocked = _task(status=TaskStatus.BLOCKED)
    need = {"kind": "capability", "ref": "cache"}
    expected = min((a, b), key=lambda t: str(t.id))
    assert resolve_blocked_need(blocked, need, [a, b]) is expected
    assert resolve_blocked_need(blocked, need, [b, a]) is expected


# ── parity guard: resume matcher must not miss what spawn resolves ────────────

def test_resume_matcher_parity_with_spawn_resolver():
    """Whatever the spawn-time resolver matches by an explicit task/capability
    signal, the resume-time matcher must resolve to the SAME candidate — they
    share ``prerequisite_matches_task`` so they cannot silently diverge again."""
    from app.finding_deduplication import resolve_prerequisite_task

    blocked = _task(status=TaskStatus.BLOCKED)
    foundation = _task(context={"finding_hash": "deadbeefcafe0002"})
    cap = _task(context={"provides_capability": "search-index"})

    cases = [
        ({"kind": "task", "ref": str(foundation.id)}, foundation),
        ({"kind": "task", "ref": "deadbeefcafe0002"}, foundation),
        ({"kind": "capability", "ref": "search-index"}, cap),
    ]
    for need, expected in cases:
        # Spawn-time resolver (producer side) matches the foundation...
        assert resolve_prerequisite_task(need, [expected]) is expected
        # ...and the resume-time matcher resolves to the same candidate.
        assert resolve_blocked_need(blocked, need, [expected]) is expected


def test_spawn_and_resume_pick_same_canonical_among_duplicates():
    """With duplicate foundations, the spawn-time resolver and the resume-time
    matcher must select the SAME canonical (earliest, id-tiebroken) provider —
    they share ``select_canonical_provider`` so a dependent attaches to one
    foundation no matter which path resolves it."""
    from app.finding_deduplication import resolve_prerequisite_task

    base = datetime(2026, 7, 1, 0, 0, 0)
    earliest = _task(
        context={"provides_capability": "vector-store"}, created_at=base
    )
    later = _task(
        context={"capability": ["vector-store"]},
        created_at=base + timedelta(hours=5),
    )
    blocked = _task(status=TaskStatus.BLOCKED)
    need = {"kind": "capability", "ref": "vector-store"}
    candidates = [later, earliest]

    spawn_choice = resolve_prerequisite_task(need, candidates)
    resume_choice = resolve_blocked_need(blocked, need, candidates)
    assert spawn_choice is earliest
    assert resume_choice is earliest
    assert spawn_choice is resume_choice


# ── guards ───────────────────────────────────────────────────────────────────

def test_self_edge_refused():
    blocked = _task(status=TaskStatus.BLOCKED)
    need = {"kind": "task", "ref": str(blocked.id)}
    # Even if the blocked task appears in its own candidate set, no self-edge.
    assert resolve_blocked_need(blocked, need, [blocked]) is None


def test_cycle_refused():
    blocked = _task(status=TaskStatus.BLOCKED)
    # foundation already depends_on the blocked task -> adding blocked->foundation
    # would close a cycle.
    foundation = _task(depends_on=[str(blocked.id)])
    need = {"kind": "task", "ref": str(foundation.id)}
    assert resolve_blocked_need(blocked, need, [foundation]) is None


def test_malformed_need_returns_none():
    blocked = _task(status=TaskStatus.BLOCKED)
    assert resolve_blocked_need(blocked, {}, [_task()]) is None
    assert resolve_blocked_need(blocked, {"kind": "task"}, [_task()]) is None
    assert resolve_blocked_need(blocked, {"ref": "x"}, [_task()]) is None
    assert resolve_blocked_need(blocked, {"kind": "bogus", "ref": "x"}, [_task()]) is None


# ── enter_blocked_state construction guard ────────────────────────────────────

def _make_blocked_task(**kwargs) -> "Task":
    return _task(status=TaskStatus.IN_PROGRESS, **kwargs)


def test_enter_blocked_state_construction_guard_fires_on_explicit_flag():
    """A need carrying structurally_dead=True is refused at BLOCKED creation;
    the task is CANCELLED+escalated rather than persisted as BLOCKED."""
    from app.task_blocked import enter_blocked_state

    task = _make_blocked_task()
    raw_dead_need = {"kind": "task", "ref": "deadbeefcafe0001", "structurally_dead": True}
    enter_blocked_state(
        task,
        waiting_on="some_prerequisite",
        reason="test",
        need=raw_dead_need,
    )
    assert task.status == TaskStatus.CANCELLED, (
        "structurally_dead need must escalate to CANCELLED, not persist BLOCKED"
    )
    assert (task.context or {}).get("precondition_escalated_to_parent") is True


def test_enter_blocked_state_construction_guard_fires_on_obs_label():
    """A need whose label starts with 'obs:' is observation-class and structurally
    dead — enter_blocked_state must CANCEL+escalate instead of persisting BLOCKED."""
    from app.task_blocked import enter_blocked_state

    task = _make_blocked_task()
    obs_need = {
        "kind": "task",
        "ref": "deadbeefcafe0002",
        "label": "obs: lane substrate layers a new system over pool/park-wake",
    }
    enter_blocked_state(
        task,
        waiting_on="obs_finding",
        reason="test",
        need=obs_need,
    )
    assert task.status == TaskStatus.CANCELLED, (
        "obs:-labelled need is observation-class and must escalate, not persist BLOCKED"
    )
    assert (task.context or {}).get("precondition_escalated_to_parent") is True



def test_enter_blocked_state_construction_guard_is_non_vacuous_for_valid_hash_with_obs_label():
    """The critical guard path: a valid finding-hash ref (passes build_need) paired
    with an obs-class label must STILL be refused — the guard fires before persisting."""
    from app.task_blocked import _raw_need_is_structurally_dead

    # Verify the detector fires correctly.
    assert _raw_need_is_structurally_dead(
        {"kind": "task", "ref": "deadbeefcafe0003", "label": "obs: some observation"}
    ) is True
    assert _raw_need_is_structurally_dead(
        {"kind": "task", "ref": "deadbeefcafe0003", "label": "OBS: case-insensitive"}
    ) is True
    assert _raw_need_is_structurally_dead(
        {"kind": "task", "ref": "deadbeefcafe0004", "structurally_dead": True}
    ) is True
    # A normal task need must NOT be detected as dead.
    assert _raw_need_is_structurally_dead(
        {"kind": "task", "ref": "deadbeefcafe0005", "label": "Implement serving guard"}
    ) is False
    assert _raw_need_is_structurally_dead(
        {"kind": "task", "ref": "deadbeefcafe0006"}
    ) is False


# ── need_is_structurally_unobservable (bounded-outcome liveness guard) ────────

def test_unobservable_need_detects_obs_class_and_structurally_dead():
    """A need no provider class can EVER satisfy — observation-class ("obs:")
    ref/label or an explicit structurally_dead flag — is unobservable, so the
    sweep must terminate it rather than re-arm the recheck forever."""
    # Observation-class by label (ref is a valid hash, but obs label dooms it).
    assert need_is_structurally_unobservable(
        {"kind": "task", "ref": "deadbeefcafe0001",
         "label": "obs: lane substrate layers a new system over pool/park-wake"}
    ) is True
    # Observation-class by ref prefix, case-insensitive.
    assert need_is_structurally_unobservable(
        {"kind": "task", "ref": "OBS: some observation"}
    ) is True
    # Explicit structurally_dead flag.
    assert need_is_structurally_unobservable(
        {"kind": "task", "ref": "deadbeefcafe0002", "structurally_dead": True}
    ) is True


def test_unobservable_need_detects_malformed_non_dict():
    """A malformed (non-dict) need can never bind to any resolver path."""
    assert need_is_structurally_unobservable(None) is True
    assert need_is_structurally_unobservable("obs: not a dict") is True


def test_observable_needs_are_never_structurally_unobservable():
    """Conservative: ONLY the construction-time structurally-dead signal proves
    unobservability — never mere ref shape. A capability may be advertised later,
    an exact task / PR may yet resolve, an empty dict carries no dead signal, and
    even a NON-exact kind=task ref can be satisfied by a KnowledgeDocument whose
    topic equals it — so none of these is falsely terminated."""
    assert need_is_structurally_unobservable({}) is False
    assert need_is_structurally_unobservable(
        {"kind": "capability", "ref": "payment-core"}
    ) is False
    assert need_is_structurally_unobservable(
        {"kind": "task", "ref": "deadbeefcafe0003"}
    ) is False
    assert need_is_structurally_unobservable(
        {"kind": "task", "ref": str(uuid.uuid4())}
    ) is False
    assert need_is_structurally_unobservable({"kind": "pr", "ref": "8123"}) is False
    # A non-exact task ref is knowledge-topic-satisfiable, so NOT unobservable
    # (this is the hollow-compute waiter shape that must stay observable).
    assert need_is_structurally_unobservable(
        {"kind": "task", "ref": "compute-hollow-need"}
    ) is False
    # A title-like task ref is likewise NOT proven unobservable by shape alone.
    assert need_is_structurally_unobservable(
        {"kind": "task", "ref": "Build the auth subsystem"}
    ) is False


def test_unknown_kind_returns_none():
    blocked = _task(status=TaskStatus.BLOCKED)
    need = {"kind": "branch", "ref": "main"}
    assert resolve_blocked_need(blocked, need, [_task()]) is None


# ── need_satisfied_by_completed (parked-resume against landed work) ───────────

def test_completed_task_need_satisfied_by_merged_foundation():
    done = _shipped()
    blocked = _task(status=TaskStatus.BLOCKED)
    need = {"kind": "task", "ref": str(done.id)}
    assert need_satisfied_by_completed(blocked, need, [done]) is True


def test_completed_finding_hash_need_satisfied_by_merged_foundation():
    done = _shipped(context={"finding_hash": "f00dcafe00001234"})
    blocked = _task(status=TaskStatus.BLOCKED)
    need = {"kind": "task", "ref": "f00dcafe00001234"}
    assert need_satisfied_by_completed(blocked, need, [done]) is True


def test_completed_capability_need_satisfied_by_advertised_marker():
    done = _shipped(context={"provides_capability": "auth-tokens"})
    blocked = _task(status=TaskStatus.BLOCKED)
    need = {"kind": "capability", "ref": "auth-tokens"}
    assert need_satisfied_by_completed(blocked, need, [done]) is True


def test_completed_pr_need_satisfied_by_owning_completed_task():
    done = _shipped(payloads=[{"type": "fix", "pr_number": 8123}])
    blocked = _task(status=TaskStatus.BLOCKED)
    need = {"kind": "pr", "ref": "8123"}
    assert need_satisfied_by_completed(blocked, need, [done]) is True


def test_completed_need_unsatisfied_returns_false():
    done = _shipped()
    blocked = _task(status=TaskStatus.BLOCKED)
    need = {"kind": "task", "ref": str(uuid.uuid4())}
    assert need_satisfied_by_completed(blocked, need, [done]) is False
    # Malformed / empty inputs are conservatively unsatisfied.
    assert need_satisfied_by_completed(blocked, {}, [done]) is False
    assert need_satisfied_by_completed(blocked, need, []) is False


def test_completed_need_never_self_satisfies():
    blocked = _shipped()
    need = {"kind": "task", "ref": str(blocked.id)}
    # The blocked task itself, even if somehow in the completed set, is excluded.
    assert need_satisfied_by_completed(blocked, need, [blocked]) is False


# ── shipped-gate: only genuinely-shipped foundations satisfy a completed need ──

def test_completed_without_merge_signal_does_not_satisfy():
    """A COMPLETED foundation with NO merge-unblock signal shipped nothing — the
    dedup layer would re-spawn it, so it must NOT unblock its dependent."""
    done = _task(status=TaskStatus.COMPLETED)  # no pr_merged
    blocked = _task(status=TaskStatus.BLOCKED)
    need = {"kind": "task", "ref": str(done.id)}
    assert need_satisfied_by_completed(blocked, need, [done]) is False
    assert completed_foundation_for_need(blocked, need, [done]) is None


def test_ghost_merge_foundation_does_not_satisfy():
    """A ghost merge (pr_merged=True but rollup parent FAILED, so code never
    reached the integration branch) must NOT unblock its dependent — aligning
    with the dedup layer that excludes it via the stranded-parent set."""
    parent_id = uuid.uuid4()
    ghost = _shipped(parent_task_id=parent_id)
    blocked = _task(status=TaskStatus.BLOCKED)
    need = {"kind": "task", "ref": str(ghost.id)}
    failed_parents = {parent_id}
    assert need_satisfied_by_completed(blocked, need, [ghost], failed_parents) is False
    assert completed_foundation_for_need(blocked, need, [ghost], failed_parents) is None
    # Same foundation, parent NOT stranded → genuinely shipped → still satisfies.
    assert need_satisfied_by_completed(blocked, need, [ghost], set()) is True


def test_staging_merge_unblock_foundation_still_satisfies():
    """A foundation that merged onto the SHARED staging base carries the
    rollup-unblock signal (pr_merged, landed_on_integration=False) and IS visible
    to a same-base dependent — it MUST still resume it (the regression to avoid)."""
    staging = _task(
        status=TaskStatus.COMPLETED,
        context={"pr_merged": True, "landed_on_integration": False,
                 "_non_merge_unblock_reason": "rollup_staging"},
    )
    blocked = _task(status=TaskStatus.BLOCKED)
    need = {"kind": "task", "ref": str(staging.id)}
    assert need_satisfied_by_completed(blocked, need, [staging]) is True


# ── need_matched_dead_foundation (provably-dead foundation) ───────────────────

def test_dead_task_need_matches_failed_foundation():
    dead = _task(status=TaskStatus.FAILED)
    blocked = _task(status=TaskStatus.BLOCKED)
    need = {"kind": "task", "ref": str(dead.id)}
    assert need_matched_dead_foundation(blocked, need, [dead]) is True


def test_dead_finding_hash_need_matches_failed_foundation():
    dead = _task(status=TaskStatus.FAILED, context={"finding_hash": "f00dcafe0000dead"})
    blocked = _task(status=TaskStatus.BLOCKED)
    need = {"kind": "task", "ref": "f00dcafe0000dead"}
    assert need_matched_dead_foundation(blocked, need, [dead]) is True


def test_dead_capability_need_matches_advertised_marker():
    dead = _task(status=TaskStatus.CANCELLED, context={"provides_capability": "auth-tokens"})
    blocked = _task(status=TaskStatus.BLOCKED)
    need = {"kind": "capability", "ref": "auth-tokens"}
    assert need_matched_dead_foundation(blocked, need, [dead]) is True


def test_dead_pr_need_matches_cancelled_owning_task():
    dead = _task(
        status=TaskStatus.CANCELLED, payloads=[{"type": "fix", "pr_number": 8200}]
    )
    blocked = _task(status=TaskStatus.BLOCKED)
    need = {"kind": "pr", "ref": "8200"}
    assert need_matched_dead_foundation(blocked, need, [dead]) is True


def test_dead_match_returns_false_when_no_terminal_candidate():
    dead = _task(status=TaskStatus.CANCELLED)
    blocked = _task(status=TaskStatus.BLOCKED)
    need = {"kind": "task", "ref": str(uuid.uuid4())}
    assert need_matched_dead_foundation(blocked, need, [dead]) is False
    # Malformed / empty inputs are conservatively unmatched.
    assert need_matched_dead_foundation(blocked, {}, [dead]) is False
    assert need_matched_dead_foundation(blocked, need, []) is False


def test_dead_match_excludes_requeue_replaced_cancellation():
    # A CANCELLED foundation that was requeue-replaced is NOT genuinely dead —
    # its work continues under the replacement clone, so it must not terminate.
    dead = _task(
        status=TaskStatus.CANCELLED,
        context={"_replaced_by_task_id": str(uuid.uuid4())},
    )
    blocked = _task(status=TaskStatus.BLOCKED)
    need = {"kind": "task", "ref": str(dead.id)}
    assert need_matched_dead_foundation(blocked, need, [dead]) is False


def test_dead_match_excludes_scope_empty_clean_exit():
    # A SCOPE_EMPTY exit is a deliberate "scope already covered" no-op, not a
    # defect, so it must not trigger a dead-foundation termination.
    dead = _task(
        status=TaskStatus.CANCELLED, context={"failure_category": "SCOPE_EMPTY"}
    )
    blocked = _task(status=TaskStatus.BLOCKED)
    need = {"kind": "task", "ref": str(dead.id)}
    assert need_matched_dead_foundation(blocked, need, [dead]) is False


def test_dead_match_never_self_matches():
    blocked = _task(status=TaskStatus.FAILED)
    need = {"kind": "task", "ref": str(blocked.id)}
    assert need_matched_dead_foundation(blocked, need, [blocked]) is False


def test_dead_match_excludes_co_deferred_shelving_by_finding_hash():
    # A co-deferred dependent's task is CANCELLED but RECOVERABLE — it re-surfaces
    # with its same-cycle-deferred prerequisite next cycle. A blocked dependent
    # whose finding-hash need matches it must NOT be classified as foundation-died.
    from app.failure_classification import CO_DEFERRED_SHELVED

    shelved = _task(
        status=TaskStatus.CANCELLED,
        context={"finding_hash": "f00dcafe0000dead", CO_DEFERRED_SHELVED: True},
    )
    blocked = _task(status=TaskStatus.BLOCKED)
    need = {"kind": "task", "ref": "f00dcafe0000dead"}
    assert need_matched_dead_foundation(blocked, need, [shelved]) is False


def test_dead_match_excludes_co_deferred_shelving_by_capability():
    # The capability-kind need path must equally exclude a co-deferred shelving.
    from app.failure_classification import CO_DEFERRED_SHELVED

    shelved = _task(
        status=TaskStatus.CANCELLED,
        context={"provides_capability": "auth-tokens", CO_DEFERRED_SHELVED: True},
    )
    blocked = _task(status=TaskStatus.BLOCKED)
    need = {"kind": "capability", "ref": "auth-tokens"}
    assert need_matched_dead_foundation(blocked, need, [shelved]) is False


# ── knowledge_substance_satisfies_need ────────────────────────────────────────

def test_dangling_task_ref_satisfied_by_completed_knowledge_document():
    """Incident shape (2026-06-07): task 7186f089 permanently BLOCKED on finding-hash
    'bfb447f23b6d44f6' whose originating task was CANCELLED, but a KnowledgeDocument
    persists the substance — the need must be satisfied, not parked or terminated."""
    from app.blocked_need_resolution import knowledge_substance_satisfies_need
    from app.knowledge_query import KnowledgeSubstance

    substance = [KnowledgeSubstance(finding_hash="bfb447f23b6d44f6", topic="better-runs-blueprint")]
    need = {"kind": "task", "ref": "bfb447f23b6d44f6"}
    assert knowledge_substance_satisfies_need(need, substance) is True


def test_knowledge_topic_equality_satisfies_need():
    from app.blocked_need_resolution import knowledge_substance_satisfies_need
    from app.knowledge_query import KnowledgeSubstance

    substance = [KnowledgeSubstance(finding_hash=None, topic="auth-subsystem-design")]
    need = {"kind": "task", "ref": "auth-subsystem-design"}
    assert knowledge_substance_satisfies_need(need, substance) is True


def test_ungrounded_compute_knowledge_does_not_satisfy_need():
    """Compute substance with no durable citations is filtered out."""
    from app.blocked_need_resolution import knowledge_substance_satisfies_need
    from app.knowledge_query import KnowledgeSubstance

    need = {"kind": "task", "ref": "gpu-proof"}
    ungrounded = [
        KnowledgeSubstance(
            finding_hash=None,
            topic="gpu-proof",
            source_kind="compute_experiment",
            source_citations=(),
            forward_quality_verdict={"analysis_mode": "fallback"},
        )
    ]
    # Ungrounded compute does not satisfy the need — the grounding gate always fires.
    assert knowledge_substance_satisfies_need(need, ungrounded) is False


def test_knowledge_finding_hash_mismatch_returns_false():
    from app.blocked_need_resolution import knowledge_substance_satisfies_need
    from app.knowledge_query import KnowledgeSubstance

    substance = [KnowledgeSubstance(finding_hash="aaaa1111", topic="some-topic")]
    need = {"kind": "task", "ref": "bbbb2222"}
    assert knowledge_substance_satisfies_need(need, substance) is False


def test_ungrounded_compute_doc_does_not_satisfy_prereq():
    from app.blocked_need_resolution import knowledge_substance_satisfies_need
    from app.knowledge_query import KnowledgeSubstance

    substance = [
        KnowledgeSubstance(
            finding_hash="compute-need-1",
            topic="compute-need-1",
            source_kind="compute_experiment",
            source_citations=(),
            forward_quality_verdict={
                "analysis_mode": "fallback",
                "citation_grounding_miss": True,
                "probes": {
                    "grounding": {
                        "checked": True,
                        "file_citations": 1,
                        "grounded": 0,
                        "unresolved": ["https://signed.example.com/jobs/result.json"],
                    }
                },
            },
        )
    ]
    need = {"kind": "task", "ref": "compute-need-1"}
    assert knowledge_substance_satisfies_need(need, substance) is False


def test_grounded_compute_negative_result_satisfies_prereq():
    from app.blocked_need_resolution import knowledge_substance_satisfies_need
    from app.knowledge_query import KnowledgeSubstance

    substance = [
        KnowledgeSubstance(
            finding_hash="compute-need-2",
            topic="compute-need-2",
            source_kind="compute_experiment",
            source_citations=("gs://bucket/jobs/result.json",),
            forward_quality_verdict={
                "analysis_mode": "llm",
                "outcome": "SPEND_CAP_EXCEEDED",
                "probes": {
                    "grounding": {
                        "checked": True,
                        "file_citations": 1,
                        "grounded": 1,
                        "unresolved": [],
                    }
                },
            },
        )
    ]
    need = {"kind": "task", "ref": "compute-need-2"}
    assert knowledge_substance_satisfies_need(need, substance) is True


def test_grounded_fallback_compute_doc_satisfies_prereq():
    # Deliberate contract: the prerequisite gate uses the LOOSE grounding predicate,
    # not the strict acceptance-evidence gate. A grounded fallback doc (analysis_mode
    # != 'llm') still unblocks dependents — requiring LLM mode would permanently starve
    # dependents when ANTHROPIC_API_KEY is absent. See also
    # test_require_acceptance_evidence_matches_documented_contract_for_grounded_fallback_doc.
    from app.blocked_need_resolution import knowledge_substance_satisfies_need
    from app.knowledge_query import KnowledgeSubstance

    substance = [
        KnowledgeSubstance(
            finding_hash="compute-need-3",
            topic="compute-need-3",
            source_kind="compute_experiment",
            source_citations=("gs://bucket/jobs/result.json",),
            forward_quality_verdict={
                "analysis_mode": "fallback",
                "outcome": "SUCCESS",
                "probes": {
                    "grounding": {
                        "checked": True,
                        "file_citations": 1,
                        "grounded": 1,
                        "unresolved": [],
                    }
                },
            },
        )
    ]
    need = {"kind": "task", "ref": "compute-need-3"}
    assert knowledge_substance_satisfies_need(need, substance) is True


def test_require_acceptance_evidence_matches_documented_contract_for_grounded_fallback_doc():
    """Pin the deliberate contract: a grounded analysis_mode=fallback compute document
    satisfies a prerequisite need even though it would NOT pass the strict acceptance-
    evidence gate (classify_knowledge_acceptance returns knowledge_acceptance_evidence=False
    for fallback mode). The prerequisite gate uses the LOOSE durable-grounding predicate,
    not the strict acceptance gate, so dependent tasks are never permanently starved when
    LLM analysis is unavailable.

    This test is the acceptance-requirement pin: it exercises the production code
    (knowledge_substance_satisfies_need) that both reaper_sweeps and capability_demand
    call, and explicitly documents the deliberate looser-than-acceptance behavior.
    """
    from app.blocked_need_resolution import knowledge_substance_satisfies_need
    from app.knowledge_query import KnowledgeSubstance, classify_knowledge_acceptance

    grounded_fallback = KnowledgeSubstance(
        finding_hash="fallback-grounded-doc",
        topic="fallback-grounded-doc",
        source_kind="compute_experiment",
        source_citations=("gs://bucket/jobs/metrics.json",),
        forward_quality_verdict={
            "analysis_mode": "fallback",
            "outcome": "SUCCESS",
            "probes": {
                "grounding": {
                    "checked": True,
                    "file_citations": 1,
                    "grounded": 1,
                    "unresolved": [],
                }
            },
        },
    )

    # Confirm the strict acceptance gate REJECTS this document (analysis_mode != 'llm').
    acceptance = classify_knowledge_acceptance(
        source_kind=grounded_fallback.source_kind,
        forward_quality_verdict=grounded_fallback.forward_quality_verdict,
        source_citations=list(grounded_fallback.source_citations or ()),
    )
    assert acceptance["knowledge_acceptance_evidence"] is False
    assert acceptance["compute_acceptance_grade"] == "measured_only"

    # But the prerequisite gate ACCEPTS it — the durable-grounding predicate passes
    # because there is a durable gs:// citation with grounded > 0 and no miss flag.
    need = {"kind": "task", "ref": "fallback-grounded-doc"}
    assert knowledge_substance_satisfies_need(need, [grounded_fallback]) is True


def test_compute_experiment_without_durable_grounding_stays_blocked():
    """Compute knowledge only unblocks dependents when the stored evidence is durable."""
    from app.blocked_need_resolution import knowledge_substance_satisfies_need
    from app.knowledge_query import (
        KnowledgeSubstance,
        knowledge_substance_has_durable_compute_grounding,
    )

    need = {"kind": "task", "ref": "blocked-compute-doc"}
    missing_probe = KnowledgeSubstance(
        finding_hash="blocked-compute-doc",
        topic="blocked-compute-doc",
        source_kind="compute_experiment",
        source_citations=("gs://bucket/jobs/result.json",),
        forward_quality_verdict={"analysis_mode": "fallback"},
    )
    transient_citation = KnowledgeSubstance(
        finding_hash="blocked-compute-doc",
        topic="blocked-compute-doc",
        source_kind="compute_experiment",
        source_citations=("https://signed.example.com/jobs/result.json",),
        forward_quality_verdict={
            "analysis_mode": "fallback",
            "probes": {
                "grounding": {
                    "checked": True,
                    "file_citations": 1,
                    "grounded": 1,
                    "unresolved": [],
                }
            },
        },
    )

    assert knowledge_substance_has_durable_compute_grounding(missing_probe) is False
    assert knowledge_substance_satisfies_need(need, [missing_probe]) is False
    assert knowledge_substance_has_durable_compute_grounding(
        transient_citation
    ) is False
    assert knowledge_substance_satisfies_need(need, [transient_citation]) is False


def test_knowledge_topic_near_miss_not_fuzzy():
    """Substring/prefix matches must NOT satisfy — exact equality only."""
    from app.blocked_need_resolution import knowledge_substance_satisfies_need
    from app.knowledge_query import KnowledgeSubstance

    substance = [KnowledgeSubstance(finding_hash=None, topic="auth-subsystem-design")]
    need = {"kind": "task", "ref": "auth"}
    assert knowledge_substance_satisfies_need(need, substance) is False


def test_knowledge_wrong_kind_not_satisfied():
    """Only kind='task' is handled — capability/pr needs are not matched."""
    from app.blocked_need_resolution import knowledge_substance_satisfies_need
    from app.knowledge_query import KnowledgeSubstance

    substance = [KnowledgeSubstance(finding_hash="abc", topic="abc")]
    assert knowledge_substance_satisfies_need({"kind": "capability", "ref": "abc"}, substance) is False
    assert knowledge_substance_satisfies_need({"kind": "pr", "ref": "abc"}, substance) is False


def test_knowledge_empty_index_returns_false():
    from app.blocked_need_resolution import knowledge_substance_satisfies_need

    need = {"kind": "task", "ref": "anything"}
    assert knowledge_substance_satisfies_need(need, []) is False


# ── fail-closed probe-absent compute-grounding gate ───────────────────────────

def test_unprobed_compute_doc_does_not_satisfy_prereq():
    """A compute substance with a durable citation but NO grounding probe must NOT
    satisfy a prereq — the probe is the only positive proof of durable grounding,
    so absence of the verdict fails closed (not open)."""
    from app.blocked_need_resolution import knowledge_substance_satisfies_need
    from app.knowledge_query import KnowledgeSubstance

    substance = [
        KnowledgeSubstance(
            finding_hash="compute-unprobed-1",
            topic="compute-unprobed-1",
            source_kind="compute_experiment",
            source_citations=("gs://bucket/jobs/result.json",),
            forward_quality_verdict=None,  # probe never ran — no verdict
        )
    ]
    need = {"kind": "task", "ref": "compute-unprobed-1"}
    assert knowledge_substance_satisfies_need(need, substance) is False


def test_no_probes_key_compute_doc_does_not_satisfy_prereq():
    """Verdict present but probes dict absent → fail closed."""
    from app.blocked_need_resolution import knowledge_substance_satisfies_need
    from app.knowledge_query import KnowledgeSubstance

    substance = [
        KnowledgeSubstance(
            finding_hash="compute-noprobes-1",
            topic="compute-noprobes-1",
            source_kind="compute_experiment",
            source_citations=("gs://bucket/jobs/result.json",),
            forward_quality_verdict={"analysis_mode": "llm"},  # no probes key
        )
    ]
    need = {"kind": "task", "ref": "compute-noprobes-1"}
    assert knowledge_substance_satisfies_need(need, substance) is False


def test_no_grounding_subprobe_compute_doc_does_not_satisfy_prereq():
    """Probes dict present but grounding sub-probe absent → fail closed."""
    from app.blocked_need_resolution import knowledge_substance_satisfies_need
    from app.knowledge_query import KnowledgeSubstance

    substance = [
        KnowledgeSubstance(
            finding_hash="compute-noground-1",
            topic="compute-noground-1",
            source_kind="compute_experiment",
            source_citations=("gs://bucket/jobs/result.json",),
            forward_quality_verdict={"analysis_mode": "llm", "probes": {}},  # no grounding
        )
    ]
    need = {"kind": "task", "ref": "compute-noground-1"}
    assert knowledge_substance_satisfies_need(need, substance) is False


def test_ephemeral_url_citation_compute_doc_does_not_satisfy_prereq():
    """A compute substance whose only citation is an ephemeral signed URL must not
    satisfy a prereq — bare http(s):// URLs are transient, not durable references."""
    from app.blocked_need_resolution import knowledge_substance_satisfies_need
    from app.knowledge_query import KnowledgeSubstance

    substance = [
        KnowledgeSubstance(
            finding_hash="compute-ephemeral-1",
            topic="compute-ephemeral-1",
            source_kind="compute_experiment",
            source_citations=("https://signed.storage.example.com/result.json?token=abc",),
            forward_quality_verdict={
                "analysis_mode": "llm",
                "probes": {
                    "grounding": {
                        "checked": True,
                        "file_citations": 1,
                        "grounded": 1,
                        "unresolved": [],
                    }
                },
            },
        )
    ]
    need = {"kind": "task", "ref": "compute-ephemeral-1"}
    assert knowledge_substance_satisfies_need(need, substance) is False
