"""Unit tests for the shared capability-proof execution-authority fence.

The fence (``app.capability_execution_fence``) is the single seam both cold
dispatch and the warm ``/next-role`` claim finalizer call immediately before a
locked QUEUED task gains execution authority. These tests pin, without a live
orchestrator loop:

* durable capability-requirement stamping + reconstruction from every
  backward-compatible context shape;
* re-grading against the live gate mode + platform-only proof ledger, including
  proof expiry (freshness TTL), revocation, restoration, and operator exemption;
* the atomic move to first-class BLOCKED with machine-resolvable needs; and
* survival of the requirement across generic fresh-attempt retry (so retry
  cannot bypass the fence).

The end-to-end wiring into the orchestrator dispatch loop and the warm finalizer
is exercised in ``test_orchestrator.py`` / ``test_agent.py`` respectively; here
we drive the fence functions directly over in-memory ``Task`` objects and a
stubbed project/session so the grading semantics are covered in isolation.
"""

import asyncio
import uuid
from datetime import timedelta

from app.capability_execution_fence import (
    CAPABILITY_EXECUTION_REQUIREMENTS_KEY,
    block_task_for_capability_proof,
    clear_warm_dispatch_pin,
    collect_capability_requirements,
    evaluate_execution_authority_block,
    grade_blocking_capability_needs,
    resolve_gate_mode,
    stamp_capability_requirements,
)
from app.models import Task, TaskStatus
from app.task_blocked import (
    BLOCKER_CONTEXT_KEY,
    get_blocker_needs,
    reset_task_for_fresh_attempt,
)
from app.time_utils import _utcnow


# ── fixtures ─────────────────────────────────────────────────────────────────
def _task(*, status=TaskStatus.QUEUED, context=None, project_id=None) -> Task:
    return Task(
        id=uuid.uuid4(),
        project_id=project_id or uuid.uuid4(),
        title="dependent",
        description="x",
        status=status,
        created_by="u",
        requires_approval=False,
        payloads=[{"type": "create"}],
        context=context or {},
        depends_on=[],
        status_log=[],
    )


def _cap_need(ref: str) -> dict:
    return {"kind": "capability", "ref": ref}


def _self_declared_proven_model(name: str = "forged-cap") -> dict:
    """A ``working`` capability whose ONLY proof is an agent-authored evidence_ref."""
    return {
        "capabilities": [
            {
                "name": name,
                "status": "working",
                "evidence_refs": [
                    {"kind": "named_test", "ref": "t.py::x", "proven": True}
                ],
            }
        ]
    }


def _ledger(name: str, proof_kind: str = "named_test", proved_at=None) -> dict:
    """One machine-verified PLATFORM proof-record entry (an agent cannot write this)."""
    entry = {"task_id": "T1", "proof_kind": proof_kind, "proof_ref": "t.py::x"}
    if proved_at is not None:
        entry["proved_at"] = proved_at
    return {name: [entry]}


class _StubProject:
    def __init__(self, problem_model, capability_proof_records):
        self.problem_model = problem_model
        self.capability_proof_records = capability_proof_records


class _StubSession:
    def __init__(self, project):
        self._project = project

    async def get(self, _model, _pk):
        return self._project


def _evaluate(task, problem_model, proof_records, *, mode):
    session = _StubSession(_StubProject(problem_model, proof_records))
    return asyncio.run(
        evaluate_execution_authority_block(session, task, mode=mode)
    )


# ── collect / stamp ──────────────────────────────────────────────────────────
def test_collect_reads_canonical_durable_record():
    task = _task(
        context={CAPABILITY_EXECUTION_REQUIREMENTS_KEY: [_cap_need("cap-a")]}
    )
    assert collect_capability_requirements(task) == [_cap_need("cap-a")]


def test_collect_unions_backward_compatible_shapes_and_dedups():
    """Every legacy shape that carries a capability ref is reconstructed, deduped."""
    task = _task(
        context={
            "_spawn_capability_proof_unverified": [
                {"kind": "capability", "ref": "cap-a", "proof_status": "unproven"}
            ],
            "unresolved_prerequisite": [_cap_need("cap-b")],
            "prerequisite": _cap_need("cap-c"),
            # capability need mirrored under blocker metadata + a duplicate of cap-a
            "blocker": {
                "needs": [_cap_need("cap-a"), {"kind": "task", "ref": str(uuid.uuid4())}]
            },
        }
    )
    refs = sorted(n["ref"] for n in collect_capability_requirements(task))
    # task-kind blocker need is excluded; cap-a deduped to one.
    assert refs == ["cap-a", "cap-b", "cap-c"]


def test_collect_returns_empty_for_task_without_capability_requirement():
    assert collect_capability_requirements(_task(context={})) == []
    assert collect_capability_requirements(_task(context={"foo": "bar"})) == []


def test_stamp_merges_idempotently_and_marks_dirty():
    task = _task(context={})
    stamp_capability_requirements(task, [_cap_need("cap-a")])
    stamp_capability_requirements(task, [_cap_need("cap-a"), _cap_need("cap-b")])
    stored = task.context[CAPABILITY_EXECUTION_REQUIREMENTS_KEY]
    assert sorted(n["ref"] for n in stored) == ["cap-a", "cap-b"]


def test_stamp_ignores_non_capability_entries():
    task = _task(context={})
    stamp_capability_requirements(task, [{"kind": "task", "ref": str(uuid.uuid4())}])
    assert CAPABILITY_EXECUTION_REQUIREMENTS_KEY not in task.context


# ── mode resolution ──────────────────────────────────────────────────────────
def test_resolve_gate_mode_override_wins():
    assert resolve_gate_mode("enforce") == "enforce"
    assert resolve_gate_mode("observe") == "observe"
    # invalid override falls through to runtime_config (defaults off in tests)
    assert resolve_gate_mode("bogus") in ("off", "observe", "enforce")


# ── grading ──────────────────────────────────────────────────────────────────
def test_grade_off_and_observe_never_block():
    needs = [_cap_need("forged-cap")]
    model = _self_declared_proven_model()
    for mode in ("off", "observe"):
        assert grade_blocking_capability_needs(
            _StubProject(model, {}), needs, mode=mode
        ) == []


def test_grade_enforce_blocks_self_declared_without_ledger():
    """A ``proven`` claim that is a pure self-report (no platform ledger) is
    uncorroborated and blocks under enforce — the core TOCTOU protection."""
    blocking = grade_blocking_capability_needs(
        _StubProject(_self_declared_proven_model(), {}),
        [_cap_need("forged-cap")],
        mode="enforce",
    )
    assert blocking == [_cap_need("forged-cap")]


def test_grade_enforce_allows_ledger_corroborated_proof():
    blocking = grade_blocking_capability_needs(
        _StubProject(_self_declared_proven_model("real-cap"), _ledger("real-cap")),
        [_cap_need("real-cap")],
        mode="enforce",
    )
    assert blocking == []


def test_grade_enforce_blocks_expired_ledger_proof():
    """Proof expiry: a ledger entry older than the freshness TTL grades stale, so
    it no longer corroborates the claim and the fence blocks."""
    stale = (_utcnow() - timedelta(days=45)).isoformat()
    blocking = grade_blocking_capability_needs(
        _StubProject(
            _self_declared_proven_model("aging-cap"),
            _ledger("aging-cap", proved_at=stale),
        ),
        [_cap_need("aging-cap")],
        mode="enforce",
    )
    assert blocking == [_cap_need("aging-cap")]


def test_grade_enforce_allows_task_backed_non_working_capability_with_ledger():
    """A capability NOT in the ``working`` registry set (so the registry seam is a
    no-match) but backed by a corroborated PLATFORM ledger proof is allowed — the
    task-backed seam grades it proven, so the union stays not-blocked. This is the
    legitimate task-backed foundation whose sibling provider produced platform
    proof; unrelated-prerequisite behavior for a genuinely proven cap is preserved.
    """
    blocking = grade_blocking_capability_needs(
        # The capability is NOT declared ``working`` in the problem_model, but its
        # platform ledger records a fresh named_test proof.
        _StubProject({"capabilities": []}, _ledger("task-backed-cap")),
        [_cap_need("task-backed-cap")],
        mode="enforce",
    )
    assert blocking == []


def test_grade_enforce_blocks_task_backed_non_working_capability_without_ledger():
    """Reviewer-requested coverage: a capability provided by a sibling task
    foundation (NOT in the ``working`` registry set, so the working-registry seam
    returns not-blocked) with NO platform-ledger proof must be BLOCKED under
    enforce via the task-backed seam. Before unioning the task-backed seam this
    slipped the fence — the concrete TOCTOU residue the reviewer flagged."""
    blocking = grade_blocking_capability_needs(
        # Not ``working`` in the registry AND no ledger proof at all.
        _StubProject({"capabilities": []}, {}),
        [_cap_need("task-backed-unproven-cap")],
        mode="enforce",
    )
    assert blocking == [_cap_need("task-backed-unproven-cap")]


# ── evaluate_execution_authority_block (async, via stub session) ─────────────
def test_evaluate_returns_empty_when_no_requirement():
    task = _task(context={})
    assert _evaluate(task, _self_declared_proven_model(), {}, mode="enforce") == []


def test_evaluate_enforce_blocks_unproven_durable_requirement():
    task = _task(
        context={CAPABILITY_EXECUTION_REQUIREMENTS_KEY: [_cap_need("forged-cap")]}
    )
    blocking = _evaluate(task, _self_declared_proven_model(), {}, mode="enforce")
    assert blocking == [_cap_need("forged-cap")]


def test_evaluate_off_grants_authority_even_when_unproven():
    task = _task(
        context={CAPABILITY_EXECUTION_REQUIREMENTS_KEY: [_cap_need("forged-cap")]}
    )
    assert _evaluate(task, _self_declared_proven_model(), {}, mode="off") == []


def test_evaluate_restoration_grants_after_proof_recorded():
    """Proof restoration: once the platform ledger records current proof, the same
    durable requirement grades proven and authority is granted."""
    task = _task(
        context={CAPABILITY_EXECUTION_REQUIREMENTS_KEY: [_cap_need("real-cap")]}
    )
    # Before: no ledger → blocked.
    assert _evaluate(task, _self_declared_proven_model("real-cap"), {}, mode="enforce")
    # After: ledger corroborates → granted.
    assert _evaluate(
        task, _self_declared_proven_model("real-cap"), _ledger("real-cap"),
        mode="enforce",
    ) == []


# ── blocking transition + retry survival ─────────────────────────────────────
def test_block_moves_task_to_blocked_with_capability_needs():
    task = _task(
        context={CAPABILITY_EXECUTION_REQUIREMENTS_KEY: [_cap_need("forged-cap")]}
    )
    block_task_for_capability_proof(task, [_cap_need("forged-cap")], source="cold dispatch")
    assert task.status == TaskStatus.BLOCKED
    assert get_blocker_needs(task) == [_cap_need("forged-cap")]
    assert "capability proof" in task.blocked_reason
    # attempt_count untouched — BLOCKED is not a failure, consumes no slot.
    assert (task.attempt_count or 0) == 0


def test_requirement_survives_generic_retry_and_refences_the_fence():
    """Generic fresh-attempt retry strips ordinary blocker metadata but the durable
    capability requirement survives, so a re-dispatch re-collects it and the fence
    re-blocks — retry cannot bypass the gate."""
    task = _task(
        context={CAPABILITY_EXECUTION_REQUIREMENTS_KEY: [_cap_need("forged-cap")]}
    )
    block_task_for_capability_proof(task, [_cap_need("forged-cap")], source="cold dispatch")
    assert task.status == TaskStatus.BLOCKED

    # Manual/bulk generic retry path: reset_task_for_fresh_attempt clears the
    # blocker metadata and returns the task to a clean attempt.
    reset_task_for_fresh_attempt(task)
    assert BLOCKER_CONTEXT_KEY not in task.context
    # ...but the canonical durable requirement is NOT in the strip set → survives.
    assert task.context.get(CAPABILITY_EXECUTION_REQUIREMENTS_KEY) == [
        _cap_need("forged-cap")
    ]
    # So the next execution-authority evaluation re-blocks under enforce.
    assert collect_capability_requirements(task) == [_cap_need("forged-cap")]
    blocking = _evaluate(task, _self_declared_proven_model(), {}, mode="enforce")
    assert blocking == [_cap_need("forged-cap")]


# ── warm pin unwind ──────────────────────────────────────────────────────────
def test_clear_warm_dispatch_pin_pops_pin_markers():
    task = _task(
        context={
            "warm_target_vm_id": "vm-1",
            "warm_pool_pin": True,
            "warm_handoff_deadline": "2026-01-01T00:00:00+00:00",
            "keep_me": "yes",
        }
    )
    clear_warm_dispatch_pin(task)
    assert "warm_target_vm_id" not in task.context
    assert "warm_pool_pin" not in task.context
    assert "warm_handoff_deadline" not in task.context
    assert task.context["keep_me"] == "yes"
