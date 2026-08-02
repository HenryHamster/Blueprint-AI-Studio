"""Tests for the cancelled-child orphan-PR filtering in rollup_management.

A child task that was CANCELLED (e.g. requeue-replaced after a timeout, or
SCOPE_EMPTY pre-empted) can leave its PR open. No live task will ever drive it
to merge, so blocking the parent's rollup on that PR strands the parent in
EVALUATING forever (incident 2026-05-28: an orphan PR from a
cancelled, retry-replaced child wedged a research rollup — the cancel path left
the PR open and it carried 0 CI checks, so the merge gate never fired either).

These cover the pure attribution + partition helpers; the GitHub-close + gate
wiring lives in _check_and_trigger_rollup and is exercised by the evaluating
suite.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace

from app.models import TaskStatus
from app.rollup_management import (
    _branch_task_id_prefix,
    _classify_prs_authoritative,
    _extract_task_pr_numbers,
    _is_owner_chain_live,
    _partition_orphan_prs,
)
from app.shared_state import derive_task_branch


# ── Helper factories ──────────────────────────────────────────────────────────

def _task(task_id_hex: str, status: TaskStatus, context: dict | None = None) -> SimpleNamespace:
    """Create a lightweight mock task for unit tests."""
    return SimpleNamespace(
        id=uuid.UUID(f"{task_id_hex[:8]}-0000-0000-0000-000000000000"),
        status=status,
        context=context or {},
    )


def test_branch_task_id_prefix_canonical_agent_branch():
    """The trailing 8-hex token of a {namespace}/{prefix}-{8hex} branch is the
    owning task's id-prefix — and matches what derive_task_branch produced."""
    task_id = "db668612-7bf4-41be-89e1-3d892be07273"
    branch = derive_task_branch(
        task_id, "refactor", "blueprint"
    )  # -> blueprint/refactor-db668612
    assert branch == "blueprint/refactor-db668612"
    assert _branch_task_id_prefix(branch) == "db668612"


def test_branch_task_id_prefix_non_blueprint_namespace():
    """A custom namespace still resolves to the trailing id-prefix."""
    assert _branch_task_id_prefix("acme/fix-0a1b2c3d") == "0a1b2c3d"


def test_branch_task_id_prefix_rejects_non_id_suffixes():
    """Branches whose trailing token isn't an 8-hex task id (promotion / CI-fix
    / custom branches) return "" so they're never mis-attributed."""
    assert _branch_task_id_prefix("main") == ""
    assert _branch_task_id_prefix("automain") == ""
    assert _branch_task_id_prefix("blueprint/some-feature-name") == ""  # not 8 hex
    assert _branch_task_id_prefix("research/c4d3402e") == ""  # staging base itself
    assert _branch_task_id_prefix("blueprint/refactor-ZZZZZZZZ") == ""  # not hex
    assert _branch_task_id_prefix("") == ""
    assert _branch_task_id_prefix(None) == ""
    assert _branch_task_id_prefix("noslash") == ""


def _pr(number: int, head_ref: str) -> dict:
    return {"number": number, "head": {"ref": head_ref}}


def test_partition_orphan_prs_splits_cancelled_from_live():
    """PRs owned by a cancelled child are orphaned; everything else is live."""
    open_prs = [
        _pr(6666, "blueprint/refactor-db668612"),  # owned by cancelled child
        _pr(6700, "blueprint/fix-aaaa1111"),  # owned by a live child
        _pr(6701, "blueprint/create-bbbb2222"),  # unknown owner
    ]
    cancelled = {"db668612"}

    live, orphan = _partition_orphan_prs(open_prs, cancelled)

    assert [p["number"] for p in orphan] == [6666]
    assert sorted(p["number"] for p in live) == [6700, 6701]


def test_partition_orphan_prs_conservative_when_no_cancelled():
    """With no cancelled children, every PR is kept live — never close a PR we
    can't prove belongs to a dead task."""
    open_prs = [
        _pr(1, "blueprint/refactor-db668612"),
        _pr(2, "blueprint/fix-aaaa1111"),
    ]
    live, orphan = _partition_orphan_prs(open_prs, set())
    assert orphan == []
    assert [p["number"] for p in live] == [1, 2]


def test_partition_orphan_prs_unattributable_branch_stays_live():
    """A PR whose head ref doesn't resolve to a task-id prefix is never treated
    as an orphan, even if some child is cancelled."""
    open_prs = [_pr(9, "main"), _pr(10, "blueprint/some-feature")]
    live, orphan = _partition_orphan_prs(open_prs, {"db668612"})
    assert orphan == []
    assert [p["number"] for p in live] == [9, 10]


def test_partition_orphan_prs_handles_missing_head_ref():
    """Malformed PR dicts (no head/ref) don't crash and are kept live."""
    open_prs = [{"number": 5}, {"number": 6, "head": {}}]
    live, orphan = _partition_orphan_prs(open_prs, {"db668612"})
    assert orphan == []
    assert [p["number"] for p in live] == [5, 6]


# ── _extract_task_pr_numbers ──────────────────────────────────────────────────

def test_extract_pr_numbers_from_review_loop():
    t = _task("aaaaaaaa", TaskStatus.CANCELLED, {"review_loop": {"pr_number": 111}})
    assert _extract_task_pr_numbers(t) == frozenset({111})


def test_extract_pr_numbers_from_pr_url():
    t = _task("aaaaaaaa", TaskStatus.CANCELLED, {"_pr_url": "https://github.com/o/r/pull/222"})
    assert _extract_task_pr_numbers(t) == frozenset({222})


def test_extract_pr_numbers_union():
    t = _task("aaaaaaaa", TaskStatus.CANCELLED, {
        "review_loop": {"pr_number": 111},
        "pr_url": "https://github.com/o/r/pull/333",
    })
    assert _extract_task_pr_numbers(t) == frozenset({111, 333})


def test_extract_pr_numbers_empty():
    t = _task("aaaaaaaa", TaskStatus.CANCELLED, {})
    assert _extract_task_pr_numbers(t) == frozenset()


def test_extract_pr_numbers_from_rollup_pr_number():
    """rollup_pr_number (set for rollup parents in awaiting-approval / security-blocked)
    must be picked up so those PRs are not seen as unowned and left blocking forever."""
    t = _task("aaaaaaaa", TaskStatus.CANCELLED, {"rollup_pr_number": 444})
    assert _extract_task_pr_numbers(t) == frozenset({444})


def test_extract_pr_numbers_from_rollup_pr_url():
    t = _task("aaaaaaaa", TaskStatus.CANCELLED, {"rollup_pr_url": "https://github.com/o/r/pull/555"})
    assert _extract_task_pr_numbers(t) == frozenset({555})


def test_extract_pr_numbers_from_first_payload():
    t = _task("aaaaaaaa", TaskStatus.CANCELLED, {})
    t.first_payload = {"pr_number": 666}
    assert _extract_task_pr_numbers(t) == frozenset({666})


def test_extract_pr_numbers_rollup_union():
    """All canonical keys are unioned together."""
    t = _task("aaaaaaaa", TaskStatus.CANCELLED, {
        "review_loop": {"pr_number": 111},
        "rollup_pr_number": 777,
    })
    assert _extract_task_pr_numbers(t) == frozenset({111, 777})


# ── _is_owner_chain_live ──────────────────────────────────────────────────────

def test_owner_chain_live_non_terminal():
    t = _task("aaaaaaaa", TaskStatus.IN_PROGRESS)
    assert _is_owner_chain_live(t, {t.id: t}) is True


def test_owner_chain_live_completed_not_live():
    t = _task("aaaaaaaa", TaskStatus.COMPLETED)
    assert _is_owner_chain_live(t, {t.id: t}) is False


def test_owner_chain_live_cancelled_no_replacement():
    t = _task("aaaaaaaa", TaskStatus.CANCELLED, {})
    assert _is_owner_chain_live(t, {t.id: t}) is False


def test_owner_chain_live_cancelled_scope_empty_not_live():
    t = _task("aaaaaaaa", TaskStatus.CANCELLED, {"failure_category": "SCOPE_EMPTY"})
    assert _is_owner_chain_live(t, {t.id: t}) is False


def test_owner_chain_live_cancelled_with_live_replacement():
    replacement_id = "bbbbbbbb-0000-0000-0000-000000000000"
    replacement = SimpleNamespace(
        id=uuid.UUID(replacement_id),
        status=TaskStatus.IN_PROGRESS,
        context={},
    )
    original = _task("aaaaaaaa", TaskStatus.CANCELLED, {"_replaced_by_task_id": replacement_id})
    tasks = {original.id: original, replacement.id: replacement}
    assert _is_owner_chain_live(original, tasks) is True


def test_owner_chain_live_cancelled_with_dead_replacement():
    replacement_id = "bbbbbbbb-0000-0000-0000-000000000000"
    replacement = SimpleNamespace(
        id=uuid.UUID(replacement_id),
        status=TaskStatus.CANCELLED,
        context={},
    )
    original = _task("aaaaaaaa", TaskStatus.CANCELLED, {"_replaced_by_task_id": replacement_id})
    tasks = {original.id: original, replacement.id: replacement}
    assert _is_owner_chain_live(original, tasks) is False


def test_owner_chain_live_replacement_not_in_set_conservative():
    """If replacement ID is present but not loaded, treat as live (conservative)."""
    unknown_id = "cccccccc-0000-0000-0000-000000000000"
    original = _task("aaaaaaaa", TaskStatus.CANCELLED, {"_replaced_by_task_id": unknown_id})
    assert _is_owner_chain_live(original, {original.id: original}) is True


def test_owner_chain_live_failed_no_replacement_dead():
    """A genuinely FAILED owner with no replacement is dead-ended — not live.
    Regression: previously `not is_non_death_cancellation(FAILED)` evaluated as True
    (live), keeping the PR blocking forever."""
    t = _task("aaaaaaaa", TaskStatus.FAILED, {})
    assert _is_owner_chain_live(t, {t.id: t}) is False


def test_owner_chain_live_failed_with_live_replacement():
    """A FAILED owner whose _replaced_by_task_id points to a live task is live."""
    replacement_id = "bbbbbbbb-0000-0000-0000-000000000000"
    replacement = SimpleNamespace(
        id=uuid.UUID(replacement_id),
        status=TaskStatus.IN_PROGRESS,
        context={},
    )
    original = _task("aaaaaaaa", TaskStatus.FAILED, {"_replaced_by_task_id": replacement_id})
    tasks = {original.id: original, replacement.id: replacement}
    assert _is_owner_chain_live(original, tasks) is True


def test_owner_chain_live_failed_with_dead_replacement():
    """A FAILED owner whose replacement is also FAILED (no further chain) is dead."""
    replacement_id = "bbbbbbbb-0000-0000-0000-000000000000"
    replacement = SimpleNamespace(
        id=uuid.UUID(replacement_id),
        status=TaskStatus.FAILED,
        context={},
    )
    original = _task("aaaaaaaa", TaskStatus.FAILED, {"_replaced_by_task_id": replacement_id})
    tasks = {original.id: original, replacement.id: replacement}
    assert _is_owner_chain_live(original, tasks) is False


# ── _classify_prs_authoritative ───────────────────────────────────────────────

def test_classify_authoritative_staging_branch_all_dead_owners():
    """Core regression: a sub-rollup PR headed from plan/650abda7 (staging branch,
    no hyphen) is correctly identified as orphaned when all owners are dead-ended.
    The old regex returned '' for 'plan/650abda7' and kept the PR blocking forever."""
    child = _task("650abda7", TaskStatus.CANCELLED, {"review_loop": {"pr_number": 111}})
    open_prs = [_pr(111, "plan/650abda7")]

    blocking, orphaned = _classify_prs_authoritative(open_prs, [child])

    assert [p["number"] for p in orphaned] == [111]
    assert blocking == []


def test_classify_authoritative_staging_branch_shapes():
    """Works regardless of head branch shape: plan/hex, research/hex, custom."""
    child = _task("aaaa1111", TaskStatus.CANCELLED, {"review_loop": {"pr_number": 42}})
    for head_ref in ("plan/aaaa1111", "research/aaaa1111", "neurips/plan-aaaa1111", "main"):
        blocking, orphaned = _classify_prs_authoritative(
            [_pr(42, head_ref)], [child]
        )
        assert [p["number"] for p in orphaned] == [42], f"failed for head_ref={head_ref!r}"


def test_classify_authoritative_live_replacement_blocks():
    """Regression: CANCELLED original with a LIVE _replaced_by_task_id clone keeps PR blocking."""
    replacement_id = "703f7a93-0000-0000-0000-000000000000"
    original = _task("650abda7", TaskStatus.CANCELLED, {
        "review_loop": {"pr_number": 111},
        "_replaced_by_task_id": replacement_id,
    })
    replacement = SimpleNamespace(
        id=uuid.UUID(replacement_id),
        status=TaskStatus.IN_PROGRESS,
        context={},
    )
    open_prs = [_pr(111, "plan/650abda7")]

    blocking, orphaned = _classify_prs_authoritative(open_prs, [original, replacement])

    assert orphaned == []
    assert [p["number"] for p in blocking] == [111]


def test_classify_authoritative_no_owner_stays_blocking():
    """A PR with no recorded task owner (human-opened) stays blocking — conservative rule."""
    unrelated_child = _task("aaaaaaaa", TaskStatus.CANCELLED, {})  # owns no PR
    open_prs = [_pr(999, "main")]

    blocking, orphaned = _classify_prs_authoritative(open_prs, [unrelated_child])

    assert orphaned == []
    assert [p["number"] for p in blocking] == [999]


def test_classify_authoritative_mixed_prs():
    """Mixed set: one orphaned (all-dead owners), one live (active owner), one unowned."""
    dead_child = _task("650abda7", TaskStatus.CANCELLED, {"review_loop": {"pr_number": 111}})
    live_child = _task("bbbbbbbb", TaskStatus.IN_PROGRESS, {"review_loop": {"pr_number": 222}})
    open_prs = [
        _pr(111, "plan/650abda7"),   # owned by dead_child → orphaned
        _pr(222, "blueprint/fix-bbbbbbbb"),  # owned by live_child → blocking
        _pr(333, "main"),             # no owner → blocking
    ]

    blocking, orphaned = _classify_prs_authoritative(open_prs, [dead_child, live_child])

    assert [p["number"] for p in orphaned] == [111]
    assert sorted(p["number"] for p in blocking) == [222, 333]


def test_classify_authoritative_requeue_both_cancelled_dead():
    """Original CANCELLED (replaced_by C2), C2 also CANCELLED with no replacement → orphaned."""
    c2_id = "703f7a93-0000-0000-0000-000000000000"
    c1 = _task("650abda7", TaskStatus.CANCELLED, {
        "review_loop": {"pr_number": 111},
        "_replaced_by_task_id": c2_id,
    })
    c2 = SimpleNamespace(id=uuid.UUID(c2_id), status=TaskStatus.CANCELLED, context={})
    open_prs = [_pr(111, "plan/650abda7")]

    blocking, orphaned = _classify_prs_authoritative(open_prs, [c1, c2])

    assert [p["number"] for p in orphaned] == [111]
    assert blocking == []


def test_classify_authoritative_empty_prs():
    open_prs: list = []
    blocking, orphaned = _classify_prs_authoritative(open_prs, [])
    assert blocking == []
    assert orphaned == []
