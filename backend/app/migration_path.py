"""Single source of truth for detecting Alembic migration files.

Replaces three inconsistent hardcoded checks in scope_complexity,
orchestrator_filters, and rollup_management with one shared predicate.
"""

_DEFAULT_ALEMBIC_VERSIONS_PATH = "backend/alembic/versions/"


def is_migration_file(
    path: str,
    alembic_versions_path: str = _DEFAULT_ALEMBIC_VERSIONS_PATH,
) -> bool:
    """Return True if *path* refers to a file inside the Alembic versions directory.

    Normalises backslashes to forward-slashes before matching so Windows-style
    paths (rare in practice) are handled consistently.  The check is a substring
    match so both ``backend/alembic/versions/0001_x.py`` and paths with a leading
    repo prefix are detected.

    Pass *alembic_versions_path* to override the default for projects whose
    migrations live at a different location.
    """
    return alembic_versions_path in path.replace("\\", "/")
