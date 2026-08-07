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

4. **.test_durations freshness** — ``backend/.test_durations`` is the committed
   pytest-split duration cache that enables time-balanced CI shard splitting.
   ``TestTestDurationsCommittedAndFresh`` asserts it is NOT in ``.gitignore``,
   IS tracked by git, is valid JSON mapping node IDs to positive floats, contains
   at least a substantial minimum number of entries, and that sampled test file
   prefixes still exist on disk (staleness ratchet).

5. **Stale documentation references** — long-lived documentation in /docs/ and
   /docs/plans/ must not reference removed code paths or UI components.
   ``TestLongLivedDocsDoNotReferenceRemovedCodePaths`` scans .md files and
   verifies none contain references to components in the removed-code registry.

6. **SQLite-masked MissingGreenlet tests** — tests that mention ``MissingGreenlet``
   in their docstring but neither raise it (fault injection) nor skip on SQLite
   (``skipif(not USE_POSTGRES)``) pass tautologically on the default aiosqlite
   CI run. ``TestMissingGreenletTestsFaithful`` AST-scans every ``test_*.py``
   file in the testing directory for unfaithful instances.

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
    ROLLBACK_FLOW_ALLOWLIST as _ROLLBACK_FLOW_ALLOWLIST,
    ROLLBACK_FLOW_ALLOWLIST_CEILING as _ROLLBACK_FLOW_ALLOWLIST_CEILING,
    rollback_flow_allowlist_within_ceiling as _rollback_flow_allowlist_within_ceiling,
    COMMIT_FLOW_ALLOWLIST as _COMMIT_FLOW_ALLOWLIST,
    COMMIT_FLOW_ALLOWLIST_CEILING as _COMMIT_FLOW_ALLOWLIST_CEILING,
    commit_flow_allowlist_within_ceiling as _commit_flow_allowlist_within_ceiling,
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
# 4. .test_durations freshness — must be committed, valid JSON, and not stale
# ---------------------------------------------------------------------------

# Minimum number of entries the committed durations file must contain.
# Set safely below the current collection size (~10k nodes) so the threshold
# does not need to be bumped on every minor test addition, but high enough to
# catch an accidentally emptied or truncated file.
_TEST_DURATIONS_MIN_ENTRIES = 5000

# When sampling committed node-ID keys for staleness, require at least this
# fraction of sampled keys to have their test file still present on disk.
_TEST_DURATIONS_STALE_TOLERANCE = 0.80


class TestTestDurationsCommittedAndFresh:
    """``backend/.test_durations`` is the committed pytest-split duration cache
    that enables time-balanced CI shard splitting.

    Guards:
    - ``.gitignore`` must NOT list the ``backend/.test_durations`` pattern.
    - The file must be tracked in git (``git ls-files`` must list it).
    - The file must be valid JSON mapping strings to positive floats.
    - The file must contain at least ``_TEST_DURATIONS_MIN_ENTRIES`` entries.
    - A sample of committed node-ID keys must still have their test files on disk.

    If the staleness check fails, regenerate with:
        bash scripts/store-backend-durations.sh
    then commit the updated backend/.test_durations.

    Skipped when the repo root is unavailable (CI backend-only Docker context).
    """

    def test_test_durations_committed_valid_and_covers_collection(self):
        """backend/.test_durations must be tracked, valid JSON, and cover the test collection."""
        import json
        import random

        gitignore_path = _REPO_ROOT / ".gitignore"
        durations_path = _BACKEND_ROOT / ".test_durations"

        if not _repo_root_available():
            pytest.skip(
                "repo root .git not available in this test environment "
                "(CI docker context is ./backend; run locally or via "
                "scripts/test-backend.sh from a checkout for enforcement)"
            )

        # (a) .gitignore must NOT contain the pattern
        if gitignore_path.exists():
            lines = gitignore_path.read_text().splitlines()
            assert "backend/.test_durations" not in lines, (
                ".gitignore still lists 'backend/.test_durations'. "
                "Remove the line so the committed durations file can be tracked. "
                "Time-balanced CI shard splitting requires this file to be committed."
            )

        # (b) File must be tracked by git
        result = subprocess.run(
            ["git", "ls-files", "backend/.test_durations"],
            cwd=str(_REPO_ROOT),
            capture_output=True,
            text=True,
        )
        tracked_paths = [line.strip() for line in result.stdout.splitlines() if line.strip()]
        assert tracked_paths, (
            "backend/.test_durations is not tracked in git. "
            "Commit it so CI shards can auto-detect it for time-based splitting. "
            "Generate with: bash scripts/store-backend-durations.sh"
        )

        # (c) File must exist on disk and be valid JSON mapping str→positive float
        assert durations_path.exists(), (
            "backend/.test_durations does not exist on disk even though git reports it tracked. "
            "Run: bash scripts/store-backend-durations.sh && git add backend/.test_durations"
        )
        try:
            with durations_path.open() as f:
                data = json.load(f)
        except json.JSONDecodeError as exc:
            pytest.fail(
                f"backend/.test_durations is not valid JSON: {exc}\n"
                "Regenerate with: bash scripts/store-backend-durations.sh"
            )
        assert isinstance(data, dict), (
            "backend/.test_durations must be a JSON object mapping node IDs to durations."
        )
        bad_values = [
            (k, v) for k, v in list(data.items())[:20]
            if not isinstance(v, (int, float)) or v <= 0
        ]
        assert not bad_values, (
            f"backend/.test_durations has entries with non-positive or non-numeric durations: "
            f"{bad_values[:5]}. All values must be positive floats."
        )

        # (d) Non-emptiness / coverage floor
        assert len(data) >= _TEST_DURATIONS_MIN_ENTRIES, (
            f"backend/.test_durations has only {len(data)} entries "
            f"(minimum: {_TEST_DURATIONS_MIN_ENTRIES}). "
            "The file appears truncated or was generated from an incomplete collection. "
            "Regenerate with: bash scripts/store-backend-durations.sh"
        )

        # (e) Cheap staleness check: sample committed node-ID keys and verify
        #     their test file prefixes still exist on disk.
        keys = list(data.keys())
        sample_size = min(200, len(keys))
        sample = random.sample(keys, sample_size)

        missing_files: list[str] = []
        for key in sample:
            # Node ID format: testing/test_foo.py::... or testing/test_foo.py::Class::method
            parts = key.split("::")
            file_part = parts[0]
            if not file_part.startswith("testing/"):
                continue
            test_file = _BACKEND_ROOT / file_part
            if not test_file.exists():
                missing_files.append(file_part)

        missing_ratio = len(missing_files) / max(sample_size, 1)
        assert missing_ratio <= (1.0 - _TEST_DURATIONS_STALE_TOLERANCE), (
            f"backend/.test_durations appears stale: {len(missing_files)}/{sample_size} "
            f"sampled node-ID keys point at test files that no longer exist on disk. "
            f"Missing (up to 10): {missing_files[:10]}\n"
            "Regenerate with: bash scripts/store-backend-durations.sh && "
            "git add backend/.test_durations && git commit"
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
# (the root conftest no-ops ``InstanceState._expire`` on the aiosqlite backend). Each entry is
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
    which the root conftest no-ops on the aiosqlite backend.  The calls silently do nothing on
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

        # --- fidelity checks 4-7: body AST patterns ---
        faithful_body = False
        for child in ast.walk(node):
            if isinstance(child, ast.Call):
                func = child.func
                # session.expunge_all() only — expire_all() is a no-op on SQLite
                if isinstance(func, ast.Attribute) and func.attr in ("expunge_all",):
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

    The root conftest no-ops ``InstanceState._expire`` on the aiosqlite backend,
    which means ``session.rollback()`` does NOT expire ORM objects on aiosqlite —
    tests that rely on rollback-triggered expiry pass tautologically on the default
    SQLite CI run even if the production fix were reverted.

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
        root conftest no-ops on the aiosqlite backend.  A bare ``session.expire(obj)`` call on
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
        commit).  But the root conftest no-ops ``InstanceState._expire`` on the
        aiosqlite backend — which ``Session.expire()`` delegates to — so bare
        expire() does nothing on the default SQLite run.  Tests that rely on it pass tautologically.

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
        managed instance — the same method no-oped by the root conftest on the
        aiosqlite backend.  It is therefore a no-op on the aiosqlite backend, making any test that
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
            "expire_all() on aiosqlite is a no-op (InstanceState._expire is "
            "patched in conftest.py on the aiosqlite backend) and cannot serve as a faithful fidelity "
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

        violations: list[str] = [
            v
            for path in all_test_files
            for v in _unfaithful_greenlet_tests(path)
            if v not in _KNOWN_UNFAITHFUL_ALLOWLIST
        ]

        assert not violations, (
            "The following tests mention MissingGreenlet in their docstring but "
            "use no recognised fidelity mechanism (see class docstring for the "
            "full list). Either inject the fault (raise MissingGreenlet via a "
            "proxy that poisons an expired attribute after rollback), "
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
    dropped columns, .test_durations freshness, and stale documentation
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
            TestTestDurationsCommittedAndFresh,
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
            ".test_durations freshness, and stale documentation references. Three of "
            "these guards silently skip when the repo root is unavailable; including "
            "this module in --standalone ensures all guards run in CI backend-only "
            "Docker context and catch regressions on every commit."
        )


# ---------------------------------------------------------------------------
# 8. Rollback-safety regression check — prevent MissingGreenlet patterns in
#    production code after session.rollback() calls
# ---------------------------------------------------------------------------

# Directory for scanning production code — with CI Docker fallback.
_PRODUCTION_APP_DIR: Path = (
    _BACKEND_ROOT / "app" if (_BACKEND_ROOT / "app").is_dir()
    else Path("/app/app")  # CI backend-only Docker layout
)


def _build_parent_map(tree: ast.AST) -> dict:
    """Return a mapping from id(child) → parent node for every node in tree."""
    parents: dict = {}
    for node in ast.walk(tree):
        for child in ast.iter_child_nodes(node):
            parents[id(child)] = node
    return parents


def _scan_expiry_flow_functions(
    filepath: Path, *, call_attr: str, error_path_only: bool
) -> list[str]:
    """Flow-aware scanner for post-expiry ORM reads (shared machinery).

    ``session.rollback()`` and ``session.commit()`` are both expiry triggers:
    each expires ALL ORM objects held by the session, so the next synchronous
    attribute access lazy-loads from async context and raises
    ``MissingGreenlet`` on asyncpg.  This scanner performs per-call post-path
    analysis: for each expiry call (``session.<call_attr>()``) it walks UP the
    AST from the call site, checking each containing block's *continuation*
    (statements that follow the call on the same execution path) for a safe
    terminator (return/raise/break/continue) or a known-safe refetch call via
    ``_classify_stmt`` and ``_post_expiry_is_flow_safe``.

    A control-flow token is only counted as safe when it appears AFTER the
    call in its own containing block.  A ``return True`` at function level does
    NOT make an expiry call inside an except handler safe when the handler
    falls through to statements that read expired ORM attributes.

    ``call_attr``
        ``"rollback"`` or ``"commit"`` — the session method whose call sites
        are analysed.
    ``error_path_only``
        When ``True`` (used for ``commit``), only call sites lexically inside
        an ``except`` handler body or a ``try/finally`` finalbody are
        considered.  A blanket ``commit`` gate is infeasible (~hundreds of
        call sites), so the commit guard narrows to the tractable, high-signal
        error-path subset.  When ``False`` (used for ``rollback``) every call
        site is analysed.

    The production regression checks (``test_rollback_missing_greenlet_regression_check``
    and ``test_commit_missing_greenlet_regression_check``) run this scanner
    against all of ``backend/app/``.  Functions the scanner cannot statically
    verify as safe (re-fetch through a helper, cross-session reads, or other
    patterns beyond the scanner's AST model) are acknowledged in
    ``ROLLBACK_FLOW_ALLOWLIST`` / ``COMMIT_FLOW_ALLOWLIST`` in
    ``backend/app/greenlet_fidelity.py``.
    """
    source = filepath.read_text(encoding="utf-8")
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []

    # Refetch patterns from ROLLBACK_SAFE_REFETCH_PATTERNS in database.py.
    _REFETCH_PATS: frozenset[str] = frozenset({
        "session.refresh", "db_session.refresh", "session.get", "db.get",
        "session.execute", "db_session.execute", "expunge_all", "expire_all",
    })
    _SAFE_NAMES: frozenset[str] = frozenset({
        # Session / DB handles
        'session', 'db_session', 'db',
        # Stdlib / logging / util
        'logger', 'logging', 'os', 'sys', 're', 'json', 'datetime',
        'contextlib', 'asyncio', 'uuid', 'time', 'math', 'traceback',
        # Common exception variable names
        'exc', 'e', 'error', 'ex', 'err', '_exc',
        # OOP patterns
        'self', 'cls',
        # Module-level observability / config singletons (never ORM objects)
        '_OBSERVABILITY', '_ws_manager', '_runtime_config', '_reaper_halt_state',
        '_dispatch_prep_contention_streak', '_warm_ctx', '_shared_state',
        # WakeDeliveryResult dataclass (reaper wakes, not an ORM model)
        '_wake_result',
        # Pre-captured string sentinels (avoid MissingGreenlet in f-strings)
        '_task_id_str', '_task_project_id_str', '_task_type_str',
        # Bool sentinel for deferred refresh pattern
        '_attrs_may_be_expired',
        # Module-level loop-state counters / dicts
        'ci_loop',
        # Enum / Pydantic / plain-dict / local-accumulator variable names
        'severity', 'body', 'payload', 'container', 'alert_session', 'doc',
        'runtime_selection', 'results',
    })

    # Compound AST statement types whose sub-statements are sequenced internally:
    # a refetch at the start of the block makes later attribute reads in the same
    # block safe.  The tightened refetch-vs-orm-read check only applies to SIMPLE
    # (non-compound) statements where both appear in the same flat expression.
    # FunctionDef / AsyncFunctionDef are treated as compound so that a nested
    # function definition containing a refetch does not mask ORM reads elsewhere
    # in its body when the definition itself is a successor of the rollback.
    _COMPOUND_STMT_TYPES = (
        ast.If, ast.Try, ast.For, ast.While, ast.With,
        ast.AsyncFor, ast.AsyncWith, ast.FunctionDef, ast.AsyncFunctionDef,
    )

    def _classify_stmt(suc: ast.stmt) -> int:
        """Return 1 (safe-terminator), -1 (bare ORM read), or 0 (neutral).

        A *refetch* (``session.execute/refresh/get``, ``expire_all``, etc.)
        makes the statement safe only when the same simple expression contains
        no ORM attribute reads on an *unrelated* object that the refetch does
        not reload.  For compound blocks (``if``/``try``/nested function defs)
        the original behaviour is preserved: a refetch anywhere in the block
        marks the block as safe, because the block internally sequences the
        fetch before any attribute reads on the returned object.

        Attributes whose name starts with an uppercase letter (e.g.
        ``module.ClassName``) are excluded from the ORM-read check — SQLAlchemy
        column names are always snake_case, so an uppercase attr is a class or
        type reference, never a mapped column access.
        """
        if isinstance(suc, (ast.Return, ast.Raise, ast.Break, ast.Continue)):
            return 1
        method_funcs = {
            id(n.func) for n in ast.walk(suc)
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
        }
        has_refetch = False
        has_orm_read = False
        for n in ast.walk(suc):
            if isinstance(n, ast.Call):
                if any(p in ast.unparse(n) for p in _REFETCH_PATS):
                    has_refetch = True
            elif isinstance(n, ast.Attribute):
                if n.attr in ('__dict__', '_dict', 'expired', 'unloaded'):
                    has_refetch = True
                elif (id(n) not in method_funcs
                      and isinstance(n.value, ast.Name)
                      and n.value.id not in _SAFE_NAMES
                      and not n.value.id[0].isupper()
                      and not n.attr.startswith('__')
                      and not n.attr[0].isupper()):
                    has_orm_read = True
        # For compound blocks a refetch anywhere in the block marks it safe
        # (the block sequences fetch before read).  For simple expressions a
        # refetch is only safe when no unrelated expired ORM attribute appears
        # in the same expression — a co-located refetch does NOT re-load a
        # different expired object passed as an argument to the refetch call.
        is_compound = isinstance(suc, _COMPOUND_STMT_TYPES)
        if has_refetch and (is_compound or not has_orm_read):
            return 1
        if has_orm_read:
            return -1
        return 0

    parents = _build_parent_map(tree)

    def _post_expiry_is_flow_safe(expiry_call: ast.AST, func_node: ast.AST) -> bool:
        """True if the post-expiry path has a safe terminator before ORM reads.

        Identical machinery for ``rollback`` and ``commit`` call sites: walk up
        the containing blocks from the call and inspect each block's
        continuation via ``_classify_stmt``.  The walk naturally continues past
        the enclosing ``except`` handler / ``try`` into the function-level
        fall-through, so an error-path ``commit`` whose except handler falls
        through to an ORM read is flagged exactly like the rollback case.
        """
        cur: ast.AST = expiry_call
        while id(cur) in parents:
            par = parents[id(cur)]
            found_block = None
            found_idx = -1
            for attr in ('body', 'orelse', 'finalbody'):
                blk = getattr(par, attr, None)
                if isinstance(blk, list):
                    for i, s in enumerate(blk):
                        if s is cur:
                            found_block, found_idx = blk, i
                            break
                if found_block is not None:
                    break
            if found_block is None and isinstance(par, ast.ExceptHandler):
                for i, s in enumerate(par.body):
                    if s is cur:
                        found_block, found_idx = par.body, i
                        break
            if found_block is not None:
                for suc in found_block[found_idx + 1:]:
                    verdict = _classify_stmt(suc)
                    if verdict == 1:
                        return True
                    if verdict == -1:
                        return False
                if par is func_node:
                    return True  # reached function end with no ORM reads → safe
            cur = par
        return True

    def _call_is_on_error_path(call: ast.AST, func_node: ast.AST) -> bool:
        """True if *call* is lexically inside an ``except`` handler body or a
        ``try/finally`` finalbody within *func_node*.

        This narrows the commit guard to the tractable error-path subset — the
        branches most likely to read stale ORM state after committing while an
        exception is in flight or a resource is being finalised.
        """
        cur: ast.AST = call
        while id(cur) in parents and cur is not func_node:
            par = parents[id(cur)]
            if isinstance(par, ast.ExceptHandler) and any(
                s is cur for s in par.body
            ):
                return True
            if isinstance(par, ast.Try) and any(s is cur for s in par.finalbody):
                return True
            cur = par
        return False

    violations: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.AsyncFunctionDef, ast.FunctionDef)):
            continue
        expiry_calls = [
            child for child in ast.walk(node)
            if isinstance(child, ast.Call)
            and isinstance(child.func, ast.Attribute)
            and child.func.attr == call_attr
        ]
        if error_path_only:
            expiry_calls = [
                c for c in expiry_calls if _call_is_on_error_path(c, node)
            ]
        if not expiry_calls:
            continue
        if any(not _post_expiry_is_flow_safe(c, node) for c in expiry_calls):
            violations.append(f"{filepath.name}::{node.name}")
    return violations


def _unsafe_rollback_functions_flow_aware(filepath: Path) -> list[str]:
    """Flow-aware rollback scanner — every ``session.rollback()`` call site.

    Thin wrapper over :func:`_scan_expiry_flow_functions`.  See that function
    for the post-path analysis; acknowledged cases are in
    ``ROLLBACK_FLOW_ALLOWLIST`` in ``backend/app/greenlet_fidelity.py``.
    """
    return _scan_expiry_flow_functions(
        filepath, call_attr="rollback", error_path_only=False
    )


def _unsafe_error_path_commit_functions_flow_aware(filepath: Path) -> list[str]:
    """Flow-aware commit scanner — the ERROR-PATH ``session.commit()`` subset.

    ``session.commit()`` is the primary expiry trigger (``expire_on_commit``
    semantics), but a blanket commit gate is infeasible (~hundreds of call
    sites).  This scanner narrows to ``commit`` calls lexically inside an
    ``except`` handler or a ``try/finally`` finalbody — the high-signal
    error-path subset — and flags those whose post-commit continuation reads an
    expired ORM attribute, reusing the exact same ``_classify_stmt`` /
    ``_post_expiry_is_flow_safe`` machinery as the rollback scanner.

    Acknowledged cases are in ``COMMIT_FLOW_ALLOWLIST`` in
    ``backend/app/greenlet_fidelity.py``.
    """
    return _scan_expiry_flow_functions(
        filepath, call_attr="commit", error_path_only=True
    )


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
    expiry is no-oped by the root ``conftest.py`` on the aiosqlite backend, so rollback-
    after-read patterns pass tautologically on CI's cheap SQLite shard
    even if the real asyncpg deployment would crash.

    This guard statically scans all ``backend/app/*.py`` production files
    for functions that call ``session.rollback()`` and verifies the same
    function body contains a safe-read pattern.  It mirrors the approach
    of ``TestMissingGreenletTestsFaithful`` but targets **production**
    code rather than test code.
    """

    def test_rollback_safe_patterns_defined_in_database(self):
        """The ROLLBACK_SAFE_PATTERNS and ROLLBACK_SAFE_REFETCH_PATTERNS constants
        in backend/app/database.py must be present.

        Exercises production code: reads database.py to verify both constants
        are defined.  ROLLBACK_SAFE_PATTERNS is the canonical list of all
        recognised safe patterns; ROLLBACK_SAFE_REFETCH_PATTERNS is the subset
        that unconditionally re-loads ORM state regardless of position (as
        opposed to control-flow tokens that are only safe in-block).
        """
        database_path = _BACKEND_ROOT / "app" / "database.py"
        assert database_path.exists(), (
            f"backend/app/database.py not found at {database_path}. "
            "The rollback safety pattern constants cannot be verified."
        )
        content = database_path.read_text()
        assert "ROLLBACK_SAFE_PATTERNS" in content, (
            "backend/app/database.py must define ROLLBACK_SAFE_PATTERNS — "
            "the canonical list of recognised safe-read patterns after "
            "session.rollback().  Restore it or update this test to match."
        )
        assert "ROLLBACK_SAFE_REFETCH_PATTERNS" in content, (
            "backend/app/database.py must define ROLLBACK_SAFE_REFETCH_PATTERNS — "
            "the subset of ROLLBACK_SAFE_PATTERNS that are unconditionally safe "
            "(refetch operations) regardless of position relative to the rollback. "
            "The flow-aware scanner uses this to distinguish refetch patterns from "
            "control-flow tokens that are only safe in the same block."
        )

    def test_rollback_missing_greenlet_regression_check(self):
        """Scan all ``backend/app/*.py`` files using the flow-aware scanner and
        fail if any function calls ``session.rollback()`` with an unsafe
        post-rollback ORM attribute read on the reachable path.

        Functions in ``_ROLLBACK_FLOW_ALLOWLIST`` (imported from
        ``app.greenlet_fidelity``) are acknowledged cases the scanner cannot
        statically verify — each entry has been manually triaged and confirmed
        safe.  The allowlist can only shrink (enforced by the monotonic ceiling
        in ``test_rollback_flow_allowlist_cannot_grow_beyond_ceiling``).
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
            for v in _unsafe_rollback_functions_flow_aware(path)
            if v not in _ROLLBACK_FLOW_ALLOWLIST
        ]

        assert not violations, (
            "The following functions in backend/app/ call session.rollback() "
            "with a reachable post-rollback ORM attribute read not covered by "
            "a safe terminator or refetch.  Each such function must either: "
            "use session.refresh/session.get/__dict__ after rollback, add an "
            "explicit return/raise/continue immediately after rollback in the "
            "same block, or snapshot ORM values before rollback.  "
            "To acknowledge cases the static scanner cannot verify, add an "
            "entry to ROLLBACK_FLOW_ALLOWLIST in backend/app/greenlet_fidelity.py "
            "with a comment explaining why the function is safe:\n"
            + "\n".join(f"  - {v}" for v in sorted(violations))
        )

    def test_rollback_flow_allowlist_cannot_grow_beyond_ceiling(self):
        """The rollback-flow allowlist must never exceed its monotonic ceiling.

        This is a growth ratchet: adding an entry to ROLLBACK_FLOW_ALLOWLIST
        requires bumping ROLLBACK_FLOW_ALLOWLIST_CEILING in
        backend/app/greenlet_fidelity.py — a deliberate, visible diff action.
        Burn-down is tracked by lowering the ceiling as entries are fixed in
        the production code.
        """
        assert _rollback_flow_allowlist_within_ceiling(), (
            f"ROLLBACK_FLOW_ALLOWLIST has {len(_ROLLBACK_FLOW_ALLOWLIST)} entries "
            f"(ceiling: {_ROLLBACK_FLOW_ALLOWLIST_CEILING}). Either fix the "
            "production function so the flow-aware scanner can verify it "
            "(use session.refresh/session.get after rollback, or snapshot ORM "
            "values before rollback) and remove from the allowlist, or — after "
            "exhausting verifiable options — deliberately bump "
            "ROLLBACK_FLOW_ALLOWLIST_CEILING in backend/app/greenlet_fidelity.py."
        )

    def test_rollback_scanner_flags_read_after_rollback_despite_unrelated_safe_token(
        self, tmp_path
    ):
        """A rollback in an except handler that falls through to an ORM read
        MUST be flagged even when an unrelated ``return`` appears later in the
        function body.

        The old whole-function scan accepted this shape because it found
        ``return True`` anywhere in the function and marked the function safe.
        The flow-aware scanner must detect that ``task.status`` is reachable
        after the rollback (the except handler has no terminator) and flag it.

        Exercises ``ROLLBACK_SAFE_REFETCH_PATTERNS`` in ``backend/app/database.py``:
        the production constant documents which patterns constitute a genuine
        safe refetch vs a control-flow token that is only safe in-block.
        """
        database_path = _BACKEND_ROOT / "app" / "database.py"
        assert database_path.exists(), "backend/app/database.py not found"
        db_content = database_path.read_text()
        assert "ROLLBACK_SAFE_REFETCH_PATTERNS" in db_content, (
            "backend/app/database.py must define ROLLBACK_SAFE_REFETCH_PATTERNS — "
            "the subset of safe patterns that are unconditionally safe (refetch "
            "operations) as opposed to control-flow tokens whose safety depends "
            "on their position relative to the rollback call."
        )

        test_file = tmp_path / "test_dangerous_fallthrough.py"
        test_file.write_text(textwrap.dedent("""\
            async def dangerous_rollback_with_unrelated_return(session, task):
                \"\"\"Rollback in except, ORM read after try — UNSAFE.\"\"\"
                try:
                    await session.commit()
                except Exception:
                    await session.rollback()
                    logging.warning("rolled back")
                # task is expired here; accessing .status raises MissingGreenlet
                print(task.status)
                return True  # unrelated safe token that fooled the old scanner
        """))
        violations = _unsafe_rollback_functions_flow_aware(test_file)
        assert any("dangerous_rollback_with_unrelated_return" in v for v in violations), (
            "A function whose rollback is in an except handler that falls through "
            "to an ORM attribute read (task.status) was NOT flagged by the flow-aware "
            "scanner, even though the post-rollback path reaches the read before any "
            "safe terminator.  The flow-aware scanner must check terminator position "
            "relative to the rollback, not just presence anywhere in the function body."
        )

    def test_rollback_scanner_flags_unrelated_expired_read_beside_refetch(
        self, tmp_path
    ):
        """A post-rollback ``session.execute`` does NOT make the same statement
        safe when it also reads an expired ORM attribute on a *different* object
        that the execute does not reload.

        Pattern caught: ``attest_criterion`` (pre-fix) called
        ``session.execute(sa_update(X).where(X.criterion_id == row.id))``
        after rollback.  The execute is an UPDATE — it does not re-load ``row``.
        ``row`` is expired; ``row.id`` in the WHERE clause raises
        ``MissingGreenlet`` on asyncpg before the execute even runs.

        The old whole-statement check returned 1 (safe) whenever a refetch
        pattern appeared anywhere in the statement, even as a sibling expression
        to an unrelated expired attribute read.  The flow-aware scanner must
        return -1 (unsafe) for this shape.

        Exercises ``_classify_stmt`` inside ``_unsafe_rollback_functions_flow_aware``
        and the ``ROLLBACK_SAFE_REFETCH_PATTERNS`` constant in
        ``backend/app/database.py`` (which documents that a refetch is only safe
        when it actually re-loads the object whose attributes are subsequently
        read).
        """
        database_path = _BACKEND_ROOT / "app" / "database.py"
        assert database_path.exists(), "backend/app/database.py not found"
        db_content = database_path.read_text()
        assert "ROLLBACK_SAFE_REFETCH_PATTERNS" in db_content, (
            "backend/app/database.py must define ROLLBACK_SAFE_REFETCH_PATTERNS — "
            "the subset of safe patterns that are unconditionally safe (refetch "
            "operations) as opposed to control-flow tokens whose safety depends "
            "on their position relative to the rollback call."
        )

        test_file = tmp_path / "test_refetch_beside_expired_read.py"
        test_file.write_text(textwrap.dedent("""\
            async def unsafe_execute_with_unrelated_expired_read(
                session, row, CriterionEvidence, sa_update
            ):
                \"\"\"session.execute(UPDATE ... WHERE x == row.id) after rollback.

                row is expired; row.id in the WHERE clause raises MissingGreenlet
                on asyncpg before the execute runs.  The refetch (session.execute)
                does NOT re-load row — it performs an UPDATE, so it is NOT a safe
                refetch for row.  This must be flagged as unsafe.
                \"\"\"
                try:
                    await session.commit()
                except Exception:
                    await session.rollback()
                # UNSAFE: row is expired; row.id is read as an argument to
                # session.execute, which does not re-load row.
                await session.execute(
                    sa_update(CriterionEvidence).where(
                        CriterionEvidence.criterion_id == row.id
                    )
                )
        """))
        violations = _unsafe_rollback_functions_flow_aware(test_file)
        assert any(
            "unsafe_execute_with_unrelated_expired_read" in v for v in violations
        ), (
            "A function that calls session.execute(UPDATE ... WHERE obj.id) after "
            "rollback was NOT flagged by the flow-aware scanner, even though 'obj' "
            "is expired and 'obj.id' is read as an argument to the execute call "
            "before the execute can re-load any state.  A co-located "
            "session.execute does NOT make a same-expression ORM attribute read "
            "safe when it does not re-load the attribute's object.  The scanner "
            "must treat a refetch-plus-unrelated-orm-read in the same simple "
            "statement as unsafe (return -1, not 1)."
        )


class TestCommitFlowSafetyRegressionCheck:
    """Error-path ``session.commit()`` coverage for the MissingGreenlet guard.

    ``session.commit()`` is the PRIMARY expiry trigger (``expire_on_commit``
    semantics, documented in ``backend/conftest.py``): after commit every ORM
    object held by the session is expired, so the next synchronous attribute
    access lazy-loads from async context and raises ``MissingGreenlet`` on
    asyncpg — exactly like ``rollback()``.  A blanket commit gate is infeasible
    (~hundreds of call sites), so this guard narrows to the tractable,
    high-signal ERROR-PATH subset: ``commit`` calls lexically inside an
    ``except`` handler or a ``try/finally`` finalbody whose post-commit
    continuation reads an expired ORM attribute.

    It reuses the exact same ``_classify_stmt`` / ``_post_expiry_is_flow_safe``
    post-path machinery as the rollback scanner.  Acknowledged cases the scanner
    cannot statically verify (or that are pending burn-down) live in
    ``COMMIT_FLOW_ALLOWLIST`` in ``backend/app/greenlet_fidelity.py`` and can
    only shrink (enforced by the monotonic ceiling).
    """

    def test_commit_flow_registry_defined_in_greenlet_fidelity(self):
        """The COMMIT_FLOW_ALLOWLIST registry must exist in production.

        Exercises the production module ``backend/app/greenlet_fidelity.py``
        (the canonical, importable registry the guard reads) to confirm the
        error-path commit-flow allowlist, its ceiling, and the ratchet helper
        are defined alongside the rollback-flow trio.
        """
        gf_path = _BACKEND_ROOT / "app" / "greenlet_fidelity.py"
        assert gf_path.exists(), (
            f"backend/app/greenlet_fidelity.py not found at {gf_path}. "
            "The commit-flow registry cannot be verified."
        )
        content = gf_path.read_text()
        for symbol in (
            "COMMIT_FLOW_ALLOWLIST",
            "COMMIT_FLOW_ALLOWLIST_CEILING",
            "def commit_flow_allowlist_within_ceiling",
        ):
            assert symbol in content, (
                f"backend/app/greenlet_fidelity.py must define {symbol} — the "
                "canonical error-path commit-flow allowlist mirrors the "
                "ROLLBACK_FLOW_ALLOWLIST pattern.  Restore it or update this "
                "test to match."
            )

    def test_commit_scanner_flags_read_after_error_path_commit(self, tmp_path):
        """A ``commit()`` inside an ``except`` handler that falls through to an
        ORM attribute read MUST be flagged.

        This is the masked-regression proof: on aiosqlite the root conftest
        no-ops ``InstanceState._expire``, so a post-commit expired read passes
        tautologically; on asyncpg ``task.status`` after the commit fires a lazy
        SELECT from async context and raises ``MissingGreenlet``.  The extended
        commit-path scanner must catch this even though the older guard only
        inspected ``rollback()`` call sites.

        Also exercises the production registry
        ``backend/app/greenlet_fidelity.py`` (read below) that this PR extends.
        """
        gf_path = _BACKEND_ROOT / "app" / "greenlet_fidelity.py"
        assert gf_path.exists(), "backend/app/greenlet_fidelity.py not found"
        assert "COMMIT_FLOW_ALLOWLIST" in gf_path.read_text(), (
            "backend/app/greenlet_fidelity.py must define COMMIT_FLOW_ALLOWLIST — "
            "the canonical error-path commit-flow allowlist the guard consults."
        )

        test_file = tmp_path / "test_error_path_commit_fallthrough.py"
        test_file.write_text(textwrap.dedent("""\
            async def commit_in_except_then_read(session, task):
                \"\"\"Commit in except, ORM read after try — greenlet hazard.\"\"\"
                try:
                    await session.flush()
                except Exception:
                    await session.commit()
                    logging.warning("committed on the error path")
                # task is expired by the commit above; reading .status fires a
                # lazy SELECT from async context -> MissingGreenlet on asyncpg.
                print(task.status)
                return None
        """))
        violations = _unsafe_error_path_commit_functions_flow_aware(test_file)
        assert any("commit_in_except_then_read" in v for v in violations), (
            "A function whose commit() is inside an except handler that falls "
            "through to an ORM attribute read (task.status) was NOT flagged by "
            "the error-path commit scanner.  session.commit() expires ORM state "
            "just like rollback(); the post-commit continuation read must be "
            "flagged (or acknowledged in COMMIT_FLOW_ALLOWLIST)."
        )

    def test_commit_scanner_flags_read_after_finally_commit(self, tmp_path):
        """A ``commit()`` in a ``try/finally`` finalbody that falls through to an
        ORM attribute read MUST be flagged (covers the finalbody branch of the
        error-path subset, not just ``except`` handlers)."""
        test_file = tmp_path / "test_finally_commit_fallthrough.py"
        test_file.write_text(textwrap.dedent("""\
            async def commit_in_finally_then_read(session, task):
                \"\"\"Commit in finally, ORM read after try — greenlet hazard.\"\"\"
                try:
                    await session.flush()
                finally:
                    await session.commit()
                print(task.status)
        """))
        violations = _unsafe_error_path_commit_functions_flow_aware(test_file)
        assert any("commit_in_finally_then_read" in v for v in violations), (
            "A commit() inside a try/finally finalbody that falls through to an "
            "expired ORM read (task.status) was NOT flagged by the error-path "
            "commit scanner.  The finalbody is part of the error-path subset."
        )

    def test_commit_scanner_ignores_happy_path_commit(self, tmp_path):
        """A ``commit()`` on the NORMAL path (not in an ``except`` handler or a
        ``finally``) followed by an ORM read must NOT be flagged.

        This bounds the narrowed guard: a blanket commit gate is infeasible, so
        only error-path commits are in scope.  Without this bound the scanner
        would flag ~hundreds of ordinary commit-then-read functions.
        """
        test_file = tmp_path / "test_happy_path_commit.py"
        test_file.write_text(textwrap.dedent("""\
            async def commit_then_read_happy_path(session, task):
                \"\"\"Commit on the normal path — outside the error-path subset.\"\"\"
                await session.commit()
                print(task.status)
        """))
        violations = _unsafe_error_path_commit_functions_flow_aware(test_file)
        assert not any(
            "commit_then_read_happy_path" in v for v in violations
        ), (
            "A commit() on the normal (non-error) path was flagged by the "
            "error-path commit scanner.  The guard is deliberately narrowed to "
            "commit calls inside except handlers / finally blocks; a happy-path "
            "commit-then-read must NOT be in scope."
        )

    def test_refetch_does_not_mask_unrelated_read_beside_error_path_commit(
        self, tmp_path
    ):
        """A post-commit ``session.execute(UPDATE ... WHERE obj.id)`` does NOT
        make the statement safe when it reads an expired ORM attribute on a
        DIFFERENT object the execute does not reload.

        Closes the refetch-masks-unrelated-read false negative on the commit
        path: the co-located ``session.execute`` is an UPDATE — it never
        reloads ``row`` — so ``row.id`` in the WHERE clause raises
        ``MissingGreenlet`` before the execute runs.  ``_classify_stmt`` must
        return -1 (unsafe), not credit the refetch as safe.
        """
        test_file = tmp_path / "test_commit_refetch_beside_read.py"
        test_file.write_text(textwrap.dedent("""\
            async def commit_then_execute_with_unrelated_expired_read(
                session, row, Other, sa_update
            ):
                \"\"\"Error-path commit; execute(UPDATE WHERE row.id) reads
                expired row.id on a different object — greenlet hazard.\"\"\"
                try:
                    await session.flush()
                except Exception:
                    await session.commit()
                await session.execute(
                    sa_update(Other).where(Other.ref_id == row.id)
                )
        """))
        violations = _unsafe_error_path_commit_functions_flow_aware(test_file)
        assert any(
            "commit_then_execute_with_unrelated_expired_read" in v
            for v in violations
        ), (
            "A function that reads an unrelated expired ORM attribute (row.id) "
            "beside a co-located session.execute after an error-path commit was "
            "NOT flagged.  A refetch that does not reload the read object must "
            "not mask the unrelated expired read (_classify_stmt must return -1)."
        )

    def test_commit_missing_greenlet_regression_check(self):
        """Scan all ``backend/app/*.py`` files with the error-path commit
        scanner and fail if any error-path ``commit()`` has an unsafe
        post-commit ORM attribute read not covered by ``COMMIT_FLOW_ALLOWLIST``.

        Functions in ``COMMIT_FLOW_ALLOWLIST`` (imported from
        ``app.greenlet_fidelity``) are acknowledged debt seeded when this
        coverage landed; the set can only shrink (enforced by
        ``test_commit_flow_allowlist_cannot_grow_beyond_ceiling``).
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
            for v in _unsafe_error_path_commit_functions_flow_aware(path)
            if v not in _COMMIT_FLOW_ALLOWLIST
        ]

        assert not violations, (
            "The following functions in backend/app/ call session.commit() from "
            "an except handler or finally block with a reachable post-commit ORM "
            "attribute read not covered by a safe terminator or refetch.  Each "
            "must either snapshot ORM values before the commit, add an explicit "
            "return/raise after the commit, re-fetch via session.get/refresh, or "
            "be acknowledged by adding an entry to COMMIT_FLOW_ALLOWLIST in "
            "backend/app/greenlet_fidelity.py (and bumping the ceiling) with a "
            "comment explaining why the function is safe or is pending burn-down:"
            "\n" + "\n".join(f"  - {v}" for v in sorted(violations))
        )

    def test_commit_flow_allowlist_cannot_grow_beyond_ceiling(self):
        """The error-path commit-flow allowlist must never exceed its monotonic
        ceiling.

        Growth ratchet: adding an entry to COMMIT_FLOW_ALLOWLIST requires
        bumping COMMIT_FLOW_ALLOWLIST_CEILING in
        backend/app/greenlet_fidelity.py — a deliberate, visible diff action.
        Burn-down is tracked by lowering the ceiling as entries are triaged and
        fixed in production.
        """
        assert _commit_flow_allowlist_within_ceiling(), (
            f"COMMIT_FLOW_ALLOWLIST has {len(_COMMIT_FLOW_ALLOWLIST)} entries "
            f"(ceiling: {_COMMIT_FLOW_ALLOWLIST_CEILING}). Either fix the "
            "production function so the scanner can verify it (snapshot ORM "
            "values before commit, or re-fetch after) and remove it from the "
            "allowlist, or — after exhausting verifiable options — deliberately "
            "bump COMMIT_FLOW_ALLOWLIST_CEILING in "
            "backend/app/greenlet_fidelity.py."
        )

        # Burn-down ratchet: the ceiling is monotonic *downward*. The four
        # functions remediated in the 2026-07-12 burn-down (pre-capture idiom /
        # post-commit re-fetch, all behaviour-preserving) must stay removed from
        # the allowlist — re-adding any of them without first re-lowering the
        # ceiling would silently re-inflate the acknowledged-debt set. Pinning
        # them here makes the named test exercise the production burn-down rather
        # than only the abstract len<=ceiling invariant.
        _remediated = {
            "autonomous_research.py::_spawn_research_task_inner",
            "orchestrator_ci_monitor.py::_monitor_evaluating_tasks",
            "orchestrator_runner_dispatch.py::_dispatch_compute_experiment_directly",
            "reaper.py::reaper_service",
        }
        _regressed = _remediated & _COMMIT_FLOW_ALLOWLIST
        assert not _regressed, (
            "These functions were burned down out of COMMIT_FLOW_ALLOWLIST but "
            "have reappeared: " + ", ".join(sorted(_regressed)) + ". A burn-down "
            "is a monotonic-downward step: re-acknowledging a remediated function "
            "requires re-raising COMMIT_FLOW_ALLOWLIST_CEILING deliberately."
        )
        assert _COMMIT_FLOW_ALLOWLIST_CEILING <= 2, (
            "COMMIT_FLOW_ALLOWLIST_CEILING regressed above the post-burn-down "
            f"value (now {_COMMIT_FLOW_ALLOWLIST_CEILING}, expected <= 2). The "
            "ceiling is a downward ratchet; raising it re-opens headroom for the "
            "acknowledged-debt set to grow again."
        )

        # The burn-down is a real production fix, not a bookkeeping edit: the
        # flow-aware scanner must actually verify each remediated function clean
        # against live backend/app source. (Skipped only where backend/app is not
        # mounted — the CI backend-only Docker context — mirroring the sibling
        # regression check.)
        if _PRODUCTION_APP_DIR.is_dir():
            for _selector in sorted(_remediated):
                _module = _selector.split("::", 1)[0]
                _path = _PRODUCTION_APP_DIR / _module
                assert _path.is_file(), (
                    f"Remediated module {_module} not found at {_path}; the "
                    "burn-down assertion cannot be verified."
                )
                _still_flagged = _unsafe_error_path_commit_functions_flow_aware(
                    _path
                )
                assert _selector not in _still_flagged, (
                    f"{_selector} was removed from COMMIT_FLOW_ALLOWLIST but the "
                    "flow-aware scanner still flags an unsafe post-commit ORM read "
                    "in it — the production remediation was reverted or is "
                    "incomplete. Restore the pre-capture / re-fetch fix."
                )


# ---------------------------------------------------------------------------
# 7. blocked_by triage taxonomy — cross-language contract drift guard
# ---------------------------------------------------------------------------


class TestBlockedByTaxonomyCrossLanguageDrift:
    """The merge-grade ``blocked_by`` triage taxonomy (design edcc1e6a §3.4) is a
    TWO-LANGUAGE contract: the agent producer (``agent/src/handlers/merge-grade.ts``)
    emits it, the backend consumer (``backend/app/release_scoring.py``, re-exported
    by ``release_qualifier_audit.py``) parses it verbatim.  A value added on one
    side without the other silently widens the contract; this guard fails on
    reintroduction.  Source-inspection only (no app imports) so it runs in the
    ``--standalone --no-postgres`` roster."""

    _CANONICAL: frozenset[str] = frozenset({"unbuilt", "out_of_scope", "blocked_external"})

    _BACKEND_SCORING = _BACKEND_ROOT / "app" / "release_scoring.py"

    @staticmethod
    def _agent_merge_grade_ts() -> Path | None:
        candidates = [
            _REPO_ROOT / "agent" / "src" / "handlers" / "merge-grade.ts",
            Path("/agent/src/handlers/merge-grade.ts"),
        ]
        for candidate in candidates:
            if candidate.is_file():
                return candidate
        return None

    def _python_taxonomy_values(self) -> frozenset[str]:
        source = self._BACKEND_SCORING.read_text()
        constants = set(
            re.findall(r'_BLOCKED_BY_\w+ = "([a-z_]+)"', source)
        )
        return frozenset(v for v in constants if v in self._CANONICAL)

    def _typescript_taxonomy_values(self, source: str) -> frozenset[str]:
        literals = set(re.findall(r"'([a-z_]+)'", source))
        return frozenset(v for v in literals if v in self._CANONICAL)

    def test_python_defines_exactly_the_three_canonical_values(self):
        assert self._python_taxonomy_values() == self._CANONICAL, (
            f"backend/app/release_scoring.py defines blocked_by values "
            f"{sorted(self._python_taxonomy_values())}, expected {sorted(self._CANONICAL)}"
        )

    def test_typescript_matches_the_python_taxonomy(self):
        ts_path = self._agent_merge_grade_ts()
        if ts_path is None:
            pytest.skip(
                "agent/src/handlers/merge-grade.ts not available in this "
                "environment (CI backend-only Docker context)"
            )
        ts_values = self._typescript_taxonomy_values(ts_path.read_text())
        py_values = self._python_taxonomy_values()
        assert ts_values == py_values == self._CANONICAL, (
            "blocked_by triage taxonomy drifted across the contract boundary: "
            f"agent={sorted(ts_values)} backend={sorted(py_values)}"
        )

    def test_default_is_unbuilt_on_both_sides(self):
        py_src = self._BACKEND_SCORING.read_text()
        assert "_DEFAULT_BLOCKED_BY = _BLOCKED_BY_UNBUILT" in py_src, (
            "backend default for an unclassified non-met residual must be "
            "unbuilt (never silently out_of_scope)"
        )
        ts_path = self._agent_merge_grade_ts()
        if ts_path is not None:
            ts_src = ts_path.read_text()
            assert re.search(r"DEFAULT_BLOCKED_BY: BlockedBy = 'unbuilt'", ts_src), (
                "agent default for an unclassified non-met residual must be "
                "'unbuilt' (never silently out_of_scope)"
            )

    def test_only_unbuilt_consumes_rung_budget_on_both_sides(self):
        py_src = self._BACKEND_SCORING.read_text()
        assert "return blocked_by in (None, _BLOCKED_BY_UNBUILT)" in py_src, (
            "blocked_by_consumes_rung_budget must be True only for None/unbuilt"
        )
        ts_path = self._agent_merge_grade_ts()
        if ts_path is not None:
            ts_src = ts_path.read_text()
            assert "blockedBy === 'unbuilt'" in ts_src, (
                "blockedByConsumesRungBudget must consume budget only for 'unbuilt'"
            )
