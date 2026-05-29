"""Alembic migration-chain validation used by rollup auto-merge."""

import app.github as github
from app.migration_chain import validate_added_migrations
from app.migration_path import is_migration_file


def _extract_rollup_migration_sources(diff: str) -> dict[str, str]:
    """Reconstruct added/changed Alembic migration file contents from a PR diff.

    Returns a mapping of ``alembic/versions`` file path -> reconstructed source
    (the post-image, from the diff's ``+`` lines). Newly-added migration files
    appear in full; this is enough to read their in-file revision/down_revision.
    An empty result means the rollup diff does not touch the versions directory.
    """
    sources: dict[str, list[str]] = {}
    current: list[str] | None = None
    for line in diff.splitlines():
        if line.startswith("diff --git "):
            current = None
        elif line.startswith("+++ "):
            path = line[4:].strip()
            if path.startswith("b/"):
                path = path[2:]
            if is_migration_file(path) and path.endswith(".py"):
                current = sources.setdefault(path, [])
            else:
                current = None
        elif current is not None and line.startswith("+") and not line.startswith("+++"):
            current.append(line[1:])
    return {path: "\n".join(lines) for path, lines in sources.items()}


def _touches_alembic_versions(filename: str) -> bool:
    return is_migration_file(filename) and filename.endswith(".py")


async def _validate_rollup_migration_chain(
    gh_token: str, owner: str, repo: str, pr_number: int
) -> tuple[str, str | None]:
    """Fail-safe Alembic-chain guard for rollup auto-merge.

    Returns ``("ok", None)`` when the rollup does not touch ``alembic/versions``
    or the combined migration set is valid (single head, unique revisions).
    Returns ``("defer", reason)`` when GitHub metadata cannot be fetched (retry
    next sweep) and ``("block", reason)`` when the combined chain is invalid or
    a touched migration file is unparseable. Never waves a merge through on an
    inability to fetch or parse the migration files.
    """
    try:
        files = await github.get_pr_files(gh_token, owner, repo, pr_number)
    except Exception as e:
        return (
            "defer",
            f"could not list rollup files to check Alembic migrations: "
            f"{type(e).__name__}: {e}",
        )
    if not any(_touches_alembic_versions(f.get("filename", "")) for f in files):
        return ("ok", None)

    try:
        diff = await github.get_pull_request_diff(gh_token, owner, repo, pr_number)
    except Exception as e:
        return (
            "defer",
            f"could not fetch rollup diff to validate Alembic migrations: "
            f"{type(e).__name__}: {e}",
        )
    if not diff:
        return (
            "defer",
            "could not fetch rollup diff to validate Alembic migrations (empty response)",
        )
    reason = validate_added_migrations(_extract_rollup_migration_sources(diff))
    return ("ok", None) if reason is None else ("block", reason)
