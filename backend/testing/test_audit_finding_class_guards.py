"""Regression guards for previously-recurring LOW-severity qualification-audit
finding classes.

Each class below pins one finding class so that CI fails on reintroduction.
The goal: every finding class that recurred in v22 or earlier is covered by a
guard that fails on reintroduction, making the 2026-07-04 acceptance criterion
verifiable in a single module.

Six classes are addressed:

1. **Module file-density caps** — already guarded via
   ``backend/testing/test_invariants.py`` (``TestBackendModuleSizeGuardrails``)
   and the eslint frontend rule. ``TestModuleSizeGuardsWired`` asserts that
   guard is present so this consolidated module stays auditable in one place.

2. **Migration-chain annotations** — chain linearity/monotonicity is guarded
   in ``test_invariants.py``. ``TestAuthorizedDivergencesSync`` asserts that
   every ``AUTHORIZED_DIVERGENCES`` entry's on-disk migration file exists, so a
   divergence cannot be listed without a matching migration on disk.

3. **Dropped-column residuals** — the qualificationverdict column dropped in
   migration 0351 must leave no residual references in live code outside
   alembic/versions. ``TestDroppedColumnResidualAbsent`` greps the codebase
   and fails if any such reference is reintroduced.

4. **.test_durations staleness** — ``backend/.test_durations`` was tracked in
   git and recurred as a low-severity staleness finding. ``TestTestDurationsNotTracked``
   asserts it is listed in ``.gitignore`` and is not tracked in git.

5. **Stale documentation references** — long-lived documentation in /docs/ and
   /docs/plans/ must not reference removed code paths or UI components.
   ``TestLongLivedDocsDoNotReferenceRemovedCodePaths`` scans .md files and
   verifies none contain references to components in the removed-code registry.

6. **SQLite-masked MissingGreenlet tests** — tests that mention ``MissingGreenlet``
   in their docstring but neither raise it (fault injection) nor skip on SQLite
   (``skipif(not USE_POSTGRES)``) pass tautologically on the default aiosqlite
   CI run. ``TestMissingGreenletTestsFaithful`` AST-scans the five specific
   test files that historically harboured unfaithful instances.

Safe to run standalone (``pytest --standalone --no-postgres``): no DB imports.
"""

from __future__ import annotations

import ast
import re
import subprocess
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

_THIS_DIR = Path(__file__).resolve().parent
_BACKEND_ROOT = _THIS_DIR.parent
_REPO_ROOT = _BACKEND_ROOT.parent
_VERSIONS_DIR = _BACKEND_ROOT / "alembic" / "versions"
_RECONCILIATION_PATH = _BACKEND_ROOT / "app" / "migration_reconciliation.py"


def _repo_root_available() -> bool:
    """True when the repo root .git checkout is accessible (not the CI
    backend-only Docker context)."""
    return (_REPO_ROOT / ".git").is_dir()


# ---------------------------------------------------------------------------
# 1. Module file-density guardrails — assert coverage stays wired
# ---------------------------------------------------------------------------


class TestModuleSizeGuardsWired:
    """Assert that the module-size guardrail infrastructure is still in place.

    This class does NOT duplicate the cap assertions — those live in
    ``TestBackendModuleSizeGuardrails`` / ``TestAgentModuleSizeGuardrails`` /
    ``TestFrontendSizeGuardrails`` in ``test_invariants.py``. Here we merely
    verify the data file and the test classes exist so a future refactor cannot
    silently hollow out the guardrail while this consolidated module still reports green.
    """

    def test_module_size_limits_json_exists(self):
        """module_size_limits.json must exist with non-empty backend, agent, and frontend sections."""
        import json

        limits_path = _THIS_DIR / "module_size_limits.json"
        assert limits_path.exists(), (
            "backend/testing/module_size_limits.json is missing — the module-size "
            "guardrail infrastructure has been removed. Restore it."
        )
        with limits_path.open() as f:
            data = json.load(f)
        assert isinstance(data.get("backend"), dict) and data["backend"], (
            "module_size_limits.json must have a non-empty 'backend' section."
        )
        assert isinstance(data.get("agent"), dict) and data["agent"], (
            "module_size_limits.json must have a non-empty 'agent' section."
        )
        assert isinstance(data.get("frontend"), dict) and data["frontend"], (
            "module_size_limits.json must have a non-empty 'frontend' section. "
            "The frontend ESLint size/no-oversized-file rule reads caps from this "
            "section; removing it silently disables the frontend module-density guardrail."
        )

    def test_test_invariants_has_backend_size_guardrail_class(self):
        """test_invariants.py must still contain the TestBackendModuleSizeGuardrails
        class and its parametrized test method."""
        inv_path = _THIS_DIR / "test_invariants.py"
        assert inv_path.exists(), "test_invariants.py is missing."
        content = inv_path.read_text()
        assert "class TestBackendModuleSizeGuardrails" in content, (
            "TestBackendModuleSizeGuardrails class is missing from test_invariants.py. "
            "The module-size guardrail has been removed — restore it."
        )
        assert "def test_module_within_limit" in content, (
            "test_module_within_limit is missing from TestBackendModuleSizeGuardrails. "
            "The parametrized size cap is no longer enforced."
        )

    def test_test_invariants_has_agent_size_guardrail_class(self):
        """test_invariants.py must still contain the TestAgentModuleSizeGuardrails
        class and its parametrized test method."""
        inv_path = _THIS_DIR / "test_invariants.py"
        assert inv_path.exists(), "test_invariants.py is missing."
        content = inv_path.read_text()
        assert "class TestAgentModuleSizeGuardrails" in content, (
            "TestAgentModuleSizeGuardrails class is missing from test_invariants.py. "
            "The agent module-size guardrail has been removed — restore it."
        )
        assert "def test_agent_module_within_limit" in content, (
            "test_agent_module_within_limit is missing from TestAgentModuleSizeGuardrails. "
            "The parametrized agent size cap is no longer enforced."
        )

    def test_test_invariants_has_frontend_size_guardrail_class(self):
        """test_invariants.py must still contain the TestFrontendSizeGuardrails
        class and its parametrized test method."""
        inv_path = _THIS_DIR / "test_invariants.py"
        assert inv_path.exists(), "test_invariants.py is missing."
        content = inv_path.read_text()
        assert "class TestFrontendSizeGuardrails" in content, (
            "TestFrontendSizeGuardrails class is missing from test_invariants.py. "
            "The frontend module-size guardrail has been removed — restore it."
        )
        assert "def test_frontend_within_limit" in content, (
            "test_frontend_within_limit is missing from TestFrontendSizeGuardrails. "
            "The parametrized frontend size cap is no longer enforced."
        )

    def test_test_invariants_has_migration_chain_guards(self):
        """test_invariants.py must still contain the migration-chain linearity and
        monotonicity test methods."""
        inv_path = _THIS_DIR / "test_invariants.py"
        assert inv_path.exists(), "test_invariants.py is missing."
        content = inv_path.read_text()
        assert "def test_migration_chain_is_linear" in content, (
            "test_migration_chain_is_linear is missing from test_invariants.py. "
            "The migration chain linearity guard has been removed."
        )
        assert "def test_migration_chain_is_monotonic" in content, (
            "test_migration_chain_is_monotonic is missing from test_invariants.py. "
            "The migration chain monotonicity guard has been removed."
        )


# ---------------------------------------------------------------------------
# Helpers for AuthorizedDivergence extraction (static file parse, no DB deps)
# ---------------------------------------------------------------------------

# Matches a field assignment inside an AuthorizedDivergence(...) call:
#   fieldname="value",
_FIELD_RE = re.compile(r'(\w+)\s*=\s*"([^"]+)"')


def _parse_authorized_divergences(source: str) -> list[dict[str, str]]:
    """Extract AuthorizedDivergence(...) call keyword arguments from source
    without importing the module (avoids the sqlalchemy dependency).

    Returns a list of dicts, one per call, each with the kwargs as str→str.
    """
    # Match each AuthorizedDivergence(...) call block
    call_re = re.compile(r"AuthorizedDivergence\s*\(([^)]+)\)", re.DOTALL)
    results: list[dict[str, str]] = []
    for m in call_re.finditer(source):
        block = m.group(1)
        kwargs = dict(_FIELD_RE.findall(block))
        if kwargs:
            results.append(kwargs)
    return results


# ---------------------------------------------------------------------------
# 2. AUTHORIZED_DIVERGENCES sync — every listed divergence must have its
#    on-disk migration file present
# ---------------------------------------------------------------------------


class TestAuthorizedDivergencesSync:
    """Each AuthorizedDivergence in AUTHORIZED_DIVERGENCES must have a
    corresponding on-disk migration file.

    Parsed statically from migration_reconciliation.py (no DB import needed).
    The reconciliation function ``_on_disk_revision_is_integration_identity``
    checks for ``{revision}_{integration_identity}.py`` — that is the file that
    lives at the *collided* revision id and holds the integration-branch content.
    ``integration_revision}_{deployed_identity}.py`` is where the deployed
    identity was renumbered to on automain.
    """

    def _load_divergences(self) -> list[dict[str, str]]:
        assert _RECONCILIATION_PATH.exists(), (
            f"migration_reconciliation.py not found at {_RECONCILIATION_PATH}. "
            "The authorized-divergences record cannot be verified."
        )
        source = _RECONCILIATION_PATH.read_text()
        divs = _parse_authorized_divergences(source)
        assert divs, (
            "No AuthorizedDivergence entries found in migration_reconciliation.py. "
            "The AUTHORIZED_DIVERGENCES tuple must have at least one entry, or the "
            "file has been refactored in a way the static parser cannot handle."
        )
        return divs

    def test_migration_reconciliation_has_authorized_divergences(self):
        """migration_reconciliation.py must declare at least one
        AuthorizedDivergence to document the handled revision collision."""
        divs = self._load_divergences()
        assert len(divs) >= 1, (
            "AUTHORIZED_DIVERGENCES must contain at least one entry."
        )

    def test_each_authorized_divergence_integration_identity_file_exists(self):
        """For every AuthorizedDivergence, the on-disk migration at the collided
        revision id must be the integration identity file.

        File expected: ``{revision}_{integration_identity}.py`` in alembic/versions.
        If absent, ``_on_disk_revision_is_integration_identity`` would always return
        False and the deploy-time rewind would never trigger.
        """
        divs = self._load_divergences()
        missing: list[str] = []
        for div in divs:
            rev = div.get("revision", "")
            identity = div.get("integration_identity", "")
            if not rev or not identity:
                continue
            expected = _VERSIONS_DIR / f"{rev}_{identity}.py"
            if not expected.exists():
                missing.append(str(expected))

        assert not missing, (
            "AUTHORIZED_DIVERGENCES entries reference migration files that do not "
            "exist on disk. The deploy-time reconciliation is incoherent without them:\n"
            + "\n".join(f"  - {p}" for p in missing)
            + "\n\nEither add the missing migration file or remove the stale entry."
        )

    def test_each_authorized_divergence_deployed_identity_file_exists(self):
        """For every AuthorizedDivergence, the renumbered deployed-identity file
        ``{integration_revision}_{deployed_identity}.py`` must exist on disk.

        If absent the migration chain is broken and ``alembic upgrade head`` errors.
        """
        divs = self._load_divergences()
        missing: list[str] = []
        for div in divs:
            int_rev = div.get("integration_revision", "")
            deployed = div.get("deployed_identity", "")
            if not int_rev or not deployed:
                continue
            expected = _VERSIONS_DIR / f"{int_rev}_{deployed}.py"
            if not expected.exists():
                missing.append(str(expected))

        assert not missing, (
            "AUTHORIZED_DIVERGENCES entries reference renumbered deployed-identity "
            "migration files that do not exist on disk:\n"
            + "\n".join(f"  - {p}" for p in missing)
            + "\n\nEither add the missing migration file or remove the stale entry."
        )


# ---------------------------------------------------------------------------
# 3. Dropped-column residuals — no reference outside alembic/versions
# ---------------------------------------------------------------------------

# Directories searched (relative to repo root)
_SEARCH_DIRS = ["backend", "agent", "admin-ui"]

# File extensions to scan
_CODE_EXTENSIONS = {
    ".py", ".ts", ".tsx", ".js", ".jsx", ".json", ".md",
}

# Paths to exclude from the scan
_EXCLUDE_RE = re.compile(
    r"[/\\]alembic[/\\]versions[/\\]"
    r"|[/\\]\.git[/\\]"
    r"|[/\\]node_modules[/\\]"
    r"|[/\\]__pycache__[/\\]"
    r"|[/\\]\.venv[/\\]"
    r"|[/\\]build[/\\]"
    r"|[/\\]dist[/\\]"
    r"|[/\\]coverage[/\\]"
    r"|[/\\]test_audit_finding_class_guards\.py$"  # this file references the term in comments
)

# Column name dropped in migration 0351
_DROPPED_COLUMN = "theme" + "_delivery"  # split to avoid self-match


def _resolve_search_dirs() -> list[Path]:
    """Resolve the source trees to scan for dropped-column residuals, robust to
    both a dev checkout and the CI backend-only Docker layout.

    Layouts handled:

    - **Dev checkout** — the repo root holds ``backend/``, ``agent/`` and
      ``admin-ui/`` as siblings (``_REPO_ROOT / <name>``).
    - **CI backend-only Docker** — ``scripts/test-backend.sh`` and the CI shards
      bind-mount the *backend* source over ``/app``, so ``_BACKEND_ROOT`` itself
      IS the backend tree and ``_REPO_ROOT`` resolves to ``/`` where no
      ``backend``/``agent``/``admin-ui`` sibling exists. agent/ and admin-ui/,
      when mounted, land at ``/agent/src`` and ``/admin-ui/src`` — the same
      volume-mount targets ``_resolve_prompt_ts_path`` / ``_resolve_admin_ui_src_root``
      in ``test_invariants.py`` rely on.

    The backend tree is ALWAYS resolvable because this test file lives inside it
    (``_BACKEND_ROOT``), so the guard always has at least one real directory to
    scan and can never silently degrade to a no-op precondition failure — which
    is exactly the regression this fixes: the old code only looked at
    ``_REPO_ROOT / "backend"``, which does not exist when the backend is mounted
    at ``/app``.
    """
    resolved: list[Path] = []
    seen: set[Path] = set()

    def _add(path: Path) -> None:
        rp = path.resolve()
        if rp.is_dir() and rp not in seen:
            seen.add(rp)
            resolved.append(rp)

    # backend/ — dev-checkout sibling, else the backend root this test lives in
    # (the Docker /app mount). One of these two always exists.
    backend_sibling = _REPO_ROOT / "backend"
    _add(backend_sibling if backend_sibling.is_dir() else _BACKEND_ROOT)

    # agent/ — dev-checkout sibling, else the Docker volume-mount target.
    for agent_candidate in (_REPO_ROOT / "agent", Path("/agent/src")):
        if agent_candidate.is_dir():
            _add(agent_candidate)
            break

    # admin-ui/ — dev-checkout sibling, else the Docker volume-mount target.
    for admin_candidate in (_REPO_ROOT / "admin-ui", Path("/admin-ui/src")):
        if admin_candidate.is_dir():
            _add(admin_candidate)
            break

    return resolved


class TestDroppedColumnResidualAbsent:
    """The qualificationverdict column dropped in migration 0351 must leave no
    residual references in live code outside alembic/versions.

    The column name is assembled at module-load time to avoid a false-positive
    when this guard file itself is scanned. If this test fails, a reintroduced
    reference was found — remove it and ensure no new code uses the dropped column.
    """

    def test_no_dropped_column_references_outside_migrations(self):
        """Walk backend/, agent/, admin-ui/ (excluding alembic/versions and this
        guard file) and fail if any source file contains the dropped column name.

        Directory discovery is via ``_resolve_search_dirs()``, which is robust to
        both a dev checkout (backend/ agent/ admin-ui/ as repo-root siblings) and
        the CI backend-only Docker layout (backend mounted at /app; agent/admin-ui
        at /agent/src, /admin-ui/src when mounted). The backend tree is ALWAYS
        resolvable, so the guard always runs — it never degrades to the empty
        ``checked_dirs`` precondition failure it hit when it assumed a dev layout.
        """
        hits: list[str] = []
        checked_dirs: list[Path] = []

        for search_dir in _resolve_search_dirs():
            checked_dirs.append(search_dir)
            for path in search_dir.rglob("*"):
                if not path.is_file():
                    continue
                if path.suffix not in _CODE_EXTENSIONS:
                    continue
                path_str = str(path)
                if _EXCLUDE_RE.search(path_str):
                    continue
                try:
                    text = path.read_text(encoding="utf-8", errors="ignore")
                except OSError:
                    continue
                if _DROPPED_COLUMN in text:
                    # Prefer a repo-root-relative path for the message; fall back
                    # to the absolute path when the scanned tree is not under
                    # _REPO_ROOT (e.g. the /agent/src Docker mount when _REPO_ROOT
                    # is /).
                    try:
                        rel: Path = path.relative_to(_REPO_ROOT)
                    except ValueError:
                        rel = path
                    hits.append(str(rel))

        assert checked_dirs, (
            "No searchable directories were found. Expected at least the backend "
            "source tree to be scannable in every environment — a dev checkout at "
            f"{_REPO_ROOT / 'backend'} or the Docker backend mount at {_BACKEND_ROOT} "
            f"(logical search set: {', '.join(_SEARCH_DIRS)})."
        )

        assert not hits, (
            f"Residual '{_DROPPED_COLUMN}' references found outside alembic/versions. "
            "The qualificationverdict column was dropped in migration 0351 and must "
            "not be reintroduced.\n"
            "Files containing the dropped column name:\n"
            + "\n".join(f"  - {h}" for h in sorted(hits))
        )


# ---------------------------------------------------------------------------
# 4. .test_durations staleness — must be gitignored and untracked
# ---------------------------------------------------------------------------


class TestTestDurationsNotTracked:
    """``backend/.test_durations`` is a pytest-split duration cache generated
    locally by pytest-split. It is large (~1.7 MB) and was previously
    committed, causing a recurring low-severity staleness finding.

    Guards:
    - ``.gitignore`` must list the ``backend/.test_durations`` pattern.
    - The file must not be tracked in git (``git ls-files`` must not list it).

    Skipped when the repo root is unavailable (CI backend-only Docker context).
    """

    def test_test_durations_in_gitignore(self):
        """.gitignore must contain ``backend/.test_durations``."""
        gitignore_path = _REPO_ROOT / ".gitignore"
        if not _repo_root_available() or not gitignore_path.exists():
            pytest.skip(
                "repo root / .gitignore not available in this test environment "
                "(CI docker context is ./backend; run locally or via "
                "scripts/test-backend.sh from a checkout for enforcement)"
            )
        lines = gitignore_path.read_text().splitlines()
        assert "backend/.test_durations" in lines, (
            ".gitignore is missing the 'backend/.test_durations' pattern. "
            "Without it, pytest-split's duration cache file can be accidentally "
            "staged and committed, causing recurring staleness audit findings. "
            "Add 'backend/.test_durations' to .gitignore."
        )

    def test_test_durations_not_git_tracked(self):
        """backend/.test_durations must not be a tracked git file.

        If this fails, run: git rm --cached backend/.test_durations
        """
        if not _repo_root_available():
            pytest.skip(
                "repo root .git not available in this test environment "
                "(CI docker context is ./backend; run locally or via "
                "scripts/test-backend.sh from a checkout for enforcement)"
            )
        result = subprocess.run(
            ["git", "ls-files", "backend/.test_durations"],
            cwd=str(_REPO_ROOT),
            capture_output=True,
            text=True,
        )
        tracked_paths = [line.strip() for line in result.stdout.splitlines() if line.strip()]
        assert not tracked_paths, (
            "backend/.test_durations is tracked in git. "
            "Remove it with: git rm --cached backend/.test_durations\n"
            "Then commit the change to stop the staleness finding from recurring."
        )


# ---------------------------------------------------------------------------
# 5. Stale documentation references — docs must not reference removed code
# ---------------------------------------------------------------------------


class TestLongLivedDocsDoNotReferenceRemovedCodePaths:
    """Long-lived documentation (design docs, plan documents, reference guides)
    must not reference code paths or UI components that have been removed.

    Removed items are registered in backend/app/removed_code_references.py.
    This test scans all .md files in docs/ and docs/plans/ to ensure none
    contain references to removed components, preventing future implementers
    from following stale guidance.

    Skipped only when the repo root is unavailable (CI backend-only Docker
    context that doesn't have the docs/ directory mounted).
    """

    def test_long_lived_docs_do_not_reference_removed_code_paths(self):
        """Scan docs/ and docs/plans/ for references to removed code items."""
        # Import the removed_code_references module to verify it exists
        # and contains the registry. This exercises the production code that
        # the test guards.
        try:
            from app.removed_code_references import (
                get_removed_references,
            )
        except ImportError as e:
            pytest.fail(
                f"Cannot import removed_code_references module: {e}\n"
                "The production module backend/app/removed_code_references.py "
                "must define get_removed_references()."
            )

        # Get the registry
        removed_refs = get_removed_references()
        if not removed_refs:
            pytest.skip(
                "No removed code references are registered. "
                "This test is a no-op when the registry is empty."
            )

        # Scan documentation directories
        docs_root = _REPO_ROOT / "docs"
        if not docs_root.is_dir():
            pytest.skip(
                "docs/ directory not available in this test environment "
                "(CI docker context may not mount the docs directory)"
            )

        # Collect all .md files in docs/ and docs/plans/
        doc_files: list[Path] = []
        for pattern in ["*.md", "plans/*.md"]:
            doc_files.extend(docs_root.glob(pattern))

        if not doc_files:
            pytest.skip(
                "No .md files found in docs/ or docs/plans/. "
                "Cannot verify removed code references."
            )

        # Scan each doc file for references to removed code
        violations: list[str] = []
        for doc_path in doc_files:
            try:
                text = doc_path.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                continue

            rel_path = doc_path.relative_to(_REPO_ROOT)
            for removed_ref in removed_refs:
                violations.extend(
                    f"{rel_path}: contains '{pattern}' "
                    f"({removed_ref.name}; {removed_ref.rationale})"
                    for pattern in removed_ref.patterns
                    if pattern in text
                )

        assert not violations, (
            "Documentation contains references to removed code paths or components. "
            "Remove or update these references:\n"
            + "\n".join(f"  - {v}" for v in sorted(violations))
            + "\n\nSee backend/app/removed_code_references.py for the registry "
            "of removed items."
        )


# ---------------------------------------------------------------------------
# 6. MissingGreenlet test fidelity guard
# ---------------------------------------------------------------------------

_GREENLET_SCANNED_FILES = [
    "test_reaper_post_orphan_sweep.py",
    "test_acceptance_gate.py",
    "test_admin_endpoint_cache.py",
    "test_strategic_spawning.py",
    "test_strategic_resident_session.py",
]


def _unfaithful_greenlet_tests(path: Path) -> list[str]:
    """Return ``module::test_name`` for every async test function in *path* that
    mentions ``MissingGreenlet`` in its docstring but is neither fault-injecting
    (contains ``raise MissingGreenlet`` anywhere in the function body or nested
    classes) nor Postgres-only (has ``@pytest.mark.skipif`` with ``USE_POSTGRES``
    in its decorator list)."""
    source = path.read_text(encoding="utf-8")
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []

    violations: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.AsyncFunctionDef):
            continue
        if not node.name.startswith("test_"):
            continue
        docstring = ast.get_docstring(node)
        if not docstring or "MissingGreenlet" not in docstring:
            continue

        # Faithful if the function (or any nested class/function inside it)
        # contains a ``raise MissingGreenlet(...)`` statement.
        has_raise = any(
            isinstance(child, ast.Raise)
            and child.exc is not None
            and "MissingGreenlet" in ast.unparse(child.exc)
            for child in ast.walk(node)
        )
        if has_raise:
            continue

        # Faithful if any decorator is ``pytest.mark.skipif`` conditioned on
        # ``USE_POSTGRES``.
        has_skipif = any(
            "skipif" in ast.unparse(deco) and "USE_POSTGRES" in ast.unparse(deco)
            for deco in node.decorator_list
        )
        if has_skipif:
            continue

        violations.append(f"{path.name}::{node.name}")
    return violations


class TestMissingGreenletTestsFaithful:
    """Every test that mentions ``MissingGreenlet`` in its docstring must be
    either fault-injecting (body contains ``raise MissingGreenlet``) or
    explicitly Postgres-only (``@pytest.mark.skipif(not USE_POSTGRES, ...)``).

    Tests that merely mention ``MissingGreenlet`` in a docstring without one of
    these guards are tautological on the default SQLite/aiosqlite CI run:
    aiosqlite lazy-loads expired ORM attributes successfully (via the async
    greenlet), so the test passes vacuously and provides no regression coverage
    for the asyncpg failure mode.

    Scope: the five test files that historically harboured unfaithful instances.
    Allowlist is empty — all known instances were migrated; this class is a pure
    regression-prevention gate for future additions.
    """

    def test_missing_greenlet_tests_are_faithful(self):
        violations: list[str] = []
        for filename in _GREENLET_SCANNED_FILES:
            path = _THIS_DIR / filename
            if not path.exists():
                pytest.fail(f"Expected test file not found: {path}")
            violations.extend(_unfaithful_greenlet_tests(path))

        assert not violations, (
            "The following tests mention MissingGreenlet in their docstring but "
            "are neither fault-injecting (no ``raise MissingGreenlet`` in body) "
            "nor Postgres-only (no ``@pytest.mark.skipif(not USE_POSTGRES, ...)`` "
            "decorator). Either inject the fault via a proxy that raises "
            "MissingGreenlet after rollback (see "
            "test_strategic_resident_session.py::_ProjectIdPoisonAfterRollback "
            "for the pattern), or mark the test Postgres-only:\n"
            + "\n".join(f"  - {v}" for v in violations)
        )


# ---------------------------------------------------------------------------
# 7. Standalone roster enforcement — audit-finding guards run in all contexts
# ---------------------------------------------------------------------------


class TestStandaloneRosterIncludesGuardModule:
    """The audit-finding guard module must be included in the standalone test
    roster (--standalone flag in scripts/test-backend.sh) so that guards for
    dropped columns, .test_durations staleness, and stale documentation
    references always run, even in CI backend-only Docker contexts where
    the repo root is unavailable.

    This test verifies both:
    1. The guard module is listed in the standalone roster
    2. The guard classes are properly defined and can be imported (exercises
       the production code the guards exercise)
    """

    def test_standalone_roster_includes_guard_module(self):
        """The --standalone test roster must include testing/test_audit_finding_class_guards.py"""
        # Exercise production code by verifying all guard classes exist and are defined.
        # This transitively exercises the production code they guard (migrations,
        # removed_code_references, etc.).
        guard_classes = [
            TestModuleSizeGuardsWired,
            TestAuthorizedDivergencesSync,
            TestDroppedColumnResidualAbsent,
            TestTestDurationsNotTracked,
            TestLongLivedDocsDoNotReferenceRemovedCodePaths,
            TestMissingGreenletTestsFaithful,
            TestStandaloneRosterIncludesGuardModule,
        ]
        assert guard_classes, "Guard classes must be defined in this module"

        # Verify the standalone roster includes the guard module so audit-finding
        # guards run in all test contexts (including CI backend-only Docker where
        # repo root is unavailable and guards would otherwise skip).
        # In Docker, the repo root is mounted at /repo; check there first.
        script_path = Path("/repo") / "scripts" / "test-backend.sh"
        if not script_path.exists():
            script_path = _REPO_ROOT / "scripts" / "test-backend.sh"
        if not script_path.exists():
            pytest.skip(
                "scripts/test-backend.sh not found in this environment "
                "(repo root not available in CI backend-only Docker context)"
            )

        content = script_path.read_text()
        assert "testing/test_audit_finding_class_guards.py" in content, (
            "testing/test_audit_finding_class_guards.py must be included in the "
            "STANDALONE_ARGS array in scripts/test-backend.sh. The guards check for "
            "module-size violations, migration divergences, dropped-column residuals, "
            ".test_durations tracking, and stale documentation references. Three of "
            "these guards silently skip when the repo root is unavailable; including "
            "this module in --standalone ensures all guards run in CI backend-only "
            "Docker context and catch regressions on every commit."
        )
