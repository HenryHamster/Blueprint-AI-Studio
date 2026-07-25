"""Standalone invariant tests — runner enrollment documentation accuracy.

These tests verify that:

  1. ``scripts/blueprint.py fleet`` exposes ONLY VM-pool subcommands (pool,
     warm, accounts, zones); it has NO runner-enrollment subcommands.
  2. No top-level ``scripts/blueprint.py`` subcommand performs runner
     enrollment.
  3. ``docs/runner-install.md`` describes the admin-UI Runtime-tab enroll flow
     as the canonical enrollment path and does NOT claim enrollment is done via
     the Blueprint CLI.
  4. The actual enrollment path is the REST endpoint
     ``POST /projects/{project_id}/runners/register`` in
     ``backend/app/routers/admin_runners.py``.

Run standalone (no DB required):
    pytest testing/test_runner_enroll_docs_accuracy.py --noconftest -v
"""

import ast
import re
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Repository layout helpers
# ---------------------------------------------------------------------------

def _resolve_repo_root() -> Path:
    """Resolve repo root with /repo fallback for Docker-shard runs."""
    candidates = (Path(__file__).resolve().parents[2], Path("/repo"))
    for candidate in candidates:
        if (candidate / "scripts" / "blueprint.py").is_file():
            return candidate
    return candidates[0]


REPO_ROOT = _resolve_repo_root()
BLUEPRINT_CLI = REPO_ROOT / "scripts" / "blueprint.py"
RUNNER_INSTALL_DOC = REPO_ROOT / "docs" / "runner-install.md"
ADMIN_RUNNERS_MODULE = REPO_ROOT / "backend" / "app" / "routers" / "admin_runners.py"


# ---------------------------------------------------------------------------
# Helper: collect argparse add_parser call names from an AST
# ---------------------------------------------------------------------------

def _collect_add_parser_names(tree: ast.AST) -> list[str]:
    """Return every name string passed as the first arg to ``add_parser()``."""
    names: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        is_add_parser = (isinstance(func, ast.Attribute) and func.attr == "add_parser")
        if not is_add_parser:
            continue
        if node.args and isinstance(node.args[0], ast.Constant):
            names.append(node.args[0].value)
    return names


def _collect_fleet_subparser_names(tree: ast.AST) -> list[str]:
    """Return the subcommand names registered under the ``fleet`` subparser.

    Strategy: find the assignment ``fleet_sub = p_fleet.add_subparsers(...)``
    then collect all ``fleet_sub.add_parser(...)`` calls.
    """
    # Identify the local name used for the fleet sub-subparser object.
    # We look for an assignment whose RHS is a Call to ``.add_subparsers``
    # whose parent target name suggests it is the fleet child parser.
    fleet_sub_var: str | None = None
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign):
            continue
        val = node.value
        if not (isinstance(val, ast.Call) and isinstance(val.func, ast.Attribute)
                and val.func.attr == "add_subparsers"):
            continue
        # Check caller is p_fleet (or similar) by checking keyword dest="fleet_cmd"
        for kw in val.keywords:
            if kw.arg == "dest" and isinstance(kw.value, ast.Constant):
                if kw.value.value == "fleet_cmd":
                    # grab target name
                    if node.targets and isinstance(node.targets[0], ast.Name):
                        fleet_sub_var = node.targets[0].id
    if fleet_sub_var is None:
        return []

    # Now collect all add_parser calls on that variable.
    subcommands: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not (isinstance(func, ast.Attribute) and func.attr == "add_parser"):
            continue
        # caller object must be the fleet_sub variable
        if not (isinstance(func.value, ast.Name) and func.value.id == fleet_sub_var):
            continue
        if node.args and isinstance(node.args[0], ast.Constant):
            subcommands.append(node.args[0].value)
    return subcommands


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def blueprint_source() -> str:
    assert BLUEPRINT_CLI.exists(), (
        f"scripts/blueprint.py not found at {BLUEPRINT_CLI} — "
        "check the repository layout"
    )
    return BLUEPRINT_CLI.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def blueprint_ast(blueprint_source: str) -> ast.AST:
    return ast.parse(blueprint_source, filename=str(BLUEPRINT_CLI))


@pytest.fixture(scope="module")
def runner_install_doc() -> str:
    assert RUNNER_INSTALL_DOC.exists(), (
        f"docs/runner-install.md not found at {RUNNER_INSTALL_DOC} — "
        "check the repository layout"
    )
    return RUNNER_INSTALL_DOC.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def admin_runners_source() -> str:
    assert ADMIN_RUNNERS_MODULE.exists(), (
        "backend/app/routers/admin_runners.py not found — check the repository layout"
    )
    return ADMIN_RUNNERS_MODULE.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Tests: fleet subcommand surface
# ---------------------------------------------------------------------------

class TestFleetSubcommandsExcludeRunnerEnrollment:
    """The fleet command manages the cloud VM account pool, not local runners.

    Runner enrollment is an admin-UI / REST API operation; the CLI must never
    expose it under ``fleet`` (or anywhere else).
    """

    EXPECTED_FLEET_SUBCOMMANDS = {"pool", "warm", "accounts", "zones"}
    FORBIDDEN_ENROLLMENT_KEYWORDS = {
        "enroll", "register", "runner-enroll", "runner-register",
        "add-runner", "create-runner",
    }

    def test_fleet_subcommands_are_exactly_pool_warm_accounts_zones(
        self, blueprint_ast: ast.AST
    ):
        """fleet sub-subparser contains exactly the four pool/infra commands."""
        found = set(_collect_fleet_subparser_names(blueprint_ast))
        assert found == self.EXPECTED_FLEET_SUBCOMMANDS, (
            f"Expected fleet subcommands to be exactly "
            f"{self.EXPECTED_FLEET_SUBCOMMANDS!r}, got {found!r}. "
            "Runner enrollment must NOT be added as a fleet subcommand — "
            "the canonical path is the admin-UI Runtime tab."
        )

    def test_fleet_subcommands_contain_no_enrollment_names(
        self, blueprint_ast: ast.AST
    ):
        """None of the fleet sub-subparser names match enrollment-related keywords."""
        found = set(_collect_fleet_subparser_names(blueprint_ast))
        overlap = found & self.FORBIDDEN_ENROLLMENT_KEYWORDS
        assert not overlap, (
            f"fleet subparser unexpectedly contains enrollment subcommands: {overlap!r}. "
            "Runner enrollment belongs in the admin-UI (Runtime tab → Devices and runners "
            "→ Enroll runner), not in the CLI fleet command."
        )

    def test_no_top_level_runner_enrollment_subcommand(
        self, blueprint_ast: ast.AST
    ):
        """No top-level subparser name matches runner-enrollment semantics."""
        all_parsers = set(_collect_add_parser_names(blueprint_ast))
        overlap = all_parsers & self.FORBIDDEN_ENROLLMENT_KEYWORDS
        assert not overlap, (
            f"scripts/blueprint.py registers runner-enrollment subcommands at the "
            f"top level or in a subparser: {overlap!r}. "
            "Enrollment must be done through the admin-UI REST API only."
        )


# ---------------------------------------------------------------------------
# Tests: runner-install.md describes the correct path
# ---------------------------------------------------------------------------

class TestRunnerInstallDocDescribesUIEnrollPath:
    """docs/runner-install.md must describe the admin-UI enrollment path.

    The doc should reference the Runtime tab / Devices and runners section /
    Enroll runner button, NOT the blueprint CLI, as the enrollment mechanism.
    """

    def test_doc_mentions_runtime_tab_enroll_flow(self, runner_install_doc: str):
        """Enrollment section references the admin-UI Runtime tab."""
        lower = runner_install_doc.lower()
        assert "runtime" in lower and "enroll" in lower, (
            "docs/runner-install.md does not mention 'Runtime' and 'enroll'. "
            "The enrollment instructions must describe the admin-UI Runtime tab "
            "flow, not a CLI command."
        )

    def test_doc_references_devices_and_runners_section(
        self, runner_install_doc: str
    ):
        """Enrollment section names the 'Devices and runners' UI section."""
        assert "devices and runners" in runner_install_doc.lower(), (
            "docs/runner-install.md does not mention 'Devices and runners'. "
            "The enrollment section should direct the operator to the "
            "Runtime tab → Devices and runners → Enroll runner flow in the admin UI."
        )

    def test_doc_references_enroll_runner_button(self, runner_install_doc: str):
        """The doc mentions the 'Enroll runner' UI control."""
        assert "enroll runner" in runner_install_doc.lower(), (
            "docs/runner-install.md does not mention 'Enroll runner'. "
            "Section 1 should name the button the operator clicks to start enrollment."
        )

    def test_doc_does_not_claim_cli_enrollment(self, runner_install_doc: str):
        """The doc must not instruct operators to use blueprint.py for enrollment."""
        # Patterns that would incorrectly attribute enrollment to the CLI.
        bad_patterns = [
            r"blueprint\.py.*enroll",
            r"blueprint\.py.*register.*runner",
            r"python.*scripts/blueprint\.py.*runner",
            r"blueprint fleet.*enroll",
            r"blueprint enroll",
        ]
        for pattern in bad_patterns:
            match = re.search(pattern, runner_install_doc, re.IGNORECASE)
            assert match is None, (
                f"docs/runner-install.md matches the forbidden pattern {pattern!r}: "
                f"...{match.group()}...\n"
                "The CLI does not support runner enrollment; do not document it as if "
                "it does. Direct operators to the admin-UI Runtime tab instead."
            )

    def test_doc_section_1_describes_ui_not_cli(self, runner_install_doc: str):
        """Section 1 (Enroll a runner) refers to the admin UI, not the CLI."""
        # Extract a window around "## 1. Enroll" to narrow scope.
        match = re.search(
            r"##\s*1\.\s*Enroll.*?(?=##\s*\d\.|\Z)",
            runner_install_doc,
            re.IGNORECASE | re.DOTALL,
        )
        assert match is not None, (
            "docs/runner-install.md does not have a '## 1. Enroll' section. "
            "The enrollment section should be the first numbered section and "
            "should describe the admin-UI flow."
        )
        section = match.group()
        # Must mention admin UI direction.
        assert re.search(r"admin.{0,5}ui|runtime.{0,10}tab", section, re.IGNORECASE), (
            "Section 1 of docs/runner-install.md does not mention the admin UI or "
            "Runtime tab. The section should direct the operator to the UI, not a "
            "CLI command."
        )

    def test_doc_one_time_token_mentioned(self, runner_install_doc: str):
        """The doc mentions the one-time token produced by enrollment."""
        assert "one-time" in runner_install_doc.lower() or "one time" in runner_install_doc.lower(), (
            "docs/runner-install.md does not mention the one-time enrollment token. "
            "Operators need to know the token is shown exactly once."
        )

    def test_doc_re_enrollment_section_references_admin_ui(
        self, runner_install_doc: str
    ):
        """The re-enrollment section (token revoked/lost) also references the admin UI.

        Looks for a numbered section whose heading contains 'Re-enrollment'
        (e.g. '## 8. Re-enrollment') and checks that its body mentions the
        admin UI or Runtime tab.  Skips if no such heading-level section exists
        (the guidance may be embedded inline instead, which is acceptable).
        """
        # Match a heading-level Re-enrollment section (## N. Re-enrollment …)
        match = re.search(
            r"^##\s+\d+\.\s+Re-enrollment.*?(?=^##\s|\Z)",
            runner_install_doc,
            re.IGNORECASE | re.DOTALL | re.MULTILINE,
        )
        if match is None:
            pytest.skip("No standalone '## N. Re-enrollment' section — skipping")
        section = match.group()
        assert re.search(r"admin.{0,5}ui|runtime.{0,10}tab", section, re.IGNORECASE), (
            "The re-enrollment section in docs/runner-install.md does not reference "
            "the admin UI or Runtime tab. Operators who lose their token should be "
            "directed to the UI, not a CLI command."
        )


# ---------------------------------------------------------------------------
# Tests: actual enrollment path is the REST API
# ---------------------------------------------------------------------------

class TestActualEnrollmentPathIsRESTAPI:
    """The enrollment REST endpoint must exist in admin_runners.py."""

    def test_register_runner_endpoint_exists(self, admin_runners_source: str):
        """POST /projects/{project_id}/runners/register is decorated in the module."""
        assert "@router.post" in admin_runners_source, (
            "backend/app/routers/admin_runners.py has no @router.post decorators. "
            "The enrollment endpoint may have been removed."
        )
        assert '"/projects/{project_id}/runners/register"' in admin_runners_source, (
            "The runner enrollment endpoint "
            "POST /projects/{project_id}/runners/register "
            "is not defined in backend/app/routers/admin_runners.py. "
            "This is the canonical enrollment path; do not remove it."
        )

    def test_register_runner_endpoint_returns_token(self, admin_runners_source: str):
        """The register endpoint docstring or body must reference the one-time token."""
        # Narrow to the function body of register_runner.
        match = re.search(
            r"async def register_runner.*?(?=\n@router|\nclass |\Z)",
            admin_runners_source,
            re.DOTALL,
        )
        assert match is not None, (
            "Could not locate the register_runner function in admin_runners.py."
        )
        body = match.group()
        assert "token" in body.lower(), (
            "register_runner in admin_runners.py does not reference 'token'. "
            "The enrollment endpoint must mint and return a one-time token."
        )

    def test_register_runner_requires_owner_role(self, admin_runners_source: str):
        """Enrollment is an owner-only operation — the endpoint must enforce it."""
        match = re.search(
            r"async def register_runner.*?(?=\n@router|\nclass |\Z)",
            admin_runners_source,
            re.DOTALL,
        )
        assert match is not None
        body = match.group()
        assert "owner" in body.lower(), (
            "register_runner in admin_runners.py does not gate on 'owner' role. "
            "Enrollment must require project-owner authorization to prevent "
            "unauthorized runners from being attached to a project."
        )

    def test_register_runner_endpoint_returns_runner_id(
        self, admin_runners_source: str
    ):
        """The response includes the runner_id alongside the token."""
        match = re.search(
            r"async def register_runner.*?(?=\n@router|\nclass |\Z)",
            admin_runners_source,
            re.DOTALL,
        )
        assert match is not None
        body = match.group()
        assert "runner_id" in body, (
            "register_runner does not return 'runner_id' in its response. "
            "Operators need the runner id to manage the runner after enrollment."
        )


# ---------------------------------------------------------------------------
# Tests: blueprint.py fleet is scoped to cloud VM pool, not local runners
# ---------------------------------------------------------------------------

class TestFleetCommandScopeIsCloudVMPool:
    """The ``fleet`` command is about the cloud VM (Max Plan) account pool.

    Its help text and implementation must NOT reference local runner enrollment
    or management — those belong in the admin-UI.
    """

    def test_fleet_help_text_describes_vm_pool(self, blueprint_source: str):
        """fleet parser's help string mentions the VM account pool, not runner enroll."""
        # Narrow to the block that registers the fleet parser.
        match = re.search(
            r'sub\.add_parser\(\s*"fleet".*?fleet_sub\.add_parser\("zones"[^\)]*\)',
            blueprint_source,
            re.DOTALL,
        )
        assert match is not None, (
            "Could not locate the fleet parser registration block in scripts/blueprint.py. "
            "Check that the fleet parser is still defined there."
        )
        block = match.group()
        # The block must mention pool / warm / account pool concepts.
        assert re.search(r"account pool|warm pool|vm pool|zone", block, re.IGNORECASE), (
            "The fleet parser help text does not describe an account/VM pool concept. "
            "fleet is a cloud VM management tool; its help should reflect that."
        )
        # And must NOT mention runner enrollment.
        for bad in ("enroll", "register runner", "local runner"):
            assert bad not in block.lower(), (
                f"The fleet parser block mentions {bad!r}, which is an enrollment "
                "concept. fleet must remain scoped to cloud VM pool management."
            )

    def test_cmd_fleet_function_dispatches_pool_subcommands(
        self, blueprint_source: str
    ):
        """cmd_fleet dispatches to pool/warm/accounts/zones, not enrollment helpers."""
        match = re.search(
            r"def cmd_fleet\(.*?\).*?(?=\ndef |\Z)",
            blueprint_source,
            re.DOTALL,
        )
        assert match is not None, (
            "cmd_fleet function not found in scripts/blueprint.py."
        )
        body = match.group()
        # Must dispatch to the four pool sub-commands.
        for expected in ("pool", "warm", "accounts", "zones"):
            assert expected in body, (
                f"cmd_fleet does not dispatch to the '{expected}' subcommand. "
                "The fleet command should only handle cloud VM pool operations."
            )
        # Must not call any enrollment helper.
        for bad in ("enroll", "register_runner", "runner_register"):
            assert bad not in body.lower(), (
                f"cmd_fleet calls '{bad}', which is an enrollment operation. "
                "Enrollment must remain in the admin-UI REST API, not the CLI."
            )


# ---------------------------------------------------------------------------
# Tests: blueprint.py module docstring accurately describes runner management
# ---------------------------------------------------------------------------

class TestBlueprintPyDocstringExcludesRunnerMgmt:
    """scripts/blueprint.py must explicitly delegate runner management to blueprint_runner.py.

    The design-doc agent derives the sys-operator-tooling description from the
    module docstring.  If that docstring claims (or implies) blueprint.py provides
    runner management, the design doc drifts from reality — all six runner
    subcommands (start, check, install, uninstall, update, status) live in the
    separate blueprint_runner.py CLI.
    """

    RUNNER_SUBCOMMANDS = ("start", "check", "install", "uninstall", "update", "status")

    def test_docstring_names_blueprint_runner_for_runner_ops(
        self, blueprint_source: str
    ):
        """Module docstring names blueprint_runner.py as the runner management CLI."""
        module_docstring = _extract_module_docstring(blueprint_source)
        assert module_docstring is not None, (
            "scripts/blueprint.py has no module-level docstring. "
            "Add one that describes its subcommand groups and delegates runner "
            "management to blueprint_runner.py."
        )
        assert "blueprint_runner" in module_docstring, (
            "scripts/blueprint.py module docstring does not mention 'blueprint_runner'. "
            "The docstring must attribute runner management (start, check, install, "
            "uninstall, update, status) to scripts/blueprint_runner.py so that "
            "design-doc agents produce accurate sys-operator-tooling descriptions."
        )

    def test_docstring_does_not_claim_runner_subcommands(
        self, blueprint_source: str
    ):
        """Module docstring 'Subcommand groups' section lists no runner-only subcommands.

        The runner-only subcommands are those that exist exclusively in
        blueprint_runner.py (install, uninstall, update, start-as-runner-daemon).
        'status' and 'check' are not tested here because blueprint.py also has
        legitimate subcommands with those names.
        """
        module_docstring = _extract_module_docstring(blueprint_source)
        if module_docstring is None:
            pytest.skip("No module docstring — covered by test_docstring_names_blueprint_runner_for_runner_ops")

        # Extract the Subcommand groups section (if present).
        match = re.search(
            r"Subcommand groups:(.*?)(?:\n\S|\Z)",
            module_docstring,
            re.DOTALL,
        )
        if match is None:
            # No explicit subcommand section — no false claims possible.
            return
        section = match.group(1)

        # Runner-exclusive subcommands that must NOT appear as a listed group in
        # blueprint.py (install/uninstall/update are unambiguously runner-only).
        runner_only_pattern = re.compile(
            r"^\s+(?:install|uninstall|update)\b",
            re.MULTILINE,
        )
        bad_match = runner_only_pattern.search(section)
        assert bad_match is None, (
            f"scripts/blueprint.py docstring 'Subcommand groups' section lists "
            f"a runner-only subcommand: {bad_match.group().strip()!r}. "
            "Runner management (install, uninstall, update) belongs entirely in "
            "scripts/blueprint_runner.py."
        )


def _extract_module_docstring(source: str) -> str | None:
    """Return the module-level docstring from Python source, or None."""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return None
    return ast.get_docstring(tree)
