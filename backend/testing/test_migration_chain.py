"""Tests for the DB-free Alembic migration-chain helpers and the rollup
auto-merge guard that consumes them."""

from unittest.mock import patch

from app.migration_chain import (
    build_migration_chain,
    find_heads,
    find_roots,
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
