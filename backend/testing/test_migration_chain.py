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
    validate_added_migrations,
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
