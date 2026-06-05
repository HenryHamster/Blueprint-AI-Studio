"""Tests for find_content_divergent_revisions — the cross-ref migration-identity
divergence detector that catches the back-merge renumber class (prod incident
2026-06-04) that every structural/single-checkout guard is blind to.

Pure, no DB — filename-list comparison only.
"""

from unittest.mock import AsyncMock

import pytest

from app.migration_chain import (
    find_content_divergent_revisions,
    plan_integration_side_renumber,
)
from app.release_scoring import compute_composite
from app.rollup_migration_validation import check_qualification_migration_identity


def _migration_source(revision: str, down_revision: str) -> str:
    """A minimal, parseable Alembic migration source (revision + down_revision)."""
    return (
        '"""migration"""\n'
        f"revision = '{revision}'\n"
        f"down_revision = '{down_revision}'\n"
        "def upgrade():\n    pass\n"
        "def downgrade():\n    pass\n"
    )


def test_no_divergence_when_refs_agree():
    base = ["0137_cascade.py", "0138_digest_cache_table.py"]
    head = ["0137_cascade.py", "0138_digest_cache_table.py"]
    assert find_content_divergent_revisions(base, head) == []


def test_normal_pr_adding_new_revisions_is_clean():
    """A normal PR extends the chain with brand-new revision ids — no shared id
    diverges, so nothing is flagged (no false positives)."""
    base = ["0137_cascade.py", "0138_digest_cache_table.py"]
    head = [*base, "0139_new_feature_table.py", "0140_another_thing.py"]
    assert find_content_divergent_revisions(base, head) == []


def test_in_place_edit_same_id_same_description_is_clean():
    """Editing a migration's body in place (same filename) is not a divergence —
    the detector is filename/identity based, so a legit re-touch never trips it."""
    base = ["0140_digest_cache_table.py"]
    head = ["0140_digest_cache_table.py"]
    assert find_content_divergent_revisions(base, head) == []


def test_same_revision_id_different_migration_is_flagged():
    """(a) A revision id reused for different content — the DB that applied the
    first silently skips the second."""
    base = ["0138_digest_cache_table.py"]
    head = ["0138_task_blocked_state_fields.py"]
    out = find_content_divergent_revisions(base, head)
    assert len(out) == 1
    assert "0138" in out[0]
    assert "digest_cache_table" in out[0] and "task_blocked_state_fields" in out[0]


def test_renumbered_migration_same_description_different_id_is_flagged():
    """(b) The same migration under two different revision ids — a renumber."""
    base = ["0138_digest_cache_table.py"]
    head = ["0140_digest_cache_table.py"]
    out = find_content_divergent_revisions(base, head)
    assert len(out) == 1
    assert "digest_cache_table" in out[0]
    assert "0138" in out[0] and "0140" in out[0]


def test_real_incident_backmerge_renumber_is_caught():
    """The actual prod incident: digest_cache_table was main's 0138; the
    main->automain back-merge renumbered it to 0140, putting automain's
    task_blocked_state_fields at 0138 and merge_gate at 0139. The on-disk chain
    is internally valid on BOTH sides (single head, filenames match revisions),
    so every structural guard passes — but revision 0138 now means two different
    things, and digest moved ids. Both must be flagged BEFORE the merge."""
    main = ["0137_cascade.py", "0138_digest_cache_table.py"]
    automain = [
        "0137_cascade.py",
        "0138_task_blocked_state_fields.py",
        "0139_project_merge_gate_runtime.py",
        "0140_digest_cache_table.py",
    ]
    out = find_content_divergent_revisions(main, automain)
    # (a) revision 0138 maps to different migrations, and (b) digest renumbered.
    assert any("0138" in d and "digest_cache_table" in d for d in out)
    assert any("digest_cache_table" in d and "0140" in d for d in out)
    assert len(out) >= 2


def test_non_migration_filenames_ignored():
    base = ["__init__.py", "env.py", "0138_digest_cache_table.py", "README.md"]
    head = ["__init__.py", "env.py", "0138_digest_cache_table.py"]
    assert find_content_divergent_revisions(base, head) == []


def test_revision_present_on_only_one_side_is_not_a_divergence():
    """A revision unique to one ref (added or removed) is not a content
    divergence — only SHARED ids/descriptions that disagree are flagged."""
    base = ["0138_digest_cache_table.py"]
    head = ["0138_digest_cache_table.py", "0139_brand_new.py"]
    assert find_content_divergent_revisions(base, head) == []


# ===========================================================================
# check_qualification_migration_identity — the qualification-time gate that
# wraps find_content_divergent_revisions with the existing GitHub fetch helper
# and returns an ok | block | defer verdict (fail-safe on fetch failure).
# ===========================================================================


def _versions_by_ref(*, main_list: list[str], candidate_list: list[str]) -> AsyncMock:
    """Branch-keyed ``github.list_versions_at_ref``: deployed target vs candidate."""

    async def _side(token, owner, repo, ref, project_id=None):
        return list(main_list) if ref == "main" else list(candidate_list)

    return AsyncMock(side_effect=_side)


@pytest.mark.asyncio
async def test_qualification_gate_ok_when_refs_agree(mocker):
    """Identical migration sets on both refs → ('ok', None)."""
    mocker.patch(
        "app.github.list_versions_at_ref",
        new=AsyncMock(return_value=["0142_parent.py", "0143_vm_contact.py"]),
    )
    verdict, detail = await check_qualification_migration_identity(
        "tok", "acme", "widget", "cand-sha", "main"
    )
    assert verdict == "ok"
    assert detail is None


@pytest.mark.asyncio
async def test_qualification_gate_blocks_on_0143_incident(mocker):
    """The revision-0143 incident: deployed main keeps 0143 == vm_contact, the
    candidate reassigned 0143 to compute-job persistence and moved vm_contact to
    0144 → ('block', reason naming the 0143 divergence)."""
    mocker.patch(
        "app.github.list_versions_at_ref",
        new=_versions_by_ref(
            main_list=["0142_parent.py", "0143_vm_contact_last_tool_use.py"],
            candidate_list=[
                "0142_parent.py",
                "0143_add_compute_job_persistence.py",
                "0144_vm_contact_last_tool_use.py",
            ],
        ),
    )
    verdict, detail = await check_qualification_migration_identity(
        "tok", "acme", "widget", "cand-sha", "main"
    )
    assert verdict == "block"
    assert "0143" in detail
    assert (
        "vm_contact_last_tool_use" in detail
        and "add_compute_job_persistence" in detail
    )


@pytest.mark.asyncio
async def test_qualification_gate_defers_when_candidate_fetch_fails(mocker):
    """A failure fetching the candidate's migration list HOLDS the release —
    ('defer', reason) — never ('ok', None). A fetch error is not 'no collision'."""
    mocker.patch(
        "app.github.list_versions_at_ref",
        new=AsyncMock(side_effect=RuntimeError("GitHub 502")),
    )
    verdict, detail = await check_qualification_migration_identity(
        "tok", "acme", "widget", "cand-sha", "main"
    )
    assert verdict == "defer"
    assert "could not list" in detail


@pytest.mark.asyncio
async def test_qualification_gate_defers_when_target_fetch_fails(mocker):
    """A failure fetching ONLY the promotion target's list still DEFERS — the
    candidate side succeeding is not enough to clear the gate."""

    async def _side(token, owner, repo, ref, project_id=None):
        if ref == "main":
            raise RuntimeError("GitHub 500 on target")
        return ["0142_parent.py", "0143_x.py"]

    mocker.patch(
        "app.github.list_versions_at_ref", new=AsyncMock(side_effect=_side)
    )
    verdict, detail = await check_qualification_migration_identity(
        "tok", "acme", "widget", "cand-sha", "main"
    )
    assert verdict == "defer"
    assert "promotion target" in detail


# ===========================================================================
# compute_composite — the migration_identity_collision hard blocker
# ===========================================================================

_POLICY = {
    "weights": {"audit": 0.40, "test_erosion": 0.30, "smoketest": 0.30},
    "threshold": 0.90,
}


def test_compute_composite_migration_collision_forces_zero():
    """An otherwise-perfect candidate (CI green, audit 1.0, erosion 1.0, smoke
    1.0) is forced to confidence 0 / promote False when migration_collision is
    True, and the blocker is named migration_identity_collision."""
    result = compute_composite(
        ci_green=True,
        audit_score=1.0,
        test_erosion=1.0,
        smoketest_score=1.0,
        policy=_POLICY,
        ci_state="green",
        migration_collision=True,
    )
    assert result["confidence"] == 0.0
    assert result["promote"] is False
    assert result["blocker"] == "migration_identity_collision"


def test_compute_composite_no_collision_promotes_clean_candidate():
    """The same perfect candidate with migration_collision False (the default)
    promotes — the new blocker is inert unless a collision is passed in."""
    result = compute_composite(
        ci_green=True,
        audit_score=1.0,
        test_erosion=1.0,
        smoketest_score=1.0,
        policy=_POLICY,
        ci_state="green",
    )
    assert result["blocker"] is None
    assert result["promote"] is True


def test_compute_composite_ci_red_outranks_migration_collision():
    """CI-red is checked before the migration collision, so a candidate that is
    both red and colliding reports ci_not_green first (the build is the more
    fundamental failure)."""
    result = compute_composite(
        ci_green=False,
        audit_score=1.0,
        test_erosion=1.0,
        smoketest_score=1.0,
        policy=_POLICY,
        ci_state="red",
        migration_collision=True,
    )
    assert result["blocker"] == "ci_not_green"


# ===========================================================================
# Reconciliation of the revision-0143 incident: the INTEGRATION side moves, the
# DEPLOYED promotion-target migration stays put (immutable).
# ===========================================================================


def test_incident_0143_reconciliation_renumbers_integration_side_only():
    """The correct reconciliation of the revision-0143 collision renumbers the
    INTEGRATION-side migration (add_compute_job_persistence) and keeps the
    DEPLOYED migration at 0143 (vm_contact_last_tool_use) — the deployed
    promotion-target migration is immutable, so the integration side (never the
    deployed side) is what resolves the chain to a clean single head.

    Deployed main carries 0143 == vm_contact_last_tool_use; automain independently
    reused 0143 for add_compute_job_persistence anchored to the SAME parent
    (0142). The plan must move compute-job persistence to a FRESH head id above
    0143 (so no deployed DB has ever stamped it), rechain it onto the deployed
    head 0143, and leave vm_contact_last_tool_use exactly where production
    applied it."""
    # Deployed promotion target (immutable): 0143 == vm_contact_last_tool_use.
    promotion_target_filenames = [
        "0142_parent.py",
        "0143_vm_contact_last_tool_use.py",
    ]
    # Integration branch's post-divergence migration: 0143 reused for compute-job
    # persistence, anchored to the shared parent 0142.
    integration_sources = {
        "backend/alembic/versions/0143_add_compute_job_persistence.py": (
            _migration_source("0143", "0142")
        ),
    }

    plan = plan_integration_side_renumber(
        promotion_target_filenames, integration_sources
    )
    assert plan is not None, (
        "the single-collision case must be mechanically reconcilable"
    )

    # The INTEGRATION migration (compute-job persistence) is the one that moves —
    # to a fresh id (0144) above the deployed head, rechained onto 0143.
    assert plan["old_revision"] == "0143"
    assert plan["new_revision"] == "0144"
    assert plan["new_down_revision"] == "0143"  # onto vm_contact, the deployed head
    assert plan["expected_schema_revision"] == "0144"
    # The renamed file keeps the compute-job-persistence identity, not vm_contact.
    assert plan["new_path"].endswith("0144_add_compute_job_persistence.py")
    assert plan["old_path"].endswith("0143_add_compute_job_persistence.py")
    # The DEPLOYED migration (vm_contact) is never renumbered / deleted — only the
    # integration migration is rewritten.
    assert all("vm_contact_last_tool_use" not in d for d in plan["deletes"])
    assert all("vm_contact_last_tool_use" not in path for path in plan["upserts"])
    # The renumbered integration source now anchors onto the deployed head 0143.
    new_source = plan["upserts"][plan["new_path"]]
    assert "revision = '0144'" in new_source
    assert "down_revision = '0143'" in new_source
