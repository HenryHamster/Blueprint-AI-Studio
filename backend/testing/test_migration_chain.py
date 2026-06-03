"""Tests for the DB-free Alembic migration-chain helpers and the rollup
auto-merge guard that consumes them."""

from unittest.mock import patch

from app.migration_chain import (
    build_migration_chain,
    find_filename_revision_mismatches,
    find_heads,
    find_roots,
    main,
    parse_revision_identifiers,
    plan_integration_side_renumber,
    plan_migration_reconciliation,
    rewrite_expected_schema_revision,
    validate_added_migrations,
    validate_combined_migration_chain,
)
from app.rollup_management import (
    _extract_rollup_migration_sources,
    _validate_rollup_migration_chain,
)


def _migration_src(revision: str, down_revision: str | None) -> str:
    down = "None" if down_revision is None else f"'{down_revision}'"
    return (
        f"revision = '{revision}'\ndown_revision = {down}\n\ndef upgrade():\n    pass\n"
    )


def _added_file_diff(path: str, revision: str, down_revision: str | None) -> str:
    body = "".join(
        f"+{line}\n" for line in _migration_src(revision, down_revision).splitlines()
    )
    return (
        f"diff --git a/{path} b/{path}\n"
        f"new file mode 100644\n"
        f"index 0000000..1111111\n"
        f"--- /dev/null\n"
        f"+++ b/{path}\n"
        f"@@ -0,0 +1,5 @@\n"
        f"{body}"
    )


# ── Pure helper ─────────────────────────────────────────────────────────────


def test_parse_revision_identifiers():
    rev, down = parse_revision_identifiers(_migration_src("0123", "0122"))
    assert (rev, down) == ("0123", "0122")
    rev, down = parse_revision_identifiers(_migration_src("0001", None))
    assert (rev, down) == ("0001", None)
    assert parse_revision_identifiers("not a migration file") == (None, None)


def test_build_chain_detects_duplicates_and_unparseable():
    sources = {
        "a.py": _migration_src("0123", "0122"),
        "b.py": _migration_src("0123", "0120"),
        "c.py": "# no revision here",
    }
    chain, duplicates, unparseable = build_migration_chain(sources)
    assert duplicates == ["0123"]
    assert unparseable == ["c.py"]
    assert chain["0123"] is not None


def test_find_heads_and_roots_linear():
    chain, _, _ = build_migration_chain(
        {
            "a.py": _migration_src("0124", "0123"),
            "b.py": _migration_src("0125", "0124"),
        }
    )
    assert find_heads(chain) == ["0125"]
    # 0124 anchors to 0123 which is outside this set -> single external root.
    assert find_roots(chain) == ["0124"]


def test_validate_clean_single_head_rollup_passes():
    sources = {
        "backend/alembic/versions/0124_a.py": _migration_src("0124", "0123"),
        "backend/alembic/versions/0125_b.py": _migration_src("0125", "0124"),
    }
    assert validate_added_migrations(sources) is None


def test_validate_empty_set_passes():
    assert validate_added_migrations({}) is None


def test_validate_duplicate_revision_blocks():
    sources = {
        "backend/alembic/versions/0124_a.py": _migration_src("0124", "0123"),
        "backend/alembic/versions/0124_b.py": _migration_src("0124", "0123"),
    }
    reason = validate_added_migrations(sources)
    assert reason is not None and "duplicate" in reason.lower()


def test_validate_two_heads_blocks():
    # Two migrations chained off the same head -> two heads in the combined set.
    sources = {
        "backend/alembic/versions/0124_a.py": _migration_src("0124", "0123"),
        "backend/alembic/versions/0125_b.py": _migration_src("0125", "0123"),
    }
    reason = validate_added_migrations(sources)
    assert reason is not None and "head" in reason.lower()


def test_validate_unparseable_blocks():
    sources = {"backend/alembic/versions/0124_a.py": "# corrupt, no revision"}
    reason = validate_added_migrations(sources)
    assert reason is not None and "parse" in reason.lower()


# ── Filename/revision desync ─────────────────────────────────────────────────


def test_find_filename_revision_mismatches_clean_set():
    sources = {
        "backend/alembic/versions/0124_a.py": _migration_src("0124", "0123"),
        "backend/alembic/versions/0125_b.py": _migration_src("0125", "0124"),
    }
    assert find_filename_revision_mismatches(sources) == []


def test_find_filename_revision_mismatches_detects_desync():
    # File renamed to 0125_ but in-file revision left at 0124.
    sources = {"backend/alembic/versions/0125_x.py": _migration_src("0124", "0123")}
    mismatches = find_filename_revision_mismatches(sources)
    assert len(mismatches) == 1
    assert "0125_x.py" in mismatches[0]
    assert "0125" in mismatches[0] and "0124" in mismatches[0]


def test_find_filename_revision_mismatches_skips_non_numeric_keys():
    # Bare names without a numeric NNNN prefix (used by build_migration_chain
    # callers) must not be flagged even though revision != name.
    sources = {
        "a.py": _migration_src("0124", "0123"),
        "b.py": _migration_src("0125", "0124"),
    }
    assert find_filename_revision_mismatches(sources) == []


def test_validate_filename_revision_desync_blocks():
    sources = {"backend/alembic/versions/0125_x.py": _migration_src("0124", "0123")}
    reason = validate_added_migrations(sources)
    assert reason is not None
    assert "0125_x.py" in reason
    assert "0125" in reason and "0124" in reason


def test_validate_unparseable_takes_precedence_over_filename_check():
    # A numeric-prefix file with no revision must report the unparseable reason,
    # not the filename-mismatch reason (unparseable guard runs first).
    sources = {"backend/alembic/versions/0125_x.py": "# corrupt, no revision"}
    reason = validate_added_migrations(sources)
    assert reason is not None and "parse" in reason.lower()


# ── Diff extraction ─────────────────────────────────────────────────────────


def test_extract_only_migration_files():
    diff = (
        _added_file_diff("backend/alembic/versions/0124_a.py", "0124", "0123")
        + "diff --git a/backend/app/models.py b/backend/app/models.py\n"
        "--- a/backend/app/models.py\n"
        "+++ b/backend/app/models.py\n"
        "@@ -1 +1 @@\n"
        "+# unrelated change\n"
    )
    sources = _extract_rollup_migration_sources(diff)
    assert set(sources) == {"backend/alembic/versions/0124_a.py"}
    assert parse_revision_identifiers(
        sources["backend/alembic/versions/0124_a.py"]
    ) == (
        "0124",
        "0123",
    )


def test_extract_no_migration_files():
    diff = (
        "diff --git a/backend/app/models.py b/backend/app/models.py\n"
        "--- a/backend/app/models.py\n"
        "+++ b/backend/app/models.py\n"
        "@@ -1 +1 @@\n"
        "+# unrelated change\n"
    )
    assert _extract_rollup_migration_sources(diff) == {}


# ── Async guard ─────────────────────────────────────────────────────────────


def _files(*paths: str) -> list[dict]:
    return [{"filename": p, "status": "added"} for p in paths]


async def test_guard_clean_rollup_ok():
    diff = _added_file_diff("backend/alembic/versions/0124_a.py", "0124", "0123")
    with (
        patch(
            "app.github.get_pr_files",
            return_value=_files("backend/alembic/versions/0124_a.py"),
        ),
        patch("app.github.get_pull_request_diff", return_value=diff),
    ):
        assert await _validate_rollup_migration_chain("t", "o", "r", 1) == ("ok", None)


async def test_guard_no_migrations_ok_skips_diff():
    with (
        patch("app.github.get_pr_files", return_value=_files("backend/app/models.py")),
        patch("app.github.get_pull_request_diff", side_effect=AssertionError) as diff_m,
    ):
        assert await _validate_rollup_migration_chain("t", "o", "r", 1) == ("ok", None)
        diff_m.assert_not_called()


async def test_guard_duplicate_revision_blocks():
    diff = _added_file_diff(
        "backend/alembic/versions/0124_a.py", "0124", "0123"
    ) + _added_file_diff("backend/alembic/versions/0124_b.py", "0124", "0123")
    with (
        patch(
            "app.github.get_pr_files",
            return_value=_files(
                "backend/alembic/versions/0124_a.py",
                "backend/alembic/versions/0124_b.py",
            ),
        ),
        patch("app.github.get_pull_request_diff", return_value=diff),
    ):
        status, reason = await _validate_rollup_migration_chain("t", "o", "r", 1)
    assert status == "block" and "duplicate" in reason.lower()


async def test_guard_two_heads_blocks():
    diff = _added_file_diff(
        "backend/alembic/versions/0124_a.py", "0124", "0123"
    ) + _added_file_diff("backend/alembic/versions/0125_b.py", "0125", "0123")
    with (
        patch(
            "app.github.get_pr_files",
            return_value=_files(
                "backend/alembic/versions/0124_a.py",
                "backend/alembic/versions/0125_b.py",
            ),
        ),
        patch("app.github.get_pull_request_diff", return_value=diff),
    ):
        status, reason = await _validate_rollup_migration_chain("t", "o", "r", 1)
    assert status == "block" and "head" in reason.lower()


async def test_guard_filename_revision_desync_blocks():
    # File 0125_x.py declares revision 0124 -> desync must BLOCK auto-merge.
    diff = _added_file_diff("backend/alembic/versions/0125_x.py", "0124", "0123")
    with (
        patch(
            "app.github.get_pr_files",
            return_value=_files("backend/alembic/versions/0125_x.py"),
        ),
        patch("app.github.get_pull_request_diff", return_value=diff),
    ):
        status, reason = await _validate_rollup_migration_chain("t", "o", "r", 1)
    assert status == "block"
    assert "0125_x.py" in reason
    assert "0125" in reason and "0124" in reason


async def test_guard_empty_diff_defers_failsafe():
    with (
        patch(
            "app.github.get_pr_files",
            return_value=_files("backend/alembic/versions/0124_a.py"),
        ),
        patch("app.github.get_pull_request_diff", return_value=""),
    ):
        status, reason = await _validate_rollup_migration_chain("t", "o", "r", 1)
    assert status == "defer" and reason


async def test_guard_pr_files_error_defers_failsafe():
    with patch("app.github.get_pr_files", side_effect=RuntimeError("github down")):
        status, reason = await _validate_rollup_migration_chain("t", "o", "r", 1)
    assert status == "defer" and "github down" in reason


# ── main() standalone guard ──────────────────────────────────────────────────


def _write_migration(
    tmp_path, filename: str, revision: str, down_revision: str | None
) -> None:
    (tmp_path / filename).write_text(_migration_src(revision, down_revision))


def test_main_clean_single_head_chain_passes(tmp_path, capsys):
    _write_migration(tmp_path, "0001_root.py", "0001", None)
    _write_migration(tmp_path, "0002_next.py", "0002", "0001")
    result = main(str(tmp_path))
    assert result == 0
    out = capsys.readouterr().out
    assert "OK" in out


def test_main_filename_revision_mismatch_fails(tmp_path, capsys):
    # File is named 0001_... but contains revision = '9999'
    (tmp_path / "0001_root.py").write_text(_migration_src("9999", None))
    result = main(str(tmp_path))
    assert result == 1
    out = capsys.readouterr().out
    assert "0001_root.py" in out
    assert "9999" in out


def test_main_two_heads_fails(tmp_path, capsys):
    # Both 0002 and 0003 chain off 0001 -> two heads
    _write_migration(tmp_path, "0001_root.py", "0001", None)
    _write_migration(tmp_path, "0002_a.py", "0002", "0001")
    _write_migration(tmp_path, "0003_b.py", "0003", "0001")
    result = main(str(tmp_path))
    assert result == 1
    out = capsys.readouterr().out
    assert "head" in out.lower()


def test_main_empty_dir_passes(tmp_path, capsys):
    result = main(str(tmp_path))
    assert result == 0


# ── Combined chain validation (target-branch-aware) ─────────────────────────


def test_validate_combined_clean_passes():
    """A rollup migration correctly anchored to the target head passes."""
    existing_filenames = ["0135_one.py", "0136_prev.py"]
    added_sources = {
        "backend/alembic/versions/0137_new.py": _migration_src("0137", "0136"),
    }
    assert validate_combined_migration_chain(existing_filenames, added_sources) is None


def test_validate_combined_duplicate_revision_blocks():
    """A revision id already present on the target branch causes a block."""
    existing_filenames = ["0136_prev.py", "0137_existing.py"]
    added_sources = {
        "backend/alembic/versions/0137_new.py": _migration_src("0137", "0136"),
    }
    reason = validate_combined_migration_chain(existing_filenames, added_sources)
    assert reason is not None
    assert "0137" in reason
    assert "already exists" in reason.lower() or "renumber" in reason.lower()


def test_validate_combined_stale_anchor_blocks():
    """A migration anchored to an older revision (not the target head) is blocked."""
    # head is 0136 but the rollup chains off 0135 (stale)
    existing_filenames = ["0135_one.py", "0136_two.py"]
    added_sources = {
        "backend/alembic/versions/0137_new.py": _migration_src("0137", "0135"),
    }
    reason = validate_combined_migration_chain(existing_filenames, added_sources)
    assert reason is not None
    assert "0135" in reason and "0136" in reason
    assert "rechain" in reason.lower() or "head" in reason.lower()


def test_validate_combined_no_existing_degrades_to_isolation():
    """Empty existing_filenames degrades to the in-isolation check."""
    added_sources = {
        "backend/alembic/versions/0001_root.py": _migration_src("0001", None),
    }
    assert validate_combined_migration_chain([], added_sources) is None


def test_validate_combined_empty_added_passes():
    """No added migrations is always valid regardless of existing chain."""
    assert validate_combined_migration_chain(["0136_prev.py"], {}) is None


# ── Async guard with target_branch ──────────────────────────────────────────


async def test_guard_with_target_branch_clean_passes():
    """Guard passes when rollup migration correctly anchors to the target head."""
    diff = _added_file_diff("backend/alembic/versions/0137_new.py", "0137", "0136")
    with (
        patch(
            "app.github.get_pr_files",
            return_value=_files("backend/alembic/versions/0137_new.py"),
        ),
        patch("app.github.get_pull_request_diff", return_value=diff),
        patch(
            "app.github.list_versions_at_ref",
            return_value=["0135_one.py", "0136_prev.py"],
        ),
    ):
        assert await _validate_rollup_migration_chain("t", "o", "r", 1, "automain") == (
            "ok",
            None,
        )


async def test_guard_with_target_branch_duplicate_blocks():
    """Guard blocks when the rollup adds a revision already on the target branch."""
    diff = _added_file_diff("backend/alembic/versions/0137_new.py", "0137", "0136")
    with (
        patch(
            "app.github.get_pr_files",
            return_value=_files("backend/alembic/versions/0137_new.py"),
        ),
        patch("app.github.get_pull_request_diff", return_value=diff),
        patch(
            "app.github.list_versions_at_ref",
            return_value=["0136_prev.py", "0137_existing.py"],
        ),
    ):
        status, reason = await _validate_rollup_migration_chain(
            "t", "o", "r", 1, "automain"
        )
    assert status == "block"
    assert "0137" in reason
    assert "already exists" in reason.lower() or "renumber" in reason.lower()


async def test_guard_with_target_branch_stale_anchor_blocks():
    """Guard blocks when the rollup migration anchors to a stale down_revision."""
    # head is 0137 but rollup chains off 0136 (stale)
    diff = _added_file_diff("backend/alembic/versions/0138_new.py", "0138", "0136")
    with (
        patch(
            "app.github.get_pr_files",
            return_value=_files("backend/alembic/versions/0138_new.py"),
        ),
        patch("app.github.get_pull_request_diff", return_value=diff),
        patch(
            "app.github.list_versions_at_ref",
            return_value=["0136_prev.py", "0137_other.py"],
        ),
    ):
        status, reason = await _validate_rollup_migration_chain(
            "t", "o", "r", 1, "automain"
        )
    assert status == "block"
    assert "0138" in reason or "0136" in reason


async def test_guard_with_target_branch_list_error_defers():
    """Guard defers (never waves through) when target branch listing fails."""
    diff = _added_file_diff("backend/alembic/versions/0137_new.py", "0137", "0136")
    with (
        patch(
            "app.github.get_pr_files",
            return_value=_files("backend/alembic/versions/0137_new.py"),
        ),
        patch("app.github.get_pull_request_diff", return_value=diff),
        patch(
            "app.github.list_versions_at_ref",
            side_effect=RuntimeError("network error"),
        ),
    ):
        status, reason = await _validate_rollup_migration_chain(
            "t", "o", "r", 1, "automain"
        )
    assert status == "defer"
    assert "network error" in reason


# ── plan_migration_reconciliation (pure renumber planner) ───────────────────


def test_plan_reconciliation_collision_renumbers_and_rechains():
    """A single migration whose revision collides with an existing one is
    renumbered to head+1 and rechained onto the current head."""
    existing = ["0136_prev.py", "0137_existing.py"]
    added = {
        "backend/alembic/versions/0137_new.py": _migration_src("0137", "0136"),
    }
    plan = plan_migration_reconciliation(existing, added)
    assert plan is not None
    assert plan["old_revision"] == "0137"
    assert plan["new_revision"] == "0138"
    assert plan["new_down_revision"] == "0137"
    assert plan["new_path"] == "backend/alembic/versions/0138_new.py"
    assert plan["expected_schema_revision"] == "0138"
    # Rewritten body reflects the new revision + anchor, and revalidates clean.
    rev, down = parse_revision_identifiers(plan["new_source"])
    assert (rev, down) == ("0138", "0137")
    assert (
        validate_combined_migration_chain(existing, {plan["new_path"]: plan["new_source"]})
        is None
    )


def test_plan_reconciliation_stale_anchor_rechains():
    """A single migration anchored to a no-longer-head revision is rechained
    onto the current head (and renumbered to the next free revision)."""
    existing = ["0136_prev.py", "0137_other.py"]
    added = {
        "backend/alembic/versions/0138_new.py": _migration_src("0138", "0136"),
    }
    plan = plan_migration_reconciliation(existing, added)
    assert plan is not None
    assert plan["new_revision"] == "0138"
    assert plan["new_down_revision"] == "0137"
    rev, down = parse_revision_identifiers(plan["new_source"])
    assert (rev, down) == ("0138", "0137")


def test_plan_reconciliation_already_valid_returns_none():
    """A migration that already chains cleanly onto the head needs no plan."""
    existing = ["0136_prev.py", "0137_head.py"]
    added = {
        "backend/alembic/versions/0138_new.py": _migration_src("0138", "0137"),
    }
    assert plan_migration_reconciliation(existing, added) is None


def test_plan_reconciliation_multiple_added_returns_none():
    """Multiple added migrations are never auto-reconciled (ambiguous)."""
    existing = ["0136_prev.py", "0137_head.py"]
    added = {
        "backend/alembic/versions/0138_a.py": _migration_src("0138", "0137"),
        "backend/alembic/versions/0139_b.py": _migration_src("0139", "0138"),
    }
    assert plan_migration_reconciliation(existing, added) is None


def test_plan_reconciliation_multi_head_returns_none():
    """Two added migrations resolving to two heads are not mechanical."""
    existing = ["0137_head.py"]
    added = {
        "backend/alembic/versions/0138_a.py": _migration_src("0138", "0137"),
        "backend/alembic/versions/0139_b.py": _migration_src("0139", "0137"),
    }
    assert plan_migration_reconciliation(existing, added) is None


def test_plan_reconciliation_unparseable_returns_none():
    """An unparseable migration file yields no plan."""
    existing = ["0137_head.py"]
    added = {"backend/alembic/versions/0138_new.py": "def upgrade():\n    pass\n"}
    assert plan_migration_reconciliation(existing, added) is None


def test_plan_reconciliation_no_existing_chain_returns_none():
    """With no existing migrations there is nothing to collide with."""
    added = {
        "backend/alembic/versions/0138_new.py": _migration_src("0138", "0137"),
    }
    assert plan_migration_reconciliation([], added) is None


def test_plan_reconciliation_stale_anchor_to_none_rechains():
    """A root-anchored (down_revision = None) migration added to a non-empty
    target branch is rechained onto the head."""
    existing = ["0136_prev.py", "0137_head.py"]
    added = {
        "backend/alembic/versions/0137_new.py": _migration_src("0137", None),
    }
    plan = plan_migration_reconciliation(existing, added)
    assert plan is not None
    rev, down = parse_revision_identifiers(plan["new_source"])
    assert (rev, down) == ("0138", "0137")


# ── plan_integration_side_renumber (back-merge mirror planner) ──────────────


def test_plan_integration_collision_no_descendants_renumbers_and_rechains():
    """The integration migration whose revision collides with the immutable
    promotion-target head is renumbered onto the unified head; with no
    descendants it becomes the new head itself."""
    # main (immutable): ... 0136 <- 0137(hotfix). automain independently reused
    # 0137 for a feature migration anchored to the shared 0136 ancestor.
    target = ["0136_prev.py", "0137_hotfix.py"]
    integration = {
        "backend/alembic/versions/0137_feature.py": _migration_src("0137", "0136"),
    }
    plan = plan_integration_side_renumber(target, integration)
    assert plan is not None
    assert plan["old_revision"] == "0137"
    assert plan["new_revision"] == "0138"
    assert plan["new_down_revision"] == "0137"
    assert plan["old_path"] == "backend/alembic/versions/0137_feature.py"
    assert plan["new_path"] == "backend/alembic/versions/0138_feature.py"
    assert plan["expected_schema_revision"] == "0138"
    assert plan["deletes"] == ["backend/alembic/versions/0137_feature.py"]
    assert set(plan["upserts"]) == {"backend/alembic/versions/0138_feature.py"}
    rev, down = parse_revision_identifiers(
        plan["upserts"]["backend/alembic/versions/0138_feature.py"]
    )
    assert (rev, down) == ("0138", "0137")
    # The reconciled integration set revalidates clean against the immutable target.
    assert validate_combined_migration_chain(target, plan["upserts"]) is None


def test_plan_integration_collision_linear_tail_rechains_only_immediate_child():
    """A colliding migration with a linear descendant tail: the colliding file is
    renumbered above the unified head, the IMMEDIATE descendant is rechained onto
    the new revision, and the rest of the tail keeps its numbers untouched."""
    target = ["0136_prev.py", "0137_hotfix.py"]
    integration = {
        "backend/alembic/versions/0137_feat.py": _migration_src("0137", "0136"),  # C
        "backend/alembic/versions/0138_b.py": _migration_src("0138", "0137"),  # D
        "backend/alembic/versions/0139_c.py": _migration_src("0139", "0138"),  # E
    }
    plan = plan_integration_side_renumber(target, integration)
    assert plan is not None
    # C renumbered to max(all)+1 == 0140, anchored to the unified head 0137.
    assert plan["old_revision"] == "0137"
    assert plan["new_revision"] == "0140"
    assert plan["new_down_revision"] == "0137"
    assert plan["deletes"] == ["backend/alembic/versions/0137_feat.py"]
    # upserts: the renamed colliding file AND the rechained immediate descendant.
    assert set(plan["upserts"]) == {
        "backend/alembic/versions/0140_feat.py",
        "backend/alembic/versions/0138_b.py",
    }
    c_rev, c_down = parse_revision_identifiers(
        plan["upserts"]["backend/alembic/versions/0140_feat.py"]
    )
    assert (c_rev, c_down) == ("0140", "0137")
    # D keeps its revision 0138 but is rechained off the renumbered C (0140).
    d_rev, d_down = parse_revision_identifiers(
        plan["upserts"]["backend/alembic/versions/0138_b.py"]
    )
    assert (d_rev, d_down) == ("0138", "0140")
    # E is the tail end — untouched (still 0139 <- 0138) and remains the head.
    assert "backend/alembic/versions/0139_c.py" not in plan["upserts"]
    assert plan["expected_schema_revision"] == "0139"
    # The full reconciled integration set revalidates clean.
    reconciled = {
        p: s
        for p, s in integration.items()
        if p not in plan["deletes"]
    }
    reconciled.update(plan["upserts"])
    assert validate_combined_migration_chain(target, reconciled) is None


def test_plan_integration_no_collision_returns_none():
    """An integration migration that already chains cleanly above the head (no
    revision-id collision) needs no plan."""
    target = ["0136_prev.py", "0137_hotfix.py"]
    integration = {
        "backend/alembic/versions/0138_new.py": _migration_src("0138", "0137"),
    }
    assert plan_integration_side_renumber(target, integration) is None


def test_plan_integration_multiple_collisions_returns_none():
    """More than one colliding integration migration is never auto-reconciled."""
    target = ["0136_a.py", "0137_b.py"]
    integration = {
        "backend/alembic/versions/0136_x.py": _migration_src("0136", "0135"),
        "backend/alembic/versions/0137_y.py": _migration_src("0137", "0136"),
    }
    assert plan_integration_side_renumber(target, integration) is None


def test_plan_integration_fanout_tail_returns_none():
    """A colliding migration with more than one child (a fan-out, not a linear
    tail) is not mechanically reconcilable."""
    target = ["0136_prev.py", "0137_hotfix.py"]
    integration = {
        "backend/alembic/versions/0137_c.py": _migration_src("0137", "0136"),  # C
        "backend/alembic/versions/0138_d.py": _migration_src("0138", "0137"),  # child
        "backend/alembic/versions/0139_e.py": _migration_src("0139", "0137"),  # child
    }
    assert plan_integration_side_renumber(target, integration) is None


def test_plan_integration_parse_failure_returns_none():
    """An unparseable integration migration file yields no plan."""
    target = ["0136_prev.py", "0137_hotfix.py"]
    integration = {
        "backend/alembic/versions/0137_c.py": _migration_src("0137", "0136"),
        "backend/alembic/versions/0138_d.py": "def upgrade():\n    pass\n",
    }
    assert plan_integration_side_renumber(target, integration) is None


def test_plan_integration_not_anchored_to_shared_ancestor_returns_none():
    """A colliding migration anchored to another integration migration (not the
    shared promotion-target ancestor) is a non-mechanical fork — return None."""
    target = ["0136_prev.py", "0137_hotfix.py"]
    integration = {
        # 0137 collides, but it anchors to 0150 which is NOT on the target chain.
        "backend/alembic/versions/0137_c.py": _migration_src("0137", "0150"),
        "backend/alembic/versions/0150_b.py": _migration_src("0150", "0136"),
    }
    assert plan_integration_side_renumber(target, integration) is None


def test_plan_integration_not_single_head_returns_none():
    """A renumber that would not resolve to exactly one clean head (the
    integration set carries a second, disconnected head) returns None — caught
    by the combined-chain re-validation before returning."""
    target = ["0136_prev.py", "0137_hotfix.py"]
    integration = {
        # 0137 collides and is anchored to the shared 0136 ancestor (mechanical),
        # but 0150 is a disconnected second head that cannot be reconciled away.
        "backend/alembic/versions/0137_c.py": _migration_src("0137", "0136"),
        "backend/alembic/versions/0150_x.py": _migration_src("0150", "0149"),
    }
    assert plan_integration_side_renumber(target, integration) is None


def test_plan_integration_empty_inputs_return_none():
    """No promotion-target chain, or no integration migrations, yields no plan."""
    integration = {
        "backend/alembic/versions/0137_c.py": _migration_src("0137", "0136"),
    }
    assert plan_integration_side_renumber([], integration) is None
    assert plan_integration_side_renumber(["0136_p.py", "0137_h.py"], {}) is None


# ── rewrite_expected_schema_revision ────────────────────────────────────────


def test_rewrite_expected_schema_revision_replaces_literal():
    src = 'X = 1\nEXPECTED_SCHEMA_REVISION = "0137"\nY = 2\n'
    out, n = rewrite_expected_schema_revision(src, "0138")
    assert n == 1
    assert 'EXPECTED_SCHEMA_REVISION = "0138"' in out
    assert '"0137"' not in out


def test_rewrite_expected_schema_revision_single_quotes_normalised():
    src = "EXPECTED_SCHEMA_REVISION = '0137'\n"
    out, n = rewrite_expected_schema_revision(src, "0140")
    assert n == 1
    assert out.strip() == 'EXPECTED_SCHEMA_REVISION = "0140"'


def test_rewrite_expected_schema_revision_absent_returns_zero():
    src = "SOMETHING_ELSE = 1\n"
    out, n = rewrite_expected_schema_revision(src, "0138")
    assert n == 0
    assert out == src
