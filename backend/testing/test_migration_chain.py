"""Tests for the DB-free Alembic migration-chain helpers and the rollup
auto-merge guard that consumes them."""

import ast
from pathlib import Path
from unittest.mock import patch

from app.migration_chain import (
    build_migration_chain,
    find_filename_revision_mismatches,
    find_heads,
    find_roots,
    main,
    next_revision,
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


def test_migration_files_do_not_import_app_modules():
    """Candidate migrations run in the serving process during promotion checks."""
    versions_dir = Path(__file__).resolve().parents[1] / "alembic" / "versions"
    # These two older, applied data migrations predate the promotion apply gate.
    # Keep the exception explicit: all new migration imports must be caught.
    legacy_offenders = {
        "0472_encrypt_oauth_token.py",
        "0531_backfill_encrypted_columns.py",
    }
    offenders = [
        path.name
        for path in versions_dir.glob("*.py")
        if path.name not in legacy_offenders
        and any(
            (
                isinstance(node, ast.ImportFrom)
                and node.module is not None
                and (node.module == "app" or node.module.startswith("app."))
            )
            or (
                isinstance(node, ast.Import)
                and any(
                    name.name == "app" or name.name.startswith("app.")
                    for name in node.names
                )
            )
            for node in ast.walk(ast.parse(path.read_text()))
        )
    ]
    assert offenders == [], (
        "Alembic migrations must not import app modules: the promotion apply gate "
        "executes candidate migrations in the serving process, where app imports "
        "resolve against the serving tree rather than the candidate. "
        f"Offenders: {offenders}"
    )
# ── Pure helper ─────────────────────────────────────────────────────────────


def test_next_revision_is_max_plus_one_padded():
    # Max + 1, zero-padded to the widest existing id's width.
    assert next_revision(["0151", "0149", "0150"]) == "0152"
    assert next_revision(["0151"]) == "0152"
    # Order independent; a single id still increments.
    assert next_revision(["0009"]) == "0010"


def test_next_revision_preserves_width():
    # The pad width follows the WIDEST existing id, not the value's natural width.
    assert next_revision(["0099"]) == "0100"
    assert next_revision(["00099"]) == "00100"
    # A wider id in the set widens the result even if it is not the max.
    assert next_revision(["123", "0007"]) == "0124"


def test_next_revision_empty_and_non_numeric():
    # No numeric ids → the conventional first revision.
    assert next_revision([]) == "0001"
    assert next_revision(["abc", "head"]) == "0001"
    # Non-numeric ids (branch labels) are ignored; numbering follows the digits.
    assert next_revision(["0151", "branch_label"]) == "0152"


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


def _migration_file(path: str, revision: str, down_revision: str | None) -> dict:
    """Create a pr_files entry for a migration file with a per-file patch."""
    down = "None" if down_revision is None else f"'{down_revision}'"
    src = (
        f"revision = '{revision}'\ndown_revision = {down}\n\ndef upgrade():\n    pass\n"
    )
    body = "".join(f"+{line}\n" for line in src.splitlines())
    return {"filename": path, "status": "added", "patch": f"@@ -0,0 +1,5 @@\n{body}"}


async def test_guard_clean_rollup_ok():
    with patch(
        "app.github.get_pr_files",
        return_value=[
            _migration_file("backend/alembic/versions/0124_a.py", "0124", "0123")
        ],
    ):
        assert await _validate_rollup_migration_chain("t", "o", "r", 1) == ("ok", None)


async def test_guard_no_migrations_ok_skips_diff():
    with (
        patch("app.github.get_pr_files", return_value=_files("backend/app/models.py")),
        patch("app.github.get_pull_request", side_effect=AssertionError) as pr_m,
    ):
        assert await _validate_rollup_migration_chain("t", "o", "r", 1) == ("ok", None)
        pr_m.assert_not_called()


async def test_guard_duplicate_revision_blocks():
    with patch(
        "app.github.get_pr_files",
        return_value=[
            _migration_file("backend/alembic/versions/0124_a.py", "0124", "0123"),
            _migration_file("backend/alembic/versions/0124_b.py", "0124", "0123"),
        ],
    ):
        status, reason = await _validate_rollup_migration_chain("t", "o", "r", 1)
    assert status == "block" and "duplicate" in reason.lower()


async def test_guard_two_heads_blocks():
    with patch(
        "app.github.get_pr_files",
        return_value=[
            _migration_file("backend/alembic/versions/0124_a.py", "0124", "0123"),
            _migration_file("backend/alembic/versions/0125_b.py", "0125", "0123"),
        ],
    ):
        status, reason = await _validate_rollup_migration_chain("t", "o", "r", 1)
    assert status == "block" and "head" in reason.lower()


async def test_guard_filename_revision_desync_blocks():
    # File 0125_x.py declares revision 0124 -> desync must BLOCK auto-merge.
    with patch(
        "app.github.get_pr_files",
        return_value=[
            _migration_file("backend/alembic/versions/0125_x.py", "0124", "0123")
        ],
    ):
        status, reason = await _validate_rollup_migration_chain("t", "o", "r", 1)
    assert status == "block"
    assert "0125_x.py" in reason
    assert "0125" in reason and "0124" in reason


async def test_guard_missing_patch_defers_failsafe():
    # A migration file with no per-file patch triggers the contents-API fallback;
    # if that fallback also fails the guard must defer, never wave through.
    with (
        patch(
            "app.github.get_pr_files",
            return_value=_files("backend/alembic/versions/0124_a.py"),
        ),
        patch(
            "app.github.get_pull_request",
            side_effect=RuntimeError("network error"),
        ),
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


def test_validate_combined_backwards_numbered_head_blocks():
    """A migration correctly anchored to the head but numbered BELOW it (a
    backwards renumber) is blocked by the combined-monotonicity check even
    though it passes the collision and anchor checks — numeric-max head
    detection would silently mis-identify the head here."""
    existing_filenames = ["0525_a.py", "0526_b.py"]  # head is 0526
    added_sources = {
        # Anchored to the real head 0526, but its own id 0300 is below it.
        "backend/alembic/versions/0300_new.py": _migration_src("0300", "0526"),
    }
    reason = validate_combined_migration_chain(existing_filenames, added_sources)
    assert reason is not None
    assert "0300" in reason and "0526" in reason
    assert "non-monotonic" in reason.lower()
    assert "renumber" in reason.lower()


def test_validate_combined_stale_anchor_instructs_renumber_not_rechain_existing():
    """The stale-anchor (b) message must instruct RENUMBERING the new migration
    onto the current head, never rechaining an existing migration onto a higher
    number — the latter is what produced the migration-identity divergence."""
    existing_filenames = ["0135_one.py", "0136_two.py"]  # head is 0136
    added_sources = {
        "backend/alembic/versions/0137_new.py": _migration_src("0137", "0135"),
    }
    reason = validate_combined_migration_chain(existing_filenames, added_sources)
    assert reason is not None
    lowered = reason.lower()
    assert "renumber your new migration onto the current head '0136'" in lowered
    assert "never rechain an existing migration" in lowered


# ── Async guard with target_branch ──────────────────────────────────────────


async def test_guard_with_target_branch_clean_passes():
    """Guard passes when rollup migration correctly anchors to the target head."""
    with (
        patch(
            "app.github.get_pr_files",
            return_value=[
                _migration_file("backend/alembic/versions/0137_new.py", "0137", "0136")
            ],
        ),
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
    with (
        patch(
            "app.github.get_pr_files",
            return_value=[
                _migration_file("backend/alembic/versions/0137_new.py", "0137", "0136")
            ],
        ),
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
    with (
        patch(
            "app.github.get_pr_files",
            return_value=[
                _migration_file("backend/alembic/versions/0138_new.py", "0138", "0136")
            ],
        ),
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
    with (
        patch(
            "app.github.get_pr_files",
            return_value=[
                _migration_file("backend/alembic/versions/0137_new.py", "0137", "0136")
            ],
        ),
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


# ── enforce_premerge_migration_guard (block-OR-defer => do-not-merge) ───────


async def test_enforce_guard_clean_chain_allows_merge():
    """An ``ok`` verdict returns (True, None) — safe to merge."""
    from app.rollup_migration_validation import enforce_premerge_migration_guard

    with (
        patch(
            "app.github.get_pr_files",
            return_value=[
                _migration_file("backend/alembic/versions/0137_new.py", "0137", "0136")
            ],
        ),
        patch(
            "app.github.list_versions_at_ref",
            return_value=["0135_one.py", "0136_prev.py"],
        ),
    ):
        ok, reason = await enforce_premerge_migration_guard(
            "t", "o", "r", 1, "automain"
        )
    assert ok is True and reason is None


async def test_enforce_guard_dual_head_holds_merge():
    """A ``block`` verdict returns (False, reason) — do not merge."""
    from app.rollup_migration_validation import enforce_premerge_migration_guard

    with (
        patch(
            "app.github.get_pr_files",
            return_value=[
                _migration_file("backend/alembic/versions/0137_new.py", "0137", "0136")
            ],
        ),
        patch(
            "app.github.list_versions_at_ref",
            return_value=["0136_prev.py", "0137_existing.py"],
        ),
    ):
        ok, reason = await enforce_premerge_migration_guard(
            "t", "o", "r", 1, "automain"
        )
    assert ok is False
    assert reason and "0137" in reason


async def test_enforce_guard_fetch_failure_holds_merge():
    """A ``defer`` verdict (fetch failure) returns (False, reason) — fail-safe hold."""
    from app.rollup_migration_validation import enforce_premerge_migration_guard

    with patch("app.github.get_pr_files", side_effect=RuntimeError("github down")):
        ok, reason = await enforce_premerge_migration_guard(
            "t", "o", "r", 1, "automain"
        )
    assert ok is False
    assert reason and "github down" in reason


async def test_enforce_guard_deployed_id_reuse_holds_merge():
    """Regression: a candidate migration reusing a revision id already deployed on
    the promotion target (the 0309-class collision) must be BLOCKED when
    promotion_target is supplied, and allowed when it is omitted.

    Mirrors the automain/main collision: 0309_performance_indexes on automain
    reused the revision id 0309 already stamped to computejob_heartbeat on main.
    """
    from app.rollup_migration_validation import enforce_premerge_migration_guard

    # The PR adds 0309_performance_indexes.py (revision=0309, different identity
    # from the deployed 0309_computejob_heartbeat.py on main).
    pr_files = [
        _migration_file(
            "backend/alembic/versions/0309_performance_indexes.py", "0309", "0308"
        )
    ]

    def _list_versions_side_effect(token, owner, repo, ref):
        if ref == "main":
            # Deployed promotion target has 0309=computejob_heartbeat — DIFFERENT body.
            return ["0308_prev.py", "0309_computejob_heartbeat.py"]
        # Integration branch (automain) does not yet have 0309.
        return ["0308_prev.py"]

    with (
        patch("app.github.get_pr_files", return_value=pr_files),
        patch(
            "app.github.list_versions_at_ref",
            side_effect=_list_versions_side_effect,
        ),
    ):
        # WITH promotion_target — must block (cross-branch deployed-id reuse).
        ok, reason = await enforce_premerge_migration_guard(
            "t", "o", "r", 1, "automain", promotion_target="main"
        )
    assert ok is False, "guard must block deployed-id reuse when promotion_target given"
    assert reason is not None and "0309" in reason

    with (
        patch("app.github.get_pr_files", return_value=pr_files),
        patch(
            "app.github.list_versions_at_ref",
            side_effect=_list_versions_side_effect,
        ),
    ):
        # WITHOUT promotion_target — must pass (guard was dormant before this fix).
        ok_no_pt, _ = await enforce_premerge_migration_guard(
            "t", "o", "r", 1, "automain"
        )
    assert ok_no_pt is True, (
        "guard must pass when promotion_target is None (dormant path)"
    )

    with (
        patch("app.github.get_pr_files", return_value=pr_files),
        patch(
            "app.github.list_versions_at_ref",
            side_effect=_list_versions_side_effect,
        ),
    ):
        # promotion_target == target_branch — guard skips cross-branch check.
        ok_same, _ = await enforce_premerge_migration_guard(
            "t", "o", "r", 1, "automain", promotion_target="automain"
        )
    assert ok_same is True, "guard must pass when promotion_target equals target_branch"


async def test_enforce_guard_clean_chain_allows_merge_with_promotion_target():
    """A clean linear chain passes even when promotion_target is supplied — no
    false-positive when the candidate revision id is not reused on the target."""
    from app.rollup_migration_validation import enforce_premerge_migration_guard

    pr_files = [_migration_file("backend/alembic/versions/0137_new.py", "0137", "0136")]

    def _list_versions_side_effect(token, owner, repo, ref):
        # Both integration branch and promotion target share the same clean chain
        # up to 0136; 0137 only exists on the PR branch.
        return ["0135_one.py", "0136_prev.py"]

    with (
        patch("app.github.get_pr_files", return_value=pr_files),
        patch(
            "app.github.list_versions_at_ref",
            side_effect=_list_versions_side_effect,
        ),
    ):
        ok, reason = await enforce_premerge_migration_guard(
            "t", "o", "r", 1, "automain", promotion_target="main"
        )
    assert ok is True and reason is None


async def test_enforce_guard_promotion_target_fetch_error_defers():
    """A fetch failure for the promotion target is fail-safe: returns (False, reason)."""
    from app.rollup_migration_validation import enforce_premerge_migration_guard

    pr_files = [_migration_file("backend/alembic/versions/0137_new.py", "0137", "0136")]

    call_count = 0

    def _list_versions_side_effect(token, owner, repo, ref):
        nonlocal call_count
        call_count += 1
        if ref == "main":
            raise RuntimeError("promotion target unreachable")
        return ["0135_one.py", "0136_prev.py"]

    with (
        patch("app.github.get_pr_files", return_value=pr_files),
        patch(
            "app.github.list_versions_at_ref",
            side_effect=_list_versions_side_effect,
        ),
    ):
        ok, reason = await enforce_premerge_migration_guard(
            "t", "o", "r", 1, "automain", promotion_target="main"
        )
    assert ok is False
    assert reason is not None and "promotion target" in reason


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
        validate_combined_migration_chain(
            existing, {plan["new_path"]: plan["new_source"]}
        )
        is None
    )
    # The single-migration plan also exposes the unified upserts/deletes shape:
    # one renamed file in each.
    assert plan["deletes"] == ["backend/alembic/versions/0137_new.py"]
    assert set(plan["upserts"]) == {"backend/alembic/versions/0138_new.py"}
    assert plan["upserts"][plan["new_path"]] == plan["new_source"]


def test_plan_reconciliation_stale_anchor_rechains():
    """A single migration anchored to a no-longer-head revision is rechained
    onto the current head; when its provisional revision already equals the
    next free slot above the live head, merge-time finalization only rewrites
    ``down_revision`` and preserves the authored revision id."""
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
    assert plan["deletes"] == []
    assert set(plan["upserts"]) == {"backend/alembic/versions/0138_new.py"}


def test_plan_reconciliation_already_valid_returns_none():
    """A migration that already chains cleanly onto the head needs no plan."""
    existing = ["0136_prev.py", "0137_head.py"]
    added = {
        "backend/alembic/versions/0138_new.py": _migration_src("0138", "0137"),
    }
    assert plan_migration_reconciliation(existing, added) is None


def test_plan_reconciliation_multiple_added_already_clean_returns_none():
    """Several chained added migrations that already extend the head cleanly (no
    collision, root anchored to the head) need no plan."""
    existing = ["0136_prev.py", "0137_head.py"]
    added = {
        "backend/alembic/versions/0138_a.py": _migration_src("0138", "0137"),
        "backend/alembic/versions/0139_b.py": _migration_src("0139", "0138"),
    }
    assert plan_migration_reconciliation(existing, added) is None


def test_plan_reconciliation_multi_migration_collision_renumbers_full_tail_monotonic():
    """A rollup bundling several chained migrations whose oldest collides with the
    target head: the colliding root AND its full linear descendant tail are
    renumbered monotonically into a contiguous block above every existing
    revision, each rechained onto its renumbered predecessor (root onto the
    head)."""
    # Branch-time head was 0123; subtasks cut 0124 (down 0123) and 0125 (down
    # 0124). Meanwhile the target advanced to head 0124, so the rollup's oldest
    # migration 0124 now collides and its anchor 0123 is stale.
    existing = ["0123_base.py", "0124_taken.py"]
    added = {
        "backend/alembic/versions/0124_a.py": _migration_src("0124", "0123"),  # root
        "backend/alembic/versions/0125_b.py": _migration_src("0125", "0124"),  # tail
    }
    plan = plan_migration_reconciliation(existing, added)
    assert plan is not None
    # Root 0124 finalizes into the next free live slot 0125, rechained onto the
    # head 0124; the full tail becomes the contiguous block 0125, 0126.
    assert plan["old_revision"] == "0124"
    assert plan["new_revision"] == "0125"
    assert plan["new_down_revision"] == "0124"
    assert plan["old_path"] == "backend/alembic/versions/0124_a.py"
    assert plan["new_path"] == "backend/alembic/versions/0125_a.py"
    # deletes: BOTH old paths being renamed away (in chain order).
    assert plan["deletes"] == [
        "backend/alembic/versions/0124_a.py",
        "backend/alembic/versions/0125_b.py",
    ]
    # upserts: BOTH renamed/renumbered files — the full tail moved up.
    assert set(plan["upserts"]) == {
        "backend/alembic/versions/0125_a.py",
        "backend/alembic/versions/0126_b.py",
    }
    root_rev, root_down = parse_revision_identifiers(
        plan["upserts"]["backend/alembic/versions/0125_a.py"]
    )
    assert (root_rev, root_down) == ("0125", "0124")
    tail_rev, tail_down = parse_revision_identifiers(
        plan["upserts"]["backend/alembic/versions/0126_b.py"]
    )
    assert (tail_rev, tail_down) == ("0126", "0125")
    assert plan["expected_schema_revision"] == "0126"
    # The full reconciled added set revalidates clean against the target.
    reconciled = {p: s for p, s in added.items() if p not in plan["deletes"]}
    reconciled.update(plan["upserts"])
    assert validate_combined_migration_chain(existing, reconciled) is None
    # Monotonicity property: the numeric-max revision IS the structural head.
    reconciled_chain, _dups, _unp = build_migration_chain(reconciled)
    assert max(reconciled_chain, key=int) == find_heads(reconciled_chain)[0]
    assert max(int(r) for r in reconciled_chain) == int(
        plan["expected_schema_revision"]
    )


def test_plan_reconciliation_multi_migration_chain_is_monotonic():
    """Explicit regression: for the multi-migration tail case every reconciled
    node points strictly downward (int(revision) > int(down_revision)) and the
    numeric-max revision equals the single structural head."""
    existing = ["0123_base.py", "0124_taken.py"]
    added = {
        "backend/alembic/versions/0124_a.py": _migration_src("0124", "0123"),
        "backend/alembic/versions/0125_b.py": _migration_src("0125", "0124"),
        "backend/alembic/versions/0126_c.py": _migration_src("0126", "0125"),
    }
    plan = plan_migration_reconciliation(existing, added)
    assert plan is not None
    reconciled = {p: s for p, s in added.items() if p not in plan["deletes"]}
    reconciled.update(plan["upserts"])
    reconciled_chain, _dups, _unp = build_migration_chain(reconciled)
    for rev, down in reconciled_chain.items():
        if down is not None and down in reconciled_chain:
            assert int(rev) > int(down), f"{rev} <- {down} is not monotonic"
    heads = find_heads(reconciled_chain)
    assert len(heads) == 1
    assert max(reconciled_chain, key=int) == heads[0]


def test_plan_reconciliation_multiple_independent_collisions_returns_none():
    """Two added migrations that each independently collide but do NOT form one
    linear chain (two roots, two heads) are ambiguous — no plan."""
    existing = ["0136_x.py", "0137_y.py"]
    added = {
        # Both anchor externally to 0135 (not in the added set) → two roots,
        # two heads; each collides with an existing revision.
        "backend/alembic/versions/0136_a.py": _migration_src("0136", "0135"),
        "backend/alembic/versions/0137_b.py": _migration_src("0137", "0135"),
    }
    assert plan_migration_reconciliation(existing, added) is None


def test_plan_reconciliation_fanout_returns_none():
    """A colliding root with more than one child is a fan-out, not a single
    linear chain. In Alembic's single-parent model a fan-out surfaces as two
    Alembic heads, so it is rejected (no plan) — the same ambiguity the
    back-merge fan-out guard rejects."""
    existing = ["0136_prev.py", "0137_head.py"]
    added = {
        "backend/alembic/versions/0137_c.py": _migration_src(
            "0137", "0136"
        ),  # root, collides
        "backend/alembic/versions/0138_d.py": _migration_src(
            "0138", "0137"
        ),  # child A → head
        "backend/alembic/versions/0139_e.py": _migration_src(
            "0139", "0137"
        ),  # child B → head
    }
    assert plan_migration_reconciliation(existing, added) is None


def test_plan_reconciliation_parse_failure_in_chain_returns_none():
    """An unparseable file within an otherwise-chained added set yields no plan."""
    existing = ["0136_prev.py", "0137_head.py"]
    added = {
        "backend/alembic/versions/0137_c.py": _migration_src("0137", "0136"),
        "backend/alembic/versions/0138_d.py": "def upgrade():\n    pass\n",
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


def test_plan_reconciliation_out_of_order_merge_fills_abandoned_hole():
    """A later-merged provisional migration fills the abandoned live gap instead
    of preserving its authored reservation number."""
    existing = ["0150_prev.py", "0151_head.py"]
    added = {
        "backend/alembic/versions/0153_feature.py": _migration_src("0153", "0151"),
    }

    plan = plan_migration_reconciliation(existing, added)

    assert plan is not None
    assert plan["old_revision"] == "0153"
    assert plan["new_revision"] == "0152"
    assert plan["new_down_revision"] == "0151"
    assert plan["new_path"] == "backend/alembic/versions/0152_feature.py"
    rev, down = parse_revision_identifiers(plan["new_source"])
    assert (rev, down) == ("0152", "0151")
    assert plan["expected_schema_revision"] == "0152"


def test_plan_reconciliation_multi_migration_fills_gap_contiguously():
    """A provisional tail that started above a now-abandoned reservation is
    finalized into the earliest contiguous live block with no dangling anchors."""
    existing = ["0122_prev.py", "0123_head.py"]
    added = {
        "backend/alembic/versions/0125_a.py": _migration_src("0125", "0123"),
        "backend/alembic/versions/0126_b.py": _migration_src("0126", "0125"),
    }

    plan = plan_migration_reconciliation(existing, added)

    assert plan is not None
    assert set(plan["upserts"]) == {
        "backend/alembic/versions/0124_a.py",
        "backend/alembic/versions/0125_b.py",
    }
    first_rev, first_down = parse_revision_identifiers(
        plan["upserts"]["backend/alembic/versions/0124_a.py"]
    )
    second_rev, second_down = parse_revision_identifiers(
        plan["upserts"]["backend/alembic/versions/0125_b.py"]
    )
    assert (first_rev, first_down) == ("0124", "0123")
    assert (second_rev, second_down) == ("0125", "0124")
    assert plan["expected_schema_revision"] == "0125"


def test_plan_reconciliation_skips_gap_reserved_on_promotion_target():
    """A gap visible on the target branch is NOT reused when a divergent
    promotion target already stamped that revision to another migration."""
    existing = ["0141_prev.py", "0142_head.py"]
    added = {
        "backend/alembic/versions/0144_feature.py": _migration_src("0144", "0141"),
    }

    plan = plan_migration_reconciliation(
        existing,
        added,
        protected_revisions={"0143"},
    )

    assert plan is not None
    assert plan["old_revision"] == "0144"
    assert plan["new_revision"] == "0144"
    assert plan["new_down_revision"] == "0142"
    assert plan["new_path"] == "backend/alembic/versions/0144_feature.py"
    rev, down = parse_revision_identifiers(plan["new_source"])
    assert (rev, down) == ("0144", "0142")


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


def test_plan_integration_collision_linear_tail_renumbers_full_tail_monotonic():
    """A colliding migration with a linear descendant tail: the colliding file AND
    every node in the tail are renumbered monotonically into a contiguous block
    above the unified head, each rechained onto its renumbered predecessor, so the
    NNNN order matches the chain order and the numeric-max revision is the head."""
    target = ["0136_prev.py", "0137_hotfix.py"]
    integration = {
        "backend/alembic/versions/0137_feat.py": _migration_src("0137", "0136"),  # C
        "backend/alembic/versions/0138_b.py": _migration_src("0138", "0137"),  # D
        "backend/alembic/versions/0139_c.py": _migration_src("0139", "0138"),  # E
    }
    plan = plan_integration_side_renumber(target, integration)
    assert plan is not None
    # C finalizes into the earliest free live slot 0138, anchored to the
    # unified head 0137.
    assert plan["old_revision"] == "0137"
    assert plan["new_revision"] == "0138"
    assert plan["new_down_revision"] == "0137"
    assert plan["old_path"] == "backend/alembic/versions/0137_feat.py"
    assert plan["new_path"] == "backend/alembic/versions/0138_feat.py"
    # deletes: ALL three old paths being renamed away (in chain order).
    assert plan["deletes"] == [
        "backend/alembic/versions/0137_feat.py",
        "backend/alembic/versions/0138_b.py",
        "backend/alembic/versions/0139_c.py",
    ]
    # upserts: ALL three renamed/renumbered files — the full tail moved up.
    assert set(plan["upserts"]) == {
        "backend/alembic/versions/0138_feat.py",
        "backend/alembic/versions/0139_b.py",
        "backend/alembic/versions/0140_c.py",
    }
    # C 0137 -> 0138 <- unified head 0137.
    c_rev, c_down = parse_revision_identifiers(
        plan["upserts"]["backend/alembic/versions/0138_feat.py"]
    )
    assert (c_rev, c_down) == ("0138", "0137")
    # D 0138 -> 0139 <- renumbered C (0138).
    d_rev, d_down = parse_revision_identifiers(
        plan["upserts"]["backend/alembic/versions/0139_b.py"]
    )
    assert (d_rev, d_down) == ("0139", "0138")
    # E 0139 -> 0140 <- renumbered D (0139); the highest revision == the head.
    e_rev, e_down = parse_revision_identifiers(
        plan["upserts"]["backend/alembic/versions/0140_c.py"]
    )
    assert (e_rev, e_down) == ("0140", "0139")
    assert plan["expected_schema_revision"] == "0140"
    # The full reconciled integration set revalidates clean.
    reconciled = {p: s for p, s in integration.items() if p not in plan["deletes"]}
    reconciled.update(plan["upserts"])
    assert validate_combined_migration_chain(target, reconciled) is None
    # Monotonicity property: the numeric-max revision IS the structural head.
    reconciled_chain, _dups, _unp = build_migration_chain(reconciled)
    assert max(reconciled_chain, key=int) == find_heads(reconciled_chain)[0]
    assert max(int(r) for r in reconciled_chain) == int(
        plan["expected_schema_revision"]
    )


def test_plan_integration_tail_chain_is_monotonic():
    """Explicit regression: for every node in the reconciled chain
    int(revision) > int(down_revision), and the numeric-max revision equals the
    single structural head returned by find_heads()."""
    target = ["0136_prev.py", "0137_hotfix.py"]
    integration = {
        "backend/alembic/versions/0137_feat.py": _migration_src("0137", "0136"),
        "backend/alembic/versions/0138_b.py": _migration_src("0138", "0137"),
        "backend/alembic/versions/0139_c.py": _migration_src("0139", "0138"),
    }
    plan = plan_integration_side_renumber(target, integration)
    assert plan is not None
    reconciled = {p: s for p, s in integration.items() if p not in plan["deletes"]}
    reconciled.update(plan["upserts"])
    reconciled_chain, _dups, _unp = build_migration_chain(reconciled)
    # Every renumbered node points strictly downward to a lower revision number.
    for rev, down in reconciled_chain.items():
        if down is not None and down in reconciled_chain:
            assert int(rev) > int(down), f"{rev} <- {down} is not monotonic"
    heads = find_heads(reconciled_chain)
    assert len(heads) == 1
    assert max(reconciled_chain, key=int) == heads[0]


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


# ── Migration 0707 — remaining design-doc KV backfill ───────────────────────


def test_backfill_0707_chains_from_0702_and_declares_kv_keys():
    """Migration 0707 must chain from 0702 and reference each KV-backed key.

    Exercises the live 0707 migration file (production code) and confirms
    EXPECTED_SCHEMA_REVISION in main.py has been bumped to match.
    """
    versions_dir = Path(__file__).resolve().parents[1] / "alembic" / "versions"
    migration_path = versions_dir / "0707_backfill_remaining_design_doc_telemetry.py"
    assert migration_path.exists(), (
        f"Migration file not found: {migration_path}. "
        "Run the 0707 backfill migration task to create it."
    )

    content = migration_path.read_text()
    revision, down_revision = parse_revision_identifiers(content)

    assert revision == "0707", f"Expected revision '0707', got {revision!r}"
    assert down_revision == "0702", (
        f"Expected down_revision '0702' (current chain head), got {down_revision!r}"
    )

    # Each KV-backed key must appear in the migration body so the backfill
    # actually seeds the dedicated store for historical projects.
    for key in ("repo_overview", "_broadcast_limit", "_auto_remediation"):
        assert key in content, (
            f"Backfill key {key!r} not referenced in 0707 migration body"
        )

    # EXPECTED_SCHEMA_REVISION in main.py must be bumped to 0707.
    from app.main import EXPECTED_SCHEMA_REVISION

    assert EXPECTED_SCHEMA_REVISION == "0707", (
        f"Expected EXPECTED_SCHEMA_REVISION == '0707', got {EXPECTED_SCHEMA_REVISION!r}. "
        "Bump EXPECTED_SCHEMA_REVISION in backend/app/main.py."
    )
