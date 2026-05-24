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


if __name__ == "__main__":
    _dir = sys.argv[1] if len(sys.argv) > 1 else None
    sys.exit(main(_dir))
