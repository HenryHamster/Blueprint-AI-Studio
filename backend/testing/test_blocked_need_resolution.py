"""Unit tests for the pure cross-task NEED resolver.

These exercise ``blocked_need_resolution.resolve_blocked_need`` as a pure matcher
over in-memory ``Task`` objects (no DB / no session) — the sweep integration is
covered separately in ``test_reaper.py``.
"""

import uuid
from datetime import datetime, timedelta

from app.blocked_need_resolution import (
    completed_foundation_for_need,
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


def test_knowledge_finding_hash_mismatch_returns_false():
    from app.blocked_need_resolution import knowledge_substance_satisfies_need
    from app.knowledge_query import KnowledgeSubstance

    substance = [KnowledgeSubstance(finding_hash="aaaa1111", topic="some-topic")]
    need = {"kind": "task", "ref": "bbbb2222"}
    assert knowledge_substance_satisfies_need(need, substance) is False


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
