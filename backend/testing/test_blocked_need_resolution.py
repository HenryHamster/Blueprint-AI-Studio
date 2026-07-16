"""Unit tests for the pure cross-task NEED resolver.

These exercise ``blocked_need_resolution.resolve_blocked_need`` as a pure matcher
over in-memory ``Task`` objects (no DB / no session) — the sweep integration is
covered separately in ``test_reaper.py``.

Also includes the acceptance-evidence test for the capability proof gate
auto-graduation sweep (``test_capability_proof_gate_graduation``), which requires
an in-memory SQLite DB to exercise the sweep's project-scanning logic.
"""

import asyncio
import uuid
from datetime import datetime, timedelta
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from sqlmodel import SQLModel

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


def _backing_path_ref() -> dict:
    """One low-confidence backing-path evidence ref (``proven=True``)."""
    return {"kind": "backing_path", "ref": "src/module.py", "proven": True}


def _unproven_ref() -> dict:
    """One machine-checked but unproven evidence ref (``proven=False``)."""
    return {"kind": "named_test", "ref": "testing/test_x.py::test_x", "proven": False}


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


def test_working_capability_proof_status_backing_path_evidence_ref_is_path_proven():
    """A working cap whose ONLY evidence_ref is a proven ``backing_path`` reads
    ``path_proven`` (the distinct structural-only tier, not ``proven``): a
    file-existence proof is not machine-verified corroboration, so it must NOT
    clear the enforce gate on its own (the gate passes only ``proven``)."""
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
    assert proof["backing-only"] == "path_proven"
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


def test_runtime_config_gate_mode_default_is_observe(monkeypatch):
    """Finding #4 (staged rollout): the boot-time runtime_config default is now
    ``"observe"`` — the safe first stage that records/surfaces but never blocks —
    not ``"off"`` (which left all enforcement inert). An explicit env override
    still wins, and an unrecognized value falls back to observe.

    Note: :func:`capability_registry.capability_proof_gate_mode` (the pure
    no-override fail-safe validator) intentionally still defaults to ``"off"``;
    this covers the DIFFERENT boot-time config default resolved from the env."""
    from app.runtime_config_state import resolve_capability_proof_gate_mode_default

    monkeypatch.delenv("CAPABILITY_PROOF_GATE_MODE", raising=False)
    assert resolve_capability_proof_gate_mode_default() == "observe"

    monkeypatch.setenv("CAPABILITY_PROOF_GATE_MODE", "enforce")
    assert resolve_capability_proof_gate_mode_default() == "enforce"

    monkeypatch.setenv("CAPABILITY_PROOF_GATE_MODE", "off")
    assert resolve_capability_proof_gate_mode_default() == "off"

    monkeypatch.setenv("CAPABILITY_PROOF_GATE_MODE", "bogus")
    assert resolve_capability_proof_gate_mode_default() == "observe"


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


async def _classify_capability_need_async(
    ref: str, proof_map: dict, *, proof_gate_mode: str = "off"
) -> dict:
    """Await ``_classify_blocker_needs`` with every candidate cache pre-populated
    (so no DB/session is touched) and the registry proof map injected — returns
    the disposition dict, isolating the registry/proof-gate branch.

    Async-native so it is safe to call from ``async def`` tests already running
    inside an event loop; the sync :func:`_classify_capability_need` wrapper below
    drives it via ``asyncio.run`` for the many synchronous ``def`` tests."""
    from app import blocked_task_unblock_sweeps as btu

    task = _capability_blocker_task(ref)
    pid = task.project_id
    return await btu._classify_blocker_needs(
        task,
        None,
        candidate_cache={pid: []},
        completed_cache={pid: []},
        dead_cache={pid: []},
        knowledge_cache={pid: []},
        registry_cache={pid: proof_map},
        proof_gate_mode=proof_gate_mode,
    )


def _classify_capability_need(ref: str, proof_map: dict, *, proof_gate_mode: str = "off") -> dict:
    """Synchronous wrapper around :func:`_classify_capability_need_async` for
    ``def`` tests (no running event loop)."""
    return asyncio.run(
        _classify_capability_need_async(ref, proof_map, proof_gate_mode=proof_gate_mode)
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


class _StubProject:
    """Minimal stand-in for the ``Project`` row the proof-map fetch reads."""

    def __init__(self, problem_model, capability_proof_records):
        self.problem_model = problem_model
        self.capability_proof_records = capability_proof_records


class _StubSession:
    """Async ``session.get`` returning one stubbed project (no DB needed)."""

    def __init__(self, project):
        self._project = project

    async def get(self, _model, _pk):
        return self._project


async def _blocked_resume_disposition(problem_model, proof_records, ref: str) -> dict:
    """Drive the REAL blocked-resume proof-map fetch, then classify a need against it.

    Deliberately goes through ``_fetch_working_capability_proof_status`` rather than
    hand-injecting a proof map: the ledger-corroboration pass this exercises lives
    inside that fetch, so a test that injected the map would bypass the very code it
    claims to cover.
    """
    from app import blocked_task_unblock_sweeps as btu

    session = _StubSession(_StubProject(problem_model, proof_records))
    proof_map = await btu._fetch_working_capability_proof_status(session, uuid.uuid4())
    return await _classify_capability_need_async(
        ref, proof_map, proof_gate_mode="enforce"
    )


def _self_declared_proven_model(name: str = "forged-cap") -> dict:
    """A ``working`` capability whose ONLY proof is an agent-authored evidence_ref.

    This is precisely what an agent can write into the JSONB ``problem_model``
    without any platform verification whatsoever.
    """
    return {
        "capabilities": [
            {"name": name, "status": "working", "evidence_refs": [_proven_ref()]}
        ]
    }


def _ledger(name: str, proof_kind: str = "named_test") -> dict:
    """One machine-verified PLATFORM proof-record entry (agent cannot write this)."""
    return {name: [{"task_id": "T1", "proof_kind": proof_kind, "proof_ref": "t.py::x"}]}


def test_self_declared_proven_ref_without_ledger_blocks_blocked_resume():
    """SECURITY (finding #4), blocked-resume seam. A capability whose ``proven``
    evidence_ref is a pure SELF-REPORT — no corroborating entry in the platform-only
    ``capability_proof_records`` ledger — must NOT satisfy a capability need under
    enforce. The dependent stays BLOCKED (need stays residual + gate-held).

    Before the fix the seam graded from ``problem_model.capabilities[].evidence_refs``
    alone, so this forged ref cleared the gate and resumed the dependent.
    """
    disp = asyncio.run(
        _blocked_resume_disposition(
            _self_declared_proven_model(), {}, "forged-cap"
        )
    )

    assert disp["completed_needs"] == [], (
        "a self-declared capability proof must never satisfy a prerequisite"
    )
    assert disp["residual"] == [{"kind": "capability", "ref": "forged-cap"}]
    assert disp["proof_gate_held"] == [{"kind": "capability", "ref": "forged-cap"}]


def test_ledger_corroborated_capability_clears_blocked_resume():
    """A capability whose ``proven`` claim IS corroborated by a machine-verified
    ``named_test`` entry in the platform ledger still satisfies the need under
    enforce — the fix blocks self-reports, not real proof."""
    disp = asyncio.run(
        _blocked_resume_disposition(
            _self_declared_proven_model("real-cap"), _ledger("real-cap"), "real-cap"
        )
    )

    assert disp["completed_needs"] == [{"kind": "capability", "ref": "real-cap"}]
    assert disp["residual"] == []
    assert disp["proof_gate_held"] == []


def test_backing_path_only_ledger_does_not_clear_blocked_resume():
    """A ledger holding only a structural ``backing_path`` entry grades
    ``path_proven``, not ``proven`` — it does not corroborate a ``proven``
    self-report, so the enforce gate still holds the dependent."""
    disp = asyncio.run(
        _blocked_resume_disposition(
            _self_declared_proven_model("structural-cap"),
            _ledger("structural-cap", proof_kind="backing_path"),
            "structural-cap",
        )
    )

    assert disp["completed_needs"] == []
    assert disp["proof_gate_held"] == [
        {"kind": "capability", "ref": "structural-cap"}
    ]


def _exemption_ledger(name: str = "human-process-cap") -> dict:
    """The operator's PLATFORM-recorded exemption (an agent cannot write this)."""
    from app.capability_registry import build_operator_exemption_record

    return {
        name: [
            build_operator_exemption_record(
                name, authorized_by="operator@example.com", reason="human process"
            )
        ]
    }


def test_platform_recorded_exemption_clears_blocked_resume_without_proof():
    """PRESERVED CARVE-OUT. An OPERATOR-recorded exemption clears the gate with NO
    machine-proof ledger entry — the earn loop deliberately never mints proof-records
    for an exempt capability, so demanding corroboration from it would nullify the
    exemption the operator granted.
    """
    model = {
        "capabilities": [
            {
                "name": "human-process-cap",
                "status": "working",
                "evidence_refs": [],
                "proof_exempt": True,
                "source": {"type": "user_message", "ref": "operator granted exemption"},
            }
        ]
    }
    disp = asyncio.run(
        _blocked_resume_disposition(model, _exemption_ledger(), "human-process-cap")
    )

    assert disp["completed_needs"] == [
        {"kind": "capability", "ref": "human-process-cap"}
    ], "an operator-recorded exemption must clear the gate without machine proof"
    assert disp["residual"] == []


def test_agent_fabricated_exemption_provenance_blocks_blocked_resume():
    """SECURITY (finding #9), blocked-resume seam. THE CORE PROPERTY OF THIS RELEASE.

    ``proof_exempt: true`` + ``source: {"type": "self_reflection"}`` is written to the
    AGENT-WRITABLE ``problem_model`` JSONB. The previous fix authorized the exemption
    off that ``source.type``, so an agent could forge one of the accepted types and
    resume every dependent blocked on the capability with nothing verified behind it.
    The authorization now lives in the platform-only ledger, so the forgery is refused
    and the dependent stays held.
    """
    for forged_type in ("self_reflection", "agent_observation", "user_message"):
        model = {
            "capabilities": [
                {
                    "name": "self-exempt-cap",
                    "status": "working",
                    "evidence_refs": [],
                    "proof_exempt": True,
                    "source": {"type": forged_type, "ref": "trust me"},
                }
            ]
        }
        # Empty platform ledger — the operator granted NO exemption.
        disp = asyncio.run(_blocked_resume_disposition(model, {}, "self-exempt-cap"))

        assert disp["completed_needs"] == [], (
            f"an agent-fabricated exemption provenance (source.type={forged_type!r}) "
            "must never clear the blocked-resume capability gate"
        )
        assert disp["proof_gate_held"] == [
            {"kind": "capability", "ref": "self-exempt-cap"}
        ]


def test_platform_exemption_holds_when_agent_drops_the_flag():
    """An operator's grant is not the agent's to revoke: the platform-recorded
    exemption clears the gate even when the problem_model no longer declares
    ``proof_exempt`` — the mirror hazard of putting authorization in agent-writable
    JSONB (an agent could nullify an exemption as easily as forge one)."""
    model = {
        "capabilities": [
            {"name": "human-process-cap", "status": "working", "evidence_refs": []}
        ]
    }
    disp = asyncio.run(
        _blocked_resume_disposition(model, _exemption_ledger(), "human-process-cap")
    )

    assert disp["completed_needs"] == [
        {"kind": "capability", "ref": "human-process-cap"}
    ]
    assert disp["residual"] == []


def test_bare_proof_exempt_blocks_blocked_resume():
    """A BARE ``proof_exempt=True`` — no cited ``source``, so no operator/strategic
    provenance — is the silent self-report escape hatch and must still BLOCK, ledger
    or no ledger."""
    model = {
        "capabilities": [
            {
                "name": "self-exempt",
                "status": "working",
                "evidence_refs": [],
                "proof_exempt": True,
            }
        ]
    }
    disp = asyncio.run(_blocked_resume_disposition(model, {}, "self-exempt"))

    assert disp["completed_needs"] == [], (
        "an unattributed proof_exempt must never clear the gate"
    )
    assert disp["proof_gate_held"] == [{"kind": "capability", "ref": "self-exempt"}]


def test_ledger_only_proof_still_clears_blocked_resume():
    """Regression guard for the fail-open evidence_refs append: a capability proven
    ONLY in the platform ledger (tier-1 append lost) is still RAISED to proven and
    clears the gate. The corroboration pass must refuse self-reports without
    stranding genuinely-verified capabilities."""
    model = {
        "capabilities": [
            # No evidence_refs at all — tier-1 reads unproven.
            {"name": "ledger-only-cap", "status": "working"}
        ]
    }
    disp = asyncio.run(
        _blocked_resume_disposition(
            model, _ledger("ledger-only-cap"), "ledger-only-cap"
        )
    )

    assert disp["completed_needs"] == [
        {"kind": "capability", "ref": "ledger-only-cap"}
    ]
    assert disp["residual"] == []


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


def test_capability_proof_gate_full_cycle():
    """End-to-end: blocked → proof recorded → unblocked under enforce mode.

    Phase 1 (unproven): a working capability with zero evidence blocks its
    dependent under enforce mode — the need stays residual (task stays BLOCKED).

    Phase 2 (proven): after proof is recorded (evidence_refs populated with a
    proven=True ref), the same capability now satisfies the need — the need
    routes to completed_needs so the task can resume.

    Phase 3 (badge consistency): annotate_capability_proof_status reflects the
    same tiered classification — a named_test proof_record yields "proven", a
    backing_path-only proof_record yields "path_proven" (not "proven"), matching
    the gate's decision.  This exercises the fix to the proof-records fallback
    path that previously over-reported backing_path-only records as "proven".

    The chain exercised end-to-end:

      working_capability_proof_status → classify_evidence_tiered →
      capability_need_blocked_by_proof_gate → _classify_blocker_needs →
      enforce-mode routing → annotate_capability_proof_status (badge)
    """
    from app.capability_registry import (
        annotate_capability_proof_status,
        capability_need_blocked_by_proof_gate,
        working_capability_proof_status,
    )
    from app.problem_model import Capability, ProblemModel

    # ── Phase 1: capability declared working with zero evidence → unproven ──
    unproven_model = ProblemModel(
        capabilities=[
            Capability(name="Container runtime", status="working"),
        ]
    )
    proof = working_capability_proof_status(unproven_model)
    assert proof == {"container-runtime": "unproven"}

    need = {"kind": "capability", "ref": "container-runtime"}
    vocab = [{"name": "container-runtime", "status": "working"}]

    # The gate blocks an unproven capability under enforce mode.
    assert capability_need_blocked_by_proof_gate(need, proof, mode="enforce") is True

    # The blocked-resume sweep routes the need to residual (stays BLOCKED).
    disp_blocked = _classify_capability_need(
        "container-runtime", proof, proof_gate_mode="enforce"
    )
    assert disp_blocked["residual"] == [
        {"kind": "capability", "ref": "container-runtime"}
    ]
    assert disp_blocked["completed_needs"] == []
    # The enforce gate records the held need in ``proof_gate_held`` (a subset of
    # ``residual``) so the disposition can HOLD the dependent BLOCKED
    # deterministically instead of letting it fall through to the optimistic-retry
    # path in the sweep (exercised end-to-end in Phase 4 below).
    assert disp_blocked["proof_gate_held"] == [
        {"kind": "capability", "ref": "container-runtime"}
    ]

    # Badge also reads unproven before proof lands.
    badge_before = annotate_capability_proof_status(vocab, {})
    assert badge_before[0]["proof_status"] == "unproven"

    # ── Phase 2: proof recorded (evidence_refs populated with a proven ref) ──
    proven_model = ProblemModel(
        capabilities=[
            Capability(
                name="Container runtime",
                status="working",
                evidence_refs=[_proven_ref()],
            ),
        ]
    )
    proof = working_capability_proof_status(proven_model)
    assert proof == {"container-runtime": "proven"}

    # The gate now lets the proven capability through under enforce mode.
    assert capability_need_blocked_by_proof_gate(need, proof, mode="enforce") is False

    # The blocked-resume sweep routes the need to completed_needs (task resumes).
    disp_unblocked = _classify_capability_need(
        "container-runtime", proof, proof_gate_mode="enforce"
    )
    assert disp_unblocked["completed_needs"] == [
        {"kind": "capability", "ref": "container-runtime"}
    ]
    assert disp_unblocked["residual"] == []

    # ── Phase 3: badge consistency — proof_records path uses tiered classification ──
    # A named_test proof_record should yield "proven" in the badge.
    named_test_records = {
        "container-runtime": [
            {
                "task_id": "provider-task-1",
                "proof_kind": "named_test",
                "proof_ref": "testing/test_blocked_need_resolution.py::test_capability_proof_gate_full_cycle",
                "proved_at": "2026-07-11T00:00:00Z",
                "commit_sha": "",
            }
        ]
    }
    badge_named_test = annotate_capability_proof_status(vocab, named_test_records)
    assert badge_named_test[0]["proof_status"] == "proven", (
        "named_test proof_records must yield 'proven' in the badge"
    )

    # A backing_path-only proof_record must yield "path_proven" (not "proven"):
    # file-existence proof is structural, not machine-verified, so it must not
    # clear the enforce gate as strongly as a green test.
    backing_only_records = {
        "container-runtime": [
            {
                "task_id": "backing:container-runtime",
                "proof_kind": "backing_path",
                "proof_ref": "container-runtime",
                "proved_at": "2026-07-11T00:00:00Z",
                "commit_sha": "",
            }
        ]
    }
    badge_backing = annotate_capability_proof_status(vocab, backing_only_records)
    assert badge_backing[0]["proof_status"] == "path_proven", (
        "backing_path-only proof_records must yield 'path_proven', not 'proven' — "
        "the badge must not over-report structural-only evidence as machine-verified"
    )

    # ── Phase 4: release-qualification enforcement point ──────────────────────
    # The THIRD enforcement seam (alongside spawn-time and blocked-resume): the
    # release qualifier derives ``capability_proof_blocked`` from the SAME tiered
    # proof map via the shared ``capability_proof_gate_release_status`` primitive
    # the qualifier now delegates to. Assert the full transition here so a
    # regression in that shared derivation surfaces in the connected cycle.
    from app.capability_registry import capability_proof_gate_release_status

    # Enforce + unproven working capability → promotion blocked, audited.
    q_blocked, q_detail = capability_proof_gate_release_status(
        unproven_model, "enforce", named_test_records
    )
    assert q_blocked is True, (
        "the release qualifier must block promotion under enforce mode when a "
        "working capability is unproven"
    )
    assert q_detail["mode"] == "enforce"
    assert q_detail["unproven"] == ["container-runtime"]
    assert q_detail["unproven_count"] == 1
    assert q_detail["total_working"] == 1

    # Enforce + proven working capability, CORROBORATED by the platform ledger →
    # promotion NOT blocked. The ledger is what clears it: the same problem_model
    # with an empty ledger stays blocked (asserted next), because a ``proven``
    # evidence_ref on the agent-writable problem_model is a self-report.
    q_blocked_proven, q_detail_proven = capability_proof_gate_release_status(
        proven_model, "enforce", named_test_records
    )
    assert q_blocked_proven is False, (
        "a PLATFORM-recorded proof must clear the release-qualification gate too"
    )
    assert q_detail_proven["unproven"] == []
    assert q_detail_proven["unproven_count"] == 0
    assert q_detail_proven["corroborated"] == ["container-runtime"]

    # SECURITY: the same self-declared proven ref with NO platform ledger entry does
    # NOT clear the release gate — including when the caller's ledger column is NULL.
    for _empty_ledger in ({}, None):
        q_blocked_forged, q_detail_forged = capability_proof_gate_release_status(
            proven_model, "enforce", _empty_ledger
        )
        assert q_blocked_forged is True, (
            "an uncorroborated self-report must never clear the release gate "
            f"(ledger={_empty_ledger!r})"
        )
        assert q_detail_forged["uncorroborated"] == ["container-runtime"]

    # Off/observe modes record the dimension but never block promotion.
    for _mode in ("off", "observe"):
        _mb, _md = capability_proof_gate_release_status(unproven_model, _mode)
        assert _mb is False, f"{_mode} mode must never block release promotion"
        assert _md == {"mode": _mode}

    # Enforce with no problem_model degrades safely (records error, no block).
    q_blocked_none, q_detail_none = capability_proof_gate_release_status(
        None, "enforce"
    )
    assert q_blocked_none is False
    assert q_detail_none["error"] == "no problem_model"

    # ── Phase F: spawn-time enforcement point (the FIRST consumer seam) ────────
    # The spawn-time resolver (research_completion) grades an explicit capability
    # prerequisite through ``spawn_capability_gate_disposition`` — the single
    # source of truth for its three-way branch. Assert the same tiered proof map
    # yields the same block/surface/ok decision the blocked-resume sweep (Phase 5)
    # and the release qualifier (Phase 4) derive, so the three seams cannot drift.
    from app.research_completion import spawn_capability_gate_disposition

    _cr_need = {"kind": "capability", "ref": "container-runtime"}
    _unproven_map = {"container-runtime": "unproven"}
    _proven_map = {"container-runtime": "proven"}

    # ENFORCE + unproven → block (dependent parks BLOCKED, matching the sweep).
    assert spawn_capability_gate_disposition(
        _cr_need, _unproven_map, mode="enforce"
    ) == ("block", "unproven")

    # ENFORCE + proven → ok (no gate, matching the sweep's resume in Phase 5/2).
    assert spawn_capability_gate_disposition(
        _cr_need, _proven_map, mode="enforce"
    ) == ("ok", "proven")

    # OBSERVE + unproven → surface: the need still resolves, but the unverified
    # status is surfaced (never gated) while enforce is off.
    assert spawn_capability_gate_disposition(
        _cr_need, _unproven_map, mode="observe"
    ) == ("surface", "unproven")

    # OFF + unproven → surface too (any non-enforce mode surfaces, never blocks).
    assert spawn_capability_gate_disposition(
        _cr_need, _unproven_map, mode="off"
    ) == ("surface", "unproven")

    # A need matching NO working registry capability → ok with proof_status None
    # (normal resolution applies; the gate never fabricates a block).
    assert spawn_capability_gate_disposition(
        {"kind": "capability", "ref": "nonexistent-cap"}, _unproven_map, mode="enforce"
    ) == ("ok", None)

    # ── Phase 5: DB-backed end-to-end sweep (enforce holds, then resumes) ──────
    # Drive the REAL blocked-resume sweep (sweep_unblock_blocked_tasks) against a
    # live in-memory DB across the full enforcement cycle:
    #
    #   enforce mode  → dependent BLOCKED on an unproven working-capability need
    #                   → sweep leaves it BLOCKED (does NOT optimistically requeue)
    #   proof lands   → record_capability_proof stamps a proven evidence_ref
    #                   → sweep resumes the dependent to QUEUED.
    #
    # This is the seam the pure-logic phases above cannot reach: WITHOUT the
    # gate-held HOLD fix, the no-dep agent-source blocker falls through the "none"
    # disposition into the optimistic-retry path and is requeued to QUEUED despite
    # the enforce gate — so the ``requeued == 0`` assertion below would fail.
    asyncio.run(_run_full_cycle_db_sweep())


async def _run_full_cycle_db_sweep() -> None:
    """End-to-end enforcement cycle against a live in-memory DB.

    Self-contained (own engine/session, runtime_config restored in ``finally``)
    so it composes with the sync pure-logic phases via ``asyncio.run`` without a
    nested-event-loop conflict.
    """
    from sqlalchemy.orm.attributes import flag_modified

    from app.blocked_task_unblock_sweeps import sweep_unblock_blocked_tasks
    from app.capability_proof import record_capability_proof
    from app.models import Project, Task, TaskStatus
    from app.shared_state import runtime_config as _rc
    from app.time_utils import _utcnow

    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    async with engine.begin() as conn:
        await conn.run_sync(SQLModel.metadata.create_all)
    Session = sessionmaker(
        bind=engine,
        class_=AsyncSession,
        autocommit=False,
        autoflush=False,
        expire_on_commit=False,
    )

    async def _stub_verify(*_a, **_k):
        # No GitHub in a unit test — a dependent with no dep edge never needs it,
        # but the sweep still requires a non-None callable to reach the resolver.
        return False

    need = {"kind": "capability", "ref": "container-runtime"}
    saved_mode = _rc.get("capability_proof_gate_mode")
    _rc["capability_proof_gate_mode"] = "enforce"
    try:
        async with Session() as s:
            project = Project(
                name="full-cycle",
                user_id="u1",
                problem_model={
                    "capabilities": [
                        {"name": "Container runtime", "status": "working"}
                    ]
                },
            )
            s.add(project)
            await s.commit()
            await s.refresh(project)

            now = _utcnow()
            # A no-dep, agent-source BLOCKED dependent waiting on the capability —
            # the exact shape the optimistic-retry path would requeue if the gate
            # hold were not authoritative.
            blocked = Task(
                project_id=project.id,
                title="dependent",
                description="x",
                status=TaskStatus.BLOCKED,
                created_by="u1",
                requires_approval=False,
                payloads=[{"type": "create"}],
                status_log=[],
                depends_on=[],
                blocker_kind="precondition",
                blocked_reason="BLOCKER: needs capability container-runtime",
                blocked_at=now,
                recheck_after=now - timedelta(minutes=1),  # due
                context={
                    "blocker": {
                        "source": "agent",
                        "waiting_on": "capability",
                        "needs": [need],
                        "need": need,
                    }
                },
            )
            s.add(blocked)
            await s.commit()
            await s.refresh(blocked)

            # ── Phase A: enforce mode HOLDS the dependent BLOCKED (no requeue). ──
            requeued = await sweep_unblock_blocked_tasks(
                s, _utcnow(), verify_pr_merged=_stub_verify
            )
            await s.refresh(blocked)
            assert requeued == 0, (
                "enforce gate must NOT requeue a dependent blocked on an unproven "
                "working capability — the gate hold must beat the optimistic-retry "
                "fall-through"
            )
            assert blocked.status == TaskStatus.BLOCKED

            # ── Phase B: a provider lands with proven acceptance evidence. ──
            # The provider is an in-memory task (never a DB foundation) — it only
            # supplies the verdict/marker that record_capability_proof stamps onto
            # the project's problem_model as a proven evidence_ref.
            provider = Task(
                id=uuid.uuid4(),
                project_id=project.id,
                title="provider",
                description="x",
                status=TaskStatus.COMPLETED,
                created_by="u1",
                requires_approval=False,
                payloads=[{"type": "create"}],
                status_log=[],
                context={
                    "provides_capability": "container-runtime",
                    "code_acceptance_verdict": {
                        "proven": True,
                        "kind": "named_test",
                        "proof_ref": (
                            "testing/test_blocked_need_resolution.py::"
                            "test_capability_proof_gate_full_cycle"
                        ),
                    },
                },
            )
            recorded = record_capability_proof(provider, project)
            assert recorded == ["container-runtime"], (
                "a COMPLETED provider with a proven acceptance verdict must record "
                "a capability proof"
            )
            s.add(project)
            await s.commit()

            # ── Phase C: the now-proven capability resumes the dependent. ──
            blocked.recheck_after = _utcnow() - timedelta(minutes=1)  # due again
            s.add(blocked)
            await s.commit()
            requeued2 = await sweep_unblock_blocked_tasks(
                s, _utcnow(), verify_pr_merged=_stub_verify
            )
            await s.refresh(blocked)
            assert requeued2 == 1, (
                "a recorded proof must let the enforce gate resume the dependent"
            )
            assert blocked.status == TaskStatus.QUEUED

            # ── Phase D: proof that landed ONLY in capability_proof_records ────
            # record_capability_proof stamps BOTH evidence_refs (tier-1) and the
            # durable capability_proof_records column (tier-2); the evidence_refs
            # append is fail-open. Simulate the tier-1 append having been lost
            # (problem_model shape blocked it) so the proof survives only in
            # capability_proof_records — the exact case the release-qualifier and
            # badge seams already tolerate. The blocked-resume enforce gate must
            # now agree: the tier-2 fold in _fetch_working_capability_proof_status
            # upgrades the capability to 'proven' and resumes its dependent.
            pm = project.problem_model
            pm["capabilities"].append(
                {"name": "GPU runtime", "status": "working"}
            )
            # Durable proof recorded ONLY in the project-level column — NO
            # evidence_refs on the capability entry itself.
            project.capability_proof_records = {
                **(project.capability_proof_records or {}),
                "gpu-runtime": [
                    {
                        "task_id": "provider-gpu",
                        "proof_kind": "named_test",
                        "proof_ref": (
                            "testing/test_blocked_need_resolution.py::"
                            "test_capability_proof_gate_full_cycle"
                        ),
                        "proved_at": _utcnow().isoformat(),
                        "commit_sha": "",
                    }
                ],
            }
            flag_modified(project, "problem_model")
            flag_modified(project, "capability_proof_records")
            s.add(project)
            await s.commit()

            gpu_need = {"kind": "capability", "ref": "gpu-runtime"}
            now2 = _utcnow()
            gpu_blocked = Task(
                project_id=project.id,
                title="gpu-dependent",
                description="x",
                status=TaskStatus.BLOCKED,
                created_by="u1",
                requires_approval=False,
                payloads=[{"type": "create"}],
                status_log=[],
                depends_on=[],
                blocker_kind="precondition",
                blocked_reason="BLOCKER: needs capability gpu-runtime",
                blocked_at=now2,
                recheck_after=now2 - timedelta(minutes=1),  # due
                context={
                    "blocker": {
                        "source": "agent",
                        "waiting_on": "capability",
                        "needs": [gpu_need],
                        "need": gpu_need,
                    }
                },
            )
            s.add(gpu_blocked)
            await s.commit()
            await s.refresh(gpu_blocked)

            requeued3 = await sweep_unblock_blocked_tasks(
                s, _utcnow(), verify_pr_merged=_stub_verify
            )
            await s.refresh(gpu_blocked)
            assert requeued3 == 1, (
                "a proof recorded only in capability_proof_records (tier-2, the "
                "fail-open evidence_refs append lost) must still clear the "
                "enforce gate — the blocked-resume seam must agree with the "
                "release-qualifier and badge seams"
            )
            assert gpu_blocked.status == TaskStatus.QUEUED

            # ── Phase E: proof minted through the REAL landing seam ───────────
            # The prior phases stamp the proof by calling record_capability_proof
            # directly. That simulates the "provider lands with acceptance
            # evidence" wiring instead of exercising it. Here we drive a provider
            # through the production integration-landing seam,
            # ``mark_landed_on_integration(provider, project)`` — the canonical
            # write for a real integration-branch merge — and assert it mints the
            # capability proof itself, then that the enforce gate resumes the
            # dependent. Without the project-aware proof recording now wired into
            # mark_landed_on_integration, the landing path would stamp only the
            # acceptance verdict and NEVER the proof, so ``requeued4`` would stay
            # 0 and the dependent would remain BLOCKED — this asserts the seam.
            from app.landed_signal import mark_landed_on_integration

            pm2 = project.problem_model
            pm2["capabilities"].append(
                {"name": "Data pipeline", "status": "working"}
            )
            flag_modified(project, "problem_model")
            s.add(project)
            await s.commit()

            pipe_need = {"kind": "capability", "ref": "data-pipeline"}
            now3 = _utcnow()
            pipe_blocked = Task(
                project_id=project.id,
                title="pipe-dependent",
                description="x",
                status=TaskStatus.BLOCKED,
                created_by="u1",
                requires_approval=False,
                payloads=[{"type": "create"}],
                status_log=[],
                depends_on=[],
                blocker_kind="precondition",
                blocked_reason="BLOCKER: needs capability data-pipeline",
                blocked_at=now3,
                recheck_after=now3 - timedelta(minutes=1),  # due
                context={
                    "blocker": {
                        "source": "agent",
                        "waiting_on": "capability",
                        "needs": [pipe_need],
                        "need": pipe_need,
                    }
                },
            )
            s.add(pipe_blocked)
            await s.commit()
            await s.refresh(pipe_blocked)

            # Enforce still holds the dependent BLOCKED before the provider lands.
            requeued_hold = await sweep_unblock_blocked_tasks(
                s, _utcnow(), verify_pr_merged=_stub_verify
            )
            await s.refresh(pipe_blocked)
            assert requeued_hold == 0
            assert pipe_blocked.status == TaskStatus.BLOCKED

            # The provider lands on the integration branch. mark_landed_on_
            # integration must mint the capability proof from its proven verdict —
            # NOT a direct record_capability_proof call.
            pipe_provider = Task(
                id=uuid.uuid4(),
                project_id=project.id,
                title="pipe-provider",
                description="x",
                status=TaskStatus.COMPLETED,
                created_by="u1",
                requires_approval=False,
                payloads=[{"type": "create"}],
                status_log=[],
                context={
                    "provides_capability": "data-pipeline",
                    "code_acceptance_verdict": {
                        "proven": True,
                        "kind": "named_test",
                        "proof_ref": (
                            "testing/test_blocked_need_resolution.py::"
                            "test_capability_proof_gate_full_cycle"
                        ),
                    },
                },
            )
            mark_landed_on_integration(pipe_provider, project)
            assert "data-pipeline" in (project.capability_proof_records or {}), (
                "the integration-landing seam must mint the capability proof "
                "itself when a project is supplied — the proof recording is no "
                "longer confined to apply_merged_landed_signal"
            )
            s.add(project)
            await s.commit()

            # The now-proven capability resumes the dependent on the next sweep.
            pipe_blocked.recheck_after = _utcnow() - timedelta(minutes=1)  # due
            s.add(pipe_blocked)
            await s.commit()
            requeued4 = await sweep_unblock_blocked_tasks(
                s, _utcnow(), verify_pr_merged=_stub_verify
            )
            await s.refresh(pipe_blocked)
            assert requeued4 == 1, (
                "a proof minted by the integration-landing seam must let the "
                "enforce gate resume the dependent — the landing path, not a "
                "direct record_capability_proof call, closed the cycle"
            )
            assert pipe_blocked.status == TaskStatus.QUEUED

            # ── Phase G: a FORGED proof kind mints no proof (enforce holds) ────
            # record_capability_proof reads code_acceptance_verdict from the
            # agent-writable task.context. A task that declared no acceptance
            # evidence keeps whatever verdict it authored (record_code_acceptance_
            # verdict no-ops for it), so an agent can self-author proven=True with
            # an UNRECOGNIZED kind. Because classify_evidence_tiered reads any
            # non-machine-but-unrecognized kind as full 'proven', such a record
            # would clear the enforce gate with nothing verified. The recording
            # seam must refuse it: only a kind in the recognized proof vocabulary
            # mints a durable proof, so the dependent stays BLOCKED.
            pm3 = project.problem_model
            pm3["capabilities"].append(
                {"name": "Forged cap", "status": "working"}
            )
            flag_modified(project, "problem_model")
            s.add(project)
            await s.commit()

            forged_provider = Task(
                id=uuid.uuid4(),
                project_id=project.id,
                title="forged-provider",
                description="x",
                status=TaskStatus.COMPLETED,
                created_by="u1",
                requires_approval=False,
                payloads=[{"type": "create"}],
                status_log=[],
                context={
                    "provides_capability": "forged-cap",
                    "code_acceptance_verdict": {
                        "proven": True,
                        # NOT a member of the recognized proof vocabulary — a
                        # self-authored kind an agent could write into JSONB.
                        "kind": "self_reflection",
                        "proof_ref": "trust me",
                    },
                },
            )
            forged_recorded = record_capability_proof(forged_provider, project)
            assert forged_recorded == [], (
                "record_capability_proof must refuse a verdict whose kind is not "
                "in the recognized proof vocabulary — a forged/self-authored kind "
                "must never mint a gate-clearing proof"
            )
            assert "forged-cap" not in (project.capability_proof_records or {}), (
                "no proof record may be written for an unrecognized proof kind"
            )
            s.add(project)
            await s.commit()

            forged_need = {"kind": "capability", "ref": "forged-cap"}
            now4 = _utcnow()
            forged_blocked = Task(
                project_id=project.id,
                title="forged-dependent",
                description="x",
                status=TaskStatus.BLOCKED,
                created_by="u1",
                requires_approval=False,
                payloads=[{"type": "create"}],
                status_log=[],
                depends_on=[],
                blocker_kind="precondition",
                blocked_reason="BLOCKER: needs capability forged-cap",
                blocked_at=now4,
                recheck_after=now4 - timedelta(minutes=1),  # due
                context={
                    "blocker": {
                        "source": "agent",
                        "waiting_on": "capability",
                        "needs": [forged_need],
                        "need": forged_need,
                    }
                },
            )
            s.add(forged_blocked)
            await s.commit()
            await s.refresh(forged_blocked)

            requeued5 = await sweep_unblock_blocked_tasks(
                s, _utcnow(), verify_pr_merged=_stub_verify
            )
            await s.refresh(forged_blocked)
            assert requeued5 == 0, (
                "a forged-kind verdict mints no proof, so the enforce gate must "
                "keep the dependent BLOCKED — no unverified self-report resumes it"
            )
            assert forged_blocked.status == TaskStatus.BLOCKED
    finally:
        if saved_mode is None:
            _rc.pop("capability_proof_gate_mode", None)
        else:
            _rc["capability_proof_gate_mode"] = saved_mode
        await engine.dispose()


def test_backing_path_only_capability_stays_gated_under_enforce():
    """ACCEPTANCE EVIDENCE. A capability declared 'working' with ONLY
    backing_path proof (proven=True but kind=backing_path) must NOT satisfy
    the enforce gate — it classifies as 'path_proven' rather than 'proven',
    and ``capability_need_blocked_by_proof_gate`` returns True under enforce
    mode because ``"path_proven" != "proven"``.

    This is the key distinction: a file existing at HEAD is NOT equivalent
    to a machine-verified test (named_test/trial_adoption) that actually
    exercises the capability. The gate stays shut until at least one
    high-confidence proven ref is recorded.
    """
    from app.capability_registry import (
        capability_need_blocked_by_proof_gate,
        working_capability_proof_status,
    )
    from app.problem_model import Capability, ProblemModel

    # ── Phase 1: backing_path-only capability classifies as path_proven ──
    model = ProblemModel(
        capabilities=[
            Capability(
                name="Container runtime",
                status="working",
                evidence_refs=[_backing_path_ref()],
            ),
        ]
    )
    proof = working_capability_proof_status(model)
    assert proof == {"container-runtime": "path_proven"}, (
        "backing_path-only evidence must classify as 'path_proven', not 'proven'"
    )

    need = {"kind": "capability", "ref": "container-runtime"}

    # ── Phase 2: gate blocks path_proven under enforce ──
    assert (
        capability_need_blocked_by_proof_gate(need, proof, mode="enforce") is True
    ), "path_proven must be gated under enforce (only 'proven' clears the gate)"

    # The blocked-resume sweep routes the need to residual (stays BLOCKED).
    disp = _classify_capability_need(
        "container-runtime", proof, proof_gate_mode="enforce"
    )
    assert disp["residual"] == [
        {"kind": "capability", "ref": "container-runtime"}
    ], "backing_path-only capability must stay residual (gated) under enforce"
    assert disp["completed_needs"] == []

    # ── Phase 3: under observe/off mode, path_proven still satisfies ──
    disp_observe = _classify_capability_need(
        "container-runtime", proof, proof_gate_mode="observe"
    )
    assert disp_observe["completed_needs"] == [
        {"kind": "capability", "ref": "container-runtime"}
    ], "observe mode must still satisfy (never blocks)"

    disp_off = _classify_capability_need(
        "container-runtime", proof, proof_gate_mode="off"
    )
    assert disp_off["completed_needs"] == [
        {"kind": "capability", "ref": "container-runtime"}
    ], "off mode must still satisfy (never blocks)"


# ---------------------------------------------------------------------------
# End-to-end enforce-gate tests: both consumer seams
# ---------------------------------------------------------------------------


def test_enforce_gate_blocks_and_unblocks_at_blocked_resume_seam():
    """ACCEPTANCE EVIDENCE (findings test:end-to-end-enforce-gate-blocking-on-both-consumers
    and test:end-to-end-integration-test-for-capability-proof-gate-enforcement-cycle).

    Blocked-resume seam: full proof-status transition under enforce mode.
    Phase 1: an unproven working capability routes the need to residual (stays BLOCKED).
    Phase 2: after proof is recorded (evidence_refs with proven=True), the same need
    routes to completed_needs (task can resume).

    This exercises _classify_blocker_needs end-to-end — the actual function called by
    the reaper blocked-resume sweep — with a proof-status transition, not just a
    predicate toggle.

    The spawn-time seam is covered by
    test_working_registry_capability_prerequisite_blocks_under_enforce in
    test_research_completion.py.
    """
    import asyncio
    from app import blocked_task_unblock_sweeps as btu
    from app.capability_registry import working_capability_proof_status

    # ── Phase 1: unproven working capability → residual (stays BLOCKED) ──
    unproven_model = {
        "capabilities": [
            {"name": "my-cap", "status": "working"},
        ]
    }
    proof_unproven = working_capability_proof_status(unproven_model)
    assert proof_unproven.get("my-cap") == "unproven"

    task = _capability_blocker_task("my-cap")
    pid = task.project_id
    disp_blocked = asyncio.run(
        btu._classify_blocker_needs(
            task,
            None,
            candidate_cache={pid: []},
            completed_cache={pid: []},
            dead_cache={pid: []},
            knowledge_cache={pid: []},
            registry_cache={pid: proof_unproven},
            proof_gate_mode="enforce",
        )
    )
    assert disp_blocked["residual"] == [{"kind": "capability", "ref": "my-cap"}], (
        "unproven working capability must route to residual (stay BLOCKED) under enforce"
    )
    assert disp_blocked["completed_needs"] == []

    # ── Phase 2: proof recorded → completed_needs (task resumes) ──
    proven_model = {
        "capabilities": [
            {
                "name": "my-cap",
                "status": "working",
                "evidence_refs": [
                    {"kind": "named_test", "ref": "test_foo::test_bar", "proven": True}
                ],
            }
        ]
    }
    proof_proven = working_capability_proof_status(proven_model)
    assert proof_proven.get("my-cap") == "proven"

    disp_resumed = asyncio.run(
        btu._classify_blocker_needs(
            task,
            None,
            candidate_cache={pid: []},
            completed_cache={pid: []},
            dead_cache={pid: []},
            knowledge_cache={pid: []},
            registry_cache={pid: proof_proven},
            proof_gate_mode="enforce",
        )
    )
    assert disp_resumed["completed_needs"] == [{"kind": "capability", "ref": "my-cap"}], (
        "after proof is recorded, the need must route to completed_needs (task resumes)"
    )
    assert disp_resumed["residual"] == []


# ---------------------------------------------------------------------------
# Auto-graduation sweep integration tests (DB-backed)
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture()
async def _grad_db():
    """In-memory SQLite session for auto-graduation sweep tests."""
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    async with engine.begin() as conn:
        await conn.run_sync(SQLModel.metadata.create_all)
    Session = sessionmaker(
        bind=engine,
        class_=AsyncSession,
        autocommit=False,
        autoflush=False,
        expire_on_commit=False,
    )
    async with Session() as s:
        yield s
    async with engine.begin() as conn:
        await conn.run_sync(SQLModel.metadata.drop_all)
    await engine.dispose()


@pytest.fixture(autouse=True)
def _clean_grad_config():
    """Restore auto-graduation runtime_config keys after each test."""
    from app.shared_state import runtime_config as _rc

    saved_enabled = _rc.get("capability_proof_gate_auto_graduation_enabled")
    saved_observe = _rc.get("capability_proof_gate_auto_graduation_observe_threshold")
    saved_enforce = _rc.get("capability_proof_gate_auto_graduation_enforce_threshold")
    saved_mode = _rc.get("capability_proof_gate_mode")
    yield
    if saved_enabled is not None:
        _rc["capability_proof_gate_auto_graduation_enabled"] = saved_enabled
    else:
        _rc.pop("capability_proof_gate_auto_graduation_enabled", None)
    if saved_observe is not None:
        _rc["capability_proof_gate_auto_graduation_observe_threshold"] = saved_observe
    else:
        _rc.pop("capability_proof_gate_auto_graduation_observe_threshold", None)
    if saved_enforce is not None:
        _rc["capability_proof_gate_auto_graduation_enforce_threshold"] = saved_enforce
    else:
        _rc.pop("capability_proof_gate_auto_graduation_enforce_threshold", None)
    if saved_mode is not None:
        _rc["capability_proof_gate_mode"] = saved_mode
    else:
        _rc.pop("capability_proof_gate_mode", None)


async def _create_project_with_caps(
    db: AsyncSession, caps: list[dict], proof_records: dict | None = None
) -> str:
    """Insert a project with the given capability list in problem_model and return its id.

    ``proof_records`` seeds the platform-only ``capability_proof_records`` ledger —
    the operator-recorded carve-out set the graduation sweep must honor.
    """
    from app.models import Project

    p = Project(
        name="grad-test",
        user_id="u1",
        problem_model={"capabilities": caps},
        capability_proof_records=proof_records or {},
    )
    db.add(p)
    await db.commit()
    await db.refresh(p)
    return str(p.id)


def _make_sweep_ctx(db: AsyncSession):
    """Build a SweepContext suitable for the auto-graduation check."""
    from app.metrics_integrity_sweep import SweepContext

    return SweepContext(
        session=db,
        now=datetime.utcnow(),
        mode="observe",
        max_projects=10,
        sample_limit=200,
        window_days=14,
    )


# ── compute_fleet_proof_coverage helper (shared with settings surface) ──────


@pytest.mark.asyncio
async def test_compute_fleet_proof_coverage_ratio_excludes_exempt(_grad_db):
    """The extracted coverage helper returns the same numerator/denominator the
    graduation sweep grades on: proven / non-exempt-working, with a mixed fleet
    yielding a fractional ratio."""
    from app.metrics_integrity_sweep import compute_fleet_proof_coverage

    await _create_project_with_caps(
        _grad_db,
        [
            {"name": "Cap A", "status": "working", "evidence_refs": [_proven_ref()]},
            {"name": "Cap B", "status": "working", "evidence_refs": []},
        ],
    )

    cov = await compute_fleet_proof_coverage(_grad_db, 10)
    assert cov["total_proven"] == 1
    assert cov["total_non_exempt_working"] == 2
    assert cov["coverage_ratio"] == 0.5
    assert cov["projects_contributing"] == 1
    assert cov["num_projects"] == 1


@pytest.mark.asyncio
async def test_compute_fleet_proof_coverage_null_ratio_when_no_denominator(_grad_db):
    """A fleet with no non-exempt working capabilities has an undefined ratio —
    the helper returns ``coverage_ratio=None`` rather than dividing by zero."""
    from app.metrics_integrity_sweep import compute_fleet_proof_coverage

    await _create_project_with_caps(
        _grad_db,
        [{"name": "Planned only", "status": "planned", "evidence_refs": []}],
    )

    cov = await compute_fleet_proof_coverage(_grad_db, 10)
    assert cov["coverage_ratio"] is None
    assert cov["total_non_exempt_working"] == 0
    assert cov["projects_contributing"] == 0
    assert cov["num_projects"] == 1


@pytest.mark.asyncio
async def test_compute_fleet_proof_coverage_empty_fleet(_grad_db):
    """No projects at all → undefined ratio and zero counts (num_projects=0)."""
    from app.metrics_integrity_sweep import compute_fleet_proof_coverage

    cov = await compute_fleet_proof_coverage(_grad_db, 10)
    assert cov["coverage_ratio"] is None
    assert cov["num_projects"] == 0
    assert cov["total_non_exempt_working"] == 0


# ── compute_trial_eligibility_projection (settings surface) ─────────────────


@pytest.mark.asyncio
async def test_compute_trial_eligibility_projection_counts_backed_unproven(_grad_db):
    """Caps with backing_paths that are unproven working are counted as
    trial-eligible; proven caps and caps with no backing_paths are not."""
    from app.metrics_integrity_sweep import compute_trial_eligibility_projection

    # One unproven cap with backing_paths (trial-eligible), one without.
    await _create_project_with_caps(
        _grad_db,
        [
            {
                "name": "Cap A",
                "status": "working",
                "backing_paths": ["backend/app/cap_a.py"],
            },
            {
                "name": "Cap B",
                "status": "working",
                "backing_paths": [],
            },
        ],
    )

    proj = await compute_trial_eligibility_projection(_grad_db, 10)
    assert proj["trial_eligible"] == 1, "only Cap A has backing_paths → 1 eligible"
    assert proj["unproven_working"] == 2, "both caps are unproven working"
    assert proj["projects_contributing"] == 1
    assert proj["num_projects"] == 1


@pytest.mark.asyncio
async def test_compute_trial_eligibility_projection_excludes_proven(_grad_db):
    """A cap that already has a named_test proven ref is NOT trial-eligible —
    it is counted as proven, not unproven, so it does not inflate the projection."""
    from app.metrics_integrity_sweep import compute_trial_eligibility_projection

    await _create_project_with_caps(
        _grad_db,
        [
            {
                "name": "Cap Proven",
                "status": "working",
                "backing_paths": ["backend/app/proven.py"],
                "evidence_refs": [_proven_ref()],
            },
        ],
    )

    proj = await compute_trial_eligibility_projection(_grad_db, 10)
    assert proj["trial_eligible"] == 0, "proven cap must not be trial-eligible"
    assert proj["unproven_working"] == 0


@pytest.mark.asyncio
async def test_compute_trial_eligibility_projection_empty_fleet(_grad_db):
    """An empty fleet yields zero counts and num_projects=0."""
    from app.metrics_integrity_sweep import compute_trial_eligibility_projection

    proj = await compute_trial_eligibility_projection(_grad_db, 10)
    assert proj["trial_eligible"] == 0
    assert proj["unproven_working"] == 0
    assert proj["projects_contributing"] == 0
    assert proj["num_projects"] == 0


# ── Core acceptance-evidence test ──────────────────────────────────────────


@pytest.mark.asyncio
async def test_capability_proof_gate_graduation(_grad_db):
    """ACCEPTANCE EVIDENCE. Full auto-graduation cycle:
    - Set up projects with all-proven capabilities (100% coverage)
    - Enable auto-graduation with observe_threshold=0.5
    - Gate starts at 'off'
    - Run the sweep → advances to 'observe'
    - With 100% coverage and enforce_threshold=0.95 → advances to 'enforce'
    - Verify gate mode is now 'enforce'
    """
    from app.metrics_integrity_sweep import (
        CheckStatus,
        check_capability_proof_gate_auto_graduation,
    )
    from app.shared_state import runtime_config as _rc

    # Setup: all-proven capabilities
    await _create_project_with_caps(
        _grad_db,
        [
            {"name": "Cap A", "status": "working", "evidence_refs": [_proven_ref()]},
            {"name": "Cap B", "status": "working", "evidence_refs": [_proven_ref()]},
        ],
    )

    # Configure auto-graduation
    _rc["capability_proof_gate_auto_graduation_enabled"] = True
    _rc["capability_proof_gate_auto_graduation_observe_threshold"] = 0.5
    _rc["capability_proof_gate_auto_graduation_enforce_threshold"] = 0.95
    _rc["capability_proof_gate_mode"] = "off"

    ctx = _make_sweep_ctx(_grad_db)

    # Step 1: off → observe (coverage = 2/2 = 1.0 >= 0.5)
    results = await check_capability_proof_gate_auto_graduation(ctx)
    assert len(results) == 1
    assert results[0].status == CheckStatus.FLAGGED
    assert "off → observe" in results[0].detail
    assert _rc["capability_proof_gate_mode"] == "observe"

    # Step 2: observe → enforce (coverage still 1.0 >= 0.95)
    results2 = await check_capability_proof_gate_auto_graduation(ctx)
    assert len(results2) == 1
    assert results2[0].status == CheckStatus.FLAGGED
    assert "observe → enforce" in results2[0].detail
    assert _rc["capability_proof_gate_mode"] == "enforce"

    # Step 3: already at enforce — no-op
    results3 = await check_capability_proof_gate_auto_graduation(ctx)
    assert len(results3) == 1
    assert results3[0].status == CheckStatus.OK
    assert "already at enforce" in results3[0].detail
    assert _rc["capability_proof_gate_mode"] == "enforce"


# ── Operator notification on advance (ACCEPTANCE EVIDENCE) ───────────────────


@pytest.mark.asyncio
async def test_auto_graduation_emits_operator_message_on_advance(_grad_db):
    """ACCEPTANCE EVIDENCE. Each auto-graduation advance routes an
    operator-visible ProjectMessage through the unified message-audience system:

    - off → observe emits ``capability_proof_gate_graduated_observe`` (operator-fyi)
    - observe → enforce emits ``capability_proof_gate_graduated_enforce``
      (operator-action-needed, since enforce begins blocking)
    - a no-op run (already enforce) emits nothing new
    """
    from sqlalchemy import select as _select

    from app.message_audience import (
        OPERATOR_ACTION_NEEDED,
        OPERATOR_FYI,
    )
    from app.metrics_integrity_sweep import (
        CheckStatus,
        check_capability_proof_gate_auto_graduation,
    )
    from app.models import ProjectMessage
    from app.shared_state import runtime_config as _rc

    await _create_project_with_caps(
        _grad_db,
        [
            {"name": "Cap A", "status": "working", "evidence_refs": [_proven_ref()]},
            {"name": "Cap B", "status": "working", "evidence_refs": [_proven_ref()]},
        ],
    )

    _rc["capability_proof_gate_auto_graduation_enabled"] = True
    _rc["capability_proof_gate_auto_graduation_observe_threshold"] = 0.5
    _rc["capability_proof_gate_auto_graduation_enforce_threshold"] = 0.95
    _rc["capability_proof_gate_mode"] = "off"

    ctx = _make_sweep_ctx(_grad_db)

    async def _messages() -> list[ProjectMessage]:
        rows = (await _grad_db.execute(_select(ProjectMessage))).scalars().all()
        return list(rows)

    # Step 1: off → observe posts an operator-fyi FYI message.
    results = await check_capability_proof_gate_auto_graduation(ctx)
    assert results[0].status == CheckStatus.FLAGGED
    msgs = await _messages()
    observe_msgs = [
        m
        for m in msgs
        if (m.context or {}).get("event_type")
        == "capability_proof_gate_graduated_observe"
    ]
    assert len(observe_msgs) == 1
    om = observe_msgs[0]
    assert om.from_type == "system"
    assert (om.context or {}).get("audience") == OPERATOR_FYI
    assert (om.context or {}).get("from_mode") == "off"
    assert (om.context or {}).get("to_mode") == "observe"
    assert "off → observe" in om.content

    # Step 2: observe → enforce posts an operator-action-needed message that
    # names the new blocking behavior.
    results2 = await check_capability_proof_gate_auto_graduation(ctx)
    assert results2[0].status == CheckStatus.FLAGGED
    enforce_msgs = [
        m
        for m in await _messages()
        if (m.context or {}).get("event_type")
        == "capability_proof_gate_graduated_enforce"
    ]
    assert len(enforce_msgs) == 1
    em = enforce_msgs[0]
    assert (em.context or {}).get("audience") == OPERATOR_ACTION_NEEDED
    assert (em.context or {}).get("to_mode") == "enforce"
    assert "BLOCKS" in em.content

    # Step 3: already at enforce — no-op, no new graduation messages.
    total_before = len(await _messages())
    results3 = await check_capability_proof_gate_auto_graduation(ctx)
    assert results3[0].status == CheckStatus.OK
    assert len(await _messages()) == total_before


# ── Disabled flag ──────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_auto_graduation_noop_when_disabled(_grad_db):
    """Auto-graduation returns OK immediately when the feature flag is disabled."""
    from app.metrics_integrity_sweep import (
        CheckStatus,
        check_capability_proof_gate_auto_graduation,
    )
    from app.shared_state import runtime_config as _rc

    await _create_project_with_caps(
        _grad_db,
        [{"name": "Cap A", "status": "working", "evidence_refs": [_proven_ref()]}],
    )

    _rc["capability_proof_gate_auto_graduation_enabled"] = False
    _rc["capability_proof_gate_mode"] = "off"

    ctx = _make_sweep_ctx(_grad_db)
    results = await check_capability_proof_gate_auto_graduation(ctx)
    assert len(results) == 1
    assert results[0].status == CheckStatus.OK
    assert "disabled" in results[0].detail.lower()
    # Gate mode unchanged
    assert _rc["capability_proof_gate_mode"] == "off"


# ── Below threshold ────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_auto_graduation_noop_when_below_threshold(_grad_db):
    """Auto-graduation does NOT advance when coverage is below threshold."""
    from app.metrics_integrity_sweep import (
        CheckStatus,
        check_capability_proof_gate_auto_graduation,
    )
    from app.shared_state import runtime_config as _rc

    # One proven, one unproven → 50% coverage
    await _create_project_with_caps(
        _grad_db,
        [
            {"name": "Cap A", "status": "working", "evidence_refs": [_proven_ref()]},
            {"name": "Cap B", "status": "working", "evidence_refs": [_unproven_ref()]},
        ],
    )

    _rc["capability_proof_gate_auto_graduation_enabled"] = True
    _rc["capability_proof_gate_auto_graduation_observe_threshold"] = 0.51
    _rc["capability_proof_gate_mode"] = "off"

    ctx = _make_sweep_ctx(_grad_db)
    results = await check_capability_proof_gate_auto_graduation(ctx)
    assert len(results) == 1
    assert results[0].status == CheckStatus.OK
    assert "below threshold" in results[0].detail.lower()
    assert _rc["capability_proof_gate_mode"] == "off"


# ── Exempt capabilities excluded ───────────────────────────────────────────


@pytest.mark.asyncio
async def test_auto_graduation_excludes_exempt_capabilities(_grad_db):
    """Exempt capabilities are excluded from both numerator and denominator.
    With 1 proven non-exempt and 1 exempt → 100% non-exempt coverage."""
    from app.metrics_integrity_sweep import (
        CheckStatus,
        check_capability_proof_gate_auto_graduation,
    )
    from app.shared_state import runtime_config as _rc

    await _create_project_with_caps(
        _grad_db,
        [
            {"name": "Cap A", "status": "working", "evidence_refs": [_proven_ref()]},
            # Exempt working cap — excluded from coverage calc. Carries
            # operator provenance so the exemption is honored (a bare
            # proof_exempt flag is no longer sufficient).
            {
                "name": "Cap B (exempt)",
                "status": "working",
                "proof_exempt": True,
                "source": {"type": "user_message", "excerpt": "operator exempt"},
            },
        ],
    )

    _rc["capability_proof_gate_auto_graduation_enabled"] = True
    _rc["capability_proof_gate_auto_graduation_observe_threshold"] = 0.5
    _rc["capability_proof_gate_mode"] = "off"

    ctx = _make_sweep_ctx(_grad_db)
    results = await check_capability_proof_gate_auto_graduation(ctx)
    assert len(results) == 1
    # Coverage = 1/1 = 100% (exempt excluded) → advances
    assert results[0].status == CheckStatus.FLAGGED
    assert "off → observe" in results[0].detail
    assert _rc["capability_proof_gate_mode"] == "observe"


# ── Already at enforce is no-op ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_auto_graduation_noop_when_already_enforce(_grad_db):
    """When gate is already at enforce, the sweep returns OK immediately without
    changing any state."""
    from app.metrics_integrity_sweep import (
        CheckStatus,
        check_capability_proof_gate_auto_graduation,
    )
    from app.shared_state import runtime_config as _rc

    await _create_project_with_caps(
        _grad_db,
        [{"name": "Cap A", "status": "working", "evidence_refs": [_proven_ref()]}],
    )

    _rc["capability_proof_gate_auto_graduation_enabled"] = True
    _rc["capability_proof_gate_mode"] = "enforce"

    ctx = _make_sweep_ctx(_grad_db)
    results = await check_capability_proof_gate_auto_graduation(ctx)
    assert len(results) == 1
    assert results[0].status == CheckStatus.OK
    assert "already at enforce" in results[0].detail
    assert _rc["capability_proof_gate_mode"] == "enforce"


# ── Never regresses ────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_auto_graduation_never_regresses(_grad_db):
    """The sweep only advances, never dials back. When in enforce mode,
    any coverage ratio returns a no-op (not a regression to observe)."""
    from app.metrics_integrity_sweep import (
        CheckStatus,
        check_capability_proof_gate_auto_graduation,
    )
    from app.shared_state import runtime_config as _rc

    # Zero proven — if gate were "off", coverage would be 0%.
    await _create_project_with_caps(
        _grad_db,
        [{"name": "Cap A", "status": "working"}],  # no evidence_refs → unproven
    )

    _rc["capability_proof_gate_auto_graduation_enabled"] = True
    _rc["capability_proof_gate_mode"] = "enforce"

    ctx = _make_sweep_ctx(_grad_db)
    results = await check_capability_proof_gate_auto_graduation(ctx)
    assert len(results) == 1
    assert results[0].status == CheckStatus.OK
    assert "already at enforce" in results[0].detail
    # Gate mode stays enforce — never regresses to observe
    assert _rc["capability_proof_gate_mode"] == "enforce"


# ── Project with no working capabilities is excluded from the aggregate ────


@pytest.mark.asyncio
async def test_auto_graduation_skips_when_no_working_caps(_grad_db):
    """A project contributing no ``working`` capabilities is excluded from the
    aggregate; with nothing to grade, the sweep returns SKIPPED rather than
    advancing the gate off a zero denominator."""
    from app.metrics_integrity_sweep import (
        CheckStatus,
        check_capability_proof_gate_auto_graduation,
    )
    from app.shared_state import runtime_config as _rc

    await _create_project_with_caps(
        _grad_db,
        [
            {"name": "Planned only", "status": "planned", "evidence_refs": []},
        ],
    )

    _rc["capability_proof_gate_auto_graduation_enabled"] = True
    _rc["capability_proof_gate_mode"] = "off"

    ctx = _make_sweep_ctx(_grad_db)
    results = await check_capability_proof_gate_auto_graduation(ctx)
    assert len(results) == 1
    assert results[0].status == CheckStatus.SKIPPED
    assert "no non-exempt" in results[0].detail.lower()
    assert _rc["capability_proof_gate_mode"] == "off"


@pytest.mark.asyncio
async def test_auto_graduation_does_not_exclude_a_self_declared_exemption(_grad_db):
    """A DECLARED ``proof_exempt`` no longer excludes a capability from the coverage
    aggregate (finding #9).

    The graduation sweep reads the problem_model tier-1 map, where an attributed
    ``proof_exempt`` declaration still grades ``proven`` (that is what keeps the earn
    loop from chasing a capability the strategist says cannot be machine-proven). It
    is NOT, however, an exemption: ``exempt_capability_names`` now requires an
    operator record in the platform-only ledger, so the capability is no longer
    excluded from the denominator — it is simply counted, and the fleet advances
    toward ENFORCE, which is the safe direction. At the gate itself the very same
    capability is refused (see
    ``test_agent_fabricated_exemption_provenance_blocks_blocked_resume``), so a
    self-declared exemption buys an agent nothing anywhere.
    """
    from app.metrics_integrity_sweep import (
        CheckStatus,
        check_capability_proof_gate_auto_graduation,
    )
    from app.shared_state import runtime_config as _rc

    await _create_project_with_caps(
        _grad_db,
        [
            {
                "name": "Exempt only",
                "status": "working",
                "proof_exempt": True,
                "source": {"type": "user_message", "excerpt": "operator exempt"},
            },
        ],
    )

    _rc["capability_proof_gate_auto_graduation_enabled"] = True
    _rc["capability_proof_gate_auto_graduation_observe_threshold"] = 0.5
    _rc["capability_proof_gate_mode"] = "off"

    ctx = _make_sweep_ctx(_grad_db)
    results = await check_capability_proof_gate_auto_graduation(ctx)
    assert len(results) == 1
    assert results[0].status == CheckStatus.FLAGGED
    assert "off → observe" in results[0].detail
    assert _rc["capability_proof_gate_mode"] == "observe"


@pytest.mark.asyncio
async def test_auto_graduation_honors_operator_platform_exemption(_grad_db):
    """ACCEPTANCE EVIDENCE (bug fix). A PLATFORM-recorded operator exemption is
    honored by the graduation sweep: an unprovable-but-exempted working capability
    is excluded from the coverage denominator, so a fleet of {1 proven + 1 exempted}
    reaches 100% non-exempt coverage and graduates all the way to ENFORCE.

    Before the fix, ``check_capability_proof_gate_auto_graduation`` selected only
    ``Project.problem_model`` and called ``exempt_capability_names(pm)`` with the
    ledger defaulting to None (an EMPTY ledger), so the operator's exemption was
    invisible: the unprovable cap graded ``unproven`` and counted against the
    denominator, pinning coverage at 50% and stalling the gate at ``observe``
    forever. The fix loads ``Project.capability_proof_records`` and threads it into
    both ``working_capability_proof_status`` and ``exempt_capability_names``.

    The exempted capability carries NO agent-writable ``proof_exempt`` flag and NO
    evidence — the ONLY thing clearing it is the platform-ledger operator-exemption
    record. This is what distinguishes the fix from the pre-existing self-declared
    path (``test_auto_graduation_does_not_exclude_a_self_declared_exemption``).
    """
    from app.capability_registry import build_operator_exemption_record
    from app.metrics_integrity_sweep import (
        CheckStatus,
        check_capability_proof_gate_auto_graduation,
    )
    from app.shared_state import runtime_config as _rc

    await _create_project_with_caps(
        _grad_db,
        [
            {"name": "Cap A", "status": "working", "evidence_refs": [_proven_ref()]},
            # A genuinely unprovable working cap: no evidence, no proof_exempt flag.
            # Only the platform ledger exempts it.
            {"name": "Human Review Cap", "status": "working"},
        ],
        proof_records={
            "human-review-cap": [
                build_operator_exemption_record(
                    "human-review-cap",
                    authorized_by="operator@example.com",
                    reason="cannot be machine-verified",
                )
            ]
        },
    )

    _rc["capability_proof_gate_auto_graduation_enabled"] = True
    _rc["capability_proof_gate_auto_graduation_observe_threshold"] = 0.5
    _rc["capability_proof_gate_auto_graduation_enforce_threshold"] = 0.95
    _rc["capability_proof_gate_mode"] = "off"

    ctx = _make_sweep_ctx(_grad_db)

    # Step 1: off → observe. Coverage = 1 proven / 1 non-exempt = 100% (the exempt
    # cap is excluded). Without the fix coverage would be 1/2 = 50%.
    results = await check_capability_proof_gate_auto_graduation(ctx)
    assert len(results) == 1
    assert results[0].status == CheckStatus.FLAGGED
    assert "off → observe" in results[0].detail
    assert _rc["capability_proof_gate_mode"] == "observe"

    # Step 2: observe → enforce. Coverage still 100% >= 0.95. This step is the
    # discriminator: without the ledger threaded in, coverage is 50% < 0.95 and the
    # gate would stall at observe forever — the exact stall this fix removes.
    results2 = await check_capability_proof_gate_auto_graduation(ctx)
    assert len(results2) == 1
    assert results2[0].status == CheckStatus.FLAGGED
    assert "observe → enforce" in results2[0].detail
    assert _rc["capability_proof_gate_mode"] == "enforce"


# ---------------------------------------------------------------------------
# Spawn-time proof-gate enforcement — both consumer seams
# ---------------------------------------------------------------------------
# These tests verify that an unproven 'working' capability prerequisite parks
# a dependent BLOCKED under enforce mode at the spawn-time seam
# (_handle_research_completion → capability_need_blocked_by_proof_gate →
# enter_blocked_state) AND that the blocked-resume sweep releases it once
# proof is recorded — exercising the full end-to-end enforcement cycle.


@pytest_asyncio.fixture()
async def _spawn_db():
    """In-memory SQLite session for spawn-time proof-gate tests."""
    from sqlalchemy.dialects.sqlite.base import SQLiteTypeCompiler

    def _visit_JSONB(self, type_, **kw):
        return "TEXT"

    SQLiteTypeCompiler.visit_JSONB = _visit_JSONB

    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    async with engine.begin() as conn:
        await conn.run_sync(SQLModel.metadata.create_all)
    Session = sessionmaker(
        bind=engine,
        class_=AsyncSession,
        autocommit=False,
        autoflush=False,
        expire_on_commit=False,
    )
    async with Session() as s:
        yield s
    async with engine.begin() as conn:
        await conn.run_sync(SQLModel.metadata.drop_all)
    await engine.dispose()


async def _proof_project_and_task(
    db, direction_id: str, caps: list[dict], proof_records: dict | None = None
):
    """A project with custom capabilities + a research task ready to complete.

    ``caps`` populates ``problem_model.capabilities`` — the AGENT-WRITABLE view.
    ``proof_records`` populates ``capability_proof_records`` — the PLATFORM-ONLY
    ledger the enforce gate corroborates against. A capability is genuinely proven
    only when BOTH agree; a ``proven`` evidence_ref in ``caps`` with no matching
    ledger entry is a self-report and must not clear the gate (audit finding #4).
    """
    from app.models import Project, Task, TaskStatus

    direction = {
        "id": direction_id,
        "text": "Proof gate direction",
        "status": "active",
        "priority": "medium",
        "paths": [],
        "added_at": "2026-01-01T00:00:00",
    }
    project = Project(
        name=f"Proof gate {direction_id}",
        user_id="test-user-123",
        autonomous_enabled=True,
        directions=[direction],
        problem_model={"capabilities": caps},
        capability_proof_records=proof_records or {},
    )
    db.add(project)
    await db.commit()
    await db.refresh(project)

    import uuid as _uuid
    task = Task(
        project_id=project.id,
        title=f"Research: {direction['text']}",
        payloads=[
            {"type": "research", "directions": [direction], "base_branch": "main"}
        ],
        context={"research_depth": 0},
        status=TaskStatus.IN_PROGRESS,
        created_by="ORCHESTRATOR",
        vm_instance_id=f"agent-vm-{_uuid.uuid4().hex[:16]}",
    )
    db.add(task)
    await db.commit()
    await db.refresh(task)
    return project, direction, task


@pytest.mark.asyncio
async def test_enforce_gate_parks_child_blocked_at_spawn_time(_spawn_db, mocker):
    """ACCEPTANCE EVIDENCE (spawn-time seam). Enforce mode + an unproven 'working'
    capability in the registry MUST park its dependent BLOCKED at spawn time:
    capability_need_blocked_by_proof_gate returns True, enter_blocked_state is
    called with the capability need, and the child task status is BLOCKED.

    Chain exercised:
      _handle_research_completion
        → working_capability_proof_status (unproven)
        → capability_need_blocked_by_proof_gate (enforce → True)
        → enter_blocked_state (BLOCKED with needs=[{"kind":"capability","ref":"..."}])
    """
    from app.research_completion import _handle_research_completion
    from app.models import Task as _T, TaskStatus
    from app.task_blocked import get_blocker_needs
    from app.shared_state import runtime_config as _rc
    from sqlmodel import select

    # Set enforce mode (restored by the autouse _clean_grad_config fixture).
    _rc["capability_proof_gate_mode"] = "enforce"

    caps = [{"name": "Container runtime", "status": "working"}]  # no evidence_refs → unproven
    _project, direction, task = await _proof_project_and_task(
        _spawn_db, "d-pgspawn01", caps
    )

    # Build a finding whose prerequisite is the unproven working capability.
    finding = {
        "title": "Run containerized experiment",
        "description": "Description: Run containerized experiment",
        "prompt": "Implement: Run containerized experiment",
        "priority": "medium",
        "scope": "small",
        "risk": "low",
        "type": "improvement",
        "direction_id": direction["id"],
        "prerequisite": {"kind": "capability", "ref": "container-runtime"},
    }

    # Patch all external side-effects so the handler runs cleanly in SQLite.
    async def _passthrough(task, *_a, **_kw):
        return task

    mocker.patch("app.task_completion._ensure_branch", new=_passthrough)
    mocker.patch("app.broadcast.broadcast_to_project", new=AsyncMock(return_value=None))
    mocker.patch("app.research_completion._validate_plan_dependencies", return_value=True)
    mocker.patch("app.research_completion._log_file_overlaps", return_value=[])
    mocker.patch("app.max_plan_pool._all_accounts", return_value=[])
    mocker.patch("app.research_completion._apply_structural_actionability_downgrade", return_value=0)
    mocker.patch("app.research_completion._is_high_stakes_finding", return_value=False)
    mocker.patch("app.research_completion._parse_research_findings", return_value=[finding])

    await _handle_research_completion(task, "", _spawn_db)

    children = (
        await _spawn_db.execute(select(_T).where(_T.parent_task_id == task.id))
    ).scalars().all()
    non_memo = [c for c in children if (c.first_payload or {}).get("type") != "memo"]
    assert len(non_memo) == 1, "exactly one non-memo child must be spawned"
    child = non_memo[0]

    assert child.status == TaskStatus.BLOCKED, (
        "an unproven working capability must gate the dependent BLOCKED under enforce"
    )
    needs = get_blocker_needs(child)
    assert needs == [{"kind": "capability", "ref": "container-runtime"}], (
        f"blocked need must name the unproven capability; got {needs!r}"
    )


@pytest.mark.asyncio
async def test_proven_cap_does_not_gate_at_spawn_time(_spawn_db, mocker):
    """Complement: a PROVEN working capability does NOT gate the dependent at spawn
    time — capability_need_blocked_by_proof_gate returns False so the child spawns
    QUEUED (not BLOCKED) even when enforce mode is active.

    "PROVEN" means PLATFORM-proven (audit finding #4): the ``proven`` evidence_ref
    below lives on the agent-writable problem_model, so on its own it is only a
    self-report and no longer clears the gate. What clears it is the corroborating
    entry in ``capability_proof_records`` — the ledger only the platform's proof
    recorders can write. Both are supplied here, which is what a genuinely earned
    capability looks like; ``test_self_declared_proven_ref_without_ledger_blocks_
    blocked_resume`` pins the case where the ledger is absent.
    """
    from app.research_completion import _handle_research_completion
    from app.models import Task as _T, TaskStatus
    from app.task_blocked import get_blocker_needs
    from app.shared_state import runtime_config as _rc
    from sqlmodel import select

    _rc["capability_proof_gate_mode"] = "enforce"

    caps = [
        {
            "name": "Container runtime",
            "status": "working",
            "evidence_refs": [_proven_ref()],
        }
    ]
    # The platform-recorded proof that corroborates the capability's claim.
    proof_records = {
        "container-runtime": [
            {
                "task_id": "T1",
                "proof_kind": "named_test",
                "proof_ref": "testing/test_x.py::test_x",
            }
        ]
    }
    _project, direction, task = await _proof_project_and_task(
        _spawn_db, "d-pgspawn02", caps, proof_records
    )

    finding = {
        "title": "Run containerized experiment",
        "description": "Description: Run containerized experiment",
        "prompt": "Implement: Run containerized experiment",
        "priority": "medium",
        "scope": "small",
        "risk": "low",
        "type": "improvement",
        "direction_id": direction["id"],
        "prerequisite": {"kind": "capability", "ref": "container-runtime"},
    }

    async def _passthrough(task, *_a, **_kw):
        return task

    mocker.patch("app.task_completion._ensure_branch", new=_passthrough)
    mocker.patch("app.broadcast.broadcast_to_project", new=AsyncMock(return_value=None))
    mocker.patch("app.research_completion._validate_plan_dependencies", return_value=True)
    mocker.patch("app.research_completion._log_file_overlaps", return_value=[])
    mocker.patch("app.max_plan_pool._all_accounts", return_value=[])
    mocker.patch("app.research_completion._apply_structural_actionability_downgrade", return_value=0)
    mocker.patch("app.research_completion._is_high_stakes_finding", return_value=False)
    mocker.patch("app.research_completion._parse_research_findings", return_value=[finding])

    await _handle_research_completion(task, "", _spawn_db)

    children = (
        await _spawn_db.execute(select(_T).where(_T.parent_task_id == task.id))
    ).scalars().all()
    non_memo = [c for c in children if (c.first_payload or {}).get("type") != "memo"]
    assert len(non_memo) == 1, "exactly one non-memo child must be spawned"
    child = non_memo[0]

    assert child.status != TaskStatus.BLOCKED, (
        "a proven working capability must NOT gate the dependent under enforce"
    )
    assert get_blocker_needs(child) == [], (
        "a proven capability prerequisite must not record a blocker need"
    )


@pytest.mark.asyncio
async def test_enforce_gate_full_cycle_blocked_then_resumed(_spawn_db, mocker):
    """ACCEPTANCE EVIDENCE (both seams). Full enforcement cycle:

    Phase 1 (spawn-time seam): an unproven working capability parks the dependent
    BLOCKED at research-completion time.

    Phase 2 (blocked-resume seam): after proof is recorded, the same need routes
    to ``completed_needs`` under the blocked-resume sweep's _classify_blocker_needs
    call — confirming the task would be unblocked.

    This is the machine-verified end-to-end evidence the direction's acceptance
    requirement asks for: the gate enforces at spawn AND releases on proof.
    """
    from app.research_completion import _handle_research_completion
    from app.models import Task as _T, TaskStatus
    from app.task_blocked import get_blocker_needs
    from app.shared_state import runtime_config as _rc
    from sqlmodel import select

    _rc["capability_proof_gate_mode"] = "enforce"

    caps = [{"name": "Container runtime", "status": "working"}]  # unproven
    _project, direction, task = await _proof_project_and_task(
        _spawn_db, "d-pgspawn03", caps
    )

    finding = {
        "title": "Run containerized experiment",
        "description": "Description: Run containerized experiment",
        "prompt": "Implement: Run containerized experiment",
        "priority": "medium",
        "scope": "small",
        "risk": "low",
        "type": "improvement",
        "direction_id": direction["id"],
        "prerequisite": {"kind": "capability", "ref": "container-runtime"},
    }

    async def _passthrough(task, *_a, **_kw):
        return task

    mocker.patch("app.task_completion._ensure_branch", new=_passthrough)
    mocker.patch("app.broadcast.broadcast_to_project", new=AsyncMock(return_value=None))
    mocker.patch("app.research_completion._validate_plan_dependencies", return_value=True)
    mocker.patch("app.research_completion._log_file_overlaps", return_value=[])
    mocker.patch("app.max_plan_pool._all_accounts", return_value=[])
    mocker.patch("app.research_completion._apply_structural_actionability_downgrade", return_value=0)
    mocker.patch("app.research_completion._is_high_stakes_finding", return_value=False)
    mocker.patch("app.research_completion._parse_research_findings", return_value=[finding])

    # ── Phase 1: spawn-time seam — unproven cap parks child BLOCKED ──
    await _handle_research_completion(task, "", _spawn_db)

    children = (
        await _spawn_db.execute(select(_T).where(_T.parent_task_id == task.id))
    ).scalars().all()
    non_memo = [c for c in children if (c.first_payload or {}).get("type") != "memo"]
    assert len(non_memo) == 1
    child = non_memo[0]
    assert child.status == TaskStatus.BLOCKED, (
        "Phase 1: unproven capability must park child BLOCKED at spawn time"
    )
    assert get_blocker_needs(child) == [
        {"kind": "capability", "ref": "container-runtime"}
    ], "Phase 1: blocked need must name the unproven capability"

    # ── Phase 2: provider acceptance records proof; resume releases consumer ──
    # Exercise the real producer rather than substituting a prebuilt proof map:
    # a completed provider advertises the capability and carries the platform's
    # green named-test acceptance verdict.  The recorder writes both the durable
    # ledger entry and the matching evidence ref on the registry capability.
    from app.capability_proof import record_capability_proof
    from app.capability_registry import consumer_gate_proof_status

    provider = _T(
        project_id=_project.id,
        title="Provider: container runtime",
        status=TaskStatus.COMPLETED,
        created_by="ORCHESTRATOR",
        payloads=[{"type": "create"}],
        context={
            "provides_capability": "container-runtime",
            "code_acceptance_verdict": {
                "proven": True,
                "kind": "named_test",
                "proof_ref": "backend/testing/test_container.py::test_runtime",
            },
        },
    )
    recorded = record_capability_proof(provider, _project)
    assert recorded == ["container-runtime"]
    assert _project.capability_proof_records["container-runtime"][0]["proof_kind"] == "named_test"

    # This is the same ledger-aware map the blocked-resume sweep receives;
    # it proves that acceptance recording, rather than a self-authored claim,
    # releases the consumer under enforce mode.
    proven_proof_map = consumer_gate_proof_status(
        _project.problem_model, _project.capability_proof_records
    )
    assert proven_proof_map["container-runtime"] == "proven"
    disp = await _classify_capability_need_async(
        "container-runtime", proven_proof_map, proof_gate_mode="enforce"
    )
    assert disp["completed_needs"] == [
        {"kind": "capability", "ref": "container-runtime"}
    ], "Phase 2: proven capability must route need to completed_needs (task resumes)"
    assert disp["residual"] == [], (
        "Phase 2: no residual needs — the task would be unblocked by the sweep"
    )
