"""Alembic migration-chain validation used by rollup auto-merge."""

import app.github as github
from app.migration_chain import (
    _filename_revision_prefix,
    build_migration_chain,
    find_content_divergent_revisions,
    find_filename_revision_mismatches,
    find_heads,
    plan_integration_side_renumber,
    plan_migration_reconciliation,
    validate_added_migrations,
    validate_combined_migration_chain,
)
from app.migration_path import is_migration_file

# Alembic versions directory, used to resolve a bare migration basename (as
# returned by ``github.list_versions_at_ref``) back to its full repo path for a
# per-file text fetch.
_ALEMBIC_VERSIONS_DIR = "backend/alembic/versions"


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


async def pr_touches_migrations(
    gh_token: str,
    owner: str,
    repo: str,
    pr_number: int,
) -> bool:
    """Fail-safe check: does *pr_number* add/modify an Alembic migration file?

    Returns ``True`` when any file in the PR touches ``alembic/versions``. On an
    inability to list the PR's files, returns ``True`` (conservative: an unknown
    file set must NOT be treated as migration-free, since the caller uses a
    ``False`` result to take a GitHub-native-auto-merge fast path that bypasses
    Blueprint's re-entry guard). Only a positive "no migration files" answer
    returns ``False``.

    Used by the task-PR and rollup merge paths to decide whether the final
    landing may be delegated to GitHub native auto-merge (non-migration PRs) or
    must stay inside Blueprint's own retry/re-entry path so the combined-chain
    guard is re-evaluated against the live target branch before merge.
    """
    try:
        files = await github.get_pr_files(gh_token, owner, repo, pr_number)
    except Exception:
        return True
    return any(_touches_alembic_versions(f.get("filename", "")) for f in files)


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
    if reason is not None:
        return ("block", reason)

    # Cross-ref content-divergence guard. The combined-chain check proves the
    # added migrations don't collide with or dangle off the target's chain by
    # revision id, but it is blind to a RENUMBER that keeps both checkouts
    # internally valid while remapping a revision the live DB already applied —
    # the main<->automain back-merge class (prod incident 2026-06-04). Comparing
    # the target's existing migrations against the ones this PR adds catches the
    # tell: the same migration (by filename description) appearing under a
    # different revision id, or a revision id reused for a different migration.
    # Uses only data already fetched (no extra I/O), so it can't add a new
    # fail-safe surface to the shared guard.
    divergences = find_content_divergent_revisions(
        existing_filenames, list(added_sources.keys())
    )
    if divergences:
        return (
            "block",
            f"Alembic revision-identity divergence between the candidate and "
            f"target '{target_branch}' (a renumber/reuse that would skip or "
            f"mishandle a migration the live DB already applied): "
            + "; ".join(divergences),
        )
    return ("ok", None)


async def enforce_premerge_migration_guard(
    gh_token: str,
    owner: str,
    repo: str,
    pr_number: int,
    target_branch: str | None,
) -> tuple[bool, str | None]:
    """Fail-safe pre-merge Alembic dual-head guard for non-autoreconcile paths.

    Wraps :func:`_validate_rollup_migration_chain` and collapses its
    ``("ok"|"block"|"defer", reason)`` verdict into a single "is it safe to
    merge?" decision. Both ``block`` (the combined chain is invalid — duplicate
    revision id / dual head) and ``defer`` (GitHub metadata could not be
    fetched, so migration safety is unknown) mean DO NOT MERGE and return
    ``(False, reason)``. Only an ``ok`` verdict returns ``(True, None)``.

    This is the shared enforcement point for the operator-approval and normal
    task-PR merge paths which — unlike the autonomous rollup path — do not run
    the mechanical auto-reconcile; for them, holding the merge and surfacing the
    dual-head reason is the correct conservative behavior. Reuses the same
    combined-chain validation policy as the rollup guard rather than inventing a
    second migration-safety rule set.
    """
    verdict, reason = await _validate_rollup_migration_chain(
        gh_token, owner, repo, pr_number, target_branch=target_branch
    )
    if verdict == "ok":
        return (True, None)
    return (False, reason)


async def _validate_migration_chain_post_merge(
    gh_token: str,
    owner: str,
    repo: str,
    pr_number: int,
    target_branch: str,
) -> tuple[str, str | None]:
    """Post-merge Alembic chain health check for the release-reconcile paths.

    Unlike the pre-merge guard (:func:`_validate_rollup_migration_chain`), this
    is safe to call AFTER the candidate has already merged onto *target_branch*.
    The pre-merge guard uses an added-vs-existing collision set that always
    reports a false self-collision at this point (the candidate's own revisions
    are now in both the "added" set and the "existing" set).

    Instead, this check lists the current migration filenames on *target_branch*
    and looks for any revision id (NNNN filename prefix) that appears in more
    than one file — a genuine third-party duplicate that the pre-merge guard
    should have prevented but may have slipped through.  It then reads every
    target-branch migration's source, builds the in-file revision/down_revision
    chain over the WHOLE set as one (NOT an added-vs-existing comparison, which
    would falsely flag the candidate's own just-merged revisions — see #7731),
    and blocks on the SAME migration-corruption taxonomy the pre-merge guard
    enforces (reusing the shared chain helpers rather than a second rule set):

      * a duplicate IN-FILE revision id — two distinct migration files declaring
        the same ``revision = '...'`` (a copy-paste where the filename ``NNNN``
        was bumped but the in-file id was not).  Chain-building collapses the two
        into one node, so the filename-prefix pass cannot see it;
      * an UNPARSEABLE migration source — a numeric-prefixed file with no
        parseable in-file ``revision``, silently dropped from the analyzed chain;
      * a filename/revision DESYNC — the ``NNNN`` filename prefix disagrees with
        the in-file ``revision`` id;
      * a genuine third-party dual head (more than one Alembic head built from
        distinct revision ids), or a broken chain (a ``down_revision`` pointing at
        a revision absent from the branch).

    The first two classes are weighted ahead of the dual-head / broken-chain
    checks: a collapsed (duplicate-id) or dropped (unparseable) node can MASK a
    genuine dual head or broken chain that would otherwise be detected, so the
    reconciler would record a spurious promotion of a target that actually fails
    the schema-revision startup gate — the exact failure the guard prevents.  A
    clean target (every revision id unique and parseable, filenames matching, the
    chain a single head with no dangling anchor) returns ``("ok", None)``.

    Returns ``("defer", reason)`` on GitHub I/O failure — including any per-file
    source fetch failure or empty body — consistent with the pre-merge guard's
    fail-safe semantics, and ``("block", reason)`` when any corruption class above
    is found.  When the PR does not touch Alembic migrations, returns
    ``("ok", None)`` immediately.
    """
    try:
        files = await github.get_pr_files(gh_token, owner, repo, pr_number)
    except Exception as e:
        return (
            "defer",
            f"could not list PR #{pr_number} files to check Alembic migrations: "
            f"{type(e).__name__}: {e}",
        )
    if not any(_touches_alembic_versions(f.get("filename", "")) for f in files):
        return ("ok", None)

    try:
        existing_filenames = await github.list_versions_at_ref(
            gh_token, owner, repo, target_branch
        )
    except Exception as e:
        return (
            "defer",
            f"could not list target branch '{target_branch}' migrations for "
            f"post-merge chain health check: {type(e).__name__}: {e}",
        )

    # Detect genuine third-party duplicates: any NNNN revision-id prefix that
    # appears in more than one migration file on the target branch.  The
    # candidate's own just-merged revisions are expected to appear exactly once;
    # a second occurrence of the same id means a different PR independently used
    # the same slot — a genuine collision the pre-merge guard should have caught.
    seen: dict[str, str] = {}
    for fn in existing_filenames:
        rev = _filename_revision_prefix(fn)
        if rev is None:
            continue
        if rev in seen:
            return (
                "block",
                f"Alembic revision id '{rev}' appears in multiple migration files "
                f"on '{target_branch}': {seen[rev]!r} and {fn!r}; genuine "
                f"third-party revision collision — chain needs manual resolution",
            )
        seen[rev] = fn

    # Full-chain health check: the filename-prefix pass above only catches a
    # duplicate revision id.  A genuine third-party collision can also surface as
    # a dual head built from DISTINCT revision ids (two leaves), or a broken
    # chain (a down_revision pointing at a revision no longer on the branch) —
    # both make the target unbootable (the schema-revision startup gate fails)
    # yet leave every filename prefix unique.  To detect those we must read each
    # migration's in-file revision/down_revision (filenames alone are not enough)
    # and reason over the WHOLE target-branch set as a single chain.  Validating
    # added-vs-existing here would falsely flag the candidate's own just-merged
    # revisions (#7731), so we deliberately build one combined chain instead.
    sources: dict[str, str] = {}
    for fn in existing_filenames:
        if _filename_revision_prefix(fn) is None:
            # Not a migration-style NNNN file — skip rather than fetch/parse it.
            continue
        path = f"{_ALEMBIC_VERSIONS_DIR}/{fn}"
        try:
            text = await github.get_file_text_at_ref(
                path,
                token=gh_token,
                owner=owner,
                repo=repo,
                ref=target_branch,
            )
        except Exception as e:
            return (
                "defer",
                f"could not fetch migration source '{path}' at '{target_branch}' "
                f"for post-merge chain health check: {type(e).__name__}: {e}",
            )
        if not text:
            return (
                "defer",
                f"empty migration source for '{path}' at '{target_branch}' — "
                f"deferring post-merge chain health check",
            )
        sources[fn] = text

    if sources:
        chain, duplicates, unparseable = build_migration_chain(sources)

        # Weight the unambiguously chain-breaking classes first: a collapsed
        # (duplicate in-file id) or dropped (unparseable) node can MASK a genuine
        # dual head / broken chain below, so surface them before the head/dangling
        # checks. This mirrors the pre-merge guard's corruption taxonomy
        # (validate_added_migrations) exactly, reusing the same chain helpers.
        if unparseable:
            return (
                "block",
                f"migration file(s) on '{target_branch}' have no parseable in-file "
                f"revision and were dropped from the chain: "
                f"{', '.join(sorted(unparseable))} — an unparseable migration makes "
                f"the target unbootable and can mask a dual head; chain needs "
                f"manual resolution.",
            )
        if duplicates:
            dup = sorted(set(duplicates))[0]
            return (
                "block",
                f"in-file Alembic revision id '{dup}' is declared by more than one "
                f"migration file on '{target_branch}' — distinct files collapsing to "
                f"one chain node can mask a dual head and the schema-revision startup "
                f"gate would fail; chain needs manual resolution.",
            )
        mismatches = find_filename_revision_mismatches(sources)
        if mismatches:
            return (
                "block",
                f"migration filename prefix disagrees with its in-file revision on "
                f"'{target_branch}': {'; '.join(sorted(mismatches))} — chain needs "
                f"manual resolution.",
            )

        heads = find_heads(chain)
        if len(heads) > 1:
            return (
                "block",
                f"target branch '{target_branch}' has {len(heads)} Alembic heads "
                f"({', '.join(sorted(heads))}) after the candidate landed — a "
                f"genuine third-party dual head; the schema-revision startup gate "
                f"would fail. Chain needs manual resolution.",
            )
        dangling = sorted(
            rev
            for rev, down in chain.items()
            if down is not None and down not in chain
        )
        if dangling:
            rev = dangling[0]
            return (
                "block",
                f"Alembic migration '{rev}' on '{target_branch}' has "
                f"down_revision '{chain[rev]}', which is not present on the "
                f"branch — broken migration chain; the schema-revision startup "
                f"gate would fail. Chain needs manual resolution.",
            )

    return ("ok", None)


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


async def _plan_backmerge_migration_reconciliation(
    gh_token: str,
    owner: str,
    repo: str,
    pr_number: int,
    integration_branch: str,
    promotion_target: str,
) -> dict | None:
    """Plan an integration-side renumber for a back-merge blocked by a dual head.

    The BACK-MERGE mirror of :func:`_plan_rollup_migration_reconciliation`, with
    the asymmetry the back-merge case demands: when ``promotion_target`` (e.g.
    ``main``) is back-merged into ``integration_branch`` (e.g. ``automain``), the
    promotion-target hotfix migration is ALREADY APPLIED IN PRODUCTION and is
    therefore immutable — it must never be renumbered. The migration that must
    move is the one on the INTEGRATION branch whose revision id independently
    reused the same ``NNNN`` slot.

    Steps (every one fail-safe to ``None`` — a failed fetch / empty result must
    never produce an unsafe auto-rewrite; the caller falls back to the guided
    resolver):

      1. Fetch the back-merge PR diff and reconstruct its added (promotion-target)
         migration sources. An empty result means the back-merge adds no migration
         — nothing to reconcile — so return ``None``.
      2. List the promotion target's current migration filenames; they supply the
         immutable colliding revision id and the unified head.
      3. List the integration branch's migration filenames, keep only the
         POST-DIVERGENCE ones (basename not shared with the promotion target — so
         the pre-divergence migrations common to both branches are dropped and
         only the colliding integration migration + its descendant tail remain),
         and read each one's full source at the integration ref.
      4. Delegate to the pure :func:`plan_integration_side_renumber` planner,
         which returns a plan only for the unambiguous single-collision case and
         ``None`` for everything else.
    """
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
        promotion_target_filenames = await github.list_versions_at_ref(
            gh_token, owner, repo, promotion_target
        )
    except Exception:
        return None
    if not promotion_target_filenames:
        return None
    try:
        integration_filenames = await github.list_versions_at_ref(
            gh_token, owner, repo, integration_branch
        )
    except Exception:
        return None

    # Drop the migrations shared with the promotion target (pre-divergence
    # history) so only the colliding integration migration and its tail remain.
    promotion_target_basenames = set(promotion_target_filenames)
    post_divergence = [
        fn for fn in integration_filenames if fn not in promotion_target_basenames
    ]
    if not post_divergence:
        return None

    integration_sources: dict[str, str] = {}
    for basename in post_divergence:
        path = f"{_ALEMBIC_VERSIONS_DIR}/{basename}"
        try:
            text = await github.get_file_text_at_ref(
                path,
                token=gh_token,
                owner=owner,
                repo=repo,
                ref=integration_branch,
            )
        except Exception:
            return None
        if not text:
            return None
        integration_sources[path] = text

    return plan_integration_side_renumber(
        promotion_target_filenames, integration_sources
    )
