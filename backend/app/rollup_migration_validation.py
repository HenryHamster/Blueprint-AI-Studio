"""Alembic migration-chain validation used by rollup auto-merge."""

import app.github as github
from app.migration_chain import (
    plan_migration_reconciliation,
    validate_added_migrations,
    validate_combined_migration_chain,
)
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
    gh_token: str,
    owner: str,
    repo: str,
    pr_number: int,
    target_branch: str | None = None,
) -> tuple[str, str | None]:
    """Fail-safe Alembic-chain guard for rollup auto-merge.

    Returns ``("ok", None)`` when the rollup does not touch ``alembic/versions``
    or the combined migration set is valid (single head, unique revisions).
    Returns ``("defer", reason)`` when GitHub metadata cannot be fetched (retry
    next sweep) and ``("block", reason)`` when the combined chain is invalid or
    a touched migration file is unparseable. Never waves a merge through on an
    inability to fetch or parse the migration files.

    When *target_branch* is given, the added migrations are validated against
    the target branch's *current* migration chain (head + existing revision
    ids), catching the parallel-development collision where two branches each
    add the same revision id or anchor to a revision that is no longer the
    head — a class the in-isolation check is blind to. A failure to list the
    target branch's migrations is fail-safe (``"defer"``), mirroring the
    diff-fetch handling. With no *target_branch* the guard degrades to the
    in-isolation check.
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
    added_sources = _extract_rollup_migration_sources(diff)

    if not target_branch:
        reason = validate_added_migrations(added_sources)
        return ("ok", None) if reason is None else ("block", reason)

    try:
        existing_filenames = await github.list_versions_at_ref(
            gh_token, owner, repo, target_branch
        )
    except Exception as e:
        return (
            "defer",
            f"could not list target branch '{target_branch}' migrations to "
            f"validate the combined Alembic chain: {type(e).__name__}: {e}",
        )
    reason = validate_combined_migration_chain(existing_filenames, added_sources)
    return ("ok", None) if reason is None else ("block", reason)


async def _plan_rollup_migration_reconciliation(
    gh_token: str,
    owner: str,
    repo: str,
    pr_number: int,
    target_branch: str | None,
) -> dict | None:
    """Plan an automatic renumber for a rollup blocked by a head collision.

    Re-fetches the rollup's added migration sources (from the PR diff) and the
    target branch's current migration filenames, then delegates to the pure
    :func:`app.migration_chain.plan_migration_reconciliation`. Returns the plan
    dict only for the unambiguous single-added-migration collision / stale-anchor
    case; returns ``None`` for everything else AND fail-safe on any fetch error
    (a failed fetch must never produce an unsafe auto-rewrite — the caller falls
    back to the operator-alert / agent-fix path).
    """
    if not target_branch:
        return None
    try:
        diff = await github.get_pull_request_diff(gh_token, owner, repo, pr_number)
    except Exception:
        return None
    if not diff:
        return None
    added_sources = _extract_rollup_migration_sources(diff)
    if not added_sources:
        return None
    try:
        existing_filenames = await github.list_versions_at_ref(
            gh_token, owner, repo, target_branch
        )
    except Exception:
        return None
    return plan_migration_reconciliation(existing_filenames, added_sources)
