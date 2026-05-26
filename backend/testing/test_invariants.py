"""
Behavioral invariant tests ("tripwires").

These tests assert that critical safety constants and patterns have not been
modified by agents. If an agent changes a safety value, CI catches it.

Run with: pytest testing/test_invariants.py -v --noconftest
"""

import os
import re
import subprocess

import pytest


# ── Safety Constants ────────────────────────────────────────────────────────


class TestSafetyConstants:
    def test_max_review_fix_iterations(self):
        """MAX_REVIEW_FIX_ITERATIONS must stay at 16."""
        from app.review_pipeline import MAX_REVIEW_FIX_ITERATIONS

        assert MAX_REVIEW_FIX_ITERATIONS == 16, (
            f"MAX_REVIEW_FIX_ITERATIONS changed to {MAX_REVIEW_FIX_ITERATIONS} — "
            "this controls review-fix loop runaway. Do not change without approval."
        )

    def test_max_retries_default(self):
        """Default max_retries must be 3."""
        from app.shared_state import runtime_config

        # The default is set from env var with fallback to 3
        assert runtime_config["max_retries"] == int(os.getenv("MAX_RETRIES", "3"))

    def test_max_autonomous_tasks_per_hour(self):
        """Rate limit for autonomous task spawning."""
        from app.shared_state import runtime_config

        assert runtime_config["max_autonomous_tasks_per_hour"] == 12

    def test_max_rollup_retries_default(self):
        """Default max_rollup_retries must be 5 — controls rollup PR creation retry ceiling."""
        import os
        from app.shared_state import runtime_config

        assert runtime_config["max_rollup_retries"] == int(
            os.getenv("MAX_ROLLUP_RETRIES", "5")
        )

    def test_provision_slots_geq_concurrent_vms(self):
        """MAX_PROVISION_SLOTS must be >= MAX_CONCURRENT_VMS default.

        If this fails: the provision slot limit (MAX_PROVISION_SLOTS env var) is
        lower than the concurrent VM limit (MAX_CONCURRENT_VMS env var), which
        creates a dispatch deadlock — slots would be reserved by active VMs before
        provisioning can complete.
        """
        from app.compute import _MAX_GLOBAL_PROVISIONS

        max_concurrent_vms = int(os.getenv("MAX_CONCURRENT_VMS", "8"))
        assert _MAX_GLOBAL_PROVISIONS >= max_concurrent_vms, (
            f"MAX_PROVISION_SLOTS ({_MAX_GLOBAL_PROVISIONS}) < MAX_CONCURRENT_VMS ({max_concurrent_vms}) — "
            "provision slot limit must be >= concurrent VM limit to avoid dispatch deadlock."
        )


# ── Schema Revision Sync ───────────────────────────────────────────────────


class TestSchemaRevisionSync:
    def test_expected_revision_matches_latest_migration(self):
        """EXPECTED_SCHEMA_REVISION must match the latest alembic migration.

        If this fails, you added a migration but forgot to bump
        EXPECTED_SCHEMA_REVISION in main.py. The orchestrator will crash
        on startup with a schema version mismatch.
        """
        import glob as globmod
        from app.main import EXPECTED_SCHEMA_REVISION

        migrations_dir = os.path.join(
            os.path.dirname(__file__), "..", "alembic", "versions"
        )
        files = globmod.glob(os.path.join(migrations_dir, "[0-9]*.py"))
        assert files, "No migration files found"

        latest = max(os.path.basename(f).split("_")[0] for f in files)
        assert EXPECTED_SCHEMA_REVISION == latest, (
            f"EXPECTED_SCHEMA_REVISION is '{EXPECTED_SCHEMA_REVISION}' but latest "
            f"migration is '{latest}'. Bump EXPECTED_SCHEMA_REVISION in main.py."
        )

    def test_agent_helper_exclusions_match_prompt_and_gitignore(self):
        """Agent helper exclusions must stay aligned with prompt.ts and gitignore.

        Skipped when running in environments without the agent/ source tree
        or repo root checkout (e.g. CI's backend-only Docker build context,
        which excludes agent/, .gitignore, and .git). Locally and in any
        environment that does see the repo root the check fires normally.
        """
        prompt_path = os.path.join(
            os.path.dirname(__file__), "..", "..", "agent", "src", "prompt.ts"
        )
        gitignore_path = os.path.join(
            os.path.dirname(__file__), "..", "..", ".gitignore"
        )
        repo_root = os.path.normpath(
            os.path.join(os.path.dirname(__file__), "..", "..")
        )
        if not (
            os.path.exists(prompt_path)
            and os.path.exists(gitignore_path)
            and os.path.isdir(os.path.join(repo_root, ".git"))
        ):
            pytest.skip(
                "repo root sources not available in this test environment "
                "(CI docker context is ./backend; run locally or via "
                "scripts/test-backend.sh from a checkout for enforcement)"
            )

        with open(prompt_path) as f:
            prompt = f.read()
        match = re.search(r"export const AGENT_HELPER_NAMES = \[(.*?)\];", prompt, re.S)
        assert match, "Could not find AGENT_HELPER_NAMES literal in agent/src/prompt.ts"
        helpers = [
            name.strip().strip("'\"")
            for name in match.group(1).split(",")
            if name.strip()
        ]

        with open(gitignore_path) as f:
            gitignore = f.read().splitlines()
        for helper in helpers:
            assert f"/{helper}" in gitignore, f".gitignore is missing /{helper}"
        assert "*.bak" in gitignore, ".gitignore is missing *.bak"

        tracked = subprocess.run(
            ["git", "ls-files"],
            cwd=repo_root,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.splitlines()
        pattern = re.compile(rf"^(?:{'|'.join(map(re.escape, helpers))})$|\.bak$")
        leaked = [path for path in tracked if pattern.search(path)]
        assert not leaked, "Tracked helper or backup files found:\n  " + "\n  ".join(
            leaked
        )

    def test_schema_gate_raises_on_mismatch(self):
        """_verify_schema_version() must raise RuntimeError when the alembic
        revision in the database does not exactly match EXPECTED_SCHEMA_REVISION.

        Regression guard for H-O1: previously the gate used
        `row[0].startswith(EXPECTED_SCHEMA_REVISION)` (so `0055_a` quietly
        passed for an expected `0055_b`) and only raised when `K_REVISION`
        was set in env (so dev/staging silently accepted stale schemas).
        """
        import asyncio
        from unittest.mock import AsyncMock, MagicMock, patch
        from app.main import _verify_schema_version, EXPECTED_SCHEMA_REVISION

        # Build a row that's deliberately mismatched: same prefix length but
        # different suffix (catches the startswith bug specifically).
        bad_revision = "9999_stale_dev_db"
        assert bad_revision != EXPECTED_SCHEMA_REVISION

        result = MagicMock()
        result.first.return_value = (bad_revision,)
        session = AsyncMock()
        session.execute = AsyncMock(return_value=result)
        # async-context-manager wiring
        session_cm = MagicMock()
        session_cm.__aenter__ = AsyncMock(return_value=session)
        session_cm.__aexit__ = AsyncMock(return_value=None)

        # Ensure K_REVISION is unset so we exercise the "non-Cloud-Run still raises" path.
        env = {k: v for k, v in os.environ.items() if k != "K_REVISION"}
        with (
            patch.dict(os.environ, env, clear=True),
            patch("app.main.AsyncSessionLocal", return_value=session_cm),
        ):
            with pytest.raises(RuntimeError, match="Schema version mismatch"):
                asyncio.run(_verify_schema_version())

    def test_schema_gate_passes_on_exact_match(self):
        """_verify_schema_version() must succeed (return None, not raise)
        when the alembic revision exactly equals EXPECTED_SCHEMA_REVISION.
        """
        import asyncio
        from unittest.mock import AsyncMock, MagicMock, patch
        from app.main import _verify_schema_version, EXPECTED_SCHEMA_REVISION

        result = MagicMock()
        result.first.return_value = (EXPECTED_SCHEMA_REVISION,)
        session = AsyncMock()
        session.execute = AsyncMock(return_value=result)
        session_cm = MagicMock()
        session_cm.__aenter__ = AsyncMock(return_value=session)
        session_cm.__aexit__ = AsyncMock(return_value=None)

        with patch("app.main.AsyncSessionLocal", return_value=session_cm):
            # Must not raise.
            asyncio.run(_verify_schema_version())

    def test_migration_filenames_match_internal_revision(self):
        """Each migration file's internal `revision = '...'` must match its
        filename prefix. Alembic orders migrations by the internal revision,
        so a mismatch means the filename is misleading about ordering.
        """
        import glob as globmod

        from app.migration_chain import find_filename_revision_mismatches

        migrations_dir = os.path.join(
            os.path.dirname(__file__), "..", "alembic", "versions"
        )
        files = globmod.glob(os.path.join(migrations_dir, "[0-9]*.py"))
        sources: dict[str, str] = {}
        for path in files:
            with open(path) as f:
                sources[os.path.basename(path)] = f.read()
        # Consume the shared helper the rollup auto-merge guard also uses, so
        # the CI invariant and the runtime guard reason about filename-vs-
        # revision desync with identical parsing.
        mismatches = find_filename_revision_mismatches(sources)
        assert not mismatches, "Migration revision mismatches:\n  " + "\n  ".join(
            mismatches
        )

    def test_migration_chain_is_linear(self):
        """The alembic migration chain must be a single linear sequence —
        no gaps, no branches, no dangling down_revision references, and no
        duplicate revision identifiers across files.

        Uses the same DB-free parser (``app.migration_chain``) that the
        rollup auto-merge guard consumes, so the invariant test and the
        runtime guard assert against identical revision/down_revision
        resolution.
        """
        import glob as globmod

        from app.migration_chain import build_migration_chain, find_heads

        migrations_dir = os.path.join(
            os.path.dirname(__file__), "..", "alembic", "versions"
        )
        files = globmod.glob(os.path.join(migrations_dir, "[0-9]*.py"))

        sources: dict[str, str] = {}
        for path in files:
            with open(path) as f:
                sources[os.path.basename(path)] = f.read()

        chain, duplicates, unparseable = build_migration_chain(sources)

        # No unparseable migration files (every file must declare a revision).
        assert not unparseable, (
            "Migration files with no parseable revision identifier:\n  "
            + "\n  ".join(sorted(unparseable))
        )

        # No two files may declare the same Alembic revision id — that is the
        # rollup-collision regression this invariant exists to catch.
        assert not duplicates, (
            "Duplicate Alembic revision identifier(s) across migration files: "
            + ", ".join(sorted(set(duplicates)))
        )

        # Every down_revision must point to a revision that exists (or None for the root)
        missing_parents = [
            f"{rev} → {down} (parent doesn't exist)"
            for rev, down in chain.items()
            if down is not None and down not in chain
        ]
        assert not missing_parents, "Broken migration chain:\n  " + "\n  ".join(
            missing_parents
        )

        # Exactly one root (down_revision=None) and one head (no one points to it)
        roots = [r for r, d in chain.items() if d is None]
        assert len(roots) == 1, (
            f"Expected exactly 1 root migration, found {len(roots)}: {roots}"
        )

        heads = find_heads(chain)
        assert len(heads) == 1, (
            f"Expected exactly 1 head migration, found {len(heads)}: {heads}"
        )

    def test_model_columns_covered_by_migrations(self):
        """Every column defined on a SQLModel table must exist in the alembic
        migration chain. If an agent adds a column to models.py but forgets
        the migration, Cloud Run startup will crash with UndefinedColumnError.

        Scans all migration files for add_column calls and column definitions
        in create_table, and checks each model's columns against them.
        """
        import glob as globmod
        import re
        from sqlmodel import SQLModel

        # Import models so SQLModel registers all tables
        from app import models  # noqa: F401

        migrations_dir = os.path.join(
            os.path.dirname(__file__), "..", "alembic", "versions"
        )
        files = globmod.glob(os.path.join(migrations_dir, "[0-9]*.py"))

        # Collect all (table, column) pairs present in migrations
        migration_columns: set[tuple[str, str]] = set()
        add_col_re = re.compile(
            r"add_column\(\s*['\"](\w+)['\"]\s*,\s*sa\.Column\(\s*['\"](\w+)['\"]"
        )
        create_table_re = re.compile(r"create_table\(\s*['\"](\w+)['\"]", re.MULTILINE)
        col_in_create_re = re.compile(r"sa\.Column\(\s*['\"](\w+)['\"]")
        # Renames: alter_column('table', 'old', new_column_name='new') → new column exists
        rename_re = re.compile(
            r"alter_column\(\s*['\"](\w+)['\"]\s*,\s*['\"](\w+)['\"].*?new_column_name\s*=\s*['\"](\w+)['\"]",
            re.DOTALL,
        )

        for path in files:
            with open(path) as f:
                content = f.read()

            # add_column('table', sa.Column('name', ...))
            for m in add_col_re.finditer(content):
                migration_columns.add((m.group(1), m.group(2)))

            # alter_column('table', 'old_name', new_column_name='new_name', ...)
            for m in rename_re.finditer(content):
                migration_columns.add((m.group(1), m.group(3)))

            # create_table('table', sa.Column('name', ...), ...)
            for ct in create_table_re.finditer(content):
                table = ct.group(1)
                # Find the matching closing paren for this create_table call
                start = ct.end()
                depth = 1
                end = start
                while end < len(content) and depth > 0:
                    if content[end] == "(":
                        depth += 1
                    elif content[end] == ")":
                        depth -= 1
                    end += 1
                block = content[start:end]
                for col_m in col_in_create_re.finditer(block):
                    migration_columns.add((table, col_m.group(1)))

        # Tables whose migrations are pending (added by a sibling task in the same PR batch).
        # Remove each entry once its migration file lands.
        PENDING_MIGRATION_TABLES: set[str] = set()

        # Check each SQLModel table's columns against migrations
        missing: list[tuple[str, str]] = []
        for table_name, table in SQLModel.metadata.tables.items():
            if table_name in PENDING_MIGRATION_TABLES:
                continue
            for col in table.columns:
                if (table_name, col.name) not in migration_columns:
                    missing.append((table_name, col.name))

        assert not missing, (
            "Columns defined on models but missing from alembic migrations:\n"
            + "\n".join(f"  - {t}.{c}" for t, c in missing)
            + "\n\nAdd a migration for each missing column, or the backend will "
            "crash on startup with UndefinedColumnError."
        )

    def test_migration_column_types_match_models(self):
        """Migration column types must be compatible with SQLModel definitions.

        Catches auto-generation errors like declaring a JSONB field as sa.Text().
        Comparison is by type family (String/Text compatible; JSONB must match
        JSONB; Integer, Boolean, UUID, ARRAY must each match).
        """
        import glob as globmod
        import re
        from sqlmodel import SQLModel
        from app import models  # noqa: F401

        def family(name: str) -> str:
            n = name.upper()
            if n in {
                "STRING",
                "TEXT",
                "VARCHAR",
                "UNICODE",
                "UNICODETEXT",
                "AUTOSTRING",
                "CHAR",
                "NCHAR",
                "NVARCHAR",
            }:
                return "STRING"
            if n in {"INTEGER", "BIGINTEGER", "SMALLINTEGER", "INT"}:
                return "INTEGER"
            if n in {"UUID", "GUID"}:
                return "UUID"
            # ``NAIVEUTCDATETIME`` is the round-2 audit TypeDecorator
            # (``app.models.NaiveUTCDateTime``) whose ``impl`` is
            # ``DateTime(timezone=True)`` — the DB column is still TIMESTAMPTZ;
            # the decorator only strips tz on read in Python. The migration
            # files emit ``sa.DateTime(timezone=True)`` directly, so the
            # families must alias.
            if n in {"DATETIME", "TIMESTAMP", "NAIVEUTCDATETIME"}:
                return "DATETIME"
            if n in {"NUMERIC", "DECIMAL"}:
                return "NUMERIC"
            return n  # JSONB, JSON, BOOLEAN, FLOAT, DATE, ARRAY, ENUM, etc.

        migrations_dir = os.path.join(
            os.path.dirname(__file__), "..", "alembic", "versions"
        )
        files = sorted(globmod.glob(os.path.join(migrations_dir, "[0-9]*.py")))

        # Extract (table, col) -> type class name from migration files. Later files win.
        add_col_re = re.compile(
            r"add_column\(\s*['\"](\w+)['\"]\s*,\s*sa\.Column\(\s*['\"](\w+)['\"]\s*,"
            r"\s*(?:sa|postgresql|sqlmodel\.sql\.sqltypes)\.(\w+)"
        )
        create_table_re = re.compile(r"create_table\(\s*['\"](\w+)['\"]", re.MULTILINE)
        col_in_create_re = re.compile(
            r"sa\.Column\(\s*['\"](\w+)['\"]\s*,"
            r"\s*(?:sa|postgresql|sqlmodel\.sql\.sqltypes)\.(\w+)"
        )

        migration_types: dict[tuple[str, str], str] = {}
        for path in files:
            with open(path) as f:
                content = f.read()
            for m in add_col_re.finditer(content):
                migration_types[(m.group(1), m.group(2))] = m.group(3)
            for ct in create_table_re.finditer(content):
                table = ct.group(1)
                start, depth, end = ct.end(), 1, ct.end()
                while end < len(content) and depth > 0:
                    if content[end] == "(":
                        depth += 1
                    elif content[end] == ")":
                        depth -= 1
                    end += 1
                for col_m in col_in_create_re.finditer(content[start:end]):
                    migration_types[(table, col_m.group(1))] = col_m.group(2)

        mismatches: list[str] = []
        for table_name, table in SQLModel.metadata.tables.items():
            for col in table.columns:
                mig_type = migration_types.get((table_name, col.name))
                if mig_type is None:
                    continue  # covered by test_model_columns_covered_by_migrations
                model_fam = family(type(col.type).__name__)
                mig_fam = family(mig_type)
                if model_fam != mig_fam:
                    mismatches.append(
                        f"  - {table_name}.{col.name}: model={type(col.type).__name__} "
                        f"({model_fam}) vs migration={mig_type} ({mig_fam})"
                    )

        assert not mismatches, (
            "Migration column types incompatible with models.py:\n"
            + "\n".join(mismatches)
            + "\n\nRegenerate the migration with the correct type to avoid runtime errors."
        )

    def test_model_indexes_covered_by_migrations(self):
        """Every index defined on SQLModel metadata (both column-level
        `index=True` and table-level `Index(...)` in `__table_args__`) must be
        created by a migration. If an agent adds an index to models.py but
        forgets the migration, queries silently fall back to sequential scans.

        Scans migrations for `op.create_index(...)`, column-level `index=True`
        inside `create_table`, and `op.drop_index(...)` to compute the effective
        index set, then checks each metadata index against it.
        """
        import glob as globmod
        import re
        from sqlmodel import SQLModel
        from app import models  # noqa: F401

        migrations_dir = os.path.join(
            os.path.dirname(__file__), "..", "alembic", "versions"
        )
        files = sorted(globmod.glob(os.path.join(migrations_dir, "[0-9]*.py")))

        # op.create_index('name', 'table', ['col', ...], ...) — handle optional op.f(...) wrapper
        create_idx_re = re.compile(
            r"create_index\(\s*(?:op\.f\(\s*)?['\"](\w+)['\"]\s*\)?\s*,"
            r"\s*['\"](\w+)['\"]\s*,\s*\[([^\]]+)\]",
            re.DOTALL,
        )
        drop_idx_re = re.compile(r"drop_index\(\s*(?:op\.f\(\s*)?['\"](\w+)['\"]")
        create_table_re = re.compile(r"create_table\(\s*['\"](\w+)['\"]", re.MULTILINE)
        col_start_re = re.compile(r"sa\.Column\(\s*['\"](\w+)['\"]")
        str_re = re.compile(r"['\"](\w+)['\"]")

        def _matched_block(text: str, start: int) -> str:
            depth, end = 1, start
            while end < len(text) and depth > 0:
                if text[end] == "(":
                    depth += 1
                elif text[end] == ")":
                    depth -= 1
                end += 1
            return text[start : end - 1]

        idx_by_name: dict[str, tuple[str, frozenset[str]]] = {}
        effective: set[tuple[str, frozenset[str]]] = set()

        for path in files:
            with open(path) as f:
                content = f.read()
            # Only consider upgrade() — drops in downgrade() must not cancel creates
            content = content.split("def downgrade", 1)[0]

            for m in create_idx_re.finditer(content):
                name, table, cols_raw = m.group(1), m.group(2), m.group(3)
                cols = frozenset(str_re.findall(cols_raw))
                if cols:
                    entry = (table, cols)
                    idx_by_name[name] = entry
                    effective.add(entry)

            for ct in create_table_re.finditer(content):
                table = ct.group(1)
                block = _matched_block(content, ct.end())
                for col_m in col_start_re.finditer(block):
                    col_block = _matched_block(block, col_m.end())
                    if re.search(r"\bindex\s*=\s*True\b", col_block):
                        effective.add((table, frozenset([col_m.group(1)])))

            for m in drop_idx_re.finditer(content):
                target = idx_by_name.get(m.group(1))
                if target:
                    effective.discard(target)

        # Tables whose migrations are pending (added by a sibling task in the same PR batch).
        # Remove each entry once its migration file lands.
        PENDING_MIGRATION_TABLES: set[str] = set()

        missing: list[str] = []
        for table_name, table in SQLModel.metadata.tables.items():
            if table_name in PENDING_MIGRATION_TABLES:
                continue
            for idx in table.indexes:
                cols = frozenset(c.name for c in idx.columns)
                if (table_name, cols) not in effective:
                    missing.append(
                        f"  - {table_name}({', '.join(sorted(cols))}) [name={idx.name}]"
                    )

        assert not missing, (
            "Indexes defined on models but missing from alembic migrations:\n"
            + "\n".join(missing)
            + "\n\nAdd an op.create_index call to a migration for each missing "
            "index, or queries will silently fall back to sequential scans."
        )

    def test_migration_downgrade_bodies_are_substantive(self):
        """Each migration's downgrade() must perform real rollback work or
        explicitly raise NotImplementedError. An empty/`pass`-only body silently
        ships a migration with no rollback path.

        Substantive = at least one of: op.drop_column, op.drop_table,
        op.drop_index, op.alter_column, op.execute (or any op.* call paired
        with a deliberate rollback). Intentionally irreversible migrations
        must `raise NotImplementedError(...)`.
        """
        import ast
        import glob as globmod

        SUBSTANTIVE_OPS = {
            "drop_column",
            "drop_table",
            "drop_index",
            "alter_column",
            "execute",
            "drop_constraint",
            "rename_table",
            "create_table",
            "create_index",
            "add_column",
            "create_foreign_key",
            "drop_foreign_key",
            "create_unique_constraint",
            "create_check_constraint",
            "batch_alter_table",
        }

        migrations_dir = os.path.join(
            os.path.dirname(__file__), "..", "alembic", "versions"
        )
        files = sorted(globmod.glob(os.path.join(migrations_dir, "[0-9]*.py")))
        assert files, "No migration files found"

        offenders: list[str] = []
        for path in files:
            with open(path) as f:
                source = f.read()
            tree = ast.parse(source)
            downgrade = next(
                (
                    n
                    for n in ast.walk(tree)
                    if isinstance(n, ast.FunctionDef) and n.name == "downgrade"
                ),
                None,
            )
            if downgrade is None:
                offenders.append(f"{os.path.basename(path)}: no downgrade() function")
                continue

            # Strip leading docstring/expression statements; what remains must be substantive.
            body = downgrade.body
            if (
                body
                and isinstance(body[0], ast.Expr)
                and isinstance(body[0].value, ast.Constant)
            ):
                body = body[1:]

            # Pure data-only migrations (upgrade body is only op.execute calls)
            # are inherently one-way — accept any downgrade body for them.
            upgrade = next(
                (
                    n
                    for n in ast.walk(tree)
                    if isinstance(n, ast.FunctionDef) and n.name == "upgrade"
                ),
                None,
            )
            if upgrade is not None:
                up_calls = [
                    n
                    for n in ast.walk(upgrade)
                    if isinstance(n, ast.Call)
                    and isinstance(n.func, ast.Attribute)
                    and isinstance(n.func.value, ast.Name)
                    and n.func.value.id == "op"
                ]
                if up_calls and all(c.func.attr == "execute" for c in up_calls):
                    continue

            # Empty or only `pass`
            if not body or all(isinstance(n, ast.Pass) for n in body):
                offenders.append(
                    f"{os.path.basename(path)}: downgrade() is empty or only `pass` — "
                    "add real rollback ops or `raise NotImplementedError(...)`"
                )
                continue

            # Accept explicit NotImplementedError raise
            raises_nie = any(
                isinstance(n, ast.Raise)
                and (
                    (
                        isinstance(n.exc, ast.Call)
                        and isinstance(n.exc.func, ast.Name)
                        and n.exc.func.id == "NotImplementedError"
                    )
                    or (
                        isinstance(n.exc, ast.Name)
                        and n.exc.id == "NotImplementedError"
                    )
                )
                for n in ast.walk(downgrade)
            )
            if raises_nie:
                continue

            # Look for a substantive op.* call anywhere in the body
            has_substantive = False
            for node in ast.walk(downgrade):
                if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                    if node.func.attr in SUBSTANTIVE_OPS:
                        has_substantive = True
                        break
            if not has_substantive:
                offenders.append(
                    f"{os.path.basename(path)}: downgrade() has no substantive op.* call "
                    "and does not raise NotImplementedError"
                )

        assert not offenders, (
            "Migrations with non-substantive downgrade() bodies:\n  "
            + "\n  ".join(offenders)
            + "\n\nEither implement a real rollback or `raise NotImplementedError(...)` "
            "to mark the migration as intentionally irreversible."
        )


# ── Security Scanner Integrity ──────────────────────────────────────────────


class TestSecurityIntegrity:
    def test_injection_pattern_count(self):
        """Security scanner must have at least 20 injection patterns.
        If this count drops, an agent may have silently removed patterns."""
        from app.security import INJECTION_PATTERNS

        assert len(INJECTION_PATTERNS) >= 20, (
            f"Only {len(INJECTION_PATTERNS)} injection patterns compiled — "
            "expected at least 20. Patterns may have been removed."
        )

    def test_raw_pattern_categories(self):
        """All critical categories must be present."""
        from app.security import INJECTION_PATTERNS

        categories = {p["category"] for p in INJECTION_PATTERNS}
        required = {
            "instruction_override",
            "role_injection",
            "boundary_marker",
            "prompt_leak",
        }
        missing = required - categories
        assert not missing, f"Missing security pattern categories: {missing}"

    def test_high_severity_patterns_exist(self):
        """Must have high-severity patterns for critical injection types."""
        from app.security import INJECTION_PATTERNS

        high_patterns = [p for p in INJECTION_PATTERNS if p["severity"] == "high"]
        assert len(high_patterns) >= 10, (
            f"Only {len(high_patterns)} high-severity patterns — expected at least 10"
        )


# ── Base-branch fallback hardening ─────────────────────────────────────────


class TestBaseBranchFallback:
    """Guard against re-introduction of hardcoded 'main' base_branch fallbacks.

    Every CI-fail → fix-spawn path must resolve base_branch through
    get_project_integration_branch() so projects whose LTS branch is not
    literally 'main' get the correct target.
    """

    def test_no_hardcoded_main_base_branch_fallback(self):
        """No production module under backend/app/ may use .get('base_branch', 'main').

        The only correct pattern is the three-chain:
          ci_loop.get('base_branch', '') or payload.get('base_branch', '') or
          get_project_integration_branch(project)

        payload_schemas.py line 90 uses Field(default='main') for the schema
        default — every spawn site overrides this, so it's explicitly allowed.
        """
        import os

        app_dir = os.path.normpath(os.path.join(os.path.dirname(__file__), "..", "app"))
        # Files where the pattern is legitimately present (schema defaults, not
        # defensive fallbacks in CI-completion paths).
        # admin.py: sibling task (feat: per-project PR title prefix) is
        # migrating those sites to get_project_integration_branch — exempt
        # until that PR merges.
        allow_list = {"payload_schemas.py", "admin.py"}

        pattern = re.compile(r'\.get\(["\']base_branch["\'],\s*["\']main["\']')
        offenders: list[str] = []
        for root, _dirs, files in os.walk(app_dir):
            for fname in files:
                if not fname.endswith(".py"):
                    continue
                if fname in allow_list:
                    continue
                fpath = os.path.join(root, fname)
                with open(fpath, encoding="utf-8") as f:
                    for lineno, line in enumerate(f, start=1):
                        if pattern.search(line):
                            rel = os.path.relpath(fpath, app_dir)
                            offenders.append(f"  {rel}:{lineno}: {line.rstrip()}")
        assert not offenders, (
            "Found hardcoded .get('base_branch', 'main') fallbacks in backend/app/.\n"
            "Replace with the three-chain pattern:\n"
            "  ci_loop.get('base_branch', '') or payload.get('base_branch', '') or\n"
            "  get_project_integration_branch(project)\n"
            "Offenders:\n" + "\n".join(offenders)
        )


# ── Deployment State Model ──────────────────────────────────────────────────


class TestDeploymentStateModel:
    def test_deployment_status_values(self):
        """DeploymentStatus enum must have all expected states."""
        from app.models import DeploymentStatus

        expected = {"MONITORING", "STABLE", "ROLLED_BACK", "PAUSED", "HALTED"}
        actual = {s.value for s in DeploymentStatus}
        assert expected == actual, (
            f"DeploymentStatus values changed: expected {expected}, got {actual}"
        )


# ── Agent Model Forward Compatibility ───────────────────────────────────────


class TestAgentModels:
    def test_agent_models_ignore_extra(self):
        """Agent-facing Pydantic models must accept unknown fields (forward compat)."""
        from app.schemas import AgentStatusPayload, HeartbeatRequest, SpawnTaskRequest

        for model_cls in [AgentStatusPayload, HeartbeatRequest, SpawnTaskRequest]:
            config = model_cls.model_config
            assert config.get("extra") == "ignore", (
                f"{model_cls.__name__} must have extra='ignore' for forward compatibility"
            )


# ── CLAUDE.md Agent Restrictions ────────────────────────────────────────────


def _find_claude_md() -> str:
    """Find CLAUDE.md — either copied into Docker image root or at repo root."""
    candidates = [
        os.path.join(
            os.path.dirname(__file__), "..", "CLAUDE.md"
        ),  # /app/CLAUDE.md (Docker, copied by CI)
        os.path.join(
            os.path.dirname(__file__), "..", "..", "CLAUDE.md"
        ),  # repo root (local dev)
    ]
    for path in candidates:
        if os.path.isfile(path):
            return path
    return ""


_CLAUDE_MD = _find_claude_md()


class TestAgentRestrictions:
    def test_claude_md_exists(self):
        """CLAUDE.md must exist in the repo root."""
        assert _CLAUDE_MD, (
            "CLAUDE.md not found — expected at repo root or copied into Docker image by CI"
        )

    def test_claude_md_restricts_workflows(self):
        """CLAUDE.md must restrict agents from modifying CI workflows."""
        assert _CLAUDE_MD, "CLAUDE.md not found"
        with open(_CLAUDE_MD) as f:
            content = f.read()
        assert ".github/workflows" in content, (
            "CLAUDE.md must mention .github/workflows/ as a restricted path"
        )

    def test_claude_md_restricts_alembic(self):
        """CLAUDE.md must restrict agents from modifying migrations."""
        assert _CLAUDE_MD, "CLAUDE.md not found"
        with open(_CLAUDE_MD) as f:
            content = f.read()
        assert "alembic" in content.lower(), (
            "CLAUDE.md must mention alembic as a restricted path"
        )


# -- Direction Lifecycle Regression Guards --


class TestDirectionLifecycleInvariants:
    def test_max_active_directions_default(self):
        """max_active_directions must remain at 10 — controls direction sprawl."""
        from app.shared_state import runtime_config

        assert runtime_config["max_active_directions"] == 10, (
            f"max_active_directions changed to {runtime_config['max_active_directions']} — "
            "this controls direction sprawl and must not change without approval."
        )

    def test_direction_text_limits(self):
        """DIRECTION_TEXT_MAX and COMPLETION_CRITERIA_MAX must match spec."""
        from app.routers.directions import DIRECTION_TEXT_MAX, COMPLETION_CRITERIA_MAX

        assert DIRECTION_TEXT_MAX == 2000, (
            f"DIRECTION_TEXT_MAX changed to {DIRECTION_TEXT_MAX} — expected 2000."
        )
        assert COMPLETION_CRITERIA_MAX == 500, (
            f"COMPLETION_CRITERIA_MAX changed to {COMPLETION_CRITERIA_MAX} — expected 500."
        )

    def test_momentum_factor_contract(self):
        """_compute_momentum_factor must return canonical values for known inputs."""
        from app.direction_metrics import _compute_momentum_factor

        # Hot streak: last 2 cycles productive → 2.0
        hot = {"research_history": [{"tasks_created": 3}, {"tasks_created": 2}]}
        assert _compute_momentum_factor(hot) == 2.0, (
            f"Hot streak returned {_compute_momentum_factor(hot)}, expected 2.0"
        )
        # Stalled: last 3 cycles empty → 0.5
        stalled = {
            "research_history": [
                {"tasks_created": 0},
                {"tasks_created": 0},
                {"tasks_created": 0},
            ]
        }
        assert _compute_momentum_factor(stalled) == 0.5, (
            f"Stalled returned {_compute_momentum_factor(stalled)}, expected 0.5"
        )
        # Neutral: no history → 1.0
        assert _compute_momentum_factor({}) == 1.0, (
            f"Neutral returned {_compute_momentum_factor({})}, expected 1.0"
        )

    def test_auto_completion_pattern_exists(self):
        """auto_completed flag must exist in the research pipeline — lost in PR #832.

        The logic lives in research_direction_state.apply_post_spawn_state (extracted from
        research_completion._handle_research_completion); check both paths so the invariant
        survives future re-organizations.
        """
        app_dir = os.path.join(os.path.dirname(__file__), "..", "app")
        candidate_paths = [
            os.path.join(app_dir, "research_direction_state.py"),
            os.path.join(app_dir, "research_completion.py"),
        ]
        found = False
        for path in candidate_paths:
            if not os.path.exists(path):
                continue
            with open(path) as f:
                if "auto_completed" in f.read():
                    found = True
                    break
        assert found, (
            "The 'auto_completed' string literal is missing from the research pipeline modules "
            f"({', '.join(os.path.basename(p) for p in candidate_paths)}). "
            "Auto-completion logic was silently removed in PR #832 and must not be removed again."
        )

    def test_direction_cap_enforced_in_post(self):
        """directions.py POST endpoint must check max_active_directions cap — lost in PR #832."""
        directions_path = os.path.join(
            os.path.dirname(__file__), "..", "app", "routers", "directions.py"
        )
        with open(directions_path) as f:
            content = f.read()
        assert "max_active_directions" in content, (
            "'max_active_directions' cap check is missing from directions.py. "
            "Direction cap enforcement was lost in PR #832 and must not be removed again."
        )


# ── Module Size Guardrails ─────────────────────────────────────────────────
# These are loose upper bounds to catch runaway growth, not tight ceilings.
# If a file hits the limit, consider a refactor — but do NOT "ratchet down"
# the limit after every cleanup. These limits should stay stable.

_SIZE_LIMITS_JSON = os.path.join(os.path.dirname(__file__), "module_size_limits.json")


def _load_size_limits() -> dict:
    """Load the size-limits JSON.  Single source of truth for both backend
    and frontend ceilings; updated by agents via the size_breach annotation
    flow when a justified change pushes a file over its limit."""
    import json

    with open(_SIZE_LIMITS_JSON) as f:
        return json.load(f)


_SIZE_LIMITS = _load_size_limits()
MODULE_SIZE_LIMITS = _SIZE_LIMITS["backend"]


def _size_breach_message(
    rel_path: str, count: int, limit: int, module_kind: str
) -> str:
    """Structured assertion error directing the failing agent to the
    size_breach annotation flow.  The agent should NOT refactor in this PR
    (that's a strategic decision); they may bump the limit + emit the
    annotation if their growth is justified, or shrink their diff if not."""
    return (
        f"{rel_path} has {count} lines (limit: {limit}).\n\n"
        f"If your change legitimately needed to grow this file:\n"
        f"  1. POST /api/agent/annotate with annotation_type='size_breach',\n"
        f"     file_path='{rel_path}', current_size={count}, and a one-sentence\n"
        f"     change_summary describing what your diff added to this file.\n"
        f"  2. Apply the new_limit returned in the response to\n"
        f"     backend/testing/module_size_limits.json under '{module_kind}'.\n"
        f"  3. Commit both the source change and the JSON edit.\n\n"
        f"If your diff is bundling unrelated code, shrink it to only what the\n"
        f"task asked for. Do NOT refactor or extract code in this PR — that's\n"
        f"a strategic decision, not a create/fix one."
    )


class TestBackendModuleSizeGuardrails:
    """Loose upper bounds to catch runaway file growth.

    These are NOT tight ceilings to ratchet down. They are generous limits
    that should only fire if a file has genuinely gotten out of hand.
    If a test fails, consider a refactor — but do NOT lower the limit afterward.
    """

    def _check_module(self, relative_path: str) -> None:
        max_lines = MODULE_SIZE_LIMITS[relative_path]
        path = os.path.join(os.path.dirname(__file__), "..", "app", relative_path)
        with open(path) as f:
            count = len(f.readlines())
        assert count <= max_lines, _size_breach_message(
            relative_path, count, max_lines, "backend"
        )

    def test_main_py(self):
        self._check_module("main.py")

    def test_task_completion(self):
        self._check_module("task_completion.py")

    def test_task_lifecycle(self):
        self._check_module("task_lifecycle.py")

    def test_orchestrator(self):
        self._check_module("orchestrator.py")

    def test_admin(self):
        self._check_module("admin.py")

    def test_compute(self):
        self._check_module("compute.py")

    def test_github(self):
        self._check_module("github.py")

    def test_admin_settings(self):
        self._check_module("routers/admin_settings.py")

    def test_admin_analytics(self):
        self._check_module("routers/admin_analytics.py")

    def test_admin_digest(self):
        self._check_module("routers/admin_digest.py")

    def test_admin_pool_accounts(self):
        self._check_module("routers/admin_pool_accounts.py")

    def test_max_plan_pool(self):
        self._check_module("max_plan_pool.py")

    def test_anomaly_detection(self):
        self._check_module("anomaly_detection.py")

    def test_agent_router(self):
        self._check_module("routers/agent.py")

    def test_services(self):
        self._check_module("services.py")

    def test_agent_status(self):
        self._check_module("routers/agent_status.py")

    def test_autonomous_research(self):
        self._check_module("autonomous_research.py")

    def test_research_completion(self):
        self._check_module("research_completion.py")

    def test_strategic_completion(self):
        self._check_module("strategic_completion.py")

    def test_rollup_management(self):
        self._check_module("rollup_management.py")

    def test_reaper(self):
        self._check_module("reaper.py")

    def test_review_pipeline(self):
        self._check_module("review_pipeline.py")

    def test_deploy_health(self):
        self._check_module("deploy_health.py")

    def test_pipeline_regression_dispatcher(self):
        self._check_module("pipeline_regression_dispatcher.py")

    def test_review_learning(self):
        self._check_module("review_learning.py")

    def test_playbook_lifecycle(self):
        self._check_module("playbook_lifecycle.py")

    def test_playbook_storage(self):
        self._check_module("playbook_storage.py")

    def test_playbook_auto(self):
        self._check_module("playbook_auto.py")


# ── Frontend File Size Guardrails ─────────────────────────────────────────────

FRONTEND_SIZE_LIMITS = _SIZE_LIMITS["frontend"]


def _resolve_admin_ui_src_root() -> str | None:
    """Locate ``admin-ui/src`` across local + Docker test environments.

    Search order:
      1. ``ADMIN_UI_SRC`` env override (caller-supplied path).
      2. ``/admin-ui/src`` (Docker volume-mount target used by
         ``scripts/test-backend.sh``).
      3. Host filesystem at ``../../admin-ui/src`` from this test file
         (``backend/testing/`` → repo root).

    Returns the first existing path or ``None`` if none resolve. The
    caller decides whether ``None`` should skip or fail — see
    ``TestFrontendSizeGuardrails`` (we fail loudly in CI to surface the
    silent-skip regression that R6 caught).
    """
    env_override = os.getenv("ADMIN_UI_SRC")
    candidates = []
    if env_override:
        candidates.append(env_override)
    candidates.append("/admin-ui/src")
    candidates.append(
        os.path.normpath(
            os.path.join(os.path.dirname(__file__), "..", "..", "admin-ui", "src")
        )
    )
    for candidate in candidates:
        if os.path.isdir(candidate):
            return candidate
    return None


_ADMIN_UI_SRC_ROOT = _resolve_admin_ui_src_root()


def _get_frontend_path(relative: str) -> str:
    if _ADMIN_UI_SRC_ROOT is None:
        # Caller must have already failed via _require_admin_ui_src(); this
        # is the defensive fallback that returns a path the assertion will
        # surface as a clear file-not-found error.
        return os.path.join("admin-ui-src-not-found", relative)
    return os.path.join(_ADMIN_UI_SRC_ROOT, relative)


class TestFrontendSizeGuardrails:
    """Loose upper bounds to catch runaway frontend file growth.

    R6 regression: previously this class skipped silently when
    ``admin-ui`` wasn't on disk — the Docker test image (``./backend``
    build context) excludes it, so CI never enforced these limits and
    breaches accumulated unnoticed. Failing loud forces the operator to
    either mount/copy ``admin-ui/src`` into the test environment, set
    ``ADMIN_UI_SRC=/path/to/admin-ui/src``, or explicitly opt out via
    ``SKIP_FRONTEND_SIZE_GUARDRAILS=1``.
    """

    @staticmethod
    def _require_admin_ui_src() -> None:
        import pytest

        if os.getenv("SKIP_FRONTEND_SIZE_GUARDRAILS") == "1":
            pytest.skip("SKIP_FRONTEND_SIZE_GUARDRAILS=1 set")
        if _ADMIN_UI_SRC_ROOT is None:
            # CI uses ./backend as docker build context (no admin-ui in image)
            # and the workflow can't be modified to mount it (GitHub App token
            # cannot push .github/workflows/ changes). Locally,
            # scripts/test-backend.sh mounts admin-ui/src so the guardrails
            # fire there. In CI we skip with a clear marker instead of failing
            # — the frontend size limits are still enforced by the local
            # pre-commit / pre-push pathway, just not the CI cycle.
            pytest.skip(
                "admin-ui/src not available in this test environment "
                "(CI docker context is ./backend; mount via test-backend.sh "
                "for local enforcement)"
            )

    def _check_frontend(self, relative_path: str) -> None:
        self._require_admin_ui_src()
        max_lines = FRONTEND_SIZE_LIMITS[relative_path]
        path = _get_frontend_path(relative_path)
        with open(path) as f:
            count = len(f.readlines())
        assert count <= max_lines, _size_breach_message(
            relative_path, count, max_lines, "frontend"
        )

    def test_useappstate(self):
        self._check_frontend("useAppState.ts")

    def test_directions_panel(self):
        self._check_frontend("components/DirectionsPanel.tsx")

    def test_catchup_panel(self):
        self._check_frontend("components/CatchUpPanel.tsx")

    def test_task_detail_drawer(self):
        self._check_frontend("components/TaskDetailDrawer.tsx")

    def test_settings_dropdown(self):
        self._check_frontend("components/SettingsDropdown.tsx")

    def test_kanban_board(self):
        self._check_frontend("components/KanbanBoard.tsx")

    def test_kanban_column(self):
        self._check_frontend("components/KanbanColumn.tsx")

    def test_app_tsx(self):
        self._check_frontend("App.tsx")

    def test_task_card(self):
        self._check_frontend("components/TaskCard.tsx")

    def test_direction_card(self):
        self._check_frontend("components/DirectionCard.tsx")

    def test_pipeline_view(self):
        self._check_frontend("components/PipelineView.tsx")

    def test_dashboard_home(self):
        self._check_frontend("components/DashboardHome.tsx")

    def test_messages_panel(self):
        self._check_frontend("components/MessagesPanel.tsx")

    def test_metrics_dashboard(self):
        self._check_frontend("components/MetricsDashboard.tsx")

    def test_project_dashboard(self):
        self._check_frontend("components/ProjectDashboard.tsx")

    def test_autonomous_status(self):
        self._check_frontend("components/AutonomousStatus.tsx")

    def test_direction_progress_section(self):
        self._check_frontend("components/DirectionProgressSection.tsx")

    def test_remediation_funnel_section(self):
        self._check_frontend("components/RemediationFunnelSection.tsx")

    def test_orchestrator_health_bar(self):
        self._check_frontend("components/OrchestratorHealthBar.tsx")

    def test_recent_errors_widget(self):
        self._check_frontend("components/RecentErrorsWidget.tsx")

    def test_medium_scope_widget(self):
        self._check_frontend("components/MediumScopeWidget.tsx")

    def test_settings_notifications_section(self):
        self._check_frontend("components/SettingsNotificationsSection.tsx")

    def test_failure_stage_widget(self):
        self._check_frontend("components/FailureStageWidget.tsx")

    def test_pr_monitor_section(self):
        self._check_frontend("components/PRMonitorSection.tsx")

    def test_direction_auto_pause_widget(self):
        self._check_frontend("components/DirectionAutoPauseWidget.tsx")

    def test_scope_conversion_rates_section(self):
        self._check_frontend("components/ScopeConversionRatesSection.tsx")

    def test_scope_trends_section(self):
        self._check_frontend("components/ScopeTrendsSection.tsx")

    def test_provider_cost_panel(self):
        self._check_frontend("components/ProviderCostPanel.tsx")

    def test_drift_signals_card(self):
        self._check_frontend("components/DriftSignalsCard.tsx")

    def test_pool_account_health_panel(self):
        self._check_frontend("components/PoolAccountHealthPanel.tsx")

    def test_system_maturity_card(self):
        self._check_frontend("components/SystemMaturityCard.tsx")

    def test_provider_model_breakdown_panel(self):
        self._check_frontend("components/ProviderModelBreakdownPanel.tsx")

    def test_task_duration_widget(self):
        self._check_frontend("components/TaskDurationWidget.tsx")


# ── Failure Classification Taxonomy ────────────────────────────────────────


class TestFailureClassificationInvariants:
    """Structural invariants over failure_classification canonical classes.

    INFRA_CLASSES governs which failures are treated as infrastructure (not
    penalizing direction health). direction_outcomes and research_completion
    both import it, so accidental additions/removals silently change rollup
    and dedup behavior across the pipeline.
    """

    def test_infra_classes_membership_exact(self):
        from app.failure_classification import (
            AGENT_STALLED,
            AGENT_TIMEOUT,
            BLOCKED_PRECONDITION,
            EXTERNAL_API,
            INFRA_CLASSES,
            PROVISION_FAILURE,
            RATE_LIMIT,
            SCOPE_EMPTY,
            SCOPE_OVERFLOW,
        )

        assert INFRA_CLASSES == frozenset(
            {
                PROVISION_FAILURE,
                RATE_LIMIT,
                AGENT_TIMEOUT,
                AGENT_STALLED,
                EXTERNAL_API,
                SCOPE_OVERFLOW,
                BLOCKED_PRECONDITION,
                SCOPE_EMPTY,
            }
        ), (
            "INFRA_CLASSES membership changed — direction_outcomes and "
            "research_completion rely on this exact set to distinguish infra "
            "failures from code failures."
        )

    def test_canonical_classes_partitioned_by_infra(self):
        from app.failure_classification import (
            AGENT_TIMEOUT,
            BLOCKED_PRECONDITION,
            CODE_ERROR,
            EXTERNAL_API,
            INFRA_CLASSES,
            PROVISION_FAILURE,
            RATE_LIMIT,
            SCOPE_OVERFLOW,
        )

        canonical = [
            CODE_ERROR,
            PROVISION_FAILURE,
            AGENT_TIMEOUT,
            EXTERNAL_API,
            RATE_LIMIT,
            SCOPE_OVERFLOW,
            BLOCKED_PRECONDITION,
        ]
        for cls in canonical:
            assert isinstance(cls, str) and cls, f"{cls!r} must be a non-empty string"
            assert cls in INFRA_CLASSES or cls == CODE_ERROR, (
                f"{cls!r} must be either CODE_ERROR or a member of INFRA_CLASSES"
            )


# ── Prompt Vocabulary Parity ───────────────────────────────────────────────

# Always-on CI mirror of the SR category bullet list in agent/src/prompt.ts
# (between SR_CATEGORIES_START / SR_CATEGORIES_END sentinels). This frozenset
# is the source of truth used by CI (where prompt.ts is not in the Docker build
# context). Keep it in sync with both:
#   - the bullet list in agent/src/prompt.ts (SR_CATEGORIES_START/END region)
#   - _REVIEW_CATEGORIES in backend/app/playbook_learning.py (excluding "other")
AGENT_SELF_REVIEW_CATEGORIES = frozenset(
    {
        "correctness",
        "error-handling",
        "security",
        "style-conventions",
        "type-safety",
        "documentation",
        "performance",
        "architecture",
        "test-coverage",
        "scope-drift",
        "regression_risk",
        "cross-system-contract",
    }
)


def _resolve_prompt_ts_path() -> "str | None":
    """Locate ``agent/src/prompt.ts`` across local + Docker test environments.

    Search order:
      1. ``AGENT_PROMPT_TS`` env override (caller-supplied path).
      2. ``/agent/src/prompt.ts`` (Docker volume-mount target used by
         ``scripts/test-backend.sh``).
      3. Host filesystem at ``../../agent/src/prompt.ts`` from this test file
         (``backend/testing/`` → repo root).

    Returns the first existing path or ``None`` if none resolve.
    """
    env_override = os.getenv("AGENT_PROMPT_TS")
    candidates = []
    if env_override:
        candidates.append(env_override)
    candidates.append("/agent/src/prompt.ts")
    candidates.append(
        os.path.normpath(
            os.path.join(
                os.path.dirname(__file__), "..", "..", "agent", "src", "prompt.ts"
            )
        )
    )
    for candidate in candidates:
        if os.path.isfile(candidate):
            return candidate
    return None


def _parse_sr_categories_from_prompt(prompt_text: str) -> "frozenset[str]":
    """Extract SR category IDs from the SR_CATEGORIES_START/END sentinel region.

    The category bullet list in prompt.ts lives inside a TypeScript template
    literal so backticks are escaped as \\` in the raw file. The regex matches
    ``- \\`<id>\\``` to extract each ID.
    """
    start = prompt_text.find("SR_CATEGORIES_START")
    end = prompt_text.find("SR_CATEGORIES_END", start + 1 if start != -1 else 0)
    if start == -1 or end == -1:
        raise ValueError(
            "SR_CATEGORIES_START/END sentinels not found in prompt.ts. "
            "Ensure the sentinel comments wrap the SR category bullet list."
        )
    region = prompt_text[start:end]
    # Matches both escaped-backtick form (\`id\`, inside TS template literals)
    # and plain-backtick form (`id`, for fixture strings in tests).
    return frozenset(re.findall(r"- \\?`([^\\`]+)\\?`", region))


def _check_sr_category_parity(
    agent_cats: "frozenset[str]", backend_cats: "frozenset[str]"
) -> "list[str]":
    """Return directional mismatch messages; empty list means exact equality."""
    messages = []
    missing_from_backend = agent_cats - backend_cats
    if missing_from_backend:
        messages.append(
            f"Categories in agent prompt but missing from backend _REVIEW_CATEGORIES: "
            f"{sorted(missing_from_backend)}. "
            f"Add them to _REVIEW_CATEGORIES in backend/app/playbook_learning.py."
        )
    missing_from_agent = backend_cats - agent_cats
    if missing_from_agent:
        messages.append(
            f"Categories in backend _REVIEW_CATEGORIES but missing from agent prompt: "
            f"{sorted(missing_from_agent)}. "
            f"Add them to the SR category bullet list in agent/src/prompt.ts "
            f"(between SR_CATEGORIES_START / SR_CATEGORIES_END sentinels) "
            f"and to AGENT_SELF_REVIEW_CATEGORIES in backend/testing/test_invariants.py."
        )
    return messages


class TestPromptVocabularyParity:
    """Cross-check that agent self-review categories and backend categorizer
    stay in sync. A PR that adds or removes a category on one side without
    updating the other will fail these tests.
    """

    def test_agent_categories_in_backend_vocabulary(self):
        """Agent self-review categories must exactly equal the backend review vocabulary.

        AGENT_SELF_REVIEW_CATEGORIES (this file) is the always-on CI mirror.
        It must bidirectionally equal set(_REVIEW_CATEGORIES.keys()) — 'other' is
        a backend-only residual catch-all and is exempt from parity.

        If this fails: a category was added or removed on one side without updating
        the other. Edit AGENT_SELF_REVIEW_CATEGORIES here AND either:
          - backend/app/playbook_learning.py (_REVIEW_CATEGORIES) for backend-side changes
          - agent/src/prompt.ts (SR_CATEGORIES_START/END region) for agent-side changes
        """
        from app.playbook_learning import _REVIEW_CATEGORIES

        backend_cats = frozenset(_REVIEW_CATEGORIES.keys()) - {"other"}
        issues = _check_sr_category_parity(AGENT_SELF_REVIEW_CATEGORIES, backend_cats)
        assert not issues, "\n".join(issues)

    def test_agent_categories_match_prompt_source(self):
        """Parsed prompt.ts categories must exactly match AGENT_SELF_REVIEW_CATEGORIES.

        Skipped when agent/src/prompt.ts is not available (e.g. CI Docker context
        which builds from ./backend only). Locally and with the agent/src mount from
        scripts/test-backend.sh this check fires and prevents the mirror from drifting.

        If this fails: the SR category bullet list in agent/src/prompt.ts differs from
        AGENT_SELF_REVIEW_CATEGORIES in this file. Update AGENT_SELF_REVIEW_CATEGORIES
        to match the authoritative prompt source (or vice versa).
        """
        prompt_path = _resolve_prompt_ts_path()
        if prompt_path is None:
            pytest.skip(
                "agent/src/prompt.ts not available in this test environment "
                "(CI docker context is ./backend; run locally or via "
                "scripts/test-backend.sh from a checkout for enforcement)"
            )
        with open(prompt_path) as f:
            prompt_text = f.read()
        parsed_cats = _parse_sr_categories_from_prompt(prompt_text)
        issues = _check_sr_category_parity(AGENT_SELF_REVIEW_CATEGORIES, parsed_cats)
        assert not issues, (
            "AGENT_SELF_REVIEW_CATEGORIES mirror has drifted from agent/src/prompt.ts:\n"
            + "\n".join(issues)
        )


class TestSrCategoryParityHelper:
    """Unit tests demonstrating both failure directions of the parity helper.

    These tests drive synthetic mismatched sets — they do NOT mutate the real
    prompt.ts or backend _REVIEW_CATEGORIES dict.
    """

    def test_catches_category_in_backend_not_in_agent(self):
        """A category present in the backend vocab but absent from the agent set is flagged."""
        agent_cats = frozenset({"correctness", "security"})
        backend_cats = frozenset({"correctness", "security", "extra-backend-only"})
        issues = _check_sr_category_parity(agent_cats, backend_cats)
        assert len(issues) == 1
        assert "extra-backend-only" in issues[0]
        assert "missing from agent prompt" in issues[0]

    def test_catches_category_in_agent_not_in_backend(self):
        """A category present in the agent set but absent from the backend vocab is flagged."""
        agent_cats = frozenset({"correctness", "security", "extra-agent-only"})
        backend_cats = frozenset({"correctness", "security"})
        issues = _check_sr_category_parity(agent_cats, backend_cats)
        assert len(issues) == 1
        assert "extra-agent-only" in issues[0]
        assert "missing from backend" in issues[0]

    def test_passes_on_exact_match(self):
        """Exact match between agent and backend category sets returns no issues."""
        cats = frozenset({"correctness", "security", "error-handling"})
        assert _check_sr_category_parity(cats, cats) == []

    def test_parse_from_prompt_pinning_drift(self):
        """When parsed-from-prompt set differs from mirror, the pinning check fails."""
        fake_prompt = (
            "<!-- SR_CATEGORIES_START -->\n"
            "- `correctness` — logic bugs\n"
            "- `not-in-mirror` — a fabricated category\n"
            "<!-- SR_CATEGORIES_END -->\n"
        )
        parsed = _parse_sr_categories_from_prompt(fake_prompt)
        assert parsed == frozenset({"correctness", "not-in-mirror"})
        real_mirror = frozenset({"correctness", "security"})
        issues = _check_sr_category_parity(real_mirror, parsed)
        assert any("not-in-mirror" in msg for msg in issues)
        assert any("security" in msg for msg in issues)


# ── Reviewer-origin set parity (cross-language) ───────────────────────────────
# The agent (agent/src/handlers/standard.ts) stamps an `origin` on every
# self-review check_results entry. The author's own entries use origin "sr" (and
# edges use "author"); every OTHER origin is a fresh-reviewer/recovery save. The
# backend's canonical set of non-author origins is _REVIEWER_ORIGINS in
# backend/app/sr_blind_spots.py — catch-rate attribution buckets these as
# reviewer saves, never author SR hits. If a future agent-side origin is added
# without updating _REVIEWER_ORIGINS, catches would silently be miscounted as
# author self-review hits; this test fails to prevent that drift.

# Author-side origins emitted by standard.ts that must be EXCLUDED from the
# reviewer-origin set.
AGENT_AUTHOR_ORIGINS = frozenset({"sr", "author"})


def _resolve_standard_ts_path() -> "str | None":
    """Locate ``agent/src/handlers/standard.ts`` across test environments.

    Search order mirrors _resolve_prompt_ts_path: env override, the Docker
    volume-mount target, then the host filesystem relative to this test file.
    Returns the first existing path or ``None`` if none resolve.
    """
    env_override = os.getenv("AGENT_STANDARD_TS")
    candidates = []
    if env_override:
        candidates.append(env_override)
    candidates.append("/agent/src/handlers/standard.ts")
    candidates.append(
        os.path.normpath(
            os.path.join(
                os.path.dirname(__file__),
                "..",
                "..",
                "agent",
                "src",
                "handlers",
                "standard.ts",
            )
        )
    )
    for candidate in candidates:
        if os.path.isfile(candidate):
            return candidate
    return None


def _parse_origin_literals_from_standard(source_text: str) -> "frozenset[str]":
    """Extract every ``origin`` string literal assigned in standard.ts.

    Matches both ``entry.origin = 'x'`` / ``sr.reviewer_recovery_origin = 'x'``
    and object-literal ``origin: 'x'`` forms. Comment lines reuse the same
    literals so including them does not change the resulting set.
    """
    return frozenset(re.findall(r"origin\s*[:=]\s*'([^']+)'", source_text))


class TestReviewerOriginParity:
    """Pin the backend canonical reviewer-origin set to the exact set of
    non-author origins the agent stamps in standard.ts. Guards against silent
    drift back into miscounting recovery catches as author self-review hits.
    """

    def test_reviewer_origins_is_single_canonical_set(self):
        """playbook_learning must reference the same frozenset as sr_blind_spots.

        The two membership sets used to diverge (one omitted lone_checker_recovery,
        the other operator_requested_recovery). They are now one canonical object.
        """
        from app.sr_blind_spots import _REVIEWER_ORIGINS as canonical
        from app.playbook_learning import _REVIEWER_ORIGINS as via_playbook

        assert via_playbook is canonical

    def test_reviewer_origins_matches_expected_five(self):
        """The canonical set is exactly the five non-author origins."""
        from app.sr_blind_spots import _REVIEWER_ORIGINS

        assert _REVIEWER_ORIGINS == frozenset(
            {
                "reviewer",
                "reviewer_disagreement",
                "reviewer_recovery_after_incomplete_sr",
                "lone_checker_recovery",
                "operator_requested_recovery",
            }
        )

    def test_reviewer_origins_match_agent_source(self):
        """Parsed standard.ts origins minus author origins must equal _REVIEWER_ORIGINS.

        Skipped when agent/src/handlers/standard.ts is not available (CI Docker
        context builds from ./backend only). Locally and via scripts/test-backend.sh
        (which mounts agent/src) this fires and prevents drift.

        If this fails: standard.ts emits a non-author origin that is not in
        _REVIEWER_ORIGINS (backend/app/sr_blind_spots.py), or vice versa. Catches
        with the missing origin would be misattributed to the author's self-review.
        """
        standard_path = _resolve_standard_ts_path()
        if standard_path is None:
            pytest.skip(
                "agent/src/handlers/standard.ts not available in this test "
                "environment (CI docker context is ./backend; run locally or via "
                "scripts/test-backend.sh from a checkout for enforcement)"
            )
        from app.sr_blind_spots import _REVIEWER_ORIGINS

        with open(standard_path) as f:
            source_text = f.read()
        all_origins = _parse_origin_literals_from_standard(source_text)
        # Sanity: the author origins we subtract must actually appear in the source.
        assert AGENT_AUTHOR_ORIGINS <= all_origins, (
            "Expected author origins {sr, author} not found in standard.ts — "
            "the origin-tagging block may have moved or been renamed."
        )
        agent_reviewer_origins = all_origins - AGENT_AUTHOR_ORIGINS
        assert agent_reviewer_origins == _REVIEWER_ORIGINS, (
            "Reviewer-origin set drift between agent and backend:\n"
            f"  in standard.ts but not _REVIEWER_ORIGINS: "
            f"{sorted(agent_reviewer_origins - _REVIEWER_ORIGINS)}\n"
            f"  in _REVIEWER_ORIGINS but not standard.ts: "
            f"{sorted(_REVIEWER_ORIGINS - agent_reviewer_origins)}\n"
            "Update _REVIEWER_ORIGINS in backend/app/sr_blind_spots.py to match "
            "the non-author origins emitted by standard.ts."
        )


# ── Playbook-Learning Facade Compatibility ────────────────────────────────────
# After the playbook-learning split, key symbols moved to sub-modules
# (review_learning, playbook_lifecycle, playbook_storage, playbook_auto).
# playbook_learning re-exports them so existing callers keep working.
# These tests lock the re-export surface so future agents cannot drop
# compatibility symbols without a CI failure.


class TestPlaybookLearningFacadeCompat:
    def test_review_categories_is_canonical_object(self):
        """playbook_learning._REVIEW_CATEGORIES must be the same object as
        review_learning._REVIEW_CATEGORIES — not a local copy.

        Guards against the facade shadowing the canonical dict, which would
        cause category-taxonomy drift between the SR categorizer and parity checks.
        """
        from app.playbook_learning import _REVIEW_CATEGORIES as via_facade
        from app.review_learning import _REVIEW_CATEGORIES as canonical

        assert via_facade is canonical, (
            "playbook_learning._REVIEW_CATEGORIES is not the same object as "
            "review_learning._REVIEW_CATEGORIES. The facade must re-export the "
            "canonical dict (not shadow it) to keep taxonomy in sync."
        )

    def test_compat_lifecycle_and_storage_symbols_accessible(self):
        """Key lifecycle and storage symbols extracted from the playbook-learning
        monolith must remain importable through app.playbook_learning.

        If this fails: a backward-compat re-export was dropped. Either restore
        the import in playbook_learning.py or update every caller that used it.
        """
        from app.playbook_learning import (
            _AUTO_PLAYBOOK_RESURRECTION_COOLDOWN_DAYS,
            _IDLE_RETIRE_DAYS,
            _PLAYBOOK_CAPACITY_BYTES,
            _STALE_AGE_DAYS,
            check_and_flag_stale_refs,
            finalize_pending_playbook_entries,
            retire_idle_auto_entries,
            retire_ineffective_patterns,
        )

        assert _AUTO_PLAYBOOK_RESURRECTION_COOLDOWN_DAYS > 0
        assert _IDLE_RETIRE_DAYS > 0
        assert _STALE_AGE_DAYS > 0
        assert _PLAYBOOK_CAPACITY_BYTES > 0
        assert callable(check_and_flag_stale_refs)
        assert callable(finalize_pending_playbook_entries)
        assert callable(retire_idle_auto_entries)
        assert callable(retire_ineffective_patterns)


# ── edges_considered Schema Invariants ────────────────────────────────────────
# MUST stay in sync with EDGES_CONSIDERED_GUIDANCE in agent/src/prompt.ts and
# the entry-reading code in admin_digest._compute_edges_considered_metrics /
# playbook_learning._compute_edges_stats.

# Required keys for each edges_considered entry in the self-review JSON.
EDGES_CONSIDERED_REQUIRED_KEYS = frozenset({"case", "handled"})
# Optional keys that may appear in an edges_considered entry.
EDGES_CONSIDERED_OPTIONAL_KEYS = frozenset({"evidence", "blind_spot"})


class TestEdgesConsideredInvariants:
    """Structural invariants for the edges_considered field in self-review JSON.

    guards against schema drift between the agent prompt guidance
    (EDGES_CONSIDERED_GUIDANCE in agent/src/prompt.ts) and the backend metric
    consumers (_compute_edges_considered_metrics, _compute_edges_stats).
    """

    def test_required_keys_are_subset_of_all_valid_keys(self):
        """Required entry keys must be a strict subset of the allowed key set."""
        all_valid = EDGES_CONSIDERED_REQUIRED_KEYS | EDGES_CONSIDERED_OPTIONAL_KEYS
        assert EDGES_CONSIDERED_REQUIRED_KEYS < all_valid, (
            "EDGES_CONSIDERED_REQUIRED_KEYS must be a strict subset of all valid keys."
        )

    def test_compute_edges_considered_metrics_returns_none_below_threshold(self):
        """_compute_edges_considered_metrics must return None when fewer than
        3 qualifying code-change tasks exist (insufficient sample)."""
        from app.routers.admin_digest import _compute_edges_considered_metrics

        result = _compute_edges_considered_metrics([])
        assert result is None, "_compute_edges_considered_metrics([]) must return None."

    def test_compute_edges_considered_metrics_return_shape(self):
        """_compute_edges_considered_metrics must return a dict with the
        expected keys when given sufficient qualifying tasks.

        If this fails: the return shape changed — update the frontend
        reader (admin-ui) and this test together to avoid contract drift.
        """
        from unittest.mock import MagicMock
        from app.routers.admin_digest import _compute_edges_considered_metrics

        EXPECTED_KEYS = {
            "sample_size",
            "tasks_with_edges_count",
            "tasks_with_edges_rate",
            "avg_edges_per_task",
            "unhandled_edge_flagged_rate",
            "correctness_rejection_rate_with_edges",
            "correctness_rejection_rate_without_edges",
            "correctness_rejection_rate_with_unhandled_flag",
            "correctness_rejection_rate_with_all_handled",
            "author_avg_edges_per_task",
            "reviewer_avg_edges_per_task",
            "tasks_with_reviewer_edges_count",
            "tasks_with_reviewer_edges_rate",
            "reviewer_caught_count",
            "reviewer_caught_rate",
        }

        def _make_task(edges, rejected_cats=None):
            t = MagicMock()
            t.first_payload = {"type": "create"}
            ctx = {"_self_review_outcome": {}}
            if edges is not None:
                ctx["_self_review_outcome"]["edges_considered"] = edges
            if rejected_cats is not None:
                ctx["_first_attempt_rejection_categories"] = rejected_cats
            ctx["_first_attempt_succeeded"] = True
            t.context = ctx
            return t

        tasks = [
            _make_task([{"case": "empty list", "handled": True}]),
            _make_task(
                [{"case": "null payload", "handled": False, "blind_spot": "unchecked"}]
            ),
            _make_task([]),
        ]
        result = _compute_edges_considered_metrics(tasks)
        assert result is not None, (
            "_compute_edges_considered_metrics must return a dict for 3+ qualifying tasks."
        )
        missing_keys = EXPECTED_KEYS - result.keys()
        assert not missing_keys, (
            f"_compute_edges_considered_metrics return dict is missing keys: {sorted(missing_keys)}. "
            "Update this test and the frontend reader together when changing the return shape."
        )

    def test_entry_handled_field_is_read_correctly(self):
        """Backend metric consumers must read the 'handled' bool from entries correctly.
        An entry with handled=False must count toward unhandled_edge_flagged_rate.
        """
        from unittest.mock import MagicMock
        from app.routers.admin_digest import _compute_edges_considered_metrics

        def _make_task(edges):
            t = MagicMock()
            t.first_payload = {"type": "create"}
            t.context = {"_self_review_outcome": {"edges_considered": edges}}
            return t

        tasks = [
            _make_task([{"case": "c1", "handled": False}]),
            _make_task([{"case": "c2", "handled": True}]),
            _make_task([{"case": "c3", "handled": True}]),
        ]
        result = _compute_edges_considered_metrics(tasks)
        assert result is not None
        assert result["tasks_with_edges_count"] == 3
        assert result["unhandled_edge_flagged_rate"] == round(1 / 3, 3), (
            "unhandled_edge_flagged_rate must equal fraction of tasks that have at least "
            "one entry with handled=False."
        )

    def test_reviewer_caught_count_via_unmatched_case(self):
        """reviewer_caught_count must count a task where the reviewer raised
        a case the author did not list (case-string mismatch)."""
        from unittest.mock import MagicMock
        from app.routers.admin_digest import _compute_edges_considered_metrics

        def _make_task(edges):
            t = MagicMock()
            t.first_payload = {"type": "create"}
            t.context = {"_self_review_outcome": {"edges_considered": edges}}
            return t

        tasks = [
            _make_task(
                [
                    {"case": "empty list", "handled": True, "origin": "author"},
                    {"case": "null payload", "handled": False, "origin": "reviewer"},
                ]
            ),
            _make_task([{"case": "c2", "handled": True, "origin": "author"}]),
            _make_task([{"case": "c3", "handled": True, "origin": "author"}]),
        ]
        result = _compute_edges_considered_metrics(tasks)
        assert result is not None
        assert result["reviewer_caught_count"] == 1
        assert result["reviewer_caught_rate"] == round(1 / 3, 3)
        assert result["tasks_with_reviewer_edges_count"] == 1

    def test_reviewer_caught_count_via_handled_downgrade(self):
        """reviewer_caught_count must count a task where the reviewer flagged
        handled=false on a case the author had marked handled=true."""
        from unittest.mock import MagicMock
        from app.routers.admin_digest import _compute_edges_considered_metrics

        def _make_task(edges):
            t = MagicMock()
            t.first_payload = {"type": "create"}
            t.context = {"_self_review_outcome": {"edges_considered": edges}}
            return t

        tasks = [
            _make_task(
                [
                    {"case": "shared", "handled": True, "origin": "author"},
                    {"case": "shared", "handled": False, "origin": "reviewer"},
                ]
            ),
            _make_task([{"case": "c2", "handled": True}]),
            _make_task([{"case": "c3", "handled": True}]),
        ]
        result = _compute_edges_considered_metrics(tasks)
        assert result is not None
        assert result["reviewer_caught_count"] == 1
        assert result["reviewer_caught_rate"] == round(1 / 3, 3)

    def test_reviewer_caught_case_match_is_normalized(self):
        """Case-string comparison must be lowercased + trimmed so that
        whitespace/case differences don't spuriously trigger a catch."""
        from unittest.mock import MagicMock
        from app.routers.admin_digest import _compute_edges_considered_metrics

        def _make_task(edges):
            t = MagicMock()
            t.first_payload = {"type": "create"}
            t.context = {"_self_review_outcome": {"edges_considered": edges}}
            return t

        tasks = [
            _make_task(
                [
                    {"case": "Empty List ", "handled": True, "origin": "author"},
                    {"case": "empty list", "handled": True, "origin": "reviewer"},
                ]
            ),
            _make_task([{"case": "c2", "handled": True}]),
            _make_task([{"case": "c3", "handled": True}]),
        ]
        result = _compute_edges_considered_metrics(tasks)
        assert result is not None
        assert result["reviewer_caught_count"] == 0, (
            "Reviewer entry whose case matches an author case (modulo case/whitespace) "
            "and is handled=true must NOT count as a reviewer catch."
        )

    def test_origin_missing_defaults_to_author(self):
        """Entries without an explicit origin field count as author entries
        for the per-origin breakdown."""
        from unittest.mock import MagicMock
        from app.routers.admin_digest import _compute_edges_considered_metrics

        def _make_task(edges):
            t = MagicMock()
            t.first_payload = {"type": "create"}
            t.context = {"_self_review_outcome": {"edges_considered": edges}}
            return t

        tasks = [
            _make_task([{"case": "c1", "handled": True}]),
            _make_task([{"case": "c2", "handled": True}]),
            _make_task([{"case": "c3", "handled": True}]),
        ]
        result = _compute_edges_considered_metrics(tasks)
        assert result is not None
        assert result["author_avg_edges_per_task"] == 1.0
        assert result["reviewer_avg_edges_per_task"] is None
        assert result["tasks_with_reviewer_edges_count"] == 0

    def test_author_unhandled_flag_partitioning(self):
        """Tasks with author-origin edges are partitioned into two sub-buckets:
        those where ≥1 author edge is handled=False (unhandled-flag bucket) and
        those where all author edges are handled=True (all-handled bucket).
        Only author-origin edges drive the split; reviewer edges are excluded.
        """
        from unittest.mock import MagicMock
        from app.routers.admin_digest import _compute_edges_considered_metrics

        def _make_task(edges, rejected_cats=None):
            t = MagicMock()
            t.first_payload = {"type": "create"}
            ctx = {"_self_review_outcome": {"edges_considered": edges}}
            if rejected_cats is not None:
                ctx["_first_attempt_rejection_categories"] = rejected_cats
            ctx["_first_attempt_succeeded"] = True
            t.context = ctx
            return t

        # task A: author flags one unhandled edge → lands in unhandled-flag bucket
        # task B: author marks all edges handled → lands in all-handled bucket
        # task C: author marks all edges handled → lands in all-handled bucket
        # Reviewer edge on task A must NOT affect which bucket it lands in.
        tasks = [
            _make_task(
                [
                    {"case": "edge1", "handled": False, "origin": "author"},
                    {"case": "edge2", "handled": True, "origin": "reviewer"},
                ],
                rejected_cats=["correctness"],
            ),
            _make_task(
                [{"case": "edge3", "handled": True, "origin": "author"}],
                rejected_cats=[],
            ),
            _make_task(
                [{"case": "edge4", "handled": True}],  # no origin → author
                rejected_cats=[],
            ),
        ]
        result = _compute_edges_considered_metrics(tasks)
        assert result is not None

        # unhandled-flag bucket: 1 task (task A), 1 rejected → rate = 1.0
        assert result["correctness_rejection_rate_with_unhandled_flag"] == 1.0, (
            "unhandled-flag bucket must compute rejection rate only for tasks where "
            "≥1 author-origin edge is handled=False"
        )
        # all-handled bucket: 2 tasks (B, C), 0 rejected → rate = 0.0
        assert result["correctness_rejection_rate_with_all_handled"] == 0.0, (
            "all-handled bucket must compute rejection rate only for tasks where "
            "all author-origin edges are handled=True"
        )

    def test_author_unhandled_flag_sample_size_guard(self):
        """Sub-buckets below the sample threshold must return None — the same
        guard that applies to the with/without-edges partition."""
        from unittest.mock import MagicMock
        from app.routers.admin_digest import _compute_edges_considered_metrics

        def _make_task(edges):
            t = MagicMock()
            t.first_payload = {"type": "create"}
            t.context = {"_self_review_outcome": {"edges_considered": edges}}
            return t

        # All three tasks have all-handled author edges → unhandled-flag bucket is empty
        tasks = [
            _make_task([{"case": "c1", "handled": True}]),
            _make_task([{"case": "c2", "handled": True}]),
            _make_task([{"case": "c3", "handled": True}]),
        ]
        result = _compute_edges_considered_metrics(tasks)
        assert result is not None
        # Empty sub-bucket: no tasks with rejection data → _rejection_rate returns None
        assert result["correctness_rejection_rate_with_unhandled_flag"] is None, (
            "correctness_rejection_rate_with_unhandled_flag must be None when the "
            "unhandled-flag sub-bucket is empty (no tasks qualify)"
        )


# ── Reviewer-Disagreement Severity Invariants ─────────────────────────────────


class TestReviewerDisagreementSeverityInvariants:
    """Contract tests for severity-stratified reviewer-disagreement tallying.

    Guards against the severity_breakdown / max_severity fields being dropped
    from the aggregate_sr_reviewer_disagreements return shape, and ensures
    _max_severity respects CRITICAL > HIGH > MEDIUM > LOW precedence.
    """

    def test_max_severity_precedence(self):
        """_max_severity must return the highest-ranked level with a nonzero count."""
        from app.sr_blind_spots import _max_severity

        assert (
            _max_severity({"CRITICAL": 1, "HIGH": 2, "MEDIUM": 0, "LOW": 0})
            == "CRITICAL"
        )
        assert (
            _max_severity({"CRITICAL": 0, "HIGH": 3, "MEDIUM": 1, "LOW": 0}) == "HIGH"
        )
        assert (
            _max_severity({"CRITICAL": 0, "HIGH": 0, "MEDIUM": 2, "LOW": 0}) == "MEDIUM"
        )
        assert _max_severity({"CRITICAL": 0, "HIGH": 0, "MEDIUM": 0, "LOW": 5}) == "LOW"
        assert _max_severity({"CRITICAL": 0, "HIGH": 0, "MEDIUM": 0, "LOW": 0}) is None

    def test_count_directions_accumulates_severity(self):
        """_count_reviewer_disagreement_directions must tally severity per bucket."""
        from unittest.mock import MagicMock
        from app.sr_blind_spots import (
            _count_reviewer_disagreement_directions,
            _SEVERITY_LEVELS,
        )

        def _make_task(findings):
            t = MagicMock()
            t.first_payload = {"type": "create"}
            t.context = {
                "_self_review_outcome": {
                    "check_results": {},
                    "reviewer_findings": findings,
                }
            }
            return t

        tasks = [
            _make_task([{"category": "correctness", "severity": "CRITICAL"}]),
            _make_task([{"category": "correctness", "severity": "HIGH"}]),
            _make_task([{"category": "correctness", "severity": "HIGH"}]),
        ]
        result = _count_reviewer_disagreement_directions(tasks)
        bucket = result[("correctness", "create")]
        assert set(bucket["severity"].keys()) == set(_SEVERITY_LEVELS), (
            "severity dict must contain all four levels: CRITICAL, HIGH, MEDIUM, LOW"
        )
        assert bucket["severity"]["CRITICAL"] == 1
        assert bucket["severity"]["HIGH"] == 2
        assert bucket["severity"]["MEDIUM"] == 0
        assert bucket["severity"]["LOW"] == 0

    def test_aggregate_return_shape_includes_severity_fields(self):
        """aggregate_sr_reviewer_disagreements items must expose severity_breakdown and max_severity.

        If this fails: either field was dropped from the return dict — update the
        frontend reader (SrReviewerDisagreementsPanel) and this test together to avoid
        contract drift.
        """
        import asyncio
        import uuid
        from unittest.mock import AsyncMock, MagicMock
        from app.sr_blind_spots import aggregate_sr_reviewer_disagreements

        def _make_task(findings):
            t = MagicMock()
            t.first_payload = {"type": "create"}
            t.context = {
                "_self_review_outcome": {
                    "check_results": {},
                    "reviewer_findings": findings,
                }
            }
            return t

        tasks = [_make_task([{"category": "correctness", "severity": "CRITICAL"}])]
        scalars_mock = MagicMock()
        scalars_mock.all.return_value = tasks
        result_mock = MagicMock()
        result_mock.scalars.return_value = scalars_mock
        session = AsyncMock()
        session.execute = AsyncMock(return_value=result_mock)

        items = asyncio.run(
            aggregate_sr_reviewer_disagreements(
                session, uuid.uuid4(), days=14, limit=10
            )
        )
        assert items, "Expected at least one result"
        item = items[0]
        assert "severity_breakdown" in item, (
            "severity_breakdown missing — frontend SrReviewerDisagreementsPanel depends on it"
        )
        assert "max_severity" in item, (
            "max_severity missing — frontend badge color-coding depends on it"
        )
        assert isinstance(item["severity_breakdown"], dict)
        assert item["max_severity"] == "CRITICAL"


# ── Design-Doc Lock Invariant ─────────────────────────────────────────────────

_LOCK_BASELINE_JSON = os.path.join(
    os.path.dirname(__file__), "lock_assignment_baseline.json"
)


def _load_lock_baseline() -> dict:
    import json

    with open(_LOCK_BASELINE_JSON) as f:
        return json.load(f)


_LOCK_BASELINE = _load_lock_baseline()


def _ast_count_attr_assignments(app_dir: str, attr_name: str) -> dict[str, int]:
    """Return per-file counts of AST-level attribute assignments for attr_name.

    Parses each backend/app/**/*.py file with ast.parse and counts ast.Assign /
    ast.AugAssign nodes whose target (or any element of a tuple target) is an
    ast.Attribute with .attr == attr_name.  Receiver name is ignored so aliases
    like proj / p / _proj are all caught.  Assignments inside docstrings,
    comments, or string literals are naturally excluded by the AST.
    """
    import ast
    import glob as globmod

    counts: dict[str, int] = {}
    for py_file in globmod.glob(os.path.join(app_dir, "**", "*.py"), recursive=True):
        with open(py_file) as fh:
            source = fh.read()
        try:
            tree = ast.parse(source, filename=py_file)
        except SyntaxError:
            continue
        count = 0
        for node in ast.walk(tree):
            if isinstance(node, (ast.Assign, ast.AugAssign)):
                targets = (
                    node.targets if isinstance(node, ast.Assign) else [node.target]
                )
                for target in targets:
                    for t in ast.walk(target):
                        if isinstance(t, ast.Attribute) and t.attr == attr_name:
                            count += 1
        if count > 0:
            rel = os.path.relpath(py_file, app_dir)
            counts[rel] = count
    return counts


def _check_lock_invariant(attr_name: str, baseline_key: str, helper_name: str) -> None:
    """Shared implementation for all three column-lock invariant tests."""
    app_dir = os.path.normpath(os.path.join(os.path.dirname(__file__), "..", "app"))
    baseline: dict[str, int] = _LOCK_BASELINE[baseline_key]
    file_counts = _ast_count_attr_assignments(app_dir, attr_name)

    violations: list[str] = []
    for rel, count in sorted(file_counts.items()):
        allowed = baseline.get(rel, 0)
        if count > allowed:
            violations.append(f"  {rel}: {count} assignments (baseline: {allowed})")

    assert not violations, (
        f"project.{attr_name} assignment counts exceed their per-file baseline:\n"
        + "\n".join(violations)
        + f"\n\nRoute new {attr_name} writes through {helper_name}() "
        "(task_locking.py). Counts may only be lowered (after migrating a site "
        "to the canonical helper), never raised. To record a legitimate decrease, "
        "lower the count in backend/testing/lock_assignment_baseline.json. "
        "Do NOT increase counts — that widens the tripwire hole."
    )


class TestDesignDocLockInvariant:
    """Tripwire: direct <var>.design_doc attribute assignments must not increase
    above their per-file baseline recorded in lock_assignment_baseline.json.

    Uses AST-based scanning so docstrings, comments, and string literals are
    excluded from the count (unlike a regex scan which would match them).

    The baseline is a ratchet — counts may only decrease as write sites are
    migrated to with_design_doc_locked() (task_locking.py). Adding a new
    assignment to any file (even an already-allowlisted one) raises the count
    above its baseline and fails this test.

    To migrate a site: route it through with_design_doc_locked() and lower
    the count in lock_assignment_baseline.json. Remove the entry when count
    reaches 0.
    """

    def test_no_new_unlocked_design_doc_writes(self):
        _check_lock_invariant("design_doc", "design_doc", "with_design_doc_locked")


# ── Directions Lock Invariant ─────────────────────────────────────────────────


class TestDirectionsLockInvariant:
    """Tripwire: direct <var>.directions attribute assignments must not increase
    above their per-file baseline recorded in lock_assignment_baseline.json.

    Uses AST-based scanning (receiver-name-agnostic: matches any receiver so
    aliases like proj / p / _proj are all caught).  Assignments inside
    docstrings, comments, or string literals are naturally excluded.

    The baseline is a ratchet — counts may only decrease as write sites are
    migrated to with_directions_locked() (task_locking.py). Adding a new
    assignment to any file (even an already-baselined one) raises the count
    above its baseline and fails this test.

    To migrate a site: route it through with_directions_locked() and lower
    the count in lock_assignment_baseline.json. Remove the entry when count
    reaches 0.
    """

    def test_no_new_unlocked_directions_writes(self):
        _check_lock_invariant("directions", "directions", "with_directions_locked")


class TestResearchCompletionDirectionsLocked:
    """Positive ratchet: research_completion.py must keep its direction writes routed
    through with_directions_locked.

    Complements TestDirectionsLockInvariant (which is a *negative* ratchet — catches
    new direct .directions = assignments).  This positive ratchet catches the mirror
    failure: a with_directions_locked call site is removed without being replaced by
    another locked call, so the operation silently disappears rather than blowing up.

    _EXPECTED_MIN_CALLS is set to 5 (the five sites migrated in PR #5704).  It must
    not decrease.  If a call site is removed, either add a replacement locked call or
    lower this constant in the same PR with an explanation.
    """

    _EXPECTED_MIN_CALLS = 5

    def test_research_completion_locked_call_count(self):
        """research_completion.py must have at least 5 with_directions_locked call sites."""
        import ast

        app_dir = os.path.normpath(os.path.join(os.path.dirname(__file__), "..", "app"))
        rc_path = os.path.join(app_dir, "research_completion.py")
        with open(rc_path) as f:
            source = f.read()
        tree = ast.parse(source, filename=rc_path)
        call_count = sum(
            1
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "with_directions_locked"
        )
        assert call_count >= self._EXPECTED_MIN_CALLS, (
            f"research_completion.py has only {call_count} call(s) to "
            f"with_directions_locked (expected >= {self._EXPECTED_MIN_CALLS}). "
            "All five migrated direction-write sites must remain routed through the "
            "canonical locked helper — do not remove calls without replacing them "
            "or lowering _EXPECTED_MIN_CALLS with an explanation."
        )


class TestRuntimeConfigProjectBranchBypasses:
    _ALLOWED_FILES = frozenset(
        {"shared_state.py", "routers/admin_settings.py", "routers/agent_status.py"}
    )
    _KEYS = ("integration_branch", "promotion_target", "task_branch_namespace")

    def test_no_new_direct_project_branch_runtime_config_reads(self):
        import ast
        import glob as globmod

        app_dir = os.path.normpath(os.path.join(os.path.dirname(__file__), "..", "app"))
        violations: list[str] = []

        for py_file in globmod.glob(
            os.path.join(app_dir, "**", "*.py"), recursive=True
        ):
            rel = os.path.relpath(py_file, app_dir)
            if rel in self._ALLOWED_FILES:
                continue
            with open(py_file) as fh:
                source = fh.read()
            try:
                tree = ast.parse(source, filename=py_file)
            except SyntaxError:
                continue
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                func = node.func
                if not (
                    isinstance(func, ast.Attribute)
                    and func.attr == "get"
                    and isinstance(func.value, ast.Name)
                    and func.value.id == "_runtime_config"
                ):
                    continue
                if (
                    node.args
                    and isinstance(node.args[0], ast.Constant)
                    and node.args[0].value in self._KEYS
                ):
                    violations.append(f"  {rel}:{node.lineno}:{node.args[0].value}")

        assert not violations, (
            "Direct _runtime_config.get(...) project-branch reads found outside the allowlist:\n"
            + "\n".join(violations)
        )


# ── Vision Lock Invariant ─────────────────────────────────────────────────────


class TestBasebranchMainFallbackInvariant:
    """Tripwire: spawn/recovery paths must not hardcode 'main' as the base_branch
    fallback. Use shared_state.get_project_integration_branch(project) instead.

    Scans the specific spawn/recovery modules whose hardcoded 'main' fallbacks were
    routed through get_project_integration_branch() in clusters 1–12 of the
    per-project base_branch fix.  If this test fails, the regression-prone pattern
    payload.get("base_branch", "main") was reintroduced in one of these files.

    To guard a newly-fixed file, add its relative path to _SPAWN_RECOVERY_MODULES.
    To register a site that is legitimately global-config (not per-project), add it
    to _ALLOWED_FILES instead — but only for true global-config defaults, not for
    unfixed bugs.
    """

    # Files fixed by clusters 1-12; scanned for the regression-prone pattern.
    _SPAWN_RECOVERY_MODULES = frozenset(
        {
            "anomaly_detection.py",
            "lifecycle.py",
            "services.py",
            "rollup_management.py",
            "task_lifecycle.py",
            "task_completion.py",
            "research_completion.py",
            "plan_completion.py",
            "issue_implementation.py",
            "routers/agent_status.py",
            "reaper.py",
        }
    )

    # Files exempted because they use a global-config default that is intentional
    # (not a per-project bypass).  Do not add unfixed-bug files here.
    _ALLOWED_FILES: frozenset[str] = frozenset()

    def test_no_hardcoded_main_base_branch_fallback(self):
        app_dir = os.path.normpath(os.path.join(os.path.dirname(__file__), "..", "app"))
        violations: list[str] = []

        for rel in sorted(self._SPAWN_RECOVERY_MODULES - self._ALLOWED_FILES):
            py_file = os.path.join(app_dir, rel)
            if not os.path.exists(py_file):
                continue
            with open(py_file) as fh:
                for lineno, line in enumerate(fh, 1):
                    stripped = line.strip()
                    if stripped.startswith("#"):
                        continue
                    if (
                        '"base_branch", "main"' in line
                        or "'base_branch', 'main'" in line
                    ):
                        violations.append(f"  {rel}:{lineno}: {stripped}")

        assert not violations, (
            "Hardcoded 'main' fallback for base_branch found in fixed spawn/recovery modules:\n"
            + "\n".join(violations)
            + "\n\nReplace payload.get('base_branch', 'main') with per-project resolution:\n"
            "  from app.shared_state import get_project_integration_branch\n"
            "  base_branch = (payload.get('base_branch')\n"
            "      or get_project_integration_branch(project))\n"
            "See shared_state.get_project_integration_branch for the fallback ordering."
        )


class TestVisionLockInvariant:
    """Tripwire: direct <var>.vision attribute assignments must not increase
    above their per-file baseline recorded in lock_assignment_baseline.json.

    Uses AST-based scanning (receiver-name-agnostic: matches any receiver so
    aliases like proj / p / _proj are all caught).  Assignments inside
    docstrings, comments, or string literals are naturally excluded.

    The baseline is a ratchet — counts may only decrease as write sites are
    migrated to with_vision_locked() (task_locking.py). Adding a new
    assignment to any file (even an already-baselined one) raises the count
    above its baseline and fails this test.

    To migrate a site: route it through with_vision_locked() and lower
    the count in lock_assignment_baseline.json. Remove the entry when count
    reaches 0.
    """

    def test_no_new_unlocked_vision_writes(self):
        _check_lock_invariant("vision", "vision", "with_vision_locked")
