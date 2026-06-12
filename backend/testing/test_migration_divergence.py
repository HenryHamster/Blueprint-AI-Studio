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
from app.rollup_migration_validation import (
    _validate_rollup_migration_chain,
    check_qualification_migration_identity,
)


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
async def test_qualification_gate_authorizes_reconciled_0143_divergence(mocker):
    """The revision-0143 incident is the ONE authorized, deploy-time-reconciled
    divergence: deployed main keeps 0143 == vm_contact, the candidate reassigned
    0143 to compute-job persistence and moved vm_contact to 0144. Because the
    deploy-time reconciliation makes this exact renumber safe, the gate no longer
    hard-blocks it → ('ok', informational note) — otherwise the version could
    never promote and the divergence could never clear (permanent deadlock)."""
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
    assert verdict == "ok"
    # The informational note is recorded (surfaces on the qualification record),
    # naming the authorized divergence and that it is non-blocking.
    assert detail is not None
    assert "authorized" in detail.lower()
    assert "0143" in detail
    assert (
        "vm_contact_last_tool_use" in detail
        and "add_compute_job_persistence" in detail
    )


@pytest.mark.asyncio
async def test_qualification_gate_still_blocks_unrelated_divergence(mocker):
    """An UNRELATED revision-identity divergence (a different revision id reused
    for different content) is NOT on the authorized registry and must still
    hard-block → ('block', reason). The authorization is exact, not a blanket
    waiver of all migration-identity collisions."""
    mocker.patch(
        "app.github.list_versions_at_ref",
        new=_versions_by_ref(
            main_list=["0138_digest_cache_table.py"],
            candidate_list=["0138_task_blocked_state_fields.py"],
        ),
    )
    verdict, detail = await check_qualification_migration_identity(
        "tok", "acme", "widget", "cand-sha", "main"
    )
    assert verdict == "block"
    assert "0138" in detail
    assert "digest_cache_table" in detail


@pytest.mark.asyncio
async def test_qualification_gate_blocks_partial_0143_identity_mismatch(mocker):
    """A divergence on revision 0143 whose identity pair does NOT match the
    authorized entry (same revision id, but an unexpected migration identity) is
    NOT authorized — exact matching means a different identity pair still
    blocks."""
    mocker.patch(
        "app.github.list_versions_at_ref",
        new=_versions_by_ref(
            main_list=["0142_parent.py", "0143_vm_contact_last_tool_use.py"],
            candidate_list=[
                "0142_parent.py",
                "0143_some_other_unrelated_migration.py",
            ],
        ),
    )
    verdict, detail = await check_qualification_migration_identity(
        "tok", "acme", "widget", "cand-sha", "main"
    )
    assert verdict == "block"
    assert "0143" in detail


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
# compute_composite — the migration_apply_drift hard blocker (scratch-DB
# apply-vs-prod-schema gate; a SECOND, distinct dimension from the filename
# identity collision above).
# ===========================================================================


def test_compute_composite_migration_apply_drift_forces_zero():
    """An otherwise-perfect candidate (CI green, audit/erosion/smoke 1.0, no
    identity collision) is forced to confidence 0 / promote False when
    migration_apply_blocked is True, with blocker migration_apply_drift."""
    result = compute_composite(
        ci_green=True,
        audit_score=1.0,
        test_erosion=1.0,
        smoketest_score=1.0,
        policy=_POLICY,
        ci_state="green",
        migration_apply_blocked=True,
    )
    assert result["confidence"] == 0.0
    assert result["promote"] is False
    assert result["blocker"] == "migration_apply_drift"


def test_compute_composite_apply_drift_default_is_inert():
    """The same perfect candidate with migration_apply_blocked left at its
    default False promotes — the new blocker is inert unless True is passed."""
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


def test_compute_composite_identity_collision_outranks_apply_drift():
    """The filename-identity collision is evaluated before the apply-drift gate,
    so a candidate failing BOTH deterministic migration dimensions reports
    migration_identity_collision first (the identity check is cheaper/earlier)."""
    result = compute_composite(
        ci_green=True,
        audit_score=1.0,
        test_erosion=1.0,
        smoketest_score=1.0,
        policy=_POLICY,
        ci_state="green",
        migration_collision=True,
        migration_apply_blocked=True,
    )
    assert result["blocker"] == "migration_identity_collision"


def test_compute_composite_ci_red_outranks_apply_drift():
    """CI-red is the most fundamental failure, checked before the apply-drift
    gate, so a candidate that is both red and apply-blocked reports
    ci_not_green first."""
    result = compute_composite(
        ci_green=False,
        audit_score=1.0,
        test_erosion=1.0,
        smoketest_score=1.0,
        policy=_POLICY,
        ci_state="red",
        migration_apply_blocked=True,
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


# ===========================================================================
# _validate_rollup_migration_chain — the PRE-MERGE guard now also checks the
# rollup's added migrations against the DEPLOYED promotion target, not just the
# integration branch it merges into. This is the blind spot that birthed the
# revision-0143 collision: a clean addition against integration that reuses a
# revision id the deployed target already stamped to a different migration.
# ===========================================================================

_VERSIONS_DIR = "backend/alembic/versions"


def _files(*paths: str) -> list[dict]:
    return [{"filename": p, "status": "added"} for p in paths]


def _added_migration_diff(path: str, revision: str, down_revision: str) -> str:
    """A unified diff that adds a single parseable migration file."""
    body = "".join(
        f"+{line}\n" for line in _migration_source(revision, down_revision).splitlines()
    )
    return (
        f"diff --git a/{path} b/{path}\n"
        f"new file mode 100644\n"
        f"index 0000000..1111111\n"
        f"--- /dev/null\n"
        f"+++ b/{path}\n"
        f"@@ -0,0 +1,7 @@\n"
        f"{body}"
    )


def _versions_by_branch(
    *,
    integration: list[str],
    promotion: list[str],
    integration_branch: str = "automain",
    promotion_branch: str = "main",
) -> AsyncMock:
    """Branch-keyed ``list_versions_at_ref``: integration vs deployed promotion."""

    async def _side(token, owner, repo, ref, project_id=None):
        if ref == promotion_branch:
            return list(promotion)
        return list(integration)

    return AsyncMock(side_effect=_side)


@pytest.mark.asyncio
async def test_premerge_guard_blocks_cross_branch_reuse_vs_promotion_target(mocker):
    """The revision-0143 class: the rollup adds 0143_add_compute_job_persistence
    anchored to the integration head 0142 (a CLEAN extension of integration), but
    the deployed promotion target already stamped 0143 to vm_contact_last_tool_use.
    The integration-only checks pass; the promotion-target check must BLOCK."""
    path = f"{_VERSIONS_DIR}/0143_add_compute_job_persistence.py"
    mocker.patch("app.github.get_pr_files", new=AsyncMock(return_value=_files(path)))
    mocker.patch(
        "app.github.get_pull_request_diff",
        new=AsyncMock(return_value=_added_migration_diff(path, "0143", "0142")),
    )
    mocker.patch(
        "app.github.list_versions_at_ref",
        new=_versions_by_branch(
            # Integration head is 0142, so adding 0143 is a clean extension there.
            integration=["0141_x.py", "0142_parent.py"],
            # Deployed main already uses 0143 for a DIFFERENT migration.
            promotion=["0142_parent.py", "0143_vm_contact_last_tool_use.py"],
        ),
    )
    status, reason = await _validate_rollup_migration_chain(
        "tok", "acme", "widget", 1, "automain", promotion_target="main"
    )
    assert status == "block"
    assert "0143" in reason
    assert "promotion target" in reason
    assert (
        "vm_contact_last_tool_use" in reason
        and "add_compute_job_persistence" in reason
    )


@pytest.mark.asyncio
async def test_premerge_guard_passes_integration_ahead_of_promotion_target(mocker):
    """No false positive on normal ahead-ness: integration is many revisions ahead
    of the deployed target, and the rollup adds a brand-new id (0151) above both.
    No shared revision id diverges, so the guard must return ('ok', None)."""
    path = f"{_VERSIONS_DIR}/0151_new_feature.py"
    mocker.patch("app.github.get_pr_files", new=AsyncMock(return_value=_files(path)))
    mocker.patch(
        "app.github.get_pull_request_diff",
        new=AsyncMock(return_value=_added_migration_diff(path, "0151", "0150")),
    )
    mocker.patch(
        "app.github.list_versions_at_ref",
        new=_versions_by_branch(
            # Integration is well ahead of the deployed target (0144..0150 exist
            # only on integration — legitimate ahead-ness, not a collision).
            integration=[f"{n:04d}_step.py" for n in range(141, 151)],
            promotion=["0142_parent.py", "0143_vm_contact_last_tool_use.py"],
        ),
    )
    assert await _validate_rollup_migration_chain(
        "tok", "acme", "widget", 1, "automain", promotion_target="main"
    ) == ("ok", None)


@pytest.mark.asyncio
async def test_premerge_guard_defers_when_promotion_target_fetch_fails(mocker):
    """Fail-safe: a failure listing the deployed promotion target's migrations
    HOLDS the merge (defer), never waves it through as 'no collision'."""
    path = f"{_VERSIONS_DIR}/0143_new.py"
    mocker.patch("app.github.get_pr_files", new=AsyncMock(return_value=_files(path)))
    mocker.patch(
        "app.github.get_pull_request_diff",
        new=AsyncMock(return_value=_added_migration_diff(path, "0143", "0142")),
    )

    async def _side(token, owner, repo, ref, project_id=None):
        if ref == "main":
            raise RuntimeError("GitHub 503 on promotion target")
        return ["0141_x.py", "0142_parent.py"]

    mocker.patch("app.github.list_versions_at_ref", new=AsyncMock(side_effect=_side))
    status, reason = await _validate_rollup_migration_chain(
        "tok", "acme", "widget", 1, "automain", promotion_target="main"
    )
    assert status == "defer"
    assert "promotion target" in reason
    assert "GitHub 503 on promotion target" in reason


# ===========================================================================
# Empty-diff fallback — GitHub 406s the whole-PR ``.diff`` media type (-> empty
# body) once a PR exceeds its diff size cap, which a large release rollup
# routinely does (v10: 107 files / 20k+ lines). Treating that as a ``defer``
# wedged EVERY migration-carrying promotion forever (the rollup only grows, so
# it never self-heals). The guard now falls back to reading each touched
# migration's source directly at the PR head — immune to the diff size cap.
# ===========================================================================


def _mock_empty_diff_head_sources(
    mocker, sources_by_path: dict[str, str], *, head_sha: str = "deadbeefcafe0001"
):
    """Drive the empty-diff fallback: an empty whole-PR diff plus readable
    per-file migration sources at the PR head. ``sources_by_path`` maps a repo
    path to its migration source (a missing path reads as unreadable -> None)."""
    mocker.patch("app.github.get_pull_request_diff", new=AsyncMock(return_value=""))
    mocker.patch(
        "app.github.get_pull_request",
        new=AsyncMock(return_value={"head": {"sha": head_sha}}),
    )

    async def _text(path, *, token, owner, repo, ref, project_id=None):
        assert ref == head_sha  # sources must be read at the PR head, not a branch
        return sources_by_path.get(path)

    mocker.patch("app.github.get_file_text_at_ref", new=AsyncMock(side_effect=_text))


@pytest.mark.asyncio
async def test_premerge_guard_empty_diff_falls_back_and_passes_clean(mocker):
    """THE WEDGE REGRESSION: a valid migration-carrying release PR whose whole-PR
    diff is empty (too large -> 406) must no longer ``defer`` forever — the
    per-file head read supplies the source and the clean addition returns
    ('ok', None) so the promotion can land."""
    path = f"{_VERSIONS_DIR}/0151_new_feature.py"
    mocker.patch("app.github.get_pr_files", new=AsyncMock(return_value=_files(path)))
    _mock_empty_diff_head_sources(mocker, {path: _migration_source("0151", "0150")})
    mocker.patch(
        "app.github.list_versions_at_ref",
        new=_versions_by_branch(
            integration=[f"{n:04d}_step.py" for n in range(141, 151)],
            promotion=["0142_parent.py", "0143_vm_contact_last_tool_use.py"],
        ),
    )
    assert await _validate_rollup_migration_chain(
        "tok", "acme", "widget", 1, "automain", promotion_target="main"
    ) == ("ok", None)


@pytest.mark.asyncio
async def test_premerge_guard_empty_diff_fallback_still_blocks_reuse(mocker):
    """The fallback path is not a weaker check: with the whole-PR diff empty, the
    head-read source still drives the cross-branch revision-identity guard, so
    the revision-0143 reuse class is BLOCKED, not waved through."""
    path = f"{_VERSIONS_DIR}/0143_add_compute_job_persistence.py"
    mocker.patch("app.github.get_pr_files", new=AsyncMock(return_value=_files(path)))
    _mock_empty_diff_head_sources(mocker, {path: _migration_source("0143", "0142")})
    mocker.patch(
        "app.github.list_versions_at_ref",
        new=_versions_by_branch(
            integration=["0141_x.py", "0142_parent.py"],
            promotion=["0142_parent.py", "0143_vm_contact_last_tool_use.py"],
        ),
    )
    status, reason = await _validate_rollup_migration_chain(
        "tok", "acme", "widget", 1, "automain", promotion_target="main"
    )
    assert status == "block"
    assert "0143" in reason and "promotion target" in reason


@pytest.mark.asyncio
async def test_premerge_guard_empty_diff_defers_when_head_source_unreadable(mocker):
    """Fail-safe preserved through the fallback: an empty diff AND an unreadable
    migration source at the head HOLDS the merge (defer), never 'ok'."""
    path = f"{_VERSIONS_DIR}/0151_new_feature.py"
    mocker.patch("app.github.get_pr_files", new=AsyncMock(return_value=_files(path)))
    _mock_empty_diff_head_sources(mocker, {})  # no source -> get_file_text returns None
    mocker.patch(
        "app.github.list_versions_at_ref",
        new=AsyncMock(return_value=["0150_parent.py"]),
    )
    status, reason = await _validate_rollup_migration_chain(
        "tok", "acme", "widget", 1, "automain", promotion_target="main"
    )
    assert status == "defer"
    assert "0151_new_feature.py" in reason


@pytest.mark.asyncio
async def test_premerge_guard_empty_diff_defers_when_pr_head_sha_missing(mocker):
    """Fail-safe: an empty diff and a PR detail with no head SHA cannot read the
    migration sources, so the guard defers rather than guessing."""
    path = f"{_VERSIONS_DIR}/0151_new_feature.py"
    mocker.patch("app.github.get_pr_files", new=AsyncMock(return_value=_files(path)))
    mocker.patch("app.github.get_pull_request_diff", new=AsyncMock(return_value=""))
    mocker.patch(
        "app.github.get_pull_request", new=AsyncMock(return_value={"head": {}})
    )
    status, reason = await _validate_rollup_migration_chain(
        "tok", "acme", "widget", 1, "automain", promotion_target="main"
    )
    assert status == "defer"
    assert "head SHA" in reason
