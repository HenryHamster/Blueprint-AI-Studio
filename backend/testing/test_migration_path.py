"""Unit tests for app.migration_path.is_migration_file.

Standalone (no DB): exercises the predicate and the Project property.
"""

from app.migration_path import _DEFAULT_ALEMBIC_VERSIONS_PATH, is_migration_file


class TestIsMigrationFile:
    def test_default_path_matches_standard_migration(self):
        assert is_migration_file("backend/alembic/versions/0042_foo.py")

    def test_default_path_matches_substring(self):
        # substring, not prefix — matches even with a leading component
        assert is_migration_file("repo/backend/alembic/versions/0001_init.py")

    def test_no_false_positive_on_non_alembic(self):
        assert not is_migration_file("backend/app/models.py")
        assert not is_migration_file("backend/alembic/env.py")
        assert not is_migration_file("alembic/versions/0001_other.py")

    def test_backslash_normalization(self):
        win_path = "backend\\alembic\\versions\\0001_init.py"
        assert is_migration_file(win_path)

    def test_custom_path_override(self):
        custom = "db/migrations/"
        assert is_migration_file(
            "db/migrations/001_init.py", alembic_versions_path=custom
        )
        assert not is_migration_file(
            "backend/alembic/versions/0001.py", alembic_versions_path=custom
        )

    def test_three_former_predicates_agree_on_standard_path(self):
        """The three original predicates had inconsistent implementations.

        scope_complexity used startswith('backend/alembic/versions/').
        orchestrator_filters used 'backend/alembic/versions/' in path.
        rollup_management used 'alembic/versions/' in path.

        All three now delegate to is_migration_file with the default prefix
        'backend/alembic/versions/', so they agree on the standard path.
        """
        path = "backend/alembic/versions/0124_project_integration_branch.py"
        assert is_migration_file(path)

    def test_default_constant_value(self):
        assert _DEFAULT_ALEMBIC_VERSIONS_PATH == "backend/alembic/versions/"


class TestProjectAlembicVersionsPath:
    def test_default_when_no_build_config(self):
        from testing.factories import make_project

        p = make_project()
        p.build_config = None
        assert p.alembic_versions_path == _DEFAULT_ALEMBIC_VERSIONS_PATH

    def test_default_when_key_absent(self):
        from testing.factories import make_project

        p = make_project()
        p.build_config = {"stale_branch_prefixes": ["blueprint/"]}
        assert p.alembic_versions_path == _DEFAULT_ALEMBIC_VERSIONS_PATH

    def test_custom_value_from_build_config(self):
        from testing.factories import make_project

        p = make_project()
        p.build_config = {"alembic_versions_path": "db/migrations/"}
        assert p.alembic_versions_path == "db/migrations/"
