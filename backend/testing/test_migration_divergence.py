"""Tests for find_content_divergent_revisions — the cross-ref migration-identity
divergence detector that catches the back-merge renumber class (prod incident
2026-06-04) that every structural/single-checkout guard is blind to.

Pure, no DB — filename-list comparison only.
"""

from unittest.mock import AsyncMock

import pytest

from app.migration_chain import (
    find_content_divergent_revisions,
    find_deployed_migration_content_edits,
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


def _versions_by_ref(
    *,
    main_list: list[str],
    candidate_list: list[str],
    changed: set[str] | None = None,
) -> AsyncMock:
    """Branch-keyed ``github.list_version_shas_at_ref``: deployed target (main) vs
    candidate, returned as ``{filename: git blob sha}`` maps.

    A filename shared across the two refs gets the SAME blob sha by default, so
    the body-immutability check content-addresses it as unchanged and skips the
    per-file body fetch (the storm-avoidance the gate now relies on). Basenames
    in *changed* get a ref-distinct sha so the gate still fetches and compares
    their bodies — used by the in-place-edit and body-fetch-failure tests that
    exercise that path.
    """
    changed = changed or set()

    def _sha(name: str, ref: str) -> str:
        return f"sha-{name}@{ref}" if name in changed else f"sha-{name}"

    async def _side(token, owner, repo, ref, project_id=None):
        names = list(main_list) if ref == "main" else list(candidate_list)
        return {n: _sha(n, ref) for n in names}

    return AsyncMock(side_effect=_side)


def _shas(names: list[str]) -> dict[str, str]:
    """Same ``{filename: blob sha}`` map for both refs (all bodies identical)."""
    return {n: f"sha-{n}" for n in names}


@pytest.mark.asyncio
async def test_qualification_gate_ok_when_refs_agree(mocker):
    """Identical migration sets on both refs → ('ok', None)."""
    mocker.patch(
        "app.github.list_version_shas_at_ref",
        new=AsyncMock(
            return_value=_shas(["0142_parent.py", "0143_vm_contact.py"])
        ),
    )
    body = mocker.patch(
        "app.github.get_file_text_at_ref",
        new=AsyncMock(return_value=_migration_source("0143", "0142")),
    )
    verdict, detail = await check_qualification_migration_identity(
        "tok", "acme", "widget", "cand-sha", "main"
    )
    assert verdict == "ok"
    assert detail is None
    # Every shared migration is byte-identical (same blob sha), so the gate must
    # NOT fetch any body — this is the fetch-storm avoidance the fix introduces.
    assert body.await_count == 0


@pytest.mark.asyncio
async def test_qualification_gate_authorizes_reconciled_0143_divergence(mocker):
    """The revision-0143 incident is the ONE authorized, deploy-time-reconciled
    divergence: deployed main keeps 0143 == vm_contact, the candidate reassigned
    0143 to compute-job persistence and moved vm_contact to 0144. Because the
    deploy-time reconciliation makes this exact renumber safe, the gate no longer
    hard-blocks it → ('ok', informational note) — otherwise the version could
    never promote and the divergence could never clear (permanent deadlock)."""
    src_0142 = _migration_source("0142", "0141")
    mocker.patch(
        "app.github.list_version_shas_at_ref",
        new=_versions_by_ref(
            main_list=["0142_parent.py", "0143_vm_contact_last_tool_use.py"],
            candidate_list=[
                "0142_parent.py",
                "0143_add_compute_job_persistence.py",
                "0144_vm_contact_last_tool_use.py",
            ],
        ),
    )
    # Only 0142_parent.py is shared between main and candidate; return identical
    # body for both refs so the body-immutability check passes cleanly.
    mocker.patch(
        "app.github.get_file_text_at_ref",
        new=AsyncMock(return_value=src_0142),
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
        "vm_contact_last_tool_use" in detail and "add_compute_job_persistence" in detail
    )


@pytest.mark.asyncio
async def test_qualification_gate_still_blocks_unrelated_divergence(mocker):
    """An UNRELATED revision-identity divergence (a different revision id reused
    for different content) is NOT on the authorized registry and must still
    hard-block → ('block', reason). The authorization is exact, not a blanket
    waiver of all migration-identity collisions."""
    mocker.patch(
        "app.github.list_version_shas_at_ref",
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
        "app.github.list_version_shas_at_ref",
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
        "app.github.list_version_shas_at_ref",
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
        return _shas(["0142_parent.py", "0143_x.py"])

    mocker.patch(
        "app.github.list_version_shas_at_ref", new=AsyncMock(side_effect=_side)
    )
    verdict, detail = await check_qualification_migration_identity(
        "tok", "acme", "widget", "cand-sha", "main"
    )
    assert verdict == "defer"
    assert "promotion target" in detail


@pytest.mark.asyncio
async def test_qualification_gate_blocks_inplace_body_edit_of_deployed_migration(
    mocker,
):
    """Body-immutability gate: a migration with the same filename on both refs
    but a DIFFERENT body must be BLOCKED ('block', reason naming the migration).
    This is the class that filename-only identity checks are blind to."""
    deployed_src = _migration_source("0309", "0308")
    tampered_src = deployed_src + "# tampered by candidate\n"

    mocker.patch(
        "app.github.list_version_shas_at_ref",
        new=_versions_by_ref(
            main_list=["0308_parent.py", "0309_computejob_heartbeat.py"],
            candidate_list=["0308_parent.py", "0309_computejob_heartbeat.py"],
            # A real in-place body edit changes the blob sha, so the gate fetches
            # and compares this migration's body (identical-sha files are skipped).
            changed={"0309_computejob_heartbeat.py"},
        ),
    )

    async def _body(path, *, token, owner, repo, ref, project_id=None):
        if ref == "main":
            return deployed_src
        return tampered_src

    mocker.patch("app.github.get_file_text_at_ref", new=AsyncMock(side_effect=_body))
    verdict, detail = await check_qualification_migration_identity(
        "tok", "acme", "widget", "cand-sha", "main"
    )
    assert verdict == "block"
    assert "0309_computejob_heartbeat" in detail
    assert "immutable" in detail or "in-place" in detail or "body" in detail.lower()


@pytest.mark.asyncio
async def test_qualification_gate_ok_when_shared_migration_body_identical(mocker):
    """A shared migration with IDENTICAL body on both refs stays ('ok', None)
    — no false positive from the body-immutability check."""
    mocker.patch(
        "app.github.list_version_shas_at_ref",
        new=_versions_by_ref(
            main_list=["0308_parent.py", "0309_computejob_heartbeat.py"],
            candidate_list=["0308_parent.py", "0309_computejob_heartbeat.py"],
        ),
    )
    body = mocker.patch(
        "app.github.get_file_text_at_ref",
        new=AsyncMock(return_value=_migration_source("0309", "0308")),
    )
    verdict, detail = await check_qualification_migration_identity(
        "tok", "acme", "widget", "cand-sha", "main"
    )
    assert verdict == "ok"
    assert detail is None
    # Identical blob sha ⇒ provably unchanged ⇒ no body fetch (no false positive
    # AND no wasted GitHub call).
    assert body.await_count == 0


@pytest.mark.asyncio
async def test_qualification_gate_does_not_fetch_bodies_for_unchanged_migrations(
    mocker,
):
    """Regression guard for the GitHub-403 fetch-storm wedge: a candidate sharing
    a large, unchanged deployed migration set with the target must be cleared by
    comparing blob shas alone, issuing ZERO per-file body fetches. Before the fix
    the gate fetched every shared migration's body (2 contents calls each, ~200
    files) on every qualification attempt, draining the token's 5000/hr budget
    into a synthetic-403 → 'defer' loop that held the release indefinitely."""
    shared = [f"{i:04d}_migration.py" for i in range(1, 216)]  # 215 shared files
    mocker.patch(
        "app.github.list_version_shas_at_ref",
        # Same list (same blob shas) on both refs — deployed migrations are
        # immutable, so the candidate carries them forward byte-for-byte.
        new=_versions_by_ref(main_list=shared, candidate_list=shared),
    )
    body = mocker.patch("app.github.get_file_text_at_ref", new=AsyncMock())
    verdict, detail = await check_qualification_migration_identity(
        "tok", "acme", "widget", "cand-sha", "main"
    )
    assert verdict == "ok"
    assert detail is None
    assert body.await_count == 0  # not one body pulled across 215 shared files


@pytest.mark.asyncio
async def test_qualification_gate_ok_when_no_shared_basenames(mocker):
    """When the candidate adds brand-new migrations not present on the target,
    there are no shared basenames and no body fetch is triggered — ('ok', None)."""
    mocker.patch(
        "app.github.list_version_shas_at_ref",
        new=_versions_by_ref(
            main_list=["0308_parent.py"],
            candidate_list=["0308_parent.py", "0309_new_feature.py"],
        ),
    )
    # 0308_parent.py IS shared; mock returns identical body so immutability passes.
    src = _migration_source("0308", "0307")
    mocker.patch(
        "app.github.get_file_text_at_ref",
        new=AsyncMock(return_value=src),
    )
    verdict, detail = await check_qualification_migration_identity(
        "tok", "acme", "widget", "cand-sha", "main"
    )
    assert verdict == "ok"
    assert detail is None


@pytest.mark.asyncio
async def test_qualification_gate_defers_when_deployed_file_fetch_fails(mocker):
    """Fail-safe: if fetching the deployed body of a shared migration raises,
    the gate returns ('defer', reason) — never ('ok') or ('block')."""
    mocker.patch(
        "app.github.list_version_shas_at_ref",
        new=_versions_by_ref(
            main_list=["0309_computejob_heartbeat.py"],
            candidate_list=["0309_computejob_heartbeat.py"],
            # Differing blob sha forces the body fetch this defer-path exercises.
            changed={"0309_computejob_heartbeat.py"},
        ),
    )

    async def _body(path, *, token, owner, repo, ref, project_id=None):
        if ref == "main":
            raise RuntimeError("GitHub 503 fetching deployed body")
        return _migration_source("0309", "0308")

    mocker.patch("app.github.get_file_text_at_ref", new=AsyncMock(side_effect=_body))
    verdict, detail = await check_qualification_migration_identity(
        "tok", "acme", "widget", "cand-sha", "main"
    )
    assert verdict == "defer"
    assert "0309_computejob_heartbeat" in detail or "promotion target" in detail


@pytest.mark.asyncio
async def test_qualification_gate_defers_when_deployed_file_empty(mocker):
    """Fail-safe: an empty body from the deployed ref is treated as an unknown
    state and returns ('defer', reason) — not ('ok') or ('block')."""
    mocker.patch(
        "app.github.list_version_shas_at_ref",
        new=_versions_by_ref(
            main_list=["0309_computejob_heartbeat.py"],
            candidate_list=["0309_computejob_heartbeat.py"],
            # Differing blob sha forces the body fetch this defer-path exercises.
            changed={"0309_computejob_heartbeat.py"},
        ),
    )

    async def _body(path, *, token, owner, repo, ref, project_id=None):
        if ref == "main":
            return ""  # empty body
        return _migration_source("0309", "0308")

    mocker.patch("app.github.get_file_text_at_ref", new=AsyncMock(side_effect=_body))
    verdict, detail = await check_qualification_migration_identity(
        "tok", "acme", "widget", "cand-sha", "main"
    )
    assert verdict == "defer"
    assert "empty" in detail or "promotion target" in detail


@pytest.mark.asyncio
async def test_qualification_gate_defers_when_candidate_body_fetch_fails(mocker):
    """Fail-safe: if fetching the candidate body of a shared migration raises,
    the gate returns ('defer', reason) — never ('ok') or ('block')."""
    deployed_src = _migration_source("0309", "0308")
    mocker.patch(
        "app.github.list_version_shas_at_ref",
        new=_versions_by_ref(
            main_list=["0309_computejob_heartbeat.py"],
            candidate_list=["0309_computejob_heartbeat.py"],
            # Differing blob sha forces the body fetch this defer-path exercises.
            changed={"0309_computejob_heartbeat.py"},
        ),
    )

    async def _body(path, *, token, owner, repo, ref, project_id=None):
        if ref == "main":
            return deployed_src
        raise RuntimeError("GitHub 502 fetching candidate body")

    mocker.patch("app.github.get_file_text_at_ref", new=AsyncMock(side_effect=_body))
    verdict, detail = await check_qualification_migration_identity(
        "tok", "acme", "widget", "cand-sha", "main"
    )
    assert verdict == "defer"
    assert "cand-sha" in detail or "candidate" in detail


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


def _modified_migration_file(path: str, revision: str, down_revision: str) -> dict:
    """Create a pr_files entry for a MODIFIED (not added) migration file."""
    src = _migration_source(revision, down_revision)
    body = "".join(f"+{line}\n" for line in src.splitlines())
    return {"filename": path, "status": "modified", "patch": f"@@ -0,0 +1,7 @@\n{body}"}


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


def _migration_file(path: str, revision: str, down_revision: str) -> dict:
    """Create a pr_files entry for a migration file with per-file patch."""
    src = _migration_source(revision, down_revision)
    body = "".join(f"+{line}\n" for line in src.splitlines())
    return {"filename": path, "status": "added", "patch": f"@@ -0,0 +1,7 @@\n{body}"}


def _renamed_migration_file(
    old_path: str,
    new_path: str,
    new_revision: str,
    new_down_revision: str,
) -> dict:
    """Create a pr_files entry for a renamed migration file."""
    src = _migration_source(new_revision, new_down_revision)
    body = "".join(f"+{line}\n" for line in src.splitlines())
    return {
        "filename": new_path,
        "previous_filename": old_path,
        "status": "renamed",
        "patch": f"@@ -0,0 +1,7 @@\n{body}",
    }


@pytest.mark.asyncio
async def test_premerge_guard_blocks_cross_branch_reuse_vs_promotion_target(mocker):
    """The revision-0143 class: the rollup adds 0143_add_compute_job_persistence
    anchored to the integration head 0142 (a CLEAN extension of integration), but
    the deployed promotion target already stamped 0143 to vm_contact_last_tool_use.
    The integration-only checks pass; the promotion-target check must BLOCK."""
    path = f"{_VERSIONS_DIR}/0143_add_compute_job_persistence.py"
    mocker.patch(
        "app.github.get_pr_files",
        new=AsyncMock(return_value=[_migration_file(path, "0143", "0142")]),
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
        "vm_contact_last_tool_use" in reason and "add_compute_job_persistence" in reason
    )


@pytest.mark.asyncio
async def test_premerge_guard_passes_integration_ahead_of_promotion_target(mocker):
    """No false positive on normal ahead-ness: integration is many revisions ahead
    of the deployed target, and the rollup adds a brand-new id (0151) above both.
    No shared revision id diverges, so the guard must return ('ok', None)."""
    path = f"{_VERSIONS_DIR}/0151_new_feature.py"
    mocker.patch(
        "app.github.get_pr_files",
        new=AsyncMock(return_value=[_migration_file(path, "0151", "0150")]),
    )
    mocker.patch(
        "app.github.list_versions_at_ref",
        new=_versions_by_branch(
            # Integration is well ahead of the deployed target (0144..0150 exist
            # only on integration — legitimate ahead-ness, not a collision).
            # Shared revisions (0142, 0143) carry the SAME identity on both
            # branches; only brand-new ids differ.
            integration=[
                "0142_parent.py",
                "0143_vm_contact_last_tool_use.py",
                *[f"{n:04d}_step.py" for n in range(144, 151)],
            ],
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
    mocker.patch(
        "app.github.get_pr_files",
        new=AsyncMock(return_value=[_migration_file(path, "0143", "0142")]),
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
            # Integration carries the same shared migrations as promotion (no
            # identity divergence on shared revisions) plus brand-new ids above.
            integration=[
                "0142_parent.py",
                "0143_vm_contact_last_tool_use.py",
                *[f"{n:04d}_step.py" for n in range(144, 151)],
            ],
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


# ===========================================================================
# find_deployed_migration_content_edits — content-immutability check for the
# in-place-edit class: same filename on deployed and candidate, different body.
# Filename-only detectors (find_content_divergent_revisions) are blind to this.
# ===========================================================================


def test_content_edit_of_deployed_migration_is_flagged():
    """In-place body edit is detected: same basename, different content."""
    base = {"0309_computejob_heartbeat.py": _migration_source("0309", "0308")}
    head = {
        "0309_computejob_heartbeat.py": _migration_source("0309", "0308")
        + "# tampered\n"
    }
    out = find_deployed_migration_content_edits(base, head)
    assert len(out) == 1
    assert "0309_computejob_heartbeat" in out[0]
    assert "in-place" in out[0] or "immutable" in out[0]


def test_content_identical_migration_is_not_flagged():
    """Same content on both sides is clean — no false positive."""
    src = _migration_source("0309", "0308")
    base = {"0309_computejob_heartbeat.py": src}
    head = {"0309_computejob_heartbeat.py": src}
    assert find_deployed_migration_content_edits(base, head) == []


def test_content_edit_whitespace_normalised():
    """Trailing-whitespace differences do not trigger a false positive."""
    src = _migration_source("0309", "0308")
    base = {"0309_heartbeat.py": src}
    head = {"0309_heartbeat.py": src.replace("pass", "pass  ")}  # trailing space
    assert find_deployed_migration_content_edits(base, head) == []


def test_content_edit_key_by_basename():
    """Full paths and basenames are normalised by basename for comparison."""
    src = _migration_source("0309", "0308")
    modified = src + "# extra\n"
    base = {"backend/alembic/versions/0309_heartbeat.py": src}
    head = {"0309_heartbeat.py": modified}
    out = find_deployed_migration_content_edits(base, head)
    assert len(out) == 1


# ===========================================================================
# Named regression pins for the v17 defect classes.
# ===========================================================================


def test_v17_defect_class_reused():
    """v17 regression pin — reused-revision class: deployed main has
    0309 == computejob_heartbeat; automain reused 0309 for performance_indexes.
    find_content_divergent_revisions must flag this before a merge."""
    main = ["0309_computejob_heartbeat.py"]
    automain = ["0309_performance_indexes.py"]
    out = find_content_divergent_revisions(main, automain)
    assert len(out) >= 1
    assert any("0309" in d for d in out)
    assert any("computejob_heartbeat" in d and "performance_indexes" in d for d in out)


def test_v17_defect_class_renumbered():
    """v17 regression pin — renumber class: computejob_heartbeat moved from
    revision 0309 (main) to 0330 (automain). find_content_divergent_revisions
    must flag the identity-under-different-id divergence."""
    main = ["0309_computejob_heartbeat.py"]
    automain = ["0330_computejob_heartbeat.py"]
    out = find_content_divergent_revisions(main, automain)
    assert len(out) >= 1
    assert any("computejob_heartbeat" in d for d in out)
    assert any("0309" in d and "0330" in d for d in out)


def test_v17_defect_class_in_place_edit():
    """v17 regression pin — in-place-edit class: a migration file with the same
    name as a deployed one but a modified body. find_deployed_migration_content_edits
    must flag this; the filename-only detector is blind to it."""
    deployed = _migration_source("0309", "0308")
    tampered = deployed + "# in-place tamper\n"
    base = {"0309_computejob_heartbeat.py": deployed}
    head = {"0309_computejob_heartbeat.py": tampered}
    out = find_deployed_migration_content_edits(base, head)
    assert len(out) >= 1
    assert any("0309_computejob_heartbeat" in m for m in out)


# ===========================================================================
# _validate_rollup_migration_chain — v17 defect class integration tests.
# Covers both tamper classes that birthed the deploy-fatal Alembic defect:
#   1. Pre-PR rename/renumber on the integration branch (invisible to PR-diff
#      checks because the tampered migration is not in the PR's added files).
#   2. In-place edit of a deployed migration (caught by file-status check).
# ===========================================================================


@pytest.mark.asyncio
async def test_in_place_edit_of_deployed_migration_body_is_blocked(mocker):
    """In-place-edit class: a migration whose filename matches a deployed
    promotion-target file but whose body differs from what was applied is
    BLOCKED. The filename-only reuse/renumber detector is blind to this class
    (same filename → same revision id and description → no filename divergence).

    Scenario: main has 0330_computejob_heartbeat.py applied; a PR on automain
    adds a file with the same name but a modified body. The file-status check
    catches the modification and blocks the merge."""
    basename = "0330_computejob_heartbeat.py"
    path = f"{_VERSIONS_DIR}/{basename}"

    deployed_src = _migration_source("0330", "0329")
    pr_src = deployed_src + "# in-place tamper\n"

    patch_body = "".join(f"+{line}\n" for line in pr_src.splitlines())
    pr_file = {
        "filename": path,
        "status": "modified",
        "patch": f"@@ -1,7 +1,8 @@\n{patch_body}",
    }
    mocker.patch("app.github.get_pr_files", new=AsyncMock(return_value=[pr_file]))
    mocker.patch(
        "app.github.list_versions_at_ref",
        new=_versions_by_branch(
            # automain has 0329 as head; 0330 not present yet → no collision
            integration=["0328_parent.py", "0329_other.py"],
            # main has 0330 already applied
            promotion=[
                "0328_parent.py",
                "0329_other.py",
                "0330_computejob_heartbeat.py",
            ],
        ),
    )
    status, reason = await _validate_rollup_migration_chain(
        "tok", "acme", "widget", 1, "automain", promotion_target="main"
    )
    assert status == "block"
    assert "0330_computejob_heartbeat" in reason
    assert "in-place" in reason or "immutable" in reason.lower()


@pytest.mark.asyncio
async def test_premerge_guard_blocks_pre_pr_rename_on_integration_branch(mocker):
    """Defect class 1 (v17 regression): a rename already on the integration
    branch — committed in a PRIOR PR, not the one being evaluated — is invisible
    to the PR-diff checks that only compare added files. The full-branch check
    must catch it: revision 0138 maps to 'old_name' on the deployed target but
    to 'new_name' on integration; the current PR only adds an unrelated 0140
    and touches no 0138 file at all."""
    path = f"{_VERSIONS_DIR}/0140_unrelated.py"
    mocker.patch(
        "app.github.get_pr_files",
        new=AsyncMock(return_value=[_migration_file(path, "0140", "0139")]),
    )
    mocker.patch(
        "app.github.list_versions_at_ref",
        new=_versions_by_branch(
            # Integration already carries the renamed 0138; the current PR adds
            # an unrelated 0140 and never touches 0138.
            integration=[
                "0137_cascade.py",
                "0138_new_name.py",
                "0139_something.py",
            ],
            # Deployed main still has 0138 under the original identity.
            promotion=["0137_cascade.py", "0138_old_name.py"],
        ),
    )
    status, reason = await _validate_rollup_migration_chain(
        "tok", "acme", "widget", 1, "automain", promotion_target="main"
    )
    assert status == "block"
    assert "0138" in reason
    assert "pre-PR" in reason or "integration branch" in reason
    assert "new_name" in reason or "old_name" in reason


@pytest.mark.asyncio
async def test_merge_guard_allows_authorized_divergence_blocks_unauthorized(mocker):
    """The merge-time full-branch check applies the same authorized-divergence
    filter as the qual-time guard.

    Allowed path — integration already carries the authorized 0143 renumber
    (committed in a prior PR, NOT this one): 0143=add_compute_job_persistence,
    0144=vm_contact_last_tool_use. A new PR adding an unrelated 0145 migration
    must NOT be blocked by the full-branch check against the deployed promotion
    target (which still has 0143=vm_contact_last_tool_use). The divergence is
    authorized and must be silently passed.

    Blocked path — an UNAUTHORIZED divergence (0142 renamed to a different
    identity on the integration branch, unknown to AUTHORIZED_DIVERGENCES) still
    hard-blocks regardless of the authorized 0143 divergence co-existing on the
    same branch.
    """
    # Integration already carries the 0143 authorized renumber from a prior PR.
    integration_with_authorized = [
        "0142_parent.py",
        "0143_add_compute_job_persistence.py",
        "0144_vm_contact_last_tool_use.py",
    ]
    # Deployed main still has 0143 = vm_contact_last_tool_use (authorized divergence).
    promotion_files = ["0142_parent.py", "0143_vm_contact_last_tool_use.py"]
    # The current PR only adds an unrelated new migration anchored to 0144.
    new_path = f"{_VERSIONS_DIR}/0145_brand_new.py"
    mocker.patch(
        "app.github.get_pr_files",
        new=AsyncMock(return_value=[_migration_file(new_path, "0145", "0144")]),
    )
    mocker.patch(
        "app.github.list_versions_at_ref",
        new=_versions_by_branch(
            integration=integration_with_authorized,
            promotion=promotion_files,
        ),
    )
    # The authorized 0143 divergence must NOT block a new unrelated PR.
    assert await _validate_rollup_migration_chain(
        "tok", "acme", "widget", 1, "automain", promotion_target="main"
    ) == ("ok", None)

    # Unauthorized divergence (0142 renamed to a different identity on integration)
    # must still hard-block even when the authorized 0143 divergence is also present.
    unauthorized_path = f"{_VERSIONS_DIR}/0145_second_new.py"
    mocker.patch(
        "app.github.get_pr_files",
        new=AsyncMock(
            return_value=[_migration_file(unauthorized_path, "0145", "0144")]
        ),
    )
    mocker.patch(
        "app.github.list_versions_at_ref",
        new=_versions_by_branch(
            integration=[
                "0142_renamed_to_something_else.py",  # unauthorized rename
                "0143_add_compute_job_persistence.py",
                "0144_vm_contact_last_tool_use.py",
            ],
            promotion=[
                "0142_parent.py",  # deployed has 0142 = parent (different identity)
                "0143_vm_contact_last_tool_use.py",
            ],
        ),
    )
    status, reason = await _validate_rollup_migration_chain(
        "tok", "acme", "widget", 1, "automain", promotion_target="main"
    )
    assert status == "block"
    assert "0142" in reason


@pytest.mark.asyncio
async def test_premerge_guard_blocks_inplace_edit_of_deployed_migration(mocker):
    """Defect class 2 (v17 regression): a PR that MODIFIES (not renames) a
    migration already on the deployed promotion target. The file keeps its name
    so filename-identity checks see no divergence; only the file-status check
    catches it. The deployed DB has already applied this revision — changing the
    file content is silently ignored at deploy time (Alembic considers it applied)
    while the actual schema change is never applied."""
    # The modified migration exists on main but NOT on automain (e.g. a hotfix
    # not yet back-merged); only the new defect-class-2 check detects this.
    modified_path = f"{_VERSIONS_DIR}/0143_vm_contact_last_tool_use.py"
    mocker.patch(
        "app.github.get_pr_files",
        new=AsyncMock(
            return_value=[_modified_migration_file(modified_path, "0143", "0142")]
        ),
    )
    mocker.patch(
        "app.github.list_versions_at_ref",
        new=_versions_by_branch(
            # Automain does not yet have 0143 (hotfix not back-merged).
            integration=["0141_x.py", "0142_parent.py"],
            # Deployed main carries 0143 as vm_contact_last_tool_use.
            promotion=["0142_parent.py", "0143_vm_contact_last_tool_use.py"],
        ),
    )
    status, reason = await _validate_rollup_migration_chain(
        "tok", "acme", "widget", 1, "automain", promotion_target="main"
    )
    assert status == "block"
    assert "0143_vm_contact_last_tool_use.py" in reason
    assert "immutable" in reason or "deployed" in reason


@pytest.mark.asyncio
async def test_premerge_guard_allows_pr_that_removes_conflicting_integration_file(
    mocker,
):
    """A PR that renames/removes the conflicting integration file resolves the
    divergence via its merge — the guard must not block it.

    Scenario: integration has BOTH 0458_add_perf_indexes.py (matches deployed
    main) AND 0458_task_updated_at_vm_dispatched_at_indexes.py (an earlier
    iteration of the PR that slipped in). The effective automain revision '0458'
    maps to 'task_updated_at_vm_dispatched_at_indexes' (last alphabetically),
    diverging from main's 'add_perf_indexes'. This PR renames the conflicting
    file to a new revision 0502, which means the post-merge integration branch
    will only carry 0458_add_perf_indexes.py — matching main. The guard must
    pass ('ok') rather than blocking."""
    old_path = f"{_VERSIONS_DIR}/0458_task_updated_at_vm_dispatched_at_indexes.py"
    new_path = f"{_VERSIONS_DIR}/0502_task_vm_dispatched_at_index.py"
    mocker.patch(
        "app.github.get_pr_files",
        new=AsyncMock(
            return_value=[
                _renamed_migration_file(old_path, new_path, "0502", "0501"),
            ]
        ),
    )
    mocker.patch(
        "app.github.list_versions_at_ref",
        new=_versions_by_branch(
            # Integration has both 0458 files (the conflict) plus the new head.
            integration=[
                "0457_encrypt_github_pat.py",
                "0458_add_perf_indexes.py",
                "0458_task_updated_at_vm_dispatched_at_indexes.py",
                "0459_no_op_head_marker.py",
                "0501_add_releaseversion_merge_evidence_gaps.py",
            ],
            # Deployed main has 0458 as add_perf_indexes (no task index file).
            promotion=[
                "0457_encrypt_github_pat.py",
                "0458_add_perf_indexes.py",
                "0459_no_op_head_marker.py",
            ],
        ),
    )
    status, reason = await _validate_rollup_migration_chain(
        "tok", "acme", "widget", 1, "automain", promotion_target="main"
    )
    assert status == "ok", f"expected ok but got {status!r}: {reason}"


@pytest.mark.asyncio
async def test_premerge_guard_blocks_pre_pr_rename_not_removed_by_pr(mocker):
    """Verify that the post-merge exclusion only applies when the PR actually
    removes the conflicting file. A PR that adds an unrelated migration while
    the integration branch carries a pre-existing unauthorized divergence (that
    the PR does NOT remove) must still be blocked."""
    # PR only adds an unrelated 0503 migration; it does NOT touch 0458.
    new_path = f"{_VERSIONS_DIR}/0503_unrelated.py"
    mocker.patch(
        "app.github.get_pr_files",
        new=AsyncMock(return_value=[_migration_file(new_path, "0503", "0502")]),
    )
    mocker.patch(
        "app.github.list_versions_at_ref",
        new=_versions_by_branch(
            integration=[
                "0457_encrypt_github_pat.py",
                "0458_add_perf_indexes.py",
                "0458_task_updated_at_vm_dispatched_at_indexes.py",
                "0502_something_else.py",
            ],
            promotion=[
                "0457_encrypt_github_pat.py",
                "0458_add_perf_indexes.py",
            ],
        ),
    )
    status, reason = await _validate_rollup_migration_chain(
        "tok", "acme", "widget", 1, "automain", promotion_target="main"
    )
    assert status == "block"
    assert "0458" in reason
