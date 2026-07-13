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
from collections.abc import Iterable

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


def next_revision(existing_revisions: "Iterable[str]") -> str:
    """The next free Alembic revision id above every id in *existing_revisions*.

    Pure numbering logic, kept here in the DB-free chain module so both the
    migration-reservation allocator (``migration_reservations``) and any future
    chain-extension caller share one tested implementation rather than two
    divergent ``max + 1`` re-rolls.

    Only digit-only revision ids participate (non-numeric ids such as branch
    labels are ignored). The result is ``max(numeric ids) + 1`` zero-padded to
    the WIDTH OF THE WIDEST existing id, so a chain of ``0151``-style ids yields
    ``0152`` (not ``152``). With no numeric ids at all the chain is treated as
    empty and ``"0001"`` is returned — the conventional first revision width.
    """
    numeric = [rev for rev in existing_revisions if rev and rev.isdigit()]
    if not numeric:
        return "0001"
    width = max(len(rev) for rev in numeric)
    nxt = max(int(rev) for rev in numeric) + 1
    return f"{nxt:0{width}d}"


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

    errors.extend(
        f"  duplicate revision '{rev}' declared by more than one file"
        for rev in sorted(set(duplicates))
    )

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


def _filename_description(identifier: str) -> str | None:
    """The descriptive suffix after a migration filename's ``NNNN_`` prefix.

    ``0138_digest_cache_table.py`` -> ``digest_cache_table``. Returns ``None``
    for identifiers without a numeric revision prefix (non-migration files), so
    callers can skip them. The description is a migration's stable identity:
    immutable migrations keep it forever, so a description appearing under two
    different revision ids means the migration was renumbered.
    """
    if _filename_revision_prefix(identifier) is None:
        return None
    basename = os.path.basename(identifier)
    stem = basename[:-3] if basename.endswith(".py") else basename
    parts = stem.split("_", 1)
    return parts[1] if len(parts) > 1 else ""


def find_content_divergent_revisions(
    base_filenames: list[str], head_filenames: list[str]
) -> list[str]:
    """Revisions whose migration *identity* diverges between two refs.

    The other guards each prove a single checkout's chain is internally valid;
    none compares a revision id's content ACROSS refs. The main<->automain
    back-merge renumber produced exactly that blind spot: a revision id the LIVE
    DB had already applied ended up mapping to a DIFFERENT migration than what
    shipped, so alembic considered it applied and skipped it, and the new
    revision boot-crashed on the missing schema (prod incident 2026-06-04).

    This catches it statically, before merge, by comparing the two refs'
    migration FILENAMES — the ``NNNN`` prefix is the revision id and the suffix
    is the migration's identity, kept in lockstep with the in-file revision by
    the migration-chain invariant. It flags two complementary divergences:

      (a) the SAME revision id describes a DIFFERENT migration on each side — an
          id was reused for new content, so a DB that applied the old meaning
          silently skips the new one; and
      (b) the SAME migration appears under DIFFERENT revision ids — a migration
          was renumbered/moved, so a DB that applied it under the old id will
          re-run (or skip) it under the new one.

    Returns human-readable strings naming each divergence; empty when the two
    refs agree on every shared revision id and migration. Pure and repo-agnostic
    (filename-only, no DB, no I/O) — protects any repo Blueprint edits, not just
    the self-repo. Distinct from :func:`validate_combined_migration_chain`, which
    only catches an *added* id colliding with an existing one; a renumber that
    moves a migration to a brand-new id leaves the on-disk chain valid and slips
    past every structural check while still breaking the live DB.

    Thin string wrapper over :func:`find_content_divergence_records` (which
    callers needing to classify a divergence — e.g. against an authorized
    reconciliation registry — use directly).
    """
    return [
        rec["message"]
        for rec in find_content_divergence_records(base_filenames, head_filenames)
    ]


def _rev_to_desc(filenames: list[str]) -> dict[str, str | None]:
    out: dict[str, str | None] = {}
    for fn in filenames:
        rev = _filename_revision_prefix(fn)
        if rev is not None:
            out[rev] = _filename_description(fn)
    return out


def _desc_to_rev(filenames: list[str]) -> dict[str | None, str]:
    out: dict[str | None, str] = {}
    for fn in filenames:
        rev = _filename_revision_prefix(fn)
        if rev is not None:
            out[_filename_description(fn)] = rev
    return out


def find_content_divergence_records(
    base_filenames: list[str], head_filenames: list[str]
) -> list[dict]:
    """Structured form of :func:`find_content_divergent_revisions`.

    Each record describes one divergence so callers can classify it (e.g. match
    it against an authorized-reconciliation registry) instead of pattern-matching
    the human-readable string. The two record shapes mirror the two divergence
    classes:

      ``{"kind": "reused", "revision", "base_identity", "head_identity", "message"}``
          the SAME revision id names a DIFFERENT migration on each ref; and
      ``{"kind": "renumbered", "identity", "base_revision", "head_revision",
         "message"}``
          the SAME migration appears under DIFFERENT revision ids.

    Ordering matches the string detector: all ``reused`` records (sorted by
    revision) first, then all ``renumbered`` records (sorted by identity).
    """
    base_rev, head_rev = _rev_to_desc(base_filenames), _rev_to_desc(head_filenames)
    base_desc, head_desc = _desc_to_rev(base_filenames), _desc_to_rev(head_filenames)

    reused: list[dict] = [
        {
            "kind": "reused",
            "revision": rev,
            "base_identity": base_rev[rev],
            "head_identity": head_rev[rev],
            "message": (
                f"revision '{rev}' maps to migration '{base_rev[rev]}' on one ref "
                f"but '{head_rev[rev]}' on the other — the same revision id was "
                f"reused for different content; a DB that applied the first will "
                f"silently skip the second"
            ),
        }
        for rev in sorted(set(base_rev) & set(head_rev))
        if base_rev[rev] != head_rev[rev]
    ]
    renumbered: list[dict] = [
        {
            "kind": "renumbered",
            "identity": desc,
            "base_revision": base_desc[desc],
            "head_revision": head_desc[desc],
            "message": (
                f"migration '{desc}' is revision '{base_desc[desc]}' on one ref but "
                f"'{head_desc[desc]}' on the other — it was renumbered; a DB that "
                f"applied it under the old id will mishandle the new id"
            ),
        }
        for desc in sorted(d for d in (set(base_desc) & set(head_desc)) if d)
        if base_desc[desc] != head_desc[desc]
    ]
    return reused + renumbered


def find_deployed_migration_content_edits(
    base_sources: dict[str, str], head_sources: dict[str, str]
) -> list[str]:
    """Detect in-place body edits to deployed migration files.

    Compares migration file content by basename across *base_sources* (the
    deployed branch) and *head_sources* (the candidate). When the same filename
    appears in both maps but with different bodies, the migration was modified
    in-place — an operation that corrupts any live database that has already
    applied the original revision, because Alembic considers the revision
    already-applied and silently skips the changed body.

    Content comparison is whitespace-normalised (trailing whitespace stripped
    per line, trailing blank lines removed) to avoid false positives from
    editor-introduced trailing spaces or newline-convention drift.  Returns
    human-readable error strings; empty when all shared files are
    content-identical.
    """

    def _normalise(src: str) -> str:
        lines = [line.rstrip() for line in src.splitlines()]
        while lines and not lines[-1]:
            lines.pop()
        return "\n".join(lines)

    base_by_name = {os.path.basename(k): v for k, v in base_sources.items()}
    head_by_name = {os.path.basename(k): v for k, v in head_sources.items()}

    return [
        (
            f"migration '{name}' was modified in-place — deployed migration files "
            f"are immutable once applied; a body edit on an already-applied revision "
            f"is silently skipped by Alembic and corrupts any live database"
        )
        for name in sorted(set(base_by_name) & set(head_by_name))
        if _normalise(base_by_name[name]) != _normalise(head_by_name[name])
    ]


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


def validate_standalone_chain(
    sources: dict[str, str],
    *,
    expected_schema_revision: str | None = None,
) -> list[str]:
    """Validate that *sources* form a single clean migration chain.

    Aggregates the filename/revision-desync, duplicate-revision,
    missing-parent, and multi-head detectors into one call. Returns a list of
    human-readable error strings; empty when the chain is valid.

    Non-contiguous revision numbering (gaps such as 0334 -> 0347) is
    explicitly valid.  Revision ids are Alembic-opaque labels; the only
    structural requirements are a single head and no duplicate ids.  Gaps arise
    naturally from the branch-cut workflow: each task branch reserves the next
    free slot at cut time, and merge-time reconciliation renumbers everything
    into the contiguous live sequence.  A gap in the on-disk chain is therefore
    a normal branch-cut artifact, not an error.

    When *expected_schema_revision* is provided, also validates that it matches
    the structural head of the chain (the revision no other revision points to).
    This lets callers prove the EXPECTED_SCHEMA_REVISION detector fires on bad
    fixtures rather than relying solely on the current chain being clean.

    Exposed as a reusable helper so the standalone smoke-set invariant test
    can (a) validate the real on-disk chain and (b) seed bad fixtures to prove
    each detector fires — providing negative coverage instead of relying solely
    on the current chain being clean.
    """
    errors: list[str] = []
    mismatches = find_filename_revision_mismatches(sources)
    errors.extend(f"filename/revision mismatch: {m}" for m in mismatches)
    chain, duplicates, unparseable = build_migration_chain(sources)
    errors.extend(f"no parseable revision in {f!r}" for f in sorted(unparseable))
    errors.extend(
        f"duplicate revision '{rev}' declared by more than one file"
        for rev in sorted(set(duplicates))
    )
    errors.extend(
        f"revision '{rev}' references down_revision '{down}' not in chain"
        for rev, down in sorted(chain.items())
        if down is not None and down not in chain
    )
    heads = find_heads(chain)
    if len(heads) != 1:
        errors.append(
            f"chain has {len(heads)} head(s) ({', '.join(sorted(heads))}); "
            "expected exactly 1"
        )
    elif expected_schema_revision is not None and heads[0] != expected_schema_revision:
        errors.append(
            f"EXPECTED_SCHEMA_REVISION is '{expected_schema_revision}' but "
            f"structural chain head is '{heads[0]}'; bump EXPECTED_SCHEMA_REVISION "
            "in main.py to match the chain head"
        )
    return errors


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

    added_chain, _duplicates, _unparseable = build_migration_chain(added_sources)

    # Build the combined revision graph: the target branch's existing revisions,
    # ordered into a linear chain by their monotonic ``NNNN`` ids (the
    # migration-chain invariant keeps the on-disk target chain single-head and
    # monotonic, so numeric order faithfully reconstructs its topological order),
    # plus the revisions this rollup adds. The head is derived TOPOLOGICALLY via
    # :func:`find_heads` over this graph rather than by numeric max — a migration
    # renumbered or rechained to carry the largest id is exactly what let a stale
    # head masquerade as valid under numeric-max detection (the v25 identity
    # divergence).
    sorted_existing = sorted(existing_revisions, key=int)
    combined_chain: dict[str, str | None] = {}
    prev: str | None = None
    for rev in sorted_existing:
        combined_chain[rev] = prev
        prev = rev
    existing_head = find_heads(combined_chain)[0]
    combined_chain.update(added_chain)

    # (a) A revision id the rollup adds already exists on the target branch.
    collisions = sorted(set(added_chain) & existing_revisions, key=int)
    if collisions:
        rev = collisions[0]
        return (
            f"Alembic revision '{rev}' already exists on the target branch; "
            f"renumber your new migration to the next free revision above the "
            f"current head '{existing_head}'."
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
            f"renumber your new migration onto the current head '{existing_head}' "
            f"(never rechain an existing migration onto a higher number — that is "
            f"what produced the migration-identity divergence)."
        )

    # (c) Combined-result monotonicity: the merged chain must resolve to a single
    # head that is also the numeric-max revision. An added migration correctly
    # anchored to the head but carrying an id at or below the head (a backwards
    # renumber) passes (a) and (b) yet leaves numeric-max pointing at a stale
    # revision — the very masquerade numeric-max head detection cannot see.
    combined_heads = find_heads(combined_chain)
    numeric_max = max(combined_chain, key=int)
    if len(combined_heads) != 1 or combined_heads[0] != numeric_max:
        added_head = find_heads(added_chain)[0]
        return (
            f"Alembic migration '{added_head}' would leave the combined chain "
            f"non-monotonic: its revision id is not the highest in the merged "
            f"chain (numeric max is '{numeric_max}'), so numeric-max head "
            f"detection would mis-identify the head. Renumber your new migration "
            f"onto the current head '{existing_head}' — never rechain an existing "
            f"migration onto a higher number."
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
    suffix = basename[len(basename.split("_", 1)[0]) :]  # leading "_..." (or "")
    return (
        os.path.join(directory, new_revision + suffix)
        if directory
        else (new_revision + suffix)
    )


def _rewrite_linear_chain(
    *,
    sources: dict[str, str],
    chain_order: list[str],
    rev_to_path: dict[str, str],
    first_revision: str,
    first_down_revision: str,
) -> tuple[dict[str, str], list[str], str, str]:
    """Rewrite a linear chain into the contiguous block starting at *first_revision*.

    Returns ``(upserts, deletes, root_new_revision, root_new_path)``. Only
    changed nodes are emitted in ``upserts``; only renamed paths appear in
    ``deletes``. When a node keeps its filename but its ``down_revision`` or
    ``revision`` changes, it is rewritten in place (upsert only, no delete).
    """
    width = len(first_revision)
    base = int(first_revision)
    upserts: dict[str, str] = {}
    deletes: list[str] = []
    prev_new_revision = first_down_revision
    root_new_path = ""
    root_new_revision = ""

    for offset, rev in enumerate(chain_order):
        desired_revision = f"{base + offset:0{width}d}"
        old_path = rev_to_path[rev]
        old_source = sources[old_path]
        new_path = _renumbered_filename(old_path, desired_revision)
        new_source = _rewrite_migration_source(
            old_source, desired_revision, prev_new_revision
        )
        if new_path != old_path:
            deletes.append(old_path)
        if new_path != old_path or new_source != old_source:
            upserts[new_path] = new_source
        if offset == 0:
            root_new_revision = desired_revision
            root_new_path = new_path
        prev_new_revision = desired_revision

    return upserts, deletes, root_new_revision, root_new_path


def plan_migration_reconciliation(
    existing_filenames: list[str],
    added_sources: dict[str, str],
    *,
    protected_revisions: Iterable[str] = (),
) -> dict | None:
    """Plan merge-time finalization of a rollup's provisional migration chain.

    Returns ``None`` unless the block is unambiguously mechanically
    reconcilable: the rollup adds migrations that form a SINGLE LINEAR CHAIN
    (per :func:`validate_added_migrations`, which already requires exactly one
    head and one root). Authored ``revision`` / ``down_revision`` values are
    treated as PROVISIONAL until merge: the root migration is finalized onto the
    target branch's CURRENT head and the entire linear descendant tail is
    rewritten into the contiguous block that starts at the next free revision
    above that head, filling abandoned provisional holes when safe.
    ``protected_revisions`` may additionally reserve numeric revision ids that are not on the
    target branch yet but are already stamped elsewhere (for example on a deployed
    promotion target during an integration-vs-production divergence window); when
    present the renumber starts above BOTH the target branch and those protected
    ids, never backfilling an unsafe cross-branch collision. Anything genuinely
    ambiguous — migrations that do not form one linear chain (multiple independent
    collisions / multiple heads / multiple roots), a fan-out tail, a parse
    failure, or an already-finalized chain — returns ``None`` so the caller falls
    back to a clear operator alert rather than guessing.

    When reconcilable, returns a plan dict::

        {
            "upserts",                       # path -> new source: every renamed +
                                             #   renumbered migration in the chain
            "deletes",                       # [old paths] being renamed away
            "old_path", "new_path",          # rename of the chain's root migration
            "old_revision", "new_revision",  # the root's NNNN renumber
            "new_down_revision",             # the root rechained onto target head
            "new_source",                    # the root's rewritten body
            "expected_schema_revision",      # the resulting single head == numeric max
        }

    The reconciled set is verified against
    :func:`validate_combined_migration_chain` plus a single-head and
    numeric-max-is-head guard before being returned, so a rewrite that would not
    resolve to one clean monotonic head yields ``None`` rather than an unsafe
    push. The scalar ``old_*``/``new_*``/``new_source`` keys describe the
    chain's root and are preserved for the degenerate single-migration case so
    existing callers keep working.
    """
    # Must be a valid single linear extension in isolation; a body/structure
    # problem (multi-head, multi-root, duplicate, unparseable) is not mechanical.
    if validate_added_migrations(added_sources) is not None:
        return None
    if not added_sources:
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

    added_chain, _duplicates, _unparseable = build_migration_chain(added_sources)

    # validate_added_migrations guaranteed exactly one head and one root.
    root_rev = find_roots(added_chain)[0]
    # revision -> path for the added set (unique: validate_added_migrations
    # rejected duplicates and unparseable files above).
    rev_to_path = {
        parse_revision_identifiers(src)[0]: path for path, src in added_sources.items()
    }

    # The added chain must be a single linear tail from its root; a diamond
    # (one root + one head but an internal fan-out) is not mechanically
    # rechainable. The tail plus the root must cover the whole added set.
    children: dict[str, list[str]] = {}
    for rev, down in added_chain.items():
        if down is not None:
            children.setdefault(down, []).append(rev)
    tail = _linear_descendant_tail(root_rev, children)
    if tail is None:
        return None
    chain_order = [root_rev, *tail]
    if len(chain_order) != len(added_chain):
        return None

    reserved_revisions = existing_revisions | {
        rev for rev in protected_revisions if isinstance(rev, str) and rev.strip()
    }
    first_revision = next_revision(reserved_revisions)
    upserts, deletes, new_revision, new_path = _rewrite_linear_chain(
        sources=added_sources,
        chain_order=chain_order,
        rev_to_path=rev_to_path,
        first_revision=first_revision,
        first_down_revision=existing_head,
    )
    if not upserts and not deletes:
        # Already finalized onto the live head with contiguous numbering.
        return None
    old_path = rev_to_path[root_rev]

    # Verify the reconciled added set against the same combined-chain validation
    # the guard uses — never return a plan that would not actually resolve.
    reconciled = {
        path: src for path, src in added_sources.items() if path not in deletes
    }
    reconciled.update(upserts)
    if validate_combined_migration_chain(existing_filenames, reconciled):
        return None
    reconciled_chain, _dups, _unp = build_migration_chain(reconciled)
    heads = find_heads(reconciled_chain)
    if len(heads) != 1:
        return None

    # Monotonicity guard: the numeric-max revision MUST be the structural head,
    # otherwise the combined-chain guard's numeric-max head shortcut would
    # mis-identify the head on the next migration. Never emit a non-monotonic
    # plan — fall back to the operator path instead.
    if max(reconciled_chain, key=int) != heads[0]:
        return None

    return {
        "upserts": upserts,
        "deletes": deletes,
        "old_path": old_path,
        "new_path": new_path,
        "old_revision": root_rev,
        "new_revision": new_revision,
        "new_down_revision": existing_head,
        "new_source": reconciled[new_path],
        "expected_schema_revision": heads[0],
    }


def _linear_descendant_tail(
    start_revision: str, children: dict[str, list[str]]
) -> list[str] | None:
    """Ordered descendants of *start_revision* when they form a simple linear tail.

    ``children`` maps a revision to the list of revisions that declare it as
    their ``down_revision``. Walks downstream from *start_revision* requiring
    every node to have at most one child; returns the descendants in chain order
    (nearest first, excluding *start_revision* itself). Returns ``None`` on any
    fan-out (a node with more than one child) or cycle — i.e. anything that is
    not a single linear tail and therefore not mechanically rechainable.
    """
    tail: list[str] = []
    seen = {start_revision}
    current = start_revision
    while True:
        kids = children.get(current, [])
        if not kids:
            return tail
        if len(kids) > 1:
            return None  # fan-out — the tail branches
        nxt = kids[0]
        if nxt in seen:
            return None  # cycle — not a linear tail
        seen.add(nxt)
        tail.append(nxt)
        current = nxt


def plan_integration_side_renumber(
    promotion_target_filenames: list[str], integration_sources: dict[str, str]
) -> dict | None:
    """Plan a mechanical renumber of the integration branch's colliding migration.

    The BACK-MERGE mirror of :func:`plan_migration_reconciliation`. When the
    promotion target (e.g. ``main``) is back-merged into the integration branch
    (e.g. ``automain``), the promotion-target migration is ALREADY APPLIED IN
    PRODUCTION and is therefore immutable — it must never be renumbered. The
    migration that must move is the one on the INTEGRATION branch whose revision
    id independently reused the same ``NNNN`` slot, together with any
    integration-branch migrations that linearly chain off it.

    ``promotion_target_filenames`` are the immutable migration filenames on the
    promotion target (the ``NNNN`` prefix is the revision id, kept in lockstep
    with the in-file revision by the migration-chain invariant); they supply the
    colliding revision id and the unified head. ``integration_sources`` maps
    path -> source for the integration branch's post-divergence migrations (the
    colliding migration and its descendant tail — NOT the migrations shared with
    the promotion target).

    Returns ``None`` unless the block is unambiguously mechanically reconcilable:
    EXACTLY ONE integration migration's revision id collides with a
    promotion-target revision id, that colliding migration is anchored to the
    shared promotion-target ancestor, and its descendants form a single linear
    tail. Anything else — zero or multiple collisions, a parse failure, a
    duplicate revision, a filename/revision desync, a fan-out tail, or a result
    that would not resolve to one clean head — returns ``None`` so the caller
    falls back to the guided resolver rather than guessing.

    When reconcilable, returns a plan dict::

        {
            "upserts",                  # path -> new source: the renamed +
                                        #   renumbered colliding migration AND
                                        #   every node in its linear descendant
                                        #   tail, renumbered monotonically
            "deletes",                  # [old colliding path, *old tail paths]
                                        #   being renamed away
            "old_path", "new_path",     # the rename of the colliding migration
            "old_revision", "new_revision",
            "new_down_revision",        # the unified promotion-target head
            "expected_schema_revision", # the resulting single head == numeric max
        }

    The colliding migration and its entire linear descendant tail are finalized
    into the contiguous block that starts at the NEXT free revision above the
    immutable promotion-target head, filling any abandoned provisional gaps on
    the integration side while keeping every deployed revision untouched. Each
    rewritten node chains onto its renumbered predecessor, so the reconciled
    chain is MONOTONIC (numeric-max revision == structural head).

    The reconciled result is verified against
    :func:`validate_combined_migration_chain` before being returned, so a
    renumber that would not resolve to exactly one clean head yields ``None``
    rather than an unsafe push. Reuses the existing chain helpers throughout —
    it does not introduce a second migration-safety rule set.
    """
    target_revisions = {
        rev
        for fn in promotion_target_filenames
        if (rev := _filename_revision_prefix(fn)) is not None
    }
    if not target_revisions:
        return None
    target_head = max(target_revisions, key=int)

    if not integration_sources:
        return None
    int_chain, duplicates, unparseable = build_migration_chain(integration_sources)
    if duplicates or unparseable:
        return None
    if find_filename_revision_mismatches(integration_sources):
        return None

    # Exactly one integration revision id may collide with a target revision id.
    collisions = set(int_chain) & target_revisions
    if len(collisions) != 1:
        return None
    colliding_rev = next(iter(collisions))

    # The colliding migration must anchor to the shared promotion-target
    # ancestor (a genuine divergence-point collision), not to another
    # integration migration (a non-mechanical fork we must not auto-renumber).
    if int_chain[colliding_rev] not in target_revisions:
        return None

    # revision -> path for the integration set (unique: no duplicates above).
    rev_to_path = {
        parse_revision_identifiers(src)[0]: path
        for path, src in integration_sources.items()
    }

    # The colliding migration's descendants must form a single linear tail.
    children: dict[str, list[str]] = {}
    for rev, down in int_chain.items():
        if down is not None:
            children.setdefault(down, []).append(rev)
    tail = _linear_descendant_tail(colliding_rev, children)
    if tail is None:
        return None

    # Chain order: the colliding migration first, then its linear descendants.
    chain_order = [colliding_rev, *tail]
    first_revision = next_revision(target_revisions)
    upserts, deletes, new_revision, new_path = _rewrite_linear_chain(
        sources=integration_sources,
        chain_order=chain_order,
        rev_to_path=rev_to_path,
        first_revision=first_revision,
        first_down_revision=target_head,
    )
    old_path = rev_to_path[colliding_rev]

    # Verify the reconciled integration set against the same combined-chain
    # validator the guard uses — never return a plan that would not resolve.
    reconciled = {
        path: src for path, src in integration_sources.items() if path not in deletes
    }
    reconciled.update(upserts)
    if validate_combined_migration_chain(promotion_target_filenames, reconciled):
        return None
    reconciled_chain, _dups, _unp = build_migration_chain(reconciled)
    heads = find_heads(reconciled_chain)
    if len(heads) != 1:
        return None

    # Monotonicity guard: the numeric-max revision MUST be the structural head,
    # otherwise the combined-chain guard's numeric-max head shortcut would
    # mis-identify the head on the next migration. Never emit a non-monotonic
    # plan — fall back to the guided resolver instead.
    if max(reconciled_chain, key=int) != heads[0]:
        return None

    return {
        "upserts": upserts,
        "deletes": deletes,
        "old_path": old_path,
        "new_path": new_path,
        "old_revision": colliding_rev,
        "new_revision": new_revision,
        "new_down_revision": target_head,
        "expected_schema_revision": heads[0],
    }


if __name__ == "__main__":
    _dir = sys.argv[1] if len(sys.argv) > 1 else None
    sys.exit(main(_dir))
