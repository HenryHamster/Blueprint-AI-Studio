"""Unit tests for the pure cross-task NEED resolver.

These exercise ``blocked_need_resolution.resolve_blocked_need`` as a pure matcher
over in-memory ``Task`` objects (no DB / no session) — the sweep integration is
covered separately in ``test_reaper.py``.
"""

import uuid

from app.blocked_need_resolution import (
    need_matched_dead_foundation,
    need_satisfied_by_completed,
    resolve_blocked_need,
)
from app.models import Task, TaskStatus


def _task(*, status=TaskStatus.IN_PROGRESS, title="cand", payloads=None,
          context=None, depends_on=None, tid=None) -> Task:
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
        status_log=[],
    )


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


def test_task_need_malformed_ref_returns_none():
    blocked = _task(status=TaskStatus.BLOCKED)
    need = {"kind": "task", "ref": "not-a-uuid"}
    assert resolve_blocked_need(blocked, need, [_task()]) is None


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


def test_pr_need_ambiguous_returns_none():
    a = _task(payloads=[{"type": "fix", "pr_number": 7}])
    b = _task(payloads=[{"type": "create", "pr_number": 7}])
    blocked = _task(status=TaskStatus.BLOCKED)
    need = {"kind": "pr", "ref": "7"}
    assert resolve_blocked_need(blocked, need, [a, b]) is None


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


def test_capability_ambiguous_returns_none():
    a = _task(payloads=[{"type": "design"}])
    b = _task(title="design")
    blocked = _task(status=TaskStatus.BLOCKED)
    need = {"kind": "capability", "ref": "design"}
    assert resolve_blocked_need(blocked, need, [a, b]) is None


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


def test_unknown_kind_returns_none():
    blocked = _task(status=TaskStatus.BLOCKED)
    need = {"kind": "branch", "ref": "main"}
    assert resolve_blocked_need(blocked, need, [_task()]) is None


# ── need_satisfied_by_completed (parked-resume against landed work) ───────────

def test_completed_task_need_satisfied_by_merged_foundation():
    done = _task(status=TaskStatus.COMPLETED)
    blocked = _task(status=TaskStatus.BLOCKED)
    need = {"kind": "task", "ref": str(done.id)}
    assert need_satisfied_by_completed(blocked, need, [done]) is True


def test_completed_pr_need_satisfied_by_owning_completed_task():
    done = _task(status=TaskStatus.COMPLETED, payloads=[{"type": "fix", "pr_number": 8123}])
    blocked = _task(status=TaskStatus.BLOCKED)
    need = {"kind": "pr", "ref": "8123"}
    assert need_satisfied_by_completed(blocked, need, [done]) is True


def test_completed_need_unsatisfied_returns_false():
    done = _task(status=TaskStatus.COMPLETED)
    blocked = _task(status=TaskStatus.BLOCKED)
    need = {"kind": "task", "ref": str(uuid.uuid4())}
    assert need_satisfied_by_completed(blocked, need, [done]) is False
    # Malformed / empty inputs are conservatively unsatisfied.
    assert need_satisfied_by_completed(blocked, {}, [done]) is False
    assert need_satisfied_by_completed(blocked, need, []) is False


def test_completed_need_never_self_satisfies():
    blocked = _task(status=TaskStatus.BLOCKED)
    need = {"kind": "task", "ref": str(blocked.id)}
    # The blocked task itself, even if somehow in the completed set, is excluded.
    assert need_satisfied_by_completed(blocked, need, [blocked]) is False


# ── need_matched_dead_foundation (provably-dead foundation) ───────────────────

def test_dead_task_need_matches_failed_foundation():
    dead = _task(status=TaskStatus.FAILED)
    blocked = _task(status=TaskStatus.BLOCKED)
    need = {"kind": "task", "ref": str(dead.id)}
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
