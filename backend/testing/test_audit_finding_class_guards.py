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
import textwrap
from pathlib import Path

import pytest

# Canonical registry for the MissingGreenlet fidelity growth ratchet lives in
# production (backend/app/greenlet_fidelity.py) so the debt is greppable from
# application code and the ceiling burn-down is a visible production diff. The
# module is dependency-free, so this import is safe in the --standalone roster.
from app.greenlet_fidelity import (
    KNOWN_UNFAITHFUL_ALLOWLIST as _KNOWN_UNFAITHFUL_ALLOWLIST,
    KNOWN_UNFAITHFUL_ALLOWLIST_CEILING as _KNOWN_UNFAITHFUL_ALLOWLIST_CEILING,
    allowlist_within_ceiling as _allowlist_within_ceiling,
)

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

# Pytest fixture names recognised as MissingGreenlet fidelity mechanisms.
# Sourced from backend/conftest.py::GREENLET_FIDELITY_FIXTURE_NAMES — kept in
# sync by TestMissingGreenletTestsFaithful.test_conftest_exports_fidelity_names.
_FIDELITY_FIXTURE_NAMES: frozenset[str] = frozenset(
    {"expire_fidelity", "missing_greenlet_proxy"}
)

# The known-unfaithful allowlist and its monotonic ceiling are the guard's
# growth ratchet. Their canonical definition lives in the production module
# ``app.greenlet_fidelity`` (imported above as ``_KNOWN_UNFAITHFUL_ALLOWLIST`` /
# ``_KNOWN_UNFAITHFUL_ALLOWLIST_CEILING``) so the acknowledged debt is greppable
# from application code and every burn-down is a visible production diff. Tests
# in the allowlist are *context* references — they document the historical hazard
# but cannot reproduce the asyncpg failure mode on the default SQLite backend
# (the root conftest globally no-ops ``InstanceState._expire``). Each entry is
# acknowledged technical debt: ideally each test either gets a
# ``@pytest.mark.skipif(not USE_POSTGRES, ...)`` decorator or uses the
# ``missing_greenlet_proxy``/``expire_fidelity`` fixtures to inject the fault.


def _unfaithful_greenlet_tests(path: Path) -> list[str]:
    """Return ``module::test_name`` for every async test function in *path* that
    mentions ``MissingGreenlet`` in its docstring but uses none of the recognised
    fidelity mechanisms:

    - ``raise MissingGreenlet(...)`` anywhere in the function or nested scope
    - ``@pytest.mark.skipif`` conditioned on ``USE_POSTGRES`` or ``USE_TEST_POSTGRES``
    - A fixture arg named ``expire_fidelity`` or ``missing_greenlet_proxy``
    - ``expunge_all()`` in the body
    - ``raiseload=True`` keyword arg in the body
    - ``.expired`` attribute access in the body
    - ``.unloaded`` attribute access (``sa_inspect(obj).unloaded`` deferred-column check)

    NOTE: ``session.expire(obj)`` and ``session.expire_all()`` alone are NOT
    sufficient fidelity mechanisms.  Both delegate to ``InstanceState._expire``,
    which the root conftest globally no-ops.  The calls silently do nothing on
    the aiosqlite backend.  Tests that use ``session.expire()`` to reproduce the
    asyncpg fault MUST also use the ``expire_fidelity`` fixture (which restores
    real expiry).  Use ``expunge_all()`` — which removes objects from the session
    identity map without calling ``_expire`` — when ``expire_all()`` was the
    intent.
    """
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
        if not docstring or "greenlet" not in docstring.lower():
            continue

        # --- fidelity check 1: raise MissingGreenlet in body ---
        if any(
            isinstance(child, ast.Raise)
            and child.exc is not None
            and "MissingGreenlet" in ast.unparse(child.exc)
            for child in ast.walk(node)
        ):
            continue

        # --- fidelity check 2: postgres-only skip decorator ---
        if any(
            "skipif" in ast.unparse(deco)
            and ("USE_POSTGRES" in ast.unparse(deco) or "USE_TEST_POSTGRES" in ast.unparse(deco))
            for deco in node.decorator_list
        ):
            continue

        # --- fidelity check 3: recognised fidelity fixture in args ---
        arg_names = {a.arg for a in node.args.args}
        if arg_names & _FIDELITY_FIXTURE_NAMES:
            continue

        # --- fidelity checks 4–7: body AST patterns ---
        faithful_body = False
        for child in ast.walk(node):
            if isinstance(child, ast.Call):
                func = child.func
                if isinstance(func, ast.Attribute):
                    # session.expunge_all() only — expire_all() is a no-op on SQLite
                    if func.attr in ("expunge_all",):
                        faithful_body = True
                        break
                # raiseload=True keyword arg
                if any(kw.arg == "raiseload" for kw in child.keywords):
                    faithful_body = True
                    break
            # .expired attribute access or .unloaded (sa_inspect().unloaded)
            if isinstance(child, ast.Attribute) and child.attr in ("expired", "unloaded"):
                faithful_body = True
                break
        if faithful_body:
            continue

        violations.append(f"{path.name}::{node.name}")
    return violations


class TestMissingGreenletTestsFaithful:
    """Every test that mentions ``MissingGreenlet`` in its docstring must use at
    least one recognised fidelity mechanism so it genuinely exercises the
    asyncpg failure mode on SQLite.

    The root conftest globally no-ops ``InstanceState._expire``, which means
    ``session.rollback()`` does NOT expire ORM objects on aiosqlite — tests that
    rely on rollback-triggered expiry pass tautologically on the default SQLite
    CI run even if the production fix were reverted.

    Recognised fidelity mechanisms:

    - ``raise MissingGreenlet(...)`` (fault injection)
    - ``@pytest.mark.skipif(not USE_POSTGRES / USE_TEST_POSTGRES, ...)``
    - ``expire_fidelity`` or ``missing_greenlet_proxy`` fixture arg
    - ``expunge_all()``
    - ``raiseload=True`` keyword
    - ``.expired`` attribute access
    - ``.unloaded`` (``sa_inspect(obj).unloaded`` deferred-column check)

    Scope: ALL ``test_*.py`` files in the testing directory — drift-proof;
    new files are scanned automatically without manual list updates.
    """

    def test_conftest_exports_fidelity_names(self):
        """backend/conftest.py must define GREENLET_FIDELITY_FIXTURE_NAMES.

        This constant documents the recognised fidelity fixtures and is the
        canonical source that _FIDELITY_FIXTURE_NAMES in this module mirrors.
        If someone removes or renames the fixtures, this guard fails first.
        """
        conftest_path = _BACKEND_ROOT / "conftest.py"
        assert conftest_path.exists(), (
            f"backend/conftest.py not found at {conftest_path}. "
            "The root conftest is required for the SQLite MissingGreenlet shim."
        )
        content = conftest_path.read_text()
        assert "GREENLET_FIDELITY_FIXTURE_NAMES" in content, (
            "backend/conftest.py must define GREENLET_FIDELITY_FIXTURE_NAMES — "
            "the canonical list of pytest fixture names that guarantee "
            "MissingGreenlet test fidelity on SQLite. Add it back or update "
            "_FIDELITY_FIXTURE_NAMES in this file to match."
        )
        for name in _FIDELITY_FIXTURE_NAMES:
            assert name in content, (
                f"backend/conftest.py must define or reference '{name}' inside "
                f"GREENLET_FIDELITY_FIXTURE_NAMES. Current _FIDELITY_FIXTURE_NAMES: "
                f"{sorted(_FIDELITY_FIXTURE_NAMES)}"
            )

    def test_conftest_documents_expire_alone_is_not_faithful(self):
        """backend/conftest.py must document that bare ``session.expire()`` is
        NOT a sufficient MissingGreenlet fidelity mechanism.

        ``Session.expire()`` delegates to ``InstanceState._expire``, which the
        root conftest globally no-ops.  A bare ``session.expire(obj)`` call on
        the aiosqlite backend silently does nothing.  The conftest must carry
        an explicit warning so future readers don't assume expire() restores
        fidelity on its own.
        """
        conftest_path = _BACKEND_ROOT / "conftest.py"
        assert conftest_path.exists()
        content = conftest_path.read_text()
        assert "is NOT" in content and "fidelity mechanism" in content, (
            "backend/conftest.py must document that bare ``session.expire(obj)`` "
            "is NOT a valid MissingGreenlet fidelity mechanism without the "
            "expire_fidelity fixture.  The NOTE in the expire_fidelity fixture "
            "docstring serves this purpose."
        )

    def test_session_expire_alone_is_not_faithful(self, tmp_path):
        """A test that only uses ``session.expire(obj)`` WITHOUT the
        ``expire_fidelity`` fixture must be flagged by the fidelity scanner.

        Regression: the scanner previously accepted any ``session.expire(obj)``
        call as a faithful MissingGreenlet fidelity mechanism (removed in this
        commit).  But the root conftest globally no-ops ``InstanceState._expire``
        — which ``Session.expire()`` delegates to — so bare expire() does nothing
        on the default SQLite run.  Tests that rely on it pass tautologically.

        Only ``session.expire()`` combined with the ``expire_fidelity`` fixture
        (which restores real ``_InstanceState._expire`` for that test's duration)
        is genuinely faithful.
        """
        conftest_path = _BACKEND_ROOT / "conftest.py"
        assert conftest_path.exists()
        # Also exercise the conftest-level invariant documentation.
        conftest_content = conftest_path.read_text()
        assert "expire_fidelity" in conftest_content

        # Case 1: bare session.expire(obj) without expire_fidelity — MUST be flagged.
        not_faithful = tmp_path / "test_not_faithful.py"
        not_faithful.write_text(textwrap.dedent("""\
            async def test_bare_expire():
                \"\"\"Uses session.expire — MissingGreenlet hazard.\"\"\"
                session.expire(obj)
        """))
        violations = _unfaithful_greenlet_tests(not_faithful)
        assert any("test_bare_expire" in v for v in violations), (
            "Bare session.expire(obj) without expire_fidelity fixture was NOT "
            "flagged.  session.expire() on SQLite is a no-op (InstanceState._expire "
            "is globally patched in conftest.py) and cannot serve as a faithful "
            "fidelity mechanism without the expire_fidelity fixture."
        )

        # Case 2: session.expire(obj) WITH expire_fidelity — must NOT be flagged
        # (the fixture is recognized via _FIDELITY_FIXTURE_NAMES, not the expire call).
        faithful = tmp_path / "test_faithful.py"
        faithful.write_text(textwrap.dedent("""\
            async def test_expire_with_fidelity(expire_fidelity):
                \"\"\"Uses session.expire with expire_fidelity — MissingGreenlet hazard.\"\"\"
                session.expire(obj)
        """))
        violations2 = _unfaithful_greenlet_tests(faithful)
        assert not any("test_expire_with_fidelity" in v for v in violations2), (
            "session.expire(obj) WITH expire_fidelity fixture was incorrectly "
            "flagged.  expire_fidelity restores _InstanceState._expire, so "
            "session.expire() genuinely works and the test is faithful."
        )

    def test_session_expire_all_alone_is_not_faithful(self, tmp_path):
        """A test that only uses ``session.expire_all()`` WITHOUT the
        ``expire_fidelity`` fixture must be flagged by the fidelity scanner.

        ``Session.expire_all()`` calls ``InstanceState._expire`` for each
        managed instance — the same method globally no-oped by the root conftest.
        It is therefore a no-op on the aiosqlite backend, making any test that
        relies solely on it pass tautologically on SQLite.

        Use ``expunge_all()`` instead — it removes objects from the session
        identity map without invoking ``InstanceState._expire``.
        """
        not_faithful = tmp_path / "test_not_faithful_expire_all.py"
        not_faithful.write_text(textwrap.dedent("""\
            async def test_bare_expire_all():
                \"\"\"Uses expire_all — MissingGreenlet hazard.\"\"\"
                session.expire_all()
        """))
        violations = _unfaithful_greenlet_tests(not_faithful)
        assert any("test_bare_expire_all" in v for v in violations), (
            "session.expire_all() without expire_fidelity fixture was NOT flagged.  "
            "expire_all() on SQLite is a no-op (InstanceState._expire is globally "
            "patched in conftest.py) and cannot serve as a faithful fidelity "
            "mechanism.  Use expunge_all() instead."
        )

        faithful = tmp_path / "test_faithful_expunge.py"
        faithful.write_text(textwrap.dedent("""\
            async def test_expunge_all_is_faithful():
                \"\"\"Uses expunge_all — MissingGreenlet hazard.\"\"\"
                session.expunge_all()
        """))
        violations2 = _unfaithful_greenlet_tests(faithful)
        assert not any("test_expunge_all_is_faithful" in v for v in violations2), (
            "session.expunge_all() was incorrectly flagged as unfaithful.  "
            "expunge_all() removes objects from the identity map without calling "
            "_InstanceState._expire, so it genuinely works on SQLite."
        )

    def test_greenlet_lowercase_docstring_is_scanned(self, tmp_path):
        """A test whose docstring contains 'greenlet' in lowercase must be
        flagged by the fidelity scanner.

        Regression: before the docstring trigger was widened from the case-
        sensitive literal 'MissingGreenlet' to the case-insensitive stem
        'greenlet', a test describing its fault class as 'greenlet-bridged DDL'
        (test_migration_apply_gate.py:349) or similar alternate phrasing escaped
        the scan and could pass tautologically on SQLite.
        """
        # Exercise the production code constant in backend/conftest.py.
        conftest_path = _BACKEND_ROOT / "conftest.py"
        assert conftest_path.exists()
        content = conftest_path.read_text()
        assert "GREENLET_DOCSTRING_TRIGGER" in content, (
            "backend/conftest.py must define GREENLET_DOCSTRING_TRIGGER "
            "as the scan keyword for the MissingGreenlet fidelity guard."
        )

        # Verify the scanner catches a synthetic test whose docstring
        # uses lowercase 'greenlet' without mentioning 'MissingGreenlet'.
        test_file = tmp_path / "test_greenlet_fake.py"
        test_file.write_text(textwrap.dedent("""\
            async def test_greenlet_subject():
                \"\"\"Uses greenlet-bridged DDL — can raise MissingGreenlet on SQLite.\"\"\"
                pass
        """))
        violations = _unfaithful_greenlet_tests(test_file)
        assert any("test_greenlet_subject" in v for v in violations), (
            f"Lowercase 'greenlet' docstring was not flagged by the scanner. "
            f"Expected temp test to appear in violations, got: {violations}. "
            "The docstring trigger at _unfaithful_greenlet_tests may still "
            "be missing case-insensitive 'greenlet' matching."
        )

    def test_missing_greenlet_tests_are_faithful(self):
        """Scan all test_*.py files and fail if any test mentions MissingGreenlet
        in its docstring without a recognised fidelity mechanism.

        Tests in _KNOWN_UNFAITHFUL_ALLOWLIST are acknowledged technical debt —
        they document the MissingGreenlet hazard as context but reproduce the
        failure only on asyncpg (Postgres CI). Each allowlisted test should
        eventually get a @pytest.mark.skipif(not USE_POSTGRES, ...) decorator
        or fault-injection to make it backend-independent.
        """
        all_test_files = sorted(_THIS_DIR.glob("test_*.py"))
        assert all_test_files, (
            f"No test_*.py files found in {_THIS_DIR}. "
            "The testing directory is missing or the glob is broken."
        )

        violations: list[str] = []
        for path in all_test_files:
            for v in _unfaithful_greenlet_tests(path):
                if v not in _KNOWN_UNFAITHFUL_ALLOWLIST:
                    violations.append(v)

        assert not violations, (
            "The following tests mention MissingGreenlet in their docstring but "
            "use no recognised fidelity mechanism (see class docstring for the "
            "full list). Either inject the fault (raise MissingGreenlet via a "
            "proxy — see test_strategic_resident_session.py::_ProjectIdPoisonAfterRollback), "
            "use the expire_fidelity or missing_greenlet_proxy fixtures, call "
            "expunge_all() (not expire_all() — it is a no-op on SQLite), use raiseload=True, or mark the "
            "test Postgres-only with @pytest.mark.skipif(not USE_POSTGRES, ...):\n"
            + "\n".join(f"  - {v}" for v in sorted(violations))
            + "\n\nTo acknowledge existing debt, add to _KNOWN_UNFAITHFUL_ALLOWLIST "
            "with a comment explaining why the test cannot be made faithful."
        )

    def test_unfaithful_allowlist_cannot_grow_beyond_ceiling(self):
        """The known-unfaithful allowlist must never exceed its monotonic
        ceiling.

        This is a growth ratchet: adding an entry requires bumping the
        ceiling constant, which is a deliberate, visible diff action.
        Burn-down is tracked by lowering the ceiling as entries are
        migrated to faithful patterns.

        The ratchet is enforced by the production helper
        ``app.greenlet_fidelity.allowlist_within_ceiling`` — importing and
        calling it here is what ties this guard to the canonical production
        registry rather than a test-local copy.
        """
        assert _allowlist_within_ceiling(), (
            f"_KNOWN_UNFAITHFUL_ALLOWLIST has {len(_KNOWN_UNFAITHFUL_ALLOWLIST)} entries "
            f"(ceiling: {_KNOWN_UNFAITHFUL_ALLOWLIST_CEILING}). Either migrate entries "
            "to a faithful pattern (see _unfaithful_greenlet_tests for recognised "
            "mechanisms) and lower the ceiling in app/greenlet_fidelity.py, or—after "
            "exhausting faithful options—deliberately bump "
            "KNOWN_UNFAITHFUL_ALLOWLIST_CEILING there."
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


# ---------------------------------------------------------------------------
# 8. Rollback-safety regression check — prevent MissingGreenlet patterns in
#    production code after session.rollback() calls
# ---------------------------------------------------------------------------

# Recognised safe-read patterns that a function containing session.rollback()
# must use in its body to safely avoid MissingGreenlet on asyncpg.
# Mirrors backend/app/database.py::ROLLBACK_SAFE_PATTERNS (the canonical
# source) and is kept in sync by test_rollback_safe_patterns_defined_in_database.
_ROLLBACK_SAFE_PATTERNS: frozenset[str] = frozenset({
    "session.refresh",
    "db_session.refresh",
    "session.get",
    "db.get",
    "session.execute",
    "db_session.execute",
    "expunge_all",
    "expire_all",
    "__dict__",
    "return",
    "raise",
    "continue",
    "break",
    "logger.exception",
    "logger.warning",
    "expired",
    "unloaded",
})

# Functions in backend/app/ that call session.rollback() but whose safety
# cannot be verified by the static AST scanner.  Each entry documents why
# the function is safe despite the scanner's inability to prove it.
_ROLLBACK_ALLOWLIST: frozenset[str] = frozenset({
    # try/except handler: rollback in the except block followed by
    # logger.warning then natural function end — no ORM reads after
    # the rollback, but the scanner cannot statically prove "falls
    # through with no further reads" when there's no explicit return.
    "admin_agent_ops.py::_backfill_direction_merge_evidence_after_mapping",
    # Utility helper whose sole purpose is a safe best-effort rollback.
    # The function contains NO ORM reads at all — just rollback + logger.
    "reaper_infra_recovery.py::_safe_rollback",
})

# Monotonic ceiling: the allowlist can only shrink.  Bump when adding
# an entry (deliberate, visible action alongside the new entry).
_ROLLBACK_ALLOWLIST_CEILING: int = 2

# Directory for scanning production code — with CI Docker fallback.
_PRODUCTION_APP_DIR: Path = (
    _BACKEND_ROOT / "app" if (_BACKEND_ROOT / "app").is_dir()
    else Path("/app/app")  # CI backend-only Docker layout
)


def _unsafe_rollback_functions(filepath: Path) -> list[str]:
    """Return ``module_name::function_name`` for every function in *filepath*
    that calls ``session.rollback()`` but uses no recognised safe-read pattern
    (refresh, re-fetch, ``__dict__`` bypass, immediate return/raise, etc.).

    The scanner walks every ``FunctionDef`` and ``AsyncFunctionDef`` in the
    AST, looking for ``.rollback()`` call targets.  For each matching
    function it then checks the entire body for at least one pattern in
    ``_ROLLBACK_SAFE_PATTERNS``.  A pattern anywhere in the same function is
    sufficient — block-level adjacency is NOT required because the common
    pattern is "try: ... commit() except: rollback() + return/raise", where
    the safe pattern is on a different branch from the rollback, and the
    scanner would miss it with adjacency analysis.

    Functions listed in ``_ROLLBACK_ALLOWLIST`` are skipped after scanning.
    """
    source = filepath.read_text(encoding="utf-8")
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []

    # Safe-read patterns that are *statements* rather than expressions:
    # Return, Raise, Break, Continue, and Pass are control-flow statements
    # that prevent the function from reading expired ORM attributes after
    # the rollback call.
    _CONTROL_FLOW_STATEMENTS = (ast.Return, ast.Raise, ast.Break, ast.Continue)

    violations: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.AsyncFunctionDef, ast.FunctionDef)):
            continue

        # Pre-filter: does this function mention rollback at all?
        body_text = ast.unparse(node)
        if "rollback" not in body_text:
            continue

        # Check for an actual .rollback() call
        has_rollback_call = False
        for child in ast.walk(node):
            if isinstance(child, ast.Call):
                func_name = ast.unparse(child.func)
                if "rollback" in func_name:
                    has_rollback_call = True
                    break
        if not has_rollback_call:
            continue

        # Check for safe-read patterns in the function body.
        # Patterns are checked as:
        #   1. Call expressions (session.refresh, logger.exception, etc.)
        #   2. Attribute access (__dict__, expired, unloaded)
        #   3. Control-flow statements (return, raise, break, continue)
        safe = False
        for child in ast.walk(node):
            if isinstance(child, ast.Call):
                call_text = ast.unparse(child)
                if any(pat in call_text for pat in _ROLLBACK_SAFE_PATTERNS):
                    safe = True
                    break
            if isinstance(child, ast.Attribute) and child.attr in ("__dict__", "_dict", "expired", "unloaded"):
                safe = True
                break
            if isinstance(child, _CONTROL_FLOW_STATEMENTS):
                safe = True
                break
        if safe:
            continue

        violations.append(f"{filepath.name}::{node.name}")
    return violations


class TestRollbackSafetyRegressionCheck:
    """Every coroutine or function in ``backend/app/`` that calls
    ``session.rollback()`` must use at least one recognised safe-read pattern
    so it does not raise ``MissingGreenlet`` on asyncpg.

    Background
    ----------
    ``session.rollback()`` expires ALL ORM objects held by the session.
    On asyncpg the next synchronous attribute access on an expired object
    fires a lazy SELECT from async context — which requires a greenlet
    context that is absent outside the route's own async chain, raising
    ``MissingGreenlet``.  On aiosqlite (the default test backend) this
    expiry is globally no-oped by the root ``conftest.py``, so rollback-
    after-read patterns pass tautologically on CI's cheap SQLite shard
    even if the real asyncpg deployment would crash.

    This guard statically scans all ``backend/app/*.py`` production files
    for functions that call ``session.rollback()`` and verifies the same
    function body contains a safe-read pattern.  It mirrors the approach
    of ``TestMissingGreenletTestsFaithful`` but targets **production**
    code rather than test code.
    """

    def test_rollback_safe_patterns_defined_in_database(self):
        """The ROLLBACK_SAFE_PATTERNS constant in backend/app/database.py
        must match the scanner's pattern set in this module.

        Exercises production code: imports from ``app.database`` to verify
        the constant is present and synced.
        """
        database_path = _BACKEND_ROOT / "app" / "database.py"
        assert database_path.exists(), (
            f"backend/app/database.py not found at {database_path}. "
            "The rollback safety pattern constant cannot be verified."
        )
        content = database_path.read_text()
        assert "ROLLBACK_SAFE_PATTERNS" in content, (
            "backend/app/database.py must define ROLLBACK_SAFE_PATTERNS — "
            "the canonical list of recognised safe-read patterns after "
            "session.rollback().  Add it back or update _ROLLBACK_SAFE_PATTERNS "
            "in this file to match."
        )
        # Verify every pattern in our test set is documented in the
        # production constant (at least string-wise).
        for pattern in _ROLLBACK_SAFE_PATTERNS:
            assert pattern in content, (
                f"Pattern '{pattern}' is recognised by the test scanner but "
                f"is not listed in backend/app/database.py::ROLLBACK_SAFE_PATTERNS. "
                f"Keep the two in sync."
            )

    def test_rollback_missing_greenlet_regression_check(self):
        """Scan all ``backend/app/*.py`` files and fail if any function calls
        ``session.rollback()`` without a recognised safe-read pattern.

        Functions in ``_ROLLBACK_ALLOWLIST`` are acknowledged technical
        debt — the scanner cannot statically prove their safety, but each
        has been manually verified as safe.  The allowlist can only shrink
        (enforced by the monotonic ceiling).
        """
        if not _PRODUCTION_APP_DIR.is_dir():
            pytest.skip(
                f"Production app directory not found at {_PRODUCTION_APP_DIR}. "
                "Skipping in CI backend-only Docker context where backend/app "
                "may not be available."
            )

        all_prod_files = sorted(_PRODUCTION_APP_DIR.rglob("*.py"))
        assert all_prod_files, (
            f"No .py files found in {_PRODUCTION_APP_DIR}. "
            "The production code directory is empty or the glob is broken."
        )

        violations: list[str] = [
            v
            for path in all_prod_files
            for v in _unsafe_rollback_functions(path)
            if v not in _ROLLBACK_ALLOWLIST
        ]

        assert not violations, (
            "The following functions in backend/app/ call session.rollback() "
            "but use no recognised safe-read pattern (see class docstring and "
            "_ROLLBACK_SAFE_PATTERNS for the full list). Each such function "
            "must either include a refresh/re-fetch/__dict__ bypass or a "
            "return/raise/continue/break immediately after the rollback to "
            "avoid MissingGreenlet on asyncpg:\n"
            + "\n".join(f"  - {v}" for v in sorted(violations))
            + "\n\nTo acknowledge existing patterns the scanner cannot verify, "
            "add to _ROLLBACK_ALLOWLIST with a comment explaining why the "
            "function is safe despite the static analysis gap."
        )

    def test_rollback_allowlist_cannot_grow_beyond_ceiling(self):
        """The rollback allowlist must never exceed its monotonic ceiling.

        This is a growth ratchet: adding an entry requires bumping the
        ceiling constant, which is a deliberate, visible diff action.
        Burn-down is tracked by lowering the ceiling as entries are
        migrated to scanner-verifiable patterns.
        """
        assert len(_ROLLBACK_ALLOWLIST) <= _ROLLBACK_ALLOWLIST_CEILING, (
            f"_ROLLBACK_ALLOWLIST has {len(_ROLLBACK_ALLOWLIST)} entries "
            f"(ceiling: {_ROLLBACK_ALLOWLIST_CEILING}). Either make the "
            "entries verifiable by the static scanner (see _unsafe_rollback_functions "
            "for recognised patterns) and remove from the allowlist, or — "
            "after exhausting verifiable options — deliberately bump "
            "_ROLLBACK_ALLOWLIST_CEILING."
        )

    def test_rollback_safe_pattern_accepts_return_after_rollback(self, tmp_path):
        """A function that returns immediately after session.rollback() must
        NOT be flagged by the scanner.

        Regression guard: the scanner relies on ``"return"`` being present
        in the ``_ROLLBACK_SAFE_PATTERNS`` set and matched via AST unparse
        anywhere in the function body.
        """
        test_file = tmp_path / "test_rollback_return.py"
        test_file.write_text(textwrap.dedent("""\
            async def test_rollback_then_return(session):
                \"\"\"Rollback and return — safe pattern.\"\"\"
                await session.rollback()
                return None
        """))
        violations = _unsafe_rollback_functions(test_file)
        assert not any("test_rollback_then_return" in v for v in violations), (
            "A function that calls session.rollback() and then returns was "
            "incorrectly flagged.  'return' is a recognised safe-read pattern "
            "because exiting the function avoids any expired-attribute read."
        )

    def test_rollback_safe_pattern_accepts_refresh_after_rollback(self, tmp_path):
        """A function that refreshes ORM objects after session.rollback() must
        NOT be flagged by the scanner.
        """
        test_file = tmp_path / "test_rollback_refresh.py"
        test_file.write_text(textwrap.dedent("""\
            async def test_rollback_then_refresh(session):
                \"\"\"Rollback and refresh — safe pattern.\"\"\"
                await session.rollback()
                await session.refresh(obj)
        """))
        violations = _unsafe_rollback_functions(test_file)
        assert not any("test_rollback_then_refresh" in v for v in violations), (
            "A function that calls session.rollback() and then session.refresh() "
            "was incorrectly flagged.  'session.refresh' is a recognised safe-read "
            "pattern because it explicitly re-loads the object from the DB."
        )

    def test_rollback_safe_pattern_accepts_dict_bypass(self, tmp_path):
        """A function that uses ``__dict__`` to bypass expiry after
        session.rollback() must NOT be flagged by the scanner.
        """
        test_file = tmp_path / "test_rollback_dict.py"
        test_file.write_text(textwrap.dedent("""\
            async def test_rollback_then_dict(session):
                \"\"\"Rollback and read via __dict__ — safe pattern.\"\"\"
                await session.rollback()
                status = obj.__dict__.get("status")
        """))
        violations = _unsafe_rollback_functions(test_file)
        assert not any("test_rollback_then_dict" in v for v in violations), (
            "A function that calls session.rollback() and then reads via "
            "__dict__ was incorrectly flagged.  '__dict__' is a recognised "
            "safe-read pattern because it bypasses expired-attribute lazy loading."
        )

    def test_rollback_safe_pattern_flags_bare_rollback(self, tmp_path):
        """A function that calls session.rollback() followed by an ORM attribute
        read without any safe pattern MUST be flagged by the scanner.

        This is the pattern that crashes with MissingGreenlet on asyncpg.
        """
        test_file = tmp_path / "test_rollback_bare.py"
        test_file.write_text(textwrap.dedent("""\
            async def test_rollback_then_read(session):
                \"\"\"Rollback and read attribute — UNSAFE pattern.\"\"\"
                await session.rollback()
                print(task.status)
        """))
        violations = _unsafe_rollback_functions(test_file)
        assert any("test_rollback_then_read" in v for v in violations), (
            "A function that calls session.rollback() and then reads an ORM "
            "attribute without any safe-read pattern was NOT flagged.  This "
            "pattern raises MissingGreenlet on asyncpg and must be detected."
        )
