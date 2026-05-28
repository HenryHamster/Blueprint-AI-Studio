"""Tests for the cancelled-child orphan-PR filtering in rollup_management.

A child task that was CANCELLED (e.g. requeue-replaced after a timeout, or
SCOPE_EMPTY pre-empted) can leave its PR open. No live task will ever drive it
to merge, so blocking the parent's rollup on that PR strands the parent in
EVALUATING forever (incident 2026-05-28: an orphan safeguard PR from a
cancelled, retry-replaced child wedged a research rollup — the cancel path left
the PR open and it carried 0 CI checks, so the merge gate never fired either).

These cover the pure attribution + partition helpers; the GitHub-close + gate
wiring lives in _check_and_trigger_rollup and is exercised by the evaluating
suite.
"""

from __future__ import annotations

from app.rollup_management import _branch_task_id_prefix, _partition_orphan_prs
from app.shared_state import derive_task_branch


def test_branch_task_id_prefix_canonical_agent_branch():
    """The trailing 8-hex token of a {namespace}/{prefix}-{8hex} branch is the
    owning task's id-prefix — and matches what derive_task_branch produced."""
    task_id = "db668612-7bf4-41be-89e1-3d892be07273"
    branch = derive_task_branch(task_id, "refactor")  # -> blueprint/refactor-db668612
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
