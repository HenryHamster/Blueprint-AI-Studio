"""DB-free Alembic migration-chain parsing and validation.

Shared by the rollup auto-merge guard (``rollup_management``) and the
migration-chain invariant test (``test_invariants``) so both assert against
the same in-file ``revision`` / ``down_revision`` parsing rather than two
divergent re-implementations. Pure string/regex logic — no DB, no I/O.
"""

import glob
import os
import re
import sys

# Mirrors the parsing used by the migration-chain invariant test. Alembic
# orders migrations by these in-file values, not by filename.
_REVISION_RE = re.compile(r"^revision\s*=\s*['\"](\w+)['\"]", re.MULTILINE)
_DOWN_REVISION_RE = re.compile(
    r"^down_revision\s*=\s*(?:['\"](\w+)['\"]|None)", re.MULTILINE
)


def parse_revision_identifiers(source: str) -> tuple[str | None, str | None]:
    """Parse ``(revision, down_revision)`` from a migration file's source.

    ``revision`` is ``None`` when no ``revision = '...'`` assignment is present
    (an unparseable / non-migration file). ``down_revision`` is ``None`` for
    the root migration or when assigned the ``None`` literal.
    """
    rev_match = _REVISION_RE.search(source)
    down_match = _DOWN_REVISION_RE.search(source)
    revision = rev_match.group(1) if rev_match else None
    down_revision = (
        down_match.group(1) if (down_match and down_match.group(1)) else None
    )
    return revision, down_revision


def build_migration_chain(
    sources: dict[str, str],
) -> tuple[dict[str, str | None], list[str], list[str]]:
    """Build the revision->down_revision chain from migration sources.

    ``sources`` maps an identifier (e.g. filename) to the file's source text.
    Returns ``(chain, duplicates, unparseable)`` where ``chain`` maps
    revision -> down_revision (or ``None``), ``duplicates`` lists revision ids
    declared by more than one source, and ``unparseable`` lists identifiers
    with no parseable ``revision`` assignment.
    """
    chain: dict[str, str | None] = {}
    duplicates: list[str] = []
    unparseable: list[str] = []
    for identifier, source in sources.items():
        revision, down_revision = parse_revision_identifiers(source)
        if revision is None:
            unparseable.append(identifier)
            continue
        if revision in chain:
            duplicates.append(revision)
        chain[revision] = down_revision
    return chain, duplicates, unparseable


def find_heads(chain: dict[str, str | None]) -> list[str]:
    """Revisions that no other revision lists as its ``down_revision``."""
    pointed_to = {d for d in chain.values() if d is not None}
    return [r for r in chain if r not in pointed_to]


def find_roots(chain: dict[str, str | None]) -> list[str]:
    """Revisions whose ``down_revision`` is ``None`` or points outside the chain.

    For a complete chain this is the single ``down_revision = None`` root; for a
    partial set (e.g. only the migrations a rollup adds) it is each migration
    anchored to a revision not present in the set.
    """
    return [r for r, d in chain.items() if d is None or d not in chain]


def main(versions_dir: str | None = None) -> int:
    """Validate the full migration chain in *versions_dir*.

    Checks that every ``[0-9]*.py`` file's filename prefix matches its
    in-file ``revision`` identifier and that the combined chain has exactly
    one head. Prints a clear, actionable message on failure. Stdlib-only —
    no FastAPI, no DB, sub-second.
    """
    if versions_dir is None:
        versions_dir = os.path.join(
            os.path.dirname(os.path.abspath(__file__)),
            "..",
            "alembic",
            "versions",
        )

    files = glob.glob(os.path.join(versions_dir, "[0-9]*.py"))
    if not files:
        print(f"[migration-chain] no migration files found in {versions_dir}")
        return 0

    errors: list[str] = []
    sources: dict[str, str] = {}

    for path in files:
        basename = os.path.basename(path)
        filename_rev = basename.split("_")[0]
        with open(path) as f:
            content = f.read()
        sources[basename] = content
        internal_rev, _ = parse_revision_identifiers(content)
        if internal_rev is None:
            errors.append(
                f"  {basename}: no revision field found — add `revision = '...'`"
            )
        elif internal_rev != filename_rev:
            errors.append(
                f"  {basename}: filename prefix is '{filename_rev}' but"
                f" in-file revision is '{internal_rev}' — they must match"
            )

    chain, duplicates, _unparseable = build_migration_chain(sources)

    for rev in sorted(set(duplicates)):
        errors.append(f"  duplicate revision '{rev}' declared by more than one file")

    heads = find_heads(chain)
    if len(heads) != 1:
        errors.append(
            f"  chain has {len(heads)} head(s) ({', '.join(sorted(heads))})"
            " — expected exactly 1; run `alembic heads` for details"
        )

    if errors:
        print("[migration-chain] FAILED — migration chain has errors:")
        for msg in errors:
            print(msg)
        return 1

    print(f"[migration-chain] OK — {len(files)} migration(s), single head")
    return 0


def _filename_revision_prefix(identifier: str) -> str | None:
    """Extract the ``NNNN`` numeric revision prefix from a migration filename.

    Mirrors the CI invariant test: take the basename, split on the first
    underscore, and return the leading segment only when it is all digits.
    Returns ``None`` for identifiers that are not migration-style filenames
    (bare names like ``a.py`` or paths with no numeric prefix) so callers that
    key sources by arbitrary identifiers see no spurious mismatches.
    """
    prefix = os.path.basename(identifier).split("_")[0]
    return prefix if prefix.isdigit() else None


def find_filename_revision_mismatches(sources: dict[str, str]) -> list[str]:
    """Identifiers whose filename ``NNNN`` prefix disagrees with their revision.

    Only keys with a migration-style numeric filename prefix (see
    ``_filename_revision_prefix``) are checked; bare names like ``a.py`` are
    skipped. For each checked key, compares the filename prefix to the in-file
    ``revision`` parsed from the source. A numeric-prefix file with no parseable
    revision is also reported (its filename claims a revision the file does not
    declare). Returns human-readable strings; empty when every filename matches.

    Shared by ``validate_added_migrations`` (the rollup auto-merge guard) and
    the ``test_migration_filenames_match_internal_revision`` invariant test so
    both reason about filename-vs-revision desync with identical parsing.
    """
    mismatches: list[str] = []
    for identifier, source in sources.items():
        filename_rev = _filename_revision_prefix(identifier)
        if filename_rev is None:
            continue
        basename = os.path.basename(identifier)
        revision, _ = parse_revision_identifiers(source)
        if revision is None:
            mismatches.append(f"{basename}: no revision field found")
        elif revision != filename_rev:
            mismatches.append(
                f"{basename}: filename says {filename_rev}, "
                f"internal revision says {revision}"
            )
    return mismatches


def validate_added_migrations(sources: dict[str, str]) -> str | None:
    """Validate that the migrations a rollup adds form one linear extension.

    Existing migrations on the target branch are immutable and already form a
    single-head chain, so a combined-chain regression is introduced solely by
    the migrations a rollup adds. Returns ``None`` when ``sources`` is empty or
    valid; otherwise a human-readable reason the combined chain would be
    invalid (unparseable file, filename/revision desync, duplicate revision id,
    or multiple heads).
    """
    if not sources:
        return None
    chain, duplicates, unparseable = build_migration_chain(sources)
    if unparseable:
        return (
            "could not parse a revision identifier from migration file(s): "
            + ", ".join(sorted(unparseable))
        )
    mismatches = find_filename_revision_mismatches(sources)
    if mismatches:
        return (
            "migration filename prefix does not match its in-file revision: "
            + "; ".join(sorted(mismatches))
        )
    if duplicates:
        return (
            "duplicate Alembic revision identifier(s) across rollup migrations: "
            + ", ".join(sorted(set(duplicates)))
        )
    heads = find_heads(chain)
    if len(heads) != 1:
        return (
            f"rollup migrations resolve to {len(heads)} Alembic heads "
            f"({', '.join(sorted(heads))}); expected exactly 1"
        )
    roots = find_roots(chain)
    if len(roots) != 1:
        return (
            f"rollup migrations anchor at {len(roots)} points "
            f"({', '.join(sorted(roots))}); expected a single linear chain"
        )
    return None


def validate_combined_migration_chain(
    existing_filenames: list[str], added_sources: dict[str, str]
) -> str | None:
    """Validate a rollup's added migrations against the target branch's chain.

    The in-isolation guard (:func:`validate_added_migrations`) only proves the
    *added* migrations form one linear extension; it treats their anchoring
    ``down_revision`` as a valid external reference. That is blind to the most
    common parallel-development collision: two independently-cut branches each
    add "the next" migration with the SAME revision id (or anchored to a
    revision that is no longer the target's head). Each branch's own CI is
    green, but merged they produce duplicate revisions / multiple Alembic heads
    and the app refuses to boot on the next schema-version check.

    This combines the two: ``existing_filenames`` are the migration filenames
    currently on the target branch (the filename ``NNNN`` prefix is the existing
    revision id, kept in lockstep with the in-file revision by the
    migration-chain invariant), and ``added_sources`` maps an identifier to the
    source of each migration the rollup adds. Returns ``None`` when the combined
    chain is valid; otherwise a precise, actionable reason naming the colliding
    or stale revision and the fix. The in-isolation checks still run first, so
    this is a strict superset of :func:`validate_added_migrations`.

    With no existing migrations (empty/greenfield target) it degrades exactly to
    :func:`validate_added_migrations`.
    """
    reason = validate_added_migrations(added_sources)
    if reason is not None:
        return reason
    if not added_sources:
        return None

    existing_revisions = {
        rev
        for fn in existing_filenames
        if (rev := _filename_revision_prefix(fn)) is not None
    }
    if not existing_revisions:
        # No existing chain to combine against — in-isolation result stands.
        return None
    existing_head = max(existing_revisions, key=int)

    added_chain, _duplicates, _unparseable = build_migration_chain(added_sources)

    # (a) A revision id the rollup adds already exists on the target branch.
    collisions = sorted(set(added_chain) & existing_revisions, key=int)
    if collisions:
        rev = collisions[0]
        return (
            f"Alembic revision '{rev}' already exists on the target branch; "
            f"renumber to the next free revision and rechain onto the current "
            f"head '{existing_head}'."
        )

    # (b) The added chain's root must attach to the target's current head; a
    # stale ``down_revision`` (anchored to an older revision, or to None when
    # the target already has migrations) would create a second Alembic head.
    roots = find_roots(added_chain)  # exactly 1 — validate_added_migrations ensured
    root_rev = roots[0]
    anchor = added_chain[root_rev]
    if anchor != existing_head:
        return (
            f"Alembic migration '{root_rev}' anchors to down_revision "
            f"'{anchor}', but the target branch head is '{existing_head}'; "
            f"rechain '{root_rev}' onto '{existing_head}'."
        )
    return None


_EXPECTED_SCHEMA_REVISION_RE = re.compile(
    r"""(?P<lead>EXPECTED_SCHEMA_REVISION\s*=\s*)['"]\w+['"]"""
)


def rewrite_expected_schema_revision(
    main_source: str, new_revision: str
) -> tuple[str, int]:
    """Rewrite the ``EXPECTED_SCHEMA_REVISION = "NNNN"`` literal in ``main.py``.

    Returns ``(rewritten_source, count)`` where ``count`` is the number of
    assignments replaced (0 when the literal is absent — the caller must not
    guess and should fall back rather than push a malformed bump). The quote
    style is normalised to double quotes, matching the in-repo declaration.
    """
    return _EXPECTED_SCHEMA_REVISION_RE.subn(
        lambda m: f'{m.group("lead")}"{new_revision}"', main_source, count=1
    )


def _rewrite_migration_source(
    source: str, new_revision: str, new_down_revision: str
) -> str:
    """Renumber a single migration file's ``revision`` / ``down_revision``.

    Replaces the first ``revision = '...'`` assignment with *new_revision* and
    the first ``down_revision = '...'`` (or ``down_revision = None``) assignment
    with *new_down_revision*, preserving the existing quote style for the
    revision token. Pure string rewrite — used only on a not-yet-merged
    rollup's own newly-added migration file.
    """

    def _sub_rev(m: "re.Match[str]") -> str:
        return m.group(0).replace(m.group(1), new_revision, 1)

    def _sub_down(m: "re.Match[str]") -> str:
        full = m.group(0)
        if m.group(1):  # down_revision = '<rev>'
            return full.replace(m.group(1), new_down_revision, 1)
        # down_revision = None  ->  down_revision = "<head>"
        return full[: full.rindex("None")] + f'"{new_down_revision}"'

    source = _REVISION_RE.sub(_sub_rev, source, count=1)
    source = _DOWN_REVISION_RE.sub(_sub_down, source, count=1)
    return source


def _renumbered_filename(old_path: str, new_revision: str) -> str:
    """Rename a migration path so its ``NNNN`` prefix matches *new_revision*.

    Keeps the directory and the descriptive suffix after the first underscore
    (e.g. ``.../0137_add_foo.py`` -> ``.../0138_add_foo.py``).
    """
    directory, basename = os.path.split(old_path)
    suffix = basename[len(basename.split("_", 1)[0]):]  # leading "_..." (or "")
    return os.path.join(directory, new_revision + suffix) if directory else (
        new_revision + suffix
    )


def plan_migration_reconciliation(
    existing_filenames: list[str], added_sources: dict[str, str]
) -> dict | None:
    """Plan a mechanical renumber of a rollup's single colliding migration.

    Returns ``None`` unless the block is unambiguously mechanically
    reconcilable: the rollup adds EXACTLY ONE migration that forms an
    otherwise-valid single linear extension (per :func:`validate_added_migrations`)
    and is blocked SOLELY by a revision-id collision with the target branch or
    a stale ``down_revision`` anchor (the two classes :func:`validate_combined_migration_chain`
    rejects). Anything else — multiple added migrations, multiple heads, a parse
    failure, or an already-valid chain — returns ``None`` so the caller falls
    back to a clear operator alert rather than guessing.

    When reconcilable, returns a plan dict::

        {
            "old_path", "new_path",          # rename source -> dest
            "old_revision", "new_revision",  # NNNN renumber
            "new_down_revision",             # rechained onto target head
            "new_source",                    # rewritten migration body
            "expected_schema_revision",      # == new_revision (the post-merge head)
        }

    The plan is verified against :func:`validate_combined_migration_chain` before
    being returned, so a renumber that would not resolve to a single clean head
    yields ``None`` rather than an unsafe push.
    """
    # Must be a valid single linear extension in isolation; a body/structure
    # problem (multi-head, duplicate, unparseable) is not mechanical.
    if validate_added_migrations(added_sources) is not None:
        return None
    if len(added_sources) != 1:
        return None

    existing_revisions = {
        rev
        for fn in existing_filenames
        if (rev := _filename_revision_prefix(fn)) is not None
    }
    if not existing_revisions:
        # No existing chain to collide against — nothing to reconcile.
        return None
    existing_head = max(existing_revisions, key=int)

    old_path, source = next(iter(added_sources.items()))
    old_revision, old_down_revision = parse_revision_identifiers(source)
    if old_revision is None:
        return None

    collides = old_revision in existing_revisions
    stale_anchor = old_down_revision != existing_head
    if not collides and not stale_anchor:
        # Already a clean linear extension onto the head — no block to fix.
        return None

    # Next free revision after the current head (head is the max, so head+1 is
    # always free and cannot collide with any existing or added revision).
    width = len(existing_head)
    new_revision = f"{int(existing_head) + 1:0{width}d}"
    new_path = _renumbered_filename(old_path, new_revision)
    new_source = _rewrite_migration_source(source, new_revision, existing_head)

    # Verify the renumbered result against the same combined-chain validation
    # the guard uses — never return a plan that would not actually resolve.
    if validate_combined_migration_chain(existing_filenames, {new_path: new_source}):
        return None

    return {
        "old_path": old_path,
        "new_path": new_path,
        "old_revision": old_revision,
        "new_revision": new_revision,
        "new_down_revision": existing_head,
        "new_source": new_source,
        "expected_schema_revision": new_revision,
    }


if __name__ == "__main__":
    _dir = sys.argv[1] if len(sys.argv) > 1 else None
    sys.exit(main(_dir))
