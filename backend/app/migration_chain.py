"""DB-free Alembic migration-chain parsing and validation.

Shared by the rollup auto-merge guard (``rollup_management``) and the
migration-chain invariant test (``test_invariants``) so both assert against
the same in-file ``revision`` / ``down_revision`` parsing rather than two
divergent re-implementations. Pure string/regex logic — no DB, no I/O.
"""

import re

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


def validate_added_migrations(sources: dict[str, str]) -> str | None:
    """Validate that the migrations a rollup adds form one linear extension.

    Existing migrations on the target branch are immutable and already form a
    single-head chain, so a combined-chain regression is introduced solely by
    the migrations a rollup adds. Returns ``None`` when ``sources`` is empty or
    valid; otherwise a human-readable reason the combined chain would be
    invalid (unparseable file, duplicate revision id, or multiple heads).
    """
    if not sources:
        return None
    chain, duplicates, unparseable = build_migration_chain(sources)
    if unparseable:
        return (
            "could not parse a revision identifier from migration file(s): "
            + ", ".join(sorted(unparseable))
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
