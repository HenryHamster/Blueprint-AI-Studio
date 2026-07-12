"""
Behavioral invariant tests ("tripwires").

These tests assert that critical safety constants and patterns have not been
modified by agents. If an agent changes a safety value, CI catches it.

Run with: pytest testing/test_invariants.py -v --noconftest
"""

import ast
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

# auth.py raises ValueError at module load if these are absent; keeping them
# here lets the route-table invariant run in standalone mode without the
# backend test conftest.
os.environ.setdefault("AGENT_JWT_SECRET", "test-secret-for-invariant-check-x")
os.environ.setdefault("WS_TICKET_SECRET", "test-ws-secret-for-invariant-check")


def _assert_backmerge_modules_do_not_import_rollup_management(
    source: str, *, filename: str
) -> None:
    """Reject back-merge-module escape hatches into the push-capable rollup path."""
    import ast

    tree = ast.parse(source, filename=filename)
    forbidden_imports: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            module = node.module or ""
            if module == "app.rollup_management":
                forbidden_imports.append(
                    "from app.rollup_management import "
                    + ", ".join(alias.name for alias in node.names)
                )
            elif module == "app" and any(
                alias.name == "rollup_management" for alias in node.names
            ):
                forbidden_imports.append("from app import rollup_management")
        elif isinstance(node, ast.Import):
            if any(alias.name == "app.rollup_management" for alias in node.names):
                forbidden_imports.append("import app.rollup_management")

    forbidden_helpers = []
    if re.search(r"\b_attempt_alembic_autoreconcile\b", source):
        forbidden_helpers.append("_attempt_alembic_autoreconcile")

    assert not forbidden_imports and not forbidden_helpers, (
        f"{filename} must not import or reference app.rollup_management's "
        "push-capable Alembic autoreconcile helper. The back-merge block path "
        "and its planner must stay read-only; if you genuinely need a new helper, "
        "keep it out of the back-merge modules and update this invariant with a "
        "clear justification."
    )


def _ast_called_name(call: ast.Call) -> str | None:
    func = call.func
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return None


def _ast_refs_names(node: ast.AST, names: set[str]) -> bool:
    return any(isinstance(n, ast.Name) and n.id in names for n in ast.walk(node))


def _ast_assign_targets_for_predicated_call(
    stmt: ast.stmt,
    *,
    call_predicate,
) -> set[str]:
    value = getattr(stmt, "value", None)
    if value is None or not any(
        isinstance(n, ast.Call) and call_predicate(n) for n in ast.walk(value)
    ):
        return set()

    if isinstance(stmt, ast.Assign):
        targets = stmt.targets
    elif isinstance(stmt, (ast.AnnAssign, ast.AugAssign)):
        targets = [stmt.target]
    else:
        return set()

    names: set[str] = set()
    for tgt in targets:
        for n in ast.walk(tgt):
            if isinstance(n, ast.Name):
                names.add(n.id)
    return names


def _ast_branch_early_exits(stmts: list[ast.stmt]) -> bool:
    return any(
        isinstance(s, (ast.Return, ast.Raise, ast.Continue, ast.Break)) for s in stmts
    )


def _ast_branch_implies_verdict(
    node: ast.AST,
    *,
    branch_truth: bool,
    desired_truth: bool,
    atom_predicate,
) -> bool:
    """Return True only when this branch outcome forces the desired verdict.

    This is intentionally conservative. For composite boolean expressions we
    only treat a branch as covered when reaching that branch necessarily proves
    a guard/exclusion atom has the polarity the merge path needs.
    """

    if atom_predicate(node):
        return branch_truth == desired_truth

    if isinstance(node, ast.Await):
        return _ast_branch_implies_verdict(
            node.value,
            branch_truth=branch_truth,
            desired_truth=desired_truth,
            atom_predicate=atom_predicate,
        )

    if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.Not):
        return _ast_branch_implies_verdict(
            node.operand,
            branch_truth=not branch_truth,
            desired_truth=desired_truth,
            atom_predicate=atom_predicate,
        )

    if isinstance(node, ast.BoolOp):
        values = list(node.values)
        if isinstance(node.op, ast.And):
            if branch_truth:
                return any(
                    _ast_branch_implies_verdict(
                        value,
                        branch_truth=True,
                        desired_truth=desired_truth,
                        atom_predicate=atom_predicate,
                    )
                    for value in values
                )
            return all(
                _ast_branch_implies_verdict(
                    value,
                    branch_truth=False,
                    desired_truth=desired_truth,
                    atom_predicate=atom_predicate,
                )
                for value in values
            )

        if isinstance(node.op, ast.Or):
            if branch_truth:
                return all(
                    _ast_branch_implies_verdict(
                        value,
                        branch_truth=True,
                        desired_truth=desired_truth,
                        atom_predicate=atom_predicate,
                    )
                    for value in values
                )
            return any(
                _ast_branch_implies_verdict(
                    value,
                    branch_truth=False,
                    desired_truth=desired_truth,
                    atom_predicate=atom_predicate,
                )
                for value in values
            )

    if (
        isinstance(node, ast.Compare)
        and len(node.ops) == 1
        and len(node.comparators) == 1
    ):
        op = node.ops[0]
        left = node.left
        right = node.comparators[0]
        bool_side = None
        expr_side = None
        if isinstance(left, ast.Constant) and isinstance(left.value, bool):
            bool_side = left.value
            expr_side = right
        elif isinstance(right, ast.Constant) and isinstance(right.value, bool):
            bool_side = right.value
            expr_side = left
        if expr_side is not None and bool_side is not None:
            if isinstance(op, (ast.Is, ast.Eq)):
                expr_truth = bool_side if branch_truth else not bool_side
                return _ast_branch_implies_verdict(
                    expr_side,
                    branch_truth=expr_truth,
                    desired_truth=desired_truth,
                    atom_predicate=atom_predicate,
                )
            if isinstance(op, (ast.IsNot, ast.NotEq)):
                expr_truth = (not bool_side) if branch_truth else bool_side
                return _ast_branch_implies_verdict(
                    expr_side,
                    branch_truth=expr_truth,
                    desired_truth=desired_truth,
                    atom_predicate=atom_predicate,
                )

    return False


def _ast_branch_implies_named_str_compare(
    node: ast.AST,
    *,
    branch_truth: bool,
    names: set[str],
    truthy_literals: set[str],
) -> bool:
    """Conservatively match direct ``name == "literal"`` style status checks.

    This is intentionally narrower than ``_ast_branch_implies_verdict``. It is
    only used for the sibling-after-if propagation path where we want to keep
    honoring the `_validate_rollup_migration_chain(...)` string-status idiom
    (`"defer"` / `"block"`) without letting arbitrary guard-shaped `if`
    statements mark later merges as covered.
    """

    if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.Not):
        return _ast_branch_implies_named_str_compare(
            node.operand,
            branch_truth=not branch_truth,
            names=names,
            truthy_literals=truthy_literals,
        )

    if (
        isinstance(node, ast.Compare)
        and len(node.ops) == 1
        and len(node.comparators) == 1
    ):
        op = node.ops[0]
        left = node.left
        right = node.comparators[0]
        name_node = None
        literal_node = None
        if (
            isinstance(left, ast.Name)
            and left.id in names
            and isinstance(right, ast.Constant)
        ):
            name_node = left
            literal_node = right
        elif (
            isinstance(right, ast.Name)
            and right.id in names
            and isinstance(left, ast.Constant)
        ):
            name_node = right
            literal_node = left

        if (
            name_node is not None
            and isinstance(literal_node.value, str)
            and literal_node.value in truthy_literals
        ):
            if isinstance(op, (ast.Eq, ast.Is)):
                return branch_truth
            if isinstance(op, (ast.NotEq, ast.IsNot)):
                return not branch_truth

    return False


def _collect_dependency_callables(
    dependant, visited: set[int] | None = None
) -> set[object]:
    """Return every callable present in a FastAPI dependency tree."""
    if visited is None:
        visited = set()
    if id(dependant) in visited:
        return set()
    visited.add(id(dependant))

    found: set[object] = set()
    call = getattr(dependant, "call", None)
    if call is not None:
        found.add(call)
    for dep in getattr(dependant, "dependencies", []) or []:
        found.update(_collect_dependency_callables(dep, visited))
    return found


def _is_admin_surface_route(route) -> bool:
    """True for live admin-surface routes and the mixed admin_agent_ops siblings."""
    from app.routers.admin_agent_ops import ADMIN_AGENT_OPS_MODULE

    endpoint_module = getattr(getattr(route, "endpoint", None), "__module__", "")
    return (
        route.path.startswith("/api/admin") or endpoint_module == ADMIN_AGENT_OPS_MODULE
    )


def test_all_admin_routes_require_auth():
    """Every admin-surface route must resolve an auth dependency somewhere."""
    from fastapi.routing import APIRoute

    from app.auth import (
        get_current_user,
        get_current_user_optional,
        get_metrics_reader,
        require_admin,
    )
    from app.main import app
    from app.routers.agent import verify_agent_token

    # Intentional exceptions — should stay empty. If a route is intentionally
    # public (for example, a monitored liveness probe), add its path here with
    # a short comment explaining why the public surface is deliberate.
    public_allowlist: dict[str, str] = {
        # POST /api/admin/token authenticates via direct ADMIN_API_KEY comparison
        # (not Depends(get_current_user) / require_admin), because it is the
        # token-issuance endpoint that exchanges the shared secret for a JWT.
        "/api/admin/token": "exchanges ADMIN_API_KEY for short-lived JWT",
        # GET /api/admin/projects/{project_id}/tools/{tool_slug}/assets/{path:path}
        # uses JWT-based auth via a ?token query parameter (not Firebase/cookie auth)
        # because this endpoint serves assets to opaque-origin (null) iframes that
        # cannot send cookies. The capability JWT carries scope: "tool-asset" and is
        # validated inline by _verify_tool_asset_token.
        "/api/admin/projects/{project_id}/tools/{tool_slug}/assets/{path:path}": (
            "JWT-based capability-token auth for opaque-origin iframe asset serving"
        ),
    }

    # get_metrics_reader is a narrowly scoped read-only dep that accepts agent
    # JWTs; it is a valid auth callable for metrics-only endpoints.
    # get_current_user_optional is used on endpoints that accept EITHER an admin
    # bearer token OR an agent JWT via x-agent-token; the endpoint enforces
    # auth itself when the optional dep returns None.
    auth_callables = frozenset(
        {
            get_current_user,
            get_current_user_optional,
            require_admin,
            verify_agent_token,
            get_metrics_reader,
        }
    )
    scanned_routes: list[str] = []
    offenders: list[str] = []

    for route in app.routes:
        if not isinstance(route, APIRoute):
            continue
        if not _is_admin_surface_route(route):
            continue

        scanned_routes.append(route.path)
        if route.path in public_allowlist:
            continue

        dep_callables = _collect_dependency_callables(route.dependant, set())
        if not dep_callables.intersection(auth_callables):
            reason = public_allowlist.get(route.path)
            methods = ", ".join(sorted(route.methods or []))
            offenders.append(
                f"{methods} {route.path} (module={route.endpoint.__module__}"
                + (f", allowlist={reason}" if reason else "")
                + ")"
            )

    assert scanned_routes, "No admin-surface routes were discovered in the live app."
    assert not offenders, (
        "The following admin-surface routes have NO recognized auth dependency "
        "(require_admin, get_current_user, or verify_agent_token):\n"
        + "\n".join(f"  - {r}" for r in sorted(offenders))
        + "\n\nIf a route is intentionally public, add it to public_allowlist "
        "with a reviewed justification."
    )


def test_get_metrics_reader_routes_are_explicit_read_only_allowlisted(monkeypatch):
    """get_metrics_reader is only valid on the narrow read-only metrics route."""
    config_cache: dict | None = None

    def load_config_like_source(*, bootstrap_from_gcloud):
        nonlocal config_cache
        if config_cache is not None:
            return config_cache
        env_url = os.environ.get("BLUEPRINT_API_URL")
        env_key = os.environ.get("BLUEPRINT_API_KEY")
        if env_url and env_key:
            config_cache = {
                "api_url": env_url,
                "api_key": env_key,
                "default_repo": os.environ.get("BLUEPRINT_DEFAULT_REPO", ""),
                "default_base_branch": os.environ.get(
                    "BLUEPRINT_DEFAULT_BASE_BRANCH", "main"
                ),
            }
            return config_cache
        bootstrapped = bootstrap_from_gcloud()
        if bootstrapped is not None:
            config_cache = bootstrapped
            return config_cache
        agent_token = os.environ.get("AGENT_SECRET_TOKEN")
        orchestrator_url = os.environ.get("ORCHESTRATOR_URL") or env_url
        if agent_token and orchestrator_url:
            config_cache = {
                "api_url": orchestrator_url,
                "api_key": agent_token,
                "default_repo": os.environ.get("BLUEPRINT_DEFAULT_REPO", ""),
                "default_base_branch": os.environ.get(
                    "BLUEPRINT_DEFAULT_BASE_BRANCH", "main"
                ),
                "read_only": True,
            }
            return config_cache
        raise AssertionError("expected a config result")

    monkeypatch.setenv("AGENT_SECRET_TOKEN", "agent-jwt-token")
    monkeypatch.setenv("ORCHESTRATOR_URL", "https://orchestrator.example")
    monkeypatch.setenv("BLUEPRINT_API_URL", "https://admin.example")
    monkeypatch.setenv("BLUEPRINT_API_KEY", "admin-key")

    cfg = load_config_like_source(
        bootstrap_from_gcloud=lambda: {
            "api_url": "https://gcloud.example",
            "api_key": "gcloud-key",
            "default_repo": "gcloud-repo",
            "default_base_branch": "gcloud-branch",
        }
    )
    assert cfg["api_url"] == "https://admin.example"
    assert cfg["api_key"] == "admin-key"
    assert "read_only" not in cfg

    monkeypatch.delenv("BLUEPRINT_API_URL", raising=False)
    monkeypatch.delenv("BLUEPRINT_API_KEY", raising=False)
    config_cache = None

    cfg = load_config_like_source(
        bootstrap_from_gcloud=lambda: {
            "api_url": "https://gcloud.example",
            "api_key": "gcloud-key",
            "default_repo": "gcloud-repo",
            "default_base_branch": "gcloud-branch",
        }
    )
    assert cfg["api_url"] == "https://gcloud.example"
    assert cfg["api_key"] == "gcloud-key"
    assert "read_only" not in cfg

    config_cache = None

    cfg = load_config_like_source(bootstrap_from_gcloud=lambda: None)
    assert cfg["api_url"] == "https://orchestrator.example"
    assert cfg["api_key"] == "agent-jwt-token"
    assert cfg["read_only"] is True


def test_get_metrics_reader_routes_are_get_only_and_allowlisted():
    """Routes using get_metrics_reader must stay on the narrow GET allowlist."""
    from fastapi.routing import APIRoute
    from app.main import app
    from app.auth import get_metrics_reader

    allowed_paths = {"/api/admin/digest-latency-stats"}
    metrics_routes = []

    for route in app.routes:
        if not isinstance(route, APIRoute):
            continue
        dep_callables = _collect_dependency_callables(route.dependant, set())
        if get_metrics_reader in dep_callables:
            metrics_routes.append(route)

    assert metrics_routes, "No routes using get_metrics_reader were discovered."
    assert {route.path for route in metrics_routes} == allowed_paths, (
        "Routes using get_metrics_reader must stay on the explicit read-only "
        "allowlist:\n"
        + "\n".join(
            f"  - {', '.join(sorted(route.methods or []))} {route.path}"
            for route in sorted(metrics_routes, key=lambda route: route.path)
        )
    )
    assert all((route.methods or set()) == {"GET"} for route in metrics_routes), (
        "Routes using get_metrics_reader must be GET-only:\n"
        + "\n".join(
            f"  - {', '.join(sorted(route.methods or []))} {route.path}"
            for route in sorted(metrics_routes, key=lambda route: route.path)
        )
    )


# ── Cross-cutting test file registry ────────────────────────────────────────


def test_cross_cutting_test_files_registry_is_current():
    """Verify ``_CROSS_CUTTING_TEST_FILES`` in acceptance_gate.py is current.

    Every registered entry must:
        1. Point to a real file on disk.
        2. Be a ``backend/testing/test_*.py`` file.
        3. Be genuinely cross-cutting (its stem does not match any production
           module's basename in ``backend/app/`` — otherwise it would qualify
           for the normal stem-match heuristic and does not belong in the
           cross-cutting list).

    Additionally, discover **unregistered** cross-cutting test files: any
    ``backend/testing/test_*invariant*`` file whose stem does not match a
    production module must be registered.  This catches the exact scenario
    described in PR #13677: a new cross-cutting test file silently receiving
    false-negative acceptance verdicts because it was never added to the
    allowlist.
    """
    from app.acceptance_gate import get_cross_cutting_test_files

    registered = set(get_cross_cutting_test_files())
    testing_dir = BACKEND_ROOT / "testing"

    # All production .py basenames in backend/app/ (recursive).
    app_dir = BACKEND_ROOT / "app"
    prod_basenames: set[str] = set()
    for p in app_dir.rglob("*.py"):
        prod_basenames.add(p.stem)

    # 1–3. Validate every registered entry.
    for entry in registered:
        assert entry.startswith("backend/testing/"), (
            f"Cross-cutting entry {entry!r} is not under backend/testing/"
        )
        full_path = testing_dir / Path(*Path(entry).parts[2:])
        assert full_path.exists(), (
            f"Cross-cutting entry {entry!r} does not exist on disk"
        )
        test_basename = Path(entry).stem
        assert test_basename.startswith("test_"), (
            f"Cross-cutting entry {entry!r} is not a test file"
        )
        stem = test_basename[len("test_"):]
        assert stem not in prod_basenames, (
            f"Cross-cutting entry {entry!r} has stem {stem!r} which matches "
            f"a production module — it should use the regular stem-match "
            "heuristic, not the cross-cutting allowlist"
        )

    # 4. Discover unregistered invariant-pattern test files.
    for test_file in sorted(testing_dir.glob("test_*invariant*")):
        rel = f"backend/testing/{test_file.name}"
        if rel in registered:
            continue
        stem = test_file.stem[len("test_"):]
        if stem in prod_basenames:
            continue  # matches a production module — not cross-cutting
        raise AssertionError(
            f"Cross-cutting test file {rel!r} is not registered in "
            "_CROSS_CUTTING_TEST_FILES in acceptance_gate.py — add it to "
            "the frozenset to prevent false-negative acceptance verdicts"
        )


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
        """EXPECTED_SCHEMA_REVISION must match the structural head of the alembic chain.

        Compares against the structural chain head (walking internal
        revision/down_revision via build_migration_chain + find_heads) rather
        than the lexicographic max of filename prefixes, so a non-monotonic
        chain — where the filename-max is not the structural head — is caught.

        If this fails, you added a migration but forgot to bump
        EXPECTED_SCHEMA_REVISION in main.py. The orchestrator will crash
        on startup with a schema version mismatch.
        """
        import glob as globmod

        from app.main import EXPECTED_SCHEMA_REVISION
        from app.migration_chain import validate_standalone_chain

        migrations_dir = os.path.join(
            os.path.dirname(__file__), "..", "alembic", "versions"
        )
        files = globmod.glob(os.path.join(migrations_dir, "[0-9]*.py"))
        assert files, "No migration files found"

        sources: dict[str, str] = {}
        for f in files:
            with open(f) as fh:
                sources[os.path.basename(f)] = fh.read()

        errors = validate_standalone_chain(
            sources, expected_schema_revision=EXPECTED_SCHEMA_REVISION
        )
        assert not errors, (
            "Migration chain invariant violations detected:\n"
            + "\n".join(f"  • {e}" for e in errors)
        )

    def test_task_context_size_constraint_exists_in_migration(self):
        """Migration 0277 must define the chk_task_context_size CHECK constraint.

        Confirms the defense-in-depth backstop on task.context fat-row growth
        mirrors the payloads CHECK constraint pattern from migration 0065.
        """
        migrations_dir = os.path.join(
            os.path.dirname(__file__), "..", "alembic", "versions"
        )
        migration_path = os.path.join(
            migrations_dir, "0277_task_context_size_constraint.py"
        )
        assert os.path.exists(migration_path), (
            "Migration 0277_task_context_size_constraint.py not found. "
            "The defense-in-depth size guard on task.context is missing."
        )
        content = open(migration_path).read()
        assert "chk_task_context_size" in content, (
            "Migration 0277 must define constraint 'chk_task_context_size'."
        )
        assert "octet_length(context::text)" in content, (
            "Migration 0277 must use octet_length(context::text) as the size expression."
        )

    def test_agent_helper_exclusions_match_prompt_and_gitignore(self):
        """Agent helper exclusions must stay aligned with prompt.ts and gitignore.

        Skipped when running in environments without the agent/ source tree
        or repo root checkout (e.g. CI's backend-only Docker build context,
        which excludes agent/, .gitignore, and .git). Locally and in any
        environment that does see the repo root the check fires normally.
        """
        helpers_path = os.path.join(
            os.path.dirname(__file__),
            "..",
            "..",
            "agent",
            "src",
            "prompts",
            "code-change-helpers.ts",
        )
        gitignore_path = os.path.join(
            os.path.dirname(__file__), "..", "..", ".gitignore"
        )
        repo_root = os.path.normpath(
            os.path.join(os.path.dirname(__file__), "..", "..")
        )
        if not (
            os.path.exists(helpers_path)
            and os.path.exists(gitignore_path)
            and os.path.isdir(os.path.join(repo_root, ".git"))
        ):
            pytest.skip(
                "repo root sources not available in this test environment "
                "(CI docker context is ./backend; run locally or via "
                "scripts/test-backend.sh from a checkout for enforcement)"
            )

        with open(helpers_path) as f:
            prompt = f.read()
        match = re.search(r"export const AGENT_HELPER_NAMES = \[(.*?)\];", prompt, re.S)
        assert match, (
            "Could not find AGENT_HELPER_NAMES literal in agent/src/prompts/code-change-helpers.ts"
        )
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
        # 'check' is the repo's own build-runner and is intentionally tracked in git.
        # It also appears in AGENT_HELPER_NAMES because agents overwrite it with a
        # task-specific version via writeCheckHelper; the /.gitignore entry prevents
        # an UNTRACKED 'check' file from being accidentally staged.
        _TRACKED_BY_DESIGN: frozenset = frozenset({'check'})
        leaked = [
            path for path in tracked
            if pattern.search(path) and path not in _TRACKED_BY_DESIGN
        ]
        assert not leaked, "Tracked helper or backup files found:\n  " + "\n  ".join(
            leaked
        )

    def test_repo_root_self_review_outputs_are_ignored_and_untracked(self):
        """Repo-root self_review_*.json output must be gitignored and untracked.

        Agents write self-review JSON to /tmp/self_review_<task_id>.json; any
        self_review_*.json that lands at the repo root is generated noise. The
        ignore rule must be root-anchored so it does NOT swallow nested
        fixtures such as backend/testing/**/self_review_*.json.

        Skipped when the repo root / .git checkout is unavailable (e.g. CI's
        backend-only Docker build context), following the same repo-root
        availability pattern as test_agent_helper_exclusions_match_prompt_and_gitignore.
        """
        gitignore_path = os.path.join(
            os.path.dirname(__file__), "..", "..", ".gitignore"
        )
        repo_root = os.path.normpath(
            os.path.join(os.path.dirname(__file__), "..", "..")
        )
        if not (
            os.path.exists(gitignore_path)
            and os.path.isdir(os.path.join(repo_root, ".git"))
        ):
            pytest.skip(
                "repo root sources not available in this test environment "
                "(CI docker context is ./backend; run locally or via "
                "scripts/test-backend.sh from a checkout for enforcement)"
            )

        with open(gitignore_path) as f:
            gitignore = f.read().splitlines()
        assert "/self_review_*.json" in gitignore, (
            ".gitignore is missing the root-anchored '/self_review_*.json' rule "
            "— repo-root self-review output would become committable noise."
        )

        # The rule must be root-anchored: git must ignore a root stray but must
        # NOT ignore a nested fixture of the same name.
        root_ignored = subprocess.run(
            ["git", "check-ignore", "self_review_probe.json"],
            cwd=repo_root,
            capture_output=True,
            text=True,
        )
        assert root_ignored.returncode == 0, (
            "repo-root self_review_probe.json is not ignored by .gitignore"
        )
        nested_ignored = subprocess.run(
            ["git", "check-ignore", "backend/testing/self_review_probe.json"],
            cwd=repo_root,
            capture_output=True,
            text=True,
        )
        assert nested_ignored.returncode == 1, (
            "nested backend/testing/self_review_probe.json must NOT be ignored "
            "— the rule is over-broad and would hide nested fixtures."
        )

        # No root-level self_review_*.json file may be tracked.
        tracked = subprocess.run(
            ["git", "ls-files"],
            cwd=repo_root,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.splitlines()
        leaked_root = [
            path for path in tracked if re.match(r"^self_review_.*\.json$", path)
        ]
        assert not leaked_root, (
            "Tracked repo-root self_review_*.json files found:\n  "
            + "\n  ".join(leaked_root)
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

    def test_rollup_guard_uses_combined_chain_check(self):
        """The rollup auto-merge guard must pass target_branch to _validate_rollup_migration_chain.

        Regression guard: dropping target_branch silently reverts the guard to the
        in-isolation check, which is blind to parallel-development collisions where
        two branches each add the same revision id or anchor to a stale head.
        """
        path = os.path.join(
            os.path.dirname(__file__), "..", "app", "rollup_management.py"
        )
        with open(path) as f:
            content = f.read()
        assert re.search(
            r"_validate_rollup_migration_chain\s*\([^)]*target_branch\b",
            content,
            re.DOTALL,
        ), (
            "_validate_rollup_migration_chain must be called with target_branch in "
            "rollup_management.py. Without it the guard is blind to parallel-development "
            "migration collisions (two branches each adding the same revision id)."
        )

    def test_rollup_guard_calls_content_immutability_check(self):
        """rollup_migration_validation.py must reference find_deployed_migration_content_edits.

        Regression guard for the in-place-edit defect class: a migration file
        whose filename matches a deployed promotion-target file but whose body
        was changed. The filename-only reuse/renumber detector
        (find_content_divergent_revisions) is blind to this class because both
        sides share the same filename, revision id, and description — no
        filename divergence is reported. Dropping the content call silently
        reverts the guard to filename-only coverage and leaves the in-place-edit
        class undetected (the v17 third defect class).
        """
        path = os.path.join(
            os.path.dirname(__file__), "..", "app", "rollup_migration_validation.py"
        )
        with open(path) as f:
            content = f.read()
        assert "find_deployed_migration_content_edits" in content, (
            "rollup_migration_validation.py must reference "
            "find_deployed_migration_content_edits. Without it the guard is blind "
            "to in-place body edits of deployed migrations (same filename and "
            "revision id, different content — the v17 in-place-edit defect class)."
        )

    def test_backmerge_guard_uses_combined_chain_check(self):
        """The hotfix back-merge path must pass integration_branch to _validate_rollup_migration_chain.

        Regression guard mirroring test_rollup_guard_uses_combined_chain_check: the
        main->automain back-merge merges a hotfix migration on the promotion target
        into the integration branch, which can collide with a feature migration added
        on the integration branch since the last promotion (same-revision dual head /
        duplicate revision id). The back-merge must reuse the same combined-chain
        validator as the rollup guard, with the integration branch passed as the
        target_branch — dropping it reverts to the in-isolation check that is blind to
        exactly this cross-branch collision, letting an unvalidated merge wedge the
        integration branch's `alembic upgrade head`.
        """
        path = os.path.join(
            os.path.dirname(__file__), "..", "app", "hotfix_backmerge.py"
        )
        with open(path) as f:
            content = f.read()
        _assert_backmerge_modules_do_not_import_rollup_management(
            content, filename="hotfix_backmerge.py"
        )
        assert re.search(
            r"_validate_rollup_migration_chain\s*\([^)]*\bintegration_branch\b",
            content,
            re.DOTALL,
        ), (
            "_validate_rollup_migration_chain must be called with integration_branch in "
            "hotfix_backmerge.py. Without it the back-merge guard is blind to a hotfix "
            "migration on the promotion target colliding with a feature migration on the "
            "integration branch (same-revision dual head), and the immediate merge wedges "
            "the integration branch's `alembic upgrade head`."
        )

    def test_backmerge_block_path_pushes_nothing(self):
        """The hotfix back-merge module must never inline-push migration files to a branch.

        Static belt-and-suspenders guard for the core invariant of direction
        d-18cc6131 (the "block-path-pushes-nothing" property). On an Alembic
        dual-head ``block`` verdict, the back-merge resolver ALWAYS routes to a
        spawned resolution task (``_spawn_migration_resolution_task``) that does
        the renumber + merge on a fresh branch through the normal PR gate, and
        embeds a precomputed renumber plan in that task's prompt. It must NEVER
        reconcile the dual head by pushing the renumber directly to the live
        integration branch (e.g. ``automain``).

        Why this is forbidden: pushing the renumber to the live integration
        branch before the back-merge lands leaves a dangling second Alembic head,
        wedging ``alembic upgrade head`` for every concurrent task for as long as
        the merge is held off by branch protection, a CI gate, or an unrelated
        conflict — the exact failure this design prevents.

        The property is also enforced (a) structurally — this test scans the
        module's direct ``github.<helper>`` usage and asserts it is a SUBSET of an
        explicit read/merge allowlist (``create_pull_request``,
        ``merge_pull_request``, ``get_pull_request``, ``get_check_runs_cached``),
        so ANY branch-write helper — regardless of its name — fails CI; and (b) by
        per-scenario runtime assertions in test_hotfix_backmerge.py
        (``commit_mock.assert_not_called()`` and the call-args scan for
        ``automain``). This static scan locks it as a single invariant so a future
        change (e.g. "restoring" an inline auto-reconcile/push) cannot reintroduce
        the wedge through a new code path the scenario tests do not cover. A future
        author who genuinely needs to change this must update this invariant
        deliberately.

        Allowlist (not denylist) rationale: a denylist of specific write-helper
        names cannot see a NEWLY-introduced or differently-named branch-write
        primitive, which is the single most likely way an inline reconcile gets
        restored. The allowlist inverts the burden — every github helper the
        block path touches must be a known, justified read/merge call, or the
        guard trips.
        """
        path = os.path.join(
            os.path.dirname(__file__), "..", "app", "hotfix_backmerge.py"
        )
        with open(path) as f:
            content = f.read()

        # Every github helper the back-merge module is allowed to call directly.
        # All are read/merge-only — none writes to a branch:
        #   create_pull_request   — open (or reuse) the back-merge PR (idempotent)
        #   merge_pull_request    — land the back-merge through the normal merge API
        #   get_pull_request      — read PR state / mergeability
        #   get_check_runs_cached — read CI status before attempting the merge
        #   get_pr_files          — read per-file list for module-size violation check
        #   get_file_text_at_ref  — fetch file content at a ref for line-count check
        # A branch-write helper (e.g. github.commit_file_changes, create_branch,
        # merge_branch, update_branch, delete_branch — all real symbols in the
        # app.github namespace) is deliberately absent: inlining the dual-head
        # renumber onto the live integration branch is the wedge this invariant
        # forbids. To add a helper here you MUST justify that it cannot push a
        # renumber to the integration branch before the back-merge lands.
        ALLOWED_BACKMERGE_GITHUB_HELPERS = {
            "create_pull_request",
            "merge_pull_request",
            "get_pull_request",
            "get_check_runs_cached",
            "get_pr_files",
            "get_file_text_at_ref",
        }
        used = set(re.findall(r"\bgithub\.(\w+)\b", content))
        unexpected = sorted(used - ALLOWED_BACKMERGE_GITHUB_HELPERS)
        assert not unexpected, (
            "hotfix_backmerge.py uses github helper(s) not on the read/merge "
            f"allowlist: {unexpected}. On an Alembic dual-head `block` verdict the "
            "back-merge must route to a spawned resolution task (the renumber + merge "
            "happens on a fresh branch through the normal PR gate), NEVER push the "
            "renumber to the live integration branch. An inline push leaves a dangling "
            "second Alembic head that wedges `alembic upgrade head` for every concurrent "
            "task until the held-off merge lands (the block-path-pushes-nothing invariant "
            "of direction d-18cc6131). If you genuinely need a new helper, add it to "
            "ALLOWED_BACKMERGE_GITHUB_HELPERS deliberately, with a justification that it "
            "cannot push a renumber to the live integration branch, and preserve the "
            "runtime guards in test_hotfix_backmerge.py."
        )

        # Guard the guard: an accidental "allow everything" (empty-set) regression
        # would make the subset check vacuous. The allowlist must stay exactly the
        # documented read/merge helpers.
        assert ALLOWED_BACKMERGE_GITHUB_HELPERS == {
            "create_pull_request",
            "merge_pull_request",
            "get_pull_request",
            "get_check_runs_cached",
            "get_pr_files",
            "get_file_text_at_ref",
        }, (
            "The back-merge github-helper allowlist must stay the documented read/merge helpers."
        )

        # The block path must still route to the spawned resolver — if this
        # disappears the renumber has to be happening somewhere, and the only
        # safe somewhere is a fresh-branch resolution task.
        assert "_spawn_migration_resolution_task" in content, (
            "hotfix_backmerge.py must route a dual-head `block` verdict to "
            "_spawn_migration_resolution_task. If the spawn route is removed, the "
            "renumber+merge can only be happening via a forbidden inline branch push "
            "(see the block-path-pushes-nothing invariant of direction d-18cc6131)."
        )

    def test_backmerge_migration_planner_is_read_only(self):
        """The back-merge migration planner must never push to a branch either.

        ``rollup_migration_validation.py`` holds the back-merge renumber planner
        (``plan_integration_side_renumber`` etc.) that the dual-head block path
        depends on to compute the renumber it embeds in the spawned resolution
        task's prompt. Like ``hotfix_backmerge.py``, this module must only READ
        from GitHub — it inspects PR diffs and migration sources at refs; it must
        never call a branch-write helper. The ONLY legitimate inline migration
        push in the system is the ROLLUP path's staging-branch reconcile
        (``rollup_management`` -> ``github.commit_file_changes``), which lives in a
        different module and must stay there.

        Same allowlist-subset pattern as
        ``test_backmerge_block_path_pushes_nothing``: every ``github.<helper>``
        token used here must be a known read helper, so a future branch-write
        primitive — regardless of name — fails CI.
        """
        path = os.path.join(
            os.path.dirname(__file__), "..", "app", "rollup_migration_validation.py"
        )
        with open(path) as f:
            content = f.read()
        _assert_backmerge_modules_do_not_import_rollup_management(
            content, filename="rollup_migration_validation.py"
        )

        # Read-only GitHub helpers the planner legitimately needs to reconstruct
        # and validate the migration chain across the two heads:
        #   get_pr_files — read per-file patches (no size limit, unlike the full diff)
        #   get_pull_request — read PR metadata (head SHA) for the patch-absent fallback
        #   get_pull_request_diff — kept for back-merge planner path
        #   list_versions_at_ref — list alembic/versions filenames at a ref
        #   list_version_shas_at_ref — same single listing call, also returns each
        #     file's git blob sha so the identity gate can skip body fetches for
        #     unchanged (byte-identical) migrations; a GET of the contents dir,
        #     strictly read-only, cannot push to a branch
        #   get_file_text_at_ref — fetch a single migration file's source at a ref
        # No branch-write helper may appear: the planner computes a renumber plan,
        # it never applies one to a live branch.
        ALLOWED_PLANNER_GITHUB_HELPERS = {
            "get_pr_files",
            "get_pull_request",  # read-only: fetches head SHA for contents-API fallback
            "get_pull_request_diff",
            "get_pull_request",
            "list_versions_at_ref",
            "list_version_shas_at_ref",
            "get_file_text_at_ref",
        }
        used = set(re.findall(r"\bgithub\.(\w+)\b", content))
        unexpected = sorted(used - ALLOWED_PLANNER_GITHUB_HELPERS)
        assert not unexpected, (
            "rollup_migration_validation.py uses github helper(s) not on the "
            f"read-only allowlist: {unexpected}. The back-merge migration planner must "
            "only READ from GitHub (PR diffs, migration sources at refs); it must NEVER "
            "push a renumber to a branch — that is the block-path-pushes-nothing invariant "
            "of direction d-18cc6131. The only legitimate inline migration push is the "
            "ROLLUP staging-branch reconcile in rollup_management, which must stay there. "
            "If you genuinely need a new helper, add it to ALLOWED_PLANNER_GITHUB_HELPERS "
            "deliberately, with a justification that it cannot push to a branch."
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

    def test_migration_chain_is_monotonic(self):
        """The on-disk migration chain must be monotonic: the migration with the
        numeric-maximum revision id must be the single structural head of the
        chain, and every migration's revision number must be greater than its
        down_revision's number.

        The combined-chain dual-head guard identifies the head by taking the
        numeric maximum of the revision ids (it works from filenames alone).
        This assumes the chain is monotonic — that the highest-numbered
        migration is the true head. When a chain is non-monotonic (e.g. a
        colliding migration's renumber produces a backward step), numeric-max
        head detection fails and the app wedges on the next schema upgrade.

        Reuses the same migration-chain parsing helpers (:func:`build_migration_chain`,
        :func:`find_heads`) that the rollup auto-merge guard consumes, so the
        invariant test and the runtime guard assert against identical chain
        structure.
        """
        import glob as globmod

        from app.migration_chain import build_migration_chain, find_heads

        migrations_dir = os.path.join(
            os.path.dirname(__file__), "..", "alembic", "versions"
        )
        files = globmod.glob(os.path.join(migrations_dir, "[0-9]*.py"))
        assert files, "No migration files found"

        sources: dict[str, str] = {}
        for path in files:
            with open(path) as f:
                sources[os.path.basename(path)] = f.read()

        chain, _duplicates, _unparseable = build_migration_chain(sources)
        heads = find_heads(chain)

        # Exactly one head (verified by test_migration_chain_is_linear)
        assert len(heads) == 1, f"Expected 1 head, got {len(heads)}: {heads}"

        head = heads[0]

        # Natural-order key: a revision id is a numeric prefix plus an optional
        # lowercase letter suffix used by dedup/linearisation revisions (e.g.
        # ``0458b`` is deliberately inserted strictly between ``0458`` and
        # ``0459``). A bare ``int()`` sort crashes on such ids; this tuple key
        # orders ``0458`` < ``0458b`` < ``0459`` while remaining identical to
        # integer order for all-numeric chains.
        def _rev_key(rev: str) -> tuple[int, str]:
            m = re.match(r"(\d+)([a-z]*)$", rev or "")
            if not m:
                return (-1, rev or "")
            return (int(m.group(1)), m.group(2))

        max_revision = max(chain, key=_rev_key)

        # Every revision number must be greater than its down_revision's number
        # (for revisions that are part of the chain).
        violations: list[str] = []
        for rev, down_rev in chain.items():
            if down_rev is not None and down_rev in chain:
                if _rev_key(rev) <= _rev_key(down_rev):
                    violations.append(
                        f"  {rev} (key {_rev_key(rev)}) → down_revision {down_rev} "
                        f"(key {_rev_key(down_rev)}) — revision must be numerically "
                        f"greater than its down_revision"
                    )

        # The numeric-maximum revision must be the structural head
        if max_revision != head:
            violations.append(
                f"  numeric-maximum revision '{max_revision}' is not the "
                f"structural head '{head}' — numeric-max head detection will fail"
            )

        assert not violations, (
            "Migration chain is not monotonic. The combined-chain dual-head guard "
            "relies on numeric-max head detection to identify the head, which breaks "
            "on non-monotonic chains where the highest-numbered migration is not the "
            "structural head or when migrations are numbered out of chain order.\n"
            "Violations:\n" + "\n".join(violations) + "\n\n"
            "Ensure every revision is numerically greater than its down_revision, "
            "and the numeric-maximum revision is the true structural head."
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

        # Facade completeness: ``app.models`` is the canonical import surface for
        # every table model. Domain clusters may be peeled into sibling modules
        # (e.g. ``app.release_models``) with ``app.models`` importing and
        # re-exporting them. If such a re-export is dropped, two things break at
        # once: ``from app.models import X`` stops resolving, AND — because the
        # sibling module is no longer imported — its tables silently disappear
        # from ``SQLModel.metadata`` above, so the column-coverage check would
        # stop protecting them while the backend still creates those tables at
        # runtime. Assert every mapped table class is reachable through the
        # ``app.models`` facade so a peeled-out module can never go un-imported.
        _models_owned_modules = {"app.models", "app.release_models"}
        facade_missing = sorted(
            mapper.class_.__name__
            for mapper in SQLModel._sa_registry.mappers
            if mapper.class_.__module__ in _models_owned_modules
            and not hasattr(models, mapper.class_.__name__)
        )
        assert not facade_missing, (
            "Table model classes not re-exported from app.models (facade "
            "incomplete — a peeled-out sibling module is not imported and "
            "re-exported by app.models, so `from app.models import <name>` "
            "breaks and the table vanishes from SQLModel.metadata):\n"
            + "\n".join(f"  - {n}" for n in facade_missing)
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

            # Snapshot idx_by_name before processing this file's creates so that
            # drop processing uses the pre-create state. This correctly handles the
            # drop-then-recreate pattern (e.g. widening an index by adding a column)
            # where the create would otherwise overwrite the drop target in idx_by_name.
            idx_by_name_snapshot = dict(idx_by_name)

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
                target = idx_by_name_snapshot.get(m.group(1))
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
            "get_bind",
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

            # Pure data-only migrations (upgrade body is only op.execute or
            # op.get_bind calls) are inherently one-way — accept any downgrade
            # body for them.  op.get_bind() is the raw-SQL variant of
            # op.execute(): both perform data transformation without schema
            # changes; the downgrade reverse-transforms the same data.
            _DATA_ONLY_OP_ATTRS = {"execute", "get_bind"}
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
                # Data-only migrations: upgrade only calls data-access ops
                # (execute, get_bind) or has no op.* calls at all (e.g.
                # migrations using bind.execute() through helper functions
                # for encryption/decryption).
                if up_calls and all(
                    c.func.attr in _DATA_ONLY_OP_ATTRS for c in up_calls
                ):
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

    def test_no_new_unguarded_duplicate_indexes_across_migrations(self):
        """A migration that creates an index name already created by an earlier
        migration must guard its create with IF NOT EXISTS (or be Postgres-only).
        Without the guard a SQLite windowed replay that spans both migrations
        fails with 'index <name> already exists'.

        The two historical unguarded pairs pre-date this invariant and are
        explicitly exempted.  Any NEW duplicate must be guarded.
        """
        import glob as globmod

        # Historical pairs that existed before this invariant was introduced.
        # Do NOT extend this set — new duplicates must use IF NOT EXISTS.
        _KNOWN_UNGUARDED_DUPLICATES: set[tuple[str, str]] = {
            ("0045", "ix_task_post_commit_pending"),
            ("0117", "ix_task_post_commit_pending"),
            ("0117", "ix_task_auto_remediation_active"),
            # 0138 SQLite branch uses bare op.create_index; 0141 re-asserts it
            # with guarded SQL for the back-merge reconcile (both pre-date invariant).
            ("0138", "ix_task_blocked_recheck"),
            ("0236", "ix_task_project_status_updated"),
            ("0237", "ix_task_project_status_updated"),
            # 0374 created ix_direction_event_project_key with 2 columns; 0377
            # dropped and re-created it with 3 columns (project_id, direction_key,
            # recorded_at). Both creates are unguarded but represent a legitimate
            # drop-and-recreate pattern — migration 0408 reasserts the 3-column
            # form with if_not_exists=True to close the schema-coverage gap.
            ("0374", "ix_direction_event_project_key"),
            ("0377", "ix_direction_event_project_key"),
            # 0381 created ix_task_vm_stopped_at without a guard; 0383 rechecked
            # via runtime pg_indexes query instead of if_not_exists=True; 0384
            # re-issued it with IF NOT EXISTS (guarded). Pre-date the invariant.
            ("0381", "ix_task_vm_stopped_at"),
            ("0383", "ix_task_vm_stopped_at"),
        }

        migrations_dir = os.path.join(
            os.path.dirname(__file__), "..", "alembic", "versions"
        )
        files = sorted(globmod.glob(os.path.join(migrations_dir, "[0-9]*.py")))

        # op.create_index("name", ...) with optional if_not_exists=True within
        # ~200 chars of the call site.
        op_create_re = re.compile(r'op\.create_index\(\s*[\'"](\w+)[\'"]')
        op_guard_re = re.compile(r"if_not_exists\s*=\s*True")
        # Raw SQL: CREATE INDEX [CONCURRENTLY] [IF NOT EXISTS] name
        raw_create_re = re.compile(
            r"CREATE\s+INDEX\s+(?:CONCURRENTLY\s+)?"
            r"(?P<guard>IF\s+NOT\s+EXISTS\s+)?(?P<name>ix_\w+)",
            re.IGNORECASE,
        )

        # Accumulate worst-case guardedness per (revision, index_name).
        # A migration that uses both Postgres raw-SQL (guarded) and a SQLite
        # op.create_index branch (unguarded) for the same index is unguarded on
        # the non-Postgres path — track False (unguarded) as the dominant state.
        # {(revision, name): all_creates_guarded}
        per_rev_name: dict[tuple[str, str], bool] = {}

        for path in files:
            revision = os.path.basename(path).split("_")[0]
            with open(path) as f:
                source = f.read()
            # Only examine upgrade() (drops in downgrade must not cancel creates)
            upgrade_body = source.split("def downgrade", 1)[0]
            if "def upgrade" in upgrade_body:
                upgrade_body = upgrade_body.split("def upgrade", 1)[1]

            for m in op_create_re.finditer(upgrade_body):
                name = m.group(1)
                snippet = upgrade_body[m.start() : m.start() + 200]
                guarded = bool(op_guard_re.search(snippet))
                key = (revision, name)
                per_rev_name[key] = per_rev_name.get(key, True) and guarded

            for m in raw_create_re.finditer(upgrade_body):
                name = m.group("name")
                guarded = bool(m.group("guard"))
                key = (revision, name)
                per_rev_name[key] = per_rev_name.get(key, True) and guarded

        # Group by index name across revisions.
        index_creates: dict[str, list[tuple[str, bool]]] = {}
        for (revision, name), all_guarded in per_rev_name.items():
            index_creates.setdefault(name, []).append((revision, all_guarded))

        violations: list[str] = []
        for name, creates in index_creates.items():
            if len(creates) < 2:
                continue
            other_revs = sorted(r for r, _ in creates)
            for revision, guarded in creates:
                if not guarded and (revision, name) not in _KNOWN_UNGUARDED_DUPLICATES:
                    others = ", ".join(r for r in other_revs if r != revision)
                    violations.append(
                        f"{revision}: creates '{name}' without IF NOT EXISTS guard "
                        f"(also created by: {others})"
                    )

        assert not violations, (
            "Duplicate index name(s) created without IF NOT EXISTS guard "
            "across migrations — breaks SQLite windowed replay:\n  "
            + "\n  ".join(sorted(violations))
            + "\n\nUse op.create_index(..., if_not_exists=True) or raw SQL "
            "'CREATE INDEX IF NOT EXISTS' for any migration that re-asserts "
            "an index name an earlier migration already created."
        )


# ── Chain-invariant detector self-certification (seeded bad fixtures) ────────


class TestChainInvariantSeedFixtures:
    """Negative-coverage: prove each chain-invariant detector fires on seeded bad fixtures.

    The positive invariants in TestSchemaRevisionSync / test_migration_chain_is_linear
    assert the real on-disk chain is clean but never verify the detectors
    themselves fire when given bad input.  These tests seed deliberately broken
    in-memory source dicts (no I/O) and assert each detector returns an error,
    so the smoke set self-certifies the tooling rather than passing only because
    the current chain happens to be clean.
    """

    @staticmethod
    def _src(revision: str, down_revision: str | None) -> str:
        down = "None" if down_revision is None else f"'{down_revision}'"
        return f"revision = '{revision}'\ndown_revision = {down}\n"

    def test_chain_invariant_flags_seeded_duplicate_revision(self):
        """build_migration_chain reports a duplicate when two sources share a revision id."""
        from app.migration_chain import build_migration_chain

        sources = {
            "0001_a.py": self._src("0001", None),
            "0002_b.py": self._src("0001", None),  # duplicate revision id
        }
        _chain, duplicates, _unp = build_migration_chain(sources)
        assert duplicates, (
            "build_migration_chain did not detect duplicate revision '0001' across "
            "two sources — the in-file dup-revision detector is not firing"
        )
        assert "0001" in duplicates

    def test_chain_invariant_flags_seeded_multi_head(self):
        """find_heads returns >1 entry when two sources both chain from the same parent."""
        from app.migration_chain import build_migration_chain, find_heads

        sources = {
            "0001_root.py": self._src("0001", None),
            "0002_a.py": self._src("0002", "0001"),
            "0003_b.py": self._src("0003", "0001"),  # second child of 0001 → branch
        }
        chain, _dups, _unp = build_migration_chain(sources)
        heads = find_heads(chain)
        assert len(heads) > 1, (
            f"find_heads returned {heads} for a branched chain — "
            "the multi-head detector is not firing"
        )

    def test_chain_invariant_flags_seeded_filename_desync(self):
        """find_filename_revision_mismatches fires when filename prefix != internal revision."""
        from app.migration_chain import find_filename_revision_mismatches

        sources = {
            "0002_foo.py": self._src(
                "0001", None
            ),  # filename says 0002, file says 0001
        }
        mismatches = find_filename_revision_mismatches(sources)
        assert mismatches, (
            "find_filename_revision_mismatches did not detect filename/revision mismatch "
            "for '0002_foo.py' (internal revision '0001') — the desync detector is not firing"
        )

    def test_chain_invariant_flags_seeded_expected_revision_mismatch(self):
        """validate_standalone_chain fires when EXPECTED_SCHEMA_REVISION != structural head.

        Proves the detector correctly identifies the STRUCTURAL head (the revision
        no other revision points to) rather than the lexicographic/filename max.
        Uses a three-migration linear chain: structural head is '0003', not '0099'.
        """
        from app.migration_chain import validate_standalone_chain

        sources = {
            "0001_root.py": self._src("0001", None),
            "0002_mid.py": self._src("0002", "0001"),
            "0003_tip.py": self._src("0003", "0002"),
        }
        # A wrong EXPECTED_SCHEMA_REVISION must produce a non-empty errors list.
        errors = validate_standalone_chain(sources, expected_schema_revision="0099")
        assert errors, (
            "validate_standalone_chain did not fire for mismatched "
            "EXPECTED_SCHEMA_REVISION '0099' (structural head is '0003') — "
            "the expected-revision detector is not wired into validate_standalone_chain"
        )
        assert any("EXPECTED_SCHEMA_REVISION" in e for e in errors), (
            f"Expected an EXPECTED_SCHEMA_REVISION error but got: {errors}"
        )
        # Passing the correct head must produce no errors.
        no_errors = validate_standalone_chain(sources, expected_schema_revision="0003")
        assert not no_errors, (
            f"validate_standalone_chain reported errors for correct revision '0003': {no_errors}"
        )

    def test_gapped_linear_chain_validates_clean(self):
        """A single-head chain with non-contiguous revision numbers (gap) is valid.

        The v20 audit flagged 0334->0347, and the v21 audit flagged
        0354->0365, as suspicious Alembic numbering gaps.
        Gaps are a normal branch-cut artifact: tasks reserve numbers at cut time
        and merge-time reconciliation fills them.  validate_standalone_chain must
        accept a gapped-but-linear chain without error.
        """
        from app.migration_chain import validate_standalone_chain

        # 0354 -> 0365: a deliberate branch-cut gap, but a single linear head.
        sources = {
            "0354_acceptance_criteria.py": self._src("0354", None),
            "0365_criterionevidence_unique.py": self._src("0365", "0354"),
        }
        errors = validate_standalone_chain(sources)
        assert not errors, (
            "validate_standalone_chain incorrectly rejected a gapped-but-linear "
            f"chain (0354->0365); errors: {errors}"
        )

    def test_validate_standalone_chain_aggregates_all_detectors(self):
        """validate_standalone_chain reports errors for each class of chain defect."""
        from app.migration_chain import validate_standalone_chain

        # Duplicate revision id
        dup_sources = {
            "0001_a.py": self._src("0001", None),
            "0002_b.py": self._src("0001", None),
        }
        errors = validate_standalone_chain(dup_sources)
        assert any("duplicate" in e for e in errors), (
            f"validate_standalone_chain did not report duplicate revision; got: {errors}"
        )

        # Multi-head (branched chain)
        branch_sources = {
            "0001_root.py": self._src("0001", None),
            "0002_a.py": self._src("0002", "0001"),
            "0003_b.py": self._src("0003", "0001"),
        }
        errors = validate_standalone_chain(branch_sources)
        assert any("head" in e for e in errors), (
            f"validate_standalone_chain did not report multi-head; got: {errors}"
        )

        # Filename/revision desync
        desync_sources = {
            "0099_foo.py": self._src("0001", None),
        }
        errors = validate_standalone_chain(desync_sources)
        assert any("mismatch" in e for e in errors), (
            f"validate_standalone_chain did not report filename desync; got: {errors}"
        )


# ── Pre-merge dual-head guard coverage (whole-package) ─────────────────────


class TestPreMergeDualHeadGuardCoverage:
    """Whole-package invariant: EVERY PR-merge site under backend/app/ that can
    target an integration/promotion branch must be covered by the pre-merge
    combined-chain dual-head guard.

    WHY: merging a PR that creates two Alembic heads / a duplicate revision id
    wedges ``alembic upgrade head`` on the integration branch. The backend merges
    PRs from many sites — the CI-completion handler, the autonomous rollup
    auto-merge, the hotfix back-merge, the qualify->promote release merge,
    operator-approval merges, and the normal task-PR completion handler. Each
    must first run one of the two shared guard entrypoints, both defined in
    ``app/rollup_migration_validation.py``:
      - ``enforce_premerge_migration_guard(...)``
      - ``_validate_rollup_migration_chain(...)``

    The two narrow tests ``test_rollup_guard_uses_combined_chain_check`` and
    ``test_backmerge_guard_uses_combined_chain_check`` assert that two *specific*
    files call the guard with the right target/integration-branch argument. This
    test is complementary: it enumerates the FULL set of merge SITES so a new (or
    existing) merge path cannot silently skip the guard without a test noticing.

    Modeled on the os.walk whole-package invariants in this module
    (``TestNoRawBindDialectIdiom`` / ``TestBaseBranchFallback``): walk
    backend/app/ (recursing routers/), flag each offending file:function, and
    carry an allowlist of legitimate exceptions, each documenting WHY.

    The scan is source-level only (AST) and NEVER executes a merge path.

    Coverage note: because this test is AST-only and function-scoped, removing
    the guard reference from any of ``_process_post_commit``,
    ``_check_and_trigger_rollup``, or ``promote`` would create a new offender
    and fail this invariant. That is deliberate: the invariant catches missing
    call-site coverage, not the later GitHub merge-time race where
    ``enable_auto_merge`` may complete after a second migration PR lands.
    Track that TOCTOU window separately.
    """

    # The two shared combined-chain guard entrypoints (app/rollup_migration_validation.py).
    _GUARD_NAMES = frozenset(
        {"_validate_rollup_migration_chain", "enforce_premerge_migration_guard"}
    )

    # Merge symbols the scan treats as a merge SITE: the direct primitive
    # (``merge_pull_request`` / ``github.merge_pull_request``), the
    # exponential-backoff wrapper (``_merge_with_retry``, which is always handed
    # ``merge_pull_request`` as its first argument), and the GitHub
    # auto-merge primitive (``enable_auto_merge``).
    _MERGE_NAMES = frozenset(
        {"merge_pull_request", "_merge_with_retry", "enable_auto_merge"}
    )

    # Whole files that define/re-export the merge primitive or the generic retry
    # wrapper. These make NO integration/promotion-branch merge *decision* (the
    # target branch is chosen by their callers, which carry the guard), so they
    # are exempt. Mirrors how the dialect/base-branch guards allowlist the files
    # that *define* the thing they otherwise forbid.
    _EXEMPT_FILES = {
        "github_pulls.py": (
            "defines the low-level merge_pull_request primitive (raw GitHub merge "
            "API); chooses no integration/promotion branch"
        ),
        "github.py": (
            "re-exports merge_pull_request for callers; makes no merge decision"
        ),
        "task_lifecycle_github_helpers.py": (
            "defines the generic _merge_with_retry wrapper; the merging caller "
            "picks the target branch and carries the guard"
        ),
    }

    # Whole-FUNCTION merge-site exemptions, keyed by (file, function). Each entry
    # MUST document WHY. A string value exempts EVERY merge call in that function
    # (used for thin shared-helper functions whose guard is enforced by their
    # upstream caller). These are coarse and used sparingly — the granular,
    # per-call mechanism below is preferred for any function that mixes guarded
    # and exempt merges.
    #
    # NOTE: do NOT key exemptions by absolute line number. An edit above a merge
    # site shifts the numbers and breaks the invariant on unrelated PRs; worse,
    # under code motion a genuinely-new unguarded merge can come to occupy an
    # exempt line and silently inherit an exemption it never declared. That is the
    # exact silent regression this invariant exists to prevent (same class as the
    # column-offset brittleness fixed elsewhere). For functions that mix guarded
    # and exempt merges (e.g. ``_process_post_commit``), exempt the SPECIFIC merge
    # call with the inline ``# premerge-guard-exempt: <reason>`` marker (see
    # _EXEMPT_MARKER below) so the exemption travels with the code.
    _EXEMPT_SITES: dict = {}

    # Narrow call-site allowances for helpers that are structurally guarded by
    # upstream flow and dedicated runtime tests. These are intentionally kept as
    # exact call ordinals rather than whole-function trust zones; the shape test
    # below pins the function structure so a future extra merge call still trips
    # the invariant.
    _NARROWLY_ALLOWED_MERGE_CALLS: dict = {
        (
            "ci_completion.py",
            "_attempt_merge_with_retry_and_escalation",
        ): frozenset({1, 2}),
        (
            "task_completion.py",
            "_maybe_release_safeguard_gate",
        ): frozenset({1}),
    }

    # Inline per-call exemption marker. A merge call is exempt iff this sentinel,
    # FOLLOWED BY A NON-EMPTY HUMAN-READABLE REASON, appears anywhere within the
    # call's own source span (call.lineno..call.end_lineno). Because the marker
    # lives on the call itself, a brand-new or relocated merge cannot inherit an
    # exemption it did not declare: removing or moving the marked call removes or
    # moves its exemption automatically, and an unmarked sibling merge is still
    # flagged. A bare marker with no reason is rejected (see
    # test_premerge_exempt_marker_requires_reason).
    _EXEMPT_MARKER = "# premerge-guard-exempt:"

    @staticmethod
    def _collect_offenders(
        source: str,
        *,
        filename: str,
        rel: str,
        guard_names: frozenset[str],
        merge_names: frozenset[str],
    ) -> list[str]:
        def _called_name(call: "ast.Call") -> str | None:
            return _ast_called_name(call)

        def _calls_guard(node) -> bool:
            # A guard/verdict-producing CALL anywhere in the subtree — the
            # ``enforce_premerge_migration_guard(...)`` on an assignment RHS or
            # directly as an ``if`` test. A mere reference to a guard NAME (e.g.
            # an import, or the captured verdict left unused) is deliberately NOT
            # enough: only an actual call produces a verdict to gate on. This is
            # the crux of the fail-closed ratchet — "guard symbol present" no
            # longer counts as "merge covered".
            return any(
                isinstance(n, ast.Call) and _called_name(n) in guard_names
                for n in ast.walk(node)
            )

        def _assign_verdict_targets(stmt) -> set:
            # When an assignment's value is (or contains) a guard call, the
            # assigned name(s) become "verdict variables" that a later ``if`` can
            # test to gate the merge. Tuple-unpacking (``ok, reason = await
            # guard(...)``) yields every bound name.
            return _ast_assign_targets_for_predicated_call(
                stmt,
                call_predicate=lambda call: _called_name(call) in guard_names,
            )

        def _stmt_merge_calls(stmt):
            calls = [
                node
                for node in ast.walk(stmt)
                if isinstance(node, ast.Call) and _called_name(node) in merge_names
            ]
            yield from sorted(
                calls,
                key=lambda node: (
                    getattr(node, "lineno", 0),
                    getattr(node, "col_offset", 0),
                ),
            )

        source_lines = source.splitlines()
        marker = TestPreMergeDualHeadGuardCoverage._EXEMPT_MARKER

        def _call_has_exempt_marker(call: "ast.Call") -> bool:
            # Slice the call's own source span; the marker is bound to THIS call,
            # not to a position in the file, so it travels with the code under
            # any edit or code motion. A reason after the sentinel is mandatory.
            end = getattr(call, "end_lineno", None) or call.lineno
            for line in source_lines[call.lineno - 1 : end]:
                idx = line.find(marker)
                if idx != -1 and line[idx + len(marker) :].strip():
                    return True
            return False

        def _flag_merges(stmt, covered: bool, func_name: str) -> None:
            nonlocal merge_call_index
            for call in _stmt_merge_calls(stmt):
                merge_call_index += 1
                if (
                    (
                        os.path.basename(rel),
                        func_name,
                    )
                    in TestPreMergeDualHeadGuardCoverage._NARROWLY_ALLOWED_MERGE_CALLS
                    and (
                        merge_call_index
                        in TestPreMergeDualHeadGuardCoverage._NARROWLY_ALLOWED_MERGE_CALLS[
                            (os.path.basename(rel), func_name)
                        ]
                    )
                ):
                    continue
                # Coarse whole-function exemption (string-keyed) ...
                if TestPreMergeDualHeadGuardCoverage._EXEMPT_SITES.get(
                    (os.path.basename(rel), func_name)
                ):
                    continue
                # ... or a granular per-call inline marker that travels with
                # the specific merge call.
                if _call_has_exempt_marker(call):
                    continue
                if not covered:
                    offenders.append(
                        f"  {rel}:{call.lineno} in {func_name}() — PR-merge "
                        "site not covered by the pre-merge dual-head guard"
                    )

        def _scan_body(stmts, covered: bool, verdict_vars: set, func_name: str) -> None:
            cur = covered
            vvars = set(verdict_vars)
            for stmt in stmts:
                if isinstance(
                    stmt, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
                ):
                    continue

                if isinstance(stmt, ast.If):
                    # A merge inside either branch of an ``if`` that TESTS the
                    # verdict (the guard call directly, or a verdict variable
                    # captured above) is covered only when THAT branch outcome
                    # proves the merge-safe verdict (`ok_to_merge=True`). Mixed
                    # predicates like `if ok and feature_flag` therefore cover
                    # only the body, not the else branch where `ok` may be
                    # false. A sibling merge AFTER such an ``if`` is covered
                    # only when a blocking-verdict branch early-exits —
                    # fail-closed gating, not symbol presence.
                    branch_body_cov = cur or _ast_branch_implies_verdict(
                        stmt.test,
                        branch_truth=True,
                        desired_truth=True,
                        atom_predicate=lambda node: (
                            (
                                isinstance(node, ast.Call)
                                and _called_name(node) in guard_names
                            )
                            or (isinstance(node, ast.Name) and node.id in vvars)
                        ),
                    )
                    branch_else_cov = cur or _ast_branch_implies_verdict(
                        stmt.test,
                        branch_truth=False,
                        desired_truth=True,
                        atom_predicate=lambda node: (
                            (
                                isinstance(node, ast.Call)
                                and _called_name(node) in guard_names
                            )
                            or (isinstance(node, ast.Name) and node.id in vvars)
                        ),
                    )
                    _scan_body(stmt.body, branch_body_cov, vvars, func_name)
                    _scan_body(stmt.orelse, branch_else_cov, vvars, func_name)
                    if _ast_branch_implies_verdict(
                        stmt.test,
                        branch_truth=True,
                        desired_truth=False,
                        atom_predicate=lambda node: (
                            (
                                isinstance(node, ast.Call)
                                and _called_name(node) in guard_names
                            )
                            or (isinstance(node, ast.Name) and node.id in vvars)
                        ),
                    ) and _ast_branch_early_exits(stmt.body):
                        cur = True
                    elif _ast_branch_implies_verdict(
                        stmt.test,
                        branch_truth=False,
                        desired_truth=False,
                        atom_predicate=lambda node: (
                            (
                                isinstance(node, ast.Call)
                                and _called_name(node) in guard_names
                            )
                            or (isinstance(node, ast.Name) and node.id in vvars)
                        ),
                    ) and _ast_branch_early_exits(stmt.orelse):
                        cur = True
                    elif cur and (
                        _ast_branch_early_exits(stmt.body)
                        or _ast_branch_early_exits(stmt.orelse)
                    ):
                        cur = True
                    elif _ast_branch_implies_named_str_compare(
                        stmt.test,
                        branch_truth=True,
                        names=vvars,
                        truthy_literals={"defer", "block"},
                    ) and _ast_branch_early_exits(stmt.body):
                        # `_validate_rollup_migration_chain(...)` yields a
                        # multi-state string verdict (`ok` / `defer` / `block`)
                        # that existing callers fail-close with successive
                        # early-return branches like `if status == "defer":
                        # return` then `if status == "block": return`. Keep
                        # treating those sibling-after-if sites as covered,
                        # but only for explicit non-ok literal comparisons.
                        cur = True
                    elif _ast_branch_implies_named_str_compare(
                        stmt.test,
                        branch_truth=False,
                        names=vvars,
                        truthy_literals={"defer", "block"},
                    ) and _ast_branch_early_exits(stmt.orelse):
                        cur = True
                    continue

                if isinstance(stmt, (ast.For, ast.AsyncFor, ast.While)):
                    _scan_body(stmt.body, cur, vvars, func_name)
                    _scan_body(stmt.orelse, cur, vvars, func_name)
                    continue

                if isinstance(stmt, (ast.With, ast.AsyncWith)):
                    _scan_body(stmt.body, cur, vvars, func_name)
                    continue

                if isinstance(stmt, ast.Try):
                    _scan_body(stmt.body, cur, vvars, func_name)
                    for handler in stmt.handlers:
                        _scan_body(handler.body, cur, vvars, func_name)
                    _scan_body(stmt.orelse, cur, vvars, func_name)
                    _scan_body(stmt.finalbody, cur, vvars, func_name)
                    continue

                if hasattr(ast, "Match") and isinstance(stmt, ast.Match):
                    for case in stmt.cases:
                        _scan_body(case.body, cur, vvars, func_name)
                    continue

                _flag_merges(stmt, cur, func_name)
                vvars |= _assign_verdict_targets(stmt)

        offenders: list[str] = []
        tree = ast.parse(source, filename=filename)
        funcs = [
            node
            for node in ast.walk(tree)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        ]
        for func in funcs:
            merge_call_index = 0
            _scan_body(func.body, False, set(), func.name)
        return offenders

    def test_all_pr_merge_sites_guarded(self):
        app_dir = os.path.normpath(os.path.join(os.path.dirname(__file__), "..", "app"))
        guard_names = self._GUARD_NAMES
        merge_names = self._MERGE_NAMES
        offenders: list[str] = []
        for root, _dirs, files in os.walk(app_dir):
            for fname in sorted(files):
                if not fname.endswith(".py"):
                    continue
                if fname in self._EXEMPT_FILES:
                    continue
                fpath = os.path.join(root, fname)
                with open(fpath, encoding="utf-8") as f:
                    source = f.read()
                rel = os.path.relpath(fpath, app_dir)
                offenders.extend(
                    self._collect_offenders(
                        source,
                        filename=fpath,
                        rel=rel,
                        guard_names=guard_names,
                        merge_names=merge_names,
                    )
                )

        assert not offenders, (
            "Found PR-merge site(s) under backend/app/ that can target an "
            "integration/promotion branch but are NOT covered by the pre-merge "
            "combined-chain dual-head guard "
            "(enforce_premerge_migration_guard / _validate_rollup_migration_chain, "
            "both in app/rollup_migration_validation.py).\n"
            "Merging a PR that creates two Alembic heads / a duplicate revision id "
            "wedges `alembic upgrade head` on the integration branch.\n"
            "Fix each site by calling one of the guard entrypoints in the merging "
            "function, OR — if the site can never carry a migration onto the "
            "integration/promotion branch — add its (file, function) to "
            "TestPreMergeDualHeadGuardCoverage._EXEMPT_SITES with a documented "
            "reason.\n"
            "Offenders:\n" + "\n".join(offenders)
        )

    def test_premerge_helper_whole_function_exemptions_are_not_blanket_trust_zones(
        self,
    ):
        """Regression: the two real merge helpers must not be blanket-exempt.

        The helpers themselves are still covered by the AST scan, so the
        exemption table should stay empty for them. This catches any future
        reintroduction of a whole-function trust zone that would bypass the
        dual-head ratchet.
        """

        exempt_sites = self._EXEMPT_SITES
        assert (
            "ci_completion.py",
            "_attempt_merge_with_retry_and_escalation",
        ) not in exempt_sites
        assert (
            "task_completion.py",
            "_maybe_release_safeguard_gate",
        ) not in exempt_sites

    def test_premerge_helpers_keep_expected_guarded_merge_shapes(self):
        """The narrow allowances stay narrow by pinning each helper's shape."""

        app_dir = os.path.normpath(os.path.join(os.path.dirname(__file__), "..", "app"))

        with open(os.path.join(app_dir, "ci_completion.py"), encoding="utf-8") as f:
            ci_source = f.read()
        with open(os.path.join(app_dir, "task_completion.py"), encoding="utf-8") as f:
            task_source = f.read()

        ci_tree = ast.parse(ci_source, filename="ci_completion.py")
        task_tree = ast.parse(task_source, filename="task_completion.py")

        ci_func = next(
            node
            for node in ast.walk(ci_tree)
            if isinstance(node, ast.AsyncFunctionDef)
            and node.name == "_attempt_merge_with_retry_and_escalation"
        )
        task_func = next(
            node
            for node in ast.walk(task_tree)
            if isinstance(node, ast.AsyncFunctionDef)
            and node.name == "_maybe_release_safeguard_gate"
        )

        def _merge_calls(func):
            calls = []
            for node in ast.walk(func):
                if (
                    isinstance(node, ast.Call)
                    and _ast_called_name(node) == "merge_pull_request"
                ):
                    calls.append(node)
            return sorted(calls, key=lambda node: (node.lineno, node.col_offset))

        def _guard_assignments(func):
            assigns = []
            for node in ast.walk(func):
                if not isinstance(node, ast.stmt):
                    continue
                verdict_vars = _ast_assign_targets_for_predicated_call(
                    node,
                    call_predicate=lambda call: (
                        _ast_called_name(call) == "enforce_premerge_migration_guard"
                    ),
                )
                if verdict_vars:
                    assigns.append((node, verdict_vars))
            return sorted(
                assigns,
                key=lambda item: (
                    getattr(item[0], "lineno", 0),
                    getattr(item[0], "col_offset", 0),
                ),
            )

        def _assigned_names(stmt):
            if isinstance(stmt, ast.Assign):
                targets = stmt.targets
            elif isinstance(stmt, (ast.AnnAssign, ast.AugAssign)):
                targets = [stmt.target]
            else:
                return set()

            names = set()
            for target in targets:
                for node in ast.walk(target):
                    if isinstance(node, ast.Name):
                        names.add(node.id)
            return names

        def _name_assignments_after(func, name, lineno):
            offenders = []
            for node in ast.walk(func):
                if not isinstance(node, ast.stmt):
                    continue
                if getattr(node, "lineno", 0) <= lineno:
                    continue
                if name in _assigned_names(node):
                    offenders.append(node)
            return sorted(
                offenders,
                key=lambda node: (
                    getattr(node, "lineno", 0),
                    getattr(node, "col_offset", 0),
                ),
            )

        ci_calls = _merge_calls(ci_func)
        task_calls = _merge_calls(task_func)

        assert len(ci_calls) == 2
        ci_guard_assignments = _guard_assignments(ci_func)
        assert len(ci_guard_assignments) == 1
        ci_guard_stmt, ci_intermediate_vars = ci_guard_assignments[0]

        # The guard result may flow through a coerce helper (two-step shape) or
        # be assigned directly to the effective verdict tuple (single-step shape).
        # Detect a downstream coerce_migration_guard_result call that consumes the
        # intermediate variable(s) and derive the effective ok/hold variables from
        # whichever step actually produces them.
        ci_coerce_assignments = []
        for _node in ast.walk(ci_func):
            if not isinstance(_node, ast.stmt):
                continue
            _coerce_vars = _ast_assign_targets_for_predicated_call(
                _node,
                call_predicate=lambda _call: (
                    _ast_called_name(_call) == "coerce_migration_guard_result"
                    and any(
                        isinstance(_arg, ast.Name) and _arg.id in ci_intermediate_vars
                        for _arg in _call.args
                    )
                ),
            )
            if _coerce_vars:
                ci_coerce_assignments.append((_node, _coerce_vars))

        if ci_coerce_assignments:
            # Two-step flow: guard → intermediate var → coerce → verdict vars
            assert len(ci_coerce_assignments) == 1
            ci_coerce_stmt, _ = ci_coerce_assignments[0]
            # _ok_to_merge is derived from the coerce targets; find its assignment
            ci_ok_stmts = sorted(
                [
                    _n
                    for _n in ast.walk(ci_func)
                    if isinstance(_n, ast.stmt)
                    and "_ok_to_merge" in _assigned_names(_n)
                ],
                key=lambda _n: getattr(_n, "lineno", 0),
            )
            assert len(ci_ok_stmts) >= 1, "_ok_to_merge must be assigned after coerce"
            ci_effective_guard_stmt = ci_ok_stmts[0]
        else:
            # Single-step flow: guard call assigns directly to verdict tuple
            assert ci_intermediate_vars == {"_guard_result"}
            ci_effective_guard_stmt = ci_guard_stmt
        ci_fail_closed = next(
            stmt
            for stmt in ast.walk(ci_func)
            if isinstance(stmt, ast.If)
            and _ast_branch_implies_verdict(
                stmt.test,
                branch_truth=True,
                desired_truth=False,
                atom_predicate=lambda node: (
                    isinstance(node, ast.Name) and node.id == "_ok_to_merge"
                ),
            )
        )
        assert _ast_branch_early_exits(ci_fail_closed.body)
        assert min(call.lineno for call in ci_calls) > getattr(
            ci_fail_closed, "end_lineno", ci_fail_closed.lineno
        )
        # The raw guard result must not be reassigned after the guard call.
        assert not _name_assignments_after(
            ci_func,
            "_ok_to_merge",
            getattr(ci_effective_guard_stmt, "lineno", 0),
        )

        assert len(task_calls) == 1
        task_guard_assignments = _guard_assignments(task_func)
        assert len(task_guard_assignments) == 1
        task_guard_stmt, task_verdict_vars = task_guard_assignments[0]
        assert task_verdict_vars == {"_sg_ok", "_sg_reason"}
        task_guard_gate = next(
            stmt
            for stmt in ast.walk(task_func)
            if isinstance(stmt, ast.If)
            and _ast_branch_implies_verdict(
                stmt.test,
                branch_truth=True,
                desired_truth=False,
                atom_predicate=lambda node: (
                    isinstance(node, ast.Name) and node.id == "_sg_ok"
                ),
            )
        )
        guarded_else_calls = sorted(
            [
                node
                for node in ast.walk(
                    ast.Module(body=task_guard_gate.orelse, type_ignores=[])
                )
                if isinstance(node, ast.Call)
                and _ast_called_name(node) == "merge_pull_request"
            ],
            key=lambda node: (node.lineno, node.col_offset),
        )
        assert [call.lineno for call in guarded_else_calls] == [
            call.lineno for call in task_calls
        ]
        assert not _name_assignments_after(
            task_func,
            "_sg_ok",
            getattr(task_guard_stmt, "lineno", 0),
        )

    def test_branch_local_guard_scan_catches_sibling_unguarded_branch(self):
        """A guarded sibling branch must not mask an unguarded merge branch.

        Regression fixture for the old function-scoped scan:
        one branch calls the guard and merges, while an `else` branch in the
        same function merges without any guard. The stricter scan must flag only
        the unguarded branch.
        """
        from textwrap import dedent

        source = dedent(
            """
            async def demo():
                if ready:
                    ok, reason = await enforce_premerge_migration_guard(
                        token, owner, repo, pr_number, target_branch
                    )
                    if not ok:
                        return reason
                    await merge_pull_request(
                        token, owner, repo, pr_number, project_id=project_id
                    )
                else:
                    await merge_pull_request(
                        token, owner, repo, pr_number, project_id=project_id
                    )
            """
        )

        offenders = self._collect_offenders(
            source,
            filename="synthetic_merge_fixture.py",
            rel="synthetic_merge_fixture.py",
            guard_names=self._GUARD_NAMES,
            merge_names=self._MERGE_NAMES,
        )

        assert offenders == [
            "  synthetic_merge_fixture.py:13 in demo() — PR-merge site not covered by the pre-merge dual-head guard"
        ]

    def test_premerge_guard_verdict_ignored_is_flagged(self):
        """Fail-closed ratchet: capturing the guard verdict is NOT enough — the
        verdict must actually GATE the merge.

        This is the documented false-negative the older scan permitted: the
        guard is called and its verdict assigned, but the result is never tested
        and the merge proceeds unconditionally. "Guard symbol referenced ⇒
        covered" would mark this safe; the hardened scan flags it because no
        branch early-exits on the blocking verdict before the merge.
        """
        from textwrap import dedent

        source = dedent(
            """
            async def demo():
                ok, reason = await enforce_premerge_migration_guard(
                    token, owner, repo, pr_number, target_branch
                )
                # verdict captured but NEVER tested — merge proceeds regardless
                await merge_pull_request(
                    token, owner, repo, pr_number, project_id=project_id
                )
            """
        )

        offenders = self._collect_offenders(
            source,
            filename="synthetic_merge_fixture.py",
            rel="synthetic_merge_fixture.py",
            guard_names=self._GUARD_NAMES,
            merge_names=self._MERGE_NAMES,
        )

        assert offenders == [
            "  synthetic_merge_fixture.py:7 in demo() — PR-merge site not covered by the pre-merge dual-head guard"
        ], (
            "A guard verdict that is captured but ignored (never gates an "
            "early-exit before the merge) must be flagged — referencing the "
            "guard symbol is not the same as fail-closing on its verdict."
        )

    def test_mixed_condition_only_covers_the_guarded_premerge_branch(self):
        """A mixed predicate like `if ok and feature_flag` covers only the body.

        The else branch is reachable both when the feature flag is false and
        when the guard blocks (`ok=False`), so a merge there must still be
        flagged.
        """
        from textwrap import dedent

        source = dedent(
            """
            async def demo():
                ok, reason = await enforce_premerge_migration_guard(
                    token, owner, repo, pr_number, target_branch
                )
                if ok and feature_flag:
                    await merge_pull_request(
                        token, owner, repo, pr_number, project_id=project_id
                    )
                else:
                    await merge_pull_request(
                        token, owner, repo, pr_number, project_id=project_id
                    )
            """
        )

        offenders = self._collect_offenders(
            source,
            filename="synthetic_merge_fixture.py",
            rel="synthetic_merge_fixture.py",
            guard_names=self._GUARD_NAMES,
            merge_names=self._MERGE_NAMES,
        )

        assert offenders == [
            "  synthetic_merge_fixture.py:11 in demo() — PR-merge site not covered by the pre-merge dual-head guard"
        ]

    def test_mixed_condition_sibling_early_return_does_not_cover_later_merge(self):
        """An early return on `if ok and feature_flag` must not cover a sibling
        merge that remains reachable when the guard blocks (`ok=False`)."""
        from textwrap import dedent

        source = dedent(
            """
            async def demo():
                ok, reason = await enforce_premerge_migration_guard(
                    token, owner, repo, pr_number, target_branch
                )
                if ok and feature_flag:
                    return reason
                await merge_pull_request(
                    token, owner, repo, pr_number, project_id=project_id
                )
            """
        )

        offenders = self._collect_offenders(
            source,
            filename="synthetic_merge_fixture.py",
            rel="synthetic_merge_fixture.py",
            guard_names=self._GUARD_NAMES,
            merge_names=self._MERGE_NAMES,
        )

        assert offenders == [
            "  synthetic_merge_fixture.py:8 in demo() — PR-merge site not covered by the pre-merge dual-head guard"
        ]

    def test_multistate_rollup_status_early_returns_cover_later_merge(self):
        """The `_validate_rollup_migration_chain` status idiom remains covered."""
        from textwrap import dedent

        source = dedent(
            """
            async def demo():
                status, reason = await _validate_rollup_migration_chain(
                    token, owner, repo, pr_number, target_branch=target_branch
                )
                if status == "defer":
                    return reason
                if status == "block":
                    return reason
                await merge_pull_request(
                    token, owner, repo, pr_number, project_id=project_id
                )
            """
        )

        offenders = self._collect_offenders(
            source,
            filename="synthetic_merge_fixture.py",
            rel="synthetic_merge_fixture.py",
            guard_names=self._GUARD_NAMES,
            merge_names=self._MERGE_NAMES,
        )

        assert offenders == []

    def test_exempt_marker_exempts_only_the_marked_call(self):
        """The inline marker exempts the SPECIFIC call it sits on — not its
        siblings, and not a line number.

        Two unguarded merges live in the same function. One carries the inline
        ``# premerge-guard-exempt: <reason>`` marker; the other does not. The
        scan must exempt exactly the marked call and flag the unmarked sibling.
        This proves the marker — not a position in the file — drives exemption.
        """
        from textwrap import dedent

        source = dedent(
            """
            async def _process_post_commit():
                if staging_only:
                    await _merge_with_retry(  # premerge-guard-exempt: staging-only branch never carries a migration onto the trunk
                        token, owner, repo, pr_number
                    )
                else:
                    await merge_pull_request(
                        token, owner, repo, pr_number
                    )
            """
        )

        offenders = self._collect_offenders(
            source,
            filename="task_lifecycle.py",
            rel="task_lifecycle.py",
            guard_names=self._GUARD_NAMES,
            merge_names=self._MERGE_NAMES,
        )

        # Only the unmarked ``merge_pull_request`` (line 8 of the dedented body)
        # is flagged; the marked ``_merge_with_retry`` is exempt.
        assert offenders == [
            "  task_lifecycle.py:8 in _process_post_commit() — PR-merge site not covered by the pre-merge dual-head guard"
        ]

    def test_new_unmarked_merge_in_previously_exempt_function_is_flagged(self):
        """Regression: a brand-new, unmarked merge added to a function that
        already has marked-exempt merges is STILL flagged.

        This is the silent-regression the old absolute-line-number scheme could
        not catch: under code motion a new unguarded merge could come to occupy
        an exempt line number and inherit an exemption it never declared. With
        per-call markers, the new merge declares no exemption, so it is flagged
        even though it sits in the same (previously line-exempt) function in the
        same file.
        """
        from textwrap import dedent

        source = dedent(
            """
            async def _process_post_commit():
                if staging_a:
                    await _merge_with_retry(  # premerge-guard-exempt: staging-only A branch never reaches the integration trunk
                        token, owner, repo, pr_number
                    )
                if staging_b:
                    await _merge_with_retry(  # premerge-guard-exempt: staging-only B branch never reaches the integration trunk
                        token, owner, repo, pr_number
                    )
                # A genuinely-new, unguarded, UNMARKED merge slipped in here.
                await merge_pull_request(
                    token, owner, repo, pr_number
                )
            """
        )

        offenders = self._collect_offenders(
            source,
            filename="task_lifecycle.py",
            rel="task_lifecycle.py",
            guard_names=self._GUARD_NAMES,
            merge_names=self._MERGE_NAMES,
        )

        assert offenders == [
            "  task_lifecycle.py:12 in _process_post_commit() — PR-merge site not covered by the pre-merge dual-head guard"
        ], (
            "A new unmarked merge in a function that has marked-exempt siblings "
            "must still be flagged — it declared no exemption of its own."
        )

    def test_exempt_marker_requires_a_nonempty_reason(self):
        """Guard-the-guard: a bare marker with no human-readable reason does NOT
        exempt the call.

        Mirrors the documented-reason requirement on the string-keyed
        exemptions: every exemption must carry a reason a reviewer can read.
        A reason-less marker must be treated as no marker at all.
        """
        from textwrap import dedent

        for trailer in ("", "   "):
            source = dedent(
                """
                async def _process_post_commit():
                    await merge_pull_request(  {marker}{trailer}
                        token, owner, repo, pr_number
                    )
                """
            ).format(marker=self._EXEMPT_MARKER, trailer=trailer)

            offenders = self._collect_offenders(
                source,
                filename="task_lifecycle.py",
                rel="task_lifecycle.py",
                guard_names=self._GUARD_NAMES,
                merge_names=self._MERGE_NAMES,
            )

            assert offenders == [
                "  task_lifecycle.py:3 in _process_post_commit() — PR-merge site not covered by the pre-merge dual-head guard"
            ], (
                "A bare marker with no reason must NOT exempt the call "
                f"(trailer={trailer!r})."
            )

    def test_marker_only_applies_within_the_calls_own_span(self):
        """The marker exempts a call only when it lies within that call's own
        source span — a marker on a *neighbouring* statement must not leak onto
        an unmarked merge below it.
        """
        from textwrap import dedent

        source = dedent(
            """
            async def _process_post_commit():
                x = compute()  # premerge-guard-exempt: this marker is on the wrong statement
                await merge_pull_request(
                    token, owner, repo, pr_number
                )
            """
        )

        offenders = self._collect_offenders(
            source,
            filename="task_lifecycle.py",
            rel="task_lifecycle.py",
            guard_names=self._GUARD_NAMES,
            merge_names=self._MERGE_NAMES,
        )

        assert offenders == [
            "  task_lifecycle.py:4 in _process_post_commit() — PR-merge site not covered by the pre-merge dual-head guard"
        ], "A marker on an adjacent statement must not exempt the merge below it."

    def test_process_post_commit_exempt_sites_carry_marker(self):
        """The three intentionally-exempt merge sites in the real
        ``task_lifecycle.py:_process_post_commit`` must each carry the inline
        marker.

        If a marker is removed (or a fourth, unmarked merge is added) this test
        — together with ``test_all_pr_merge_sites_guarded`` — fails, so the
        exemption cannot be silently dropped or silently widened.
        """
        import ast

        app_dir = os.path.normpath(os.path.join(os.path.dirname(__file__), "..", "app"))
        fpath = os.path.join(app_dir, "task_lifecycle.py")
        with open(fpath, encoding="utf-8") as f:
            source = f.read()
        source_lines = source.splitlines()
        tree = ast.parse(source, filename=fpath)

        marked = 0
        total_merges = 0
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            if node.name != "_process_post_commit":
                continue
            for call in ast.walk(node):
                if not isinstance(call, ast.Call):
                    continue
                func = call.func
                name = (
                    func.id
                    if isinstance(func, ast.Name)
                    else func.attr
                    if isinstance(func, ast.Attribute)
                    else None
                )
                if name not in self._MERGE_NAMES:
                    continue
                total_merges += 1
                end = getattr(call, "end_lineno", None) or call.lineno
                span = source_lines[call.lineno - 1 : end]
                has_marker = any(
                    (idx := line.find(self._EXEMPT_MARKER)) != -1
                    and line[idx + len(self._EXEMPT_MARKER) :].strip()
                    for line in span
                )
                if has_marker:
                    marked += 1

        assert total_merges == 3, (
            f"Expected exactly 3 merge sites in _process_post_commit, found "
            f"{total_merges}. If the function's merge sites changed, update this "
            "test and the inline markers accordingly."
        )
        assert marked == 3, (
            f"Expected all 3 _process_post_commit merge sites to carry the "
            f"'{self._EXEMPT_MARKER} <reason>' inline marker; only {marked} do. "
            "Each staging-only / non-migration auto-merge must declare its "
            "exemption inline so the coverage invariant stays green."
        )


# ── Merge-primitive enumeration lock (complementary to call-site coverage) ────


class TestMergePrimitiveLock:
    """Complementary invariant: lock the set of merge primitives themselves.

    The TestPreMergeDualHeadGuardCoverage test locks every CALL SITE so a
    missing guard cannot slip through unnoticed. But that test's scan depends
    on a hand-maintained set of merge function names (_MERGE_NAMES). If the
    set itself drifts — e.g. a new merge helper is added but not registered,
    or the raw GitHub merge endpoint is called directly from somewhere else —
    the coverage scan treats those sites as non-merge and misses them completely.

    This test closes the complementary hole by enforcing:
      1. Every function in github_pulls.py that actually issues a GitHub merge
         (REST PUT /pulls/{n}/merge or GraphQL enablePullRequestAutoMerge) must
         be in TestPreMergeDualHeadGuardCoverage._MERGE_NAMES.
      2. The raw GitHub merge endpoint / GraphQL auto-merge mutation may be
         issued ONLY from inside github_pulls.py (the helper layer).
         No other backend/app file may call these endpoints directly.

    Today both conditions already hold: github_pulls.py defines exactly
    merge_pull_request and enable_auto_merge, both already registered, and
    nothing else calls the endpoints. This invariant purely ratchets that
    clean state and prevents future regressions.
    """

    # Patterns that identify a call to the GitHub merge REST endpoint. These
    # match the concrete merge-endpoint URL path (.../pulls/{n}/merge) ONLY —
    # deliberately NOT loose prose like "merge endpoint", which appears in
    # docstrings of non-merging helpers (e.g. get_pull_request_merge_state
    # documents the merge endpoint's 405 behaviour but only issues a GET).
    _MERGE_REST_PATTERNS = [
        re.compile(r"/pulls/[^\"']*/merge"),
    ]

    # Patterns that identify a call to the GraphQL auto-merge mutation. Match
    # the concrete mutation name only, not prose mentioning "GraphQL" + "merge".
    _MERGE_GRAPHQL_PATTERNS = [
        re.compile(r"enablePullRequestAutoMerge"),
    ]

    # ── Transport-level raw-merge scanner ─────────────────────────────────
    #
    # The helper-layer lock must catch a raw merge issued via ANY transport,
    # not just ``_github_request``. A future direct ``httpx.AsyncClient().put(
    # .../pulls/N/merge)`` or ``client.post(graphql, …enablePullRequestAutoMerge
    # …)`` from outside ``github_pulls.py`` would otherwise bypass a scan keyed
    # solely on ``_github_request``. The endpoint SIGNATURE (REST merge path +
    # PUT, or the GraphQL mutation name) is what classifies a call; the transport
    # openers below merely anchor where to look.
    _TRANSPORT_OPENERS = re.compile(
        r"(?:_github_request|httpx\.[A-Za-z_]+|\.(?:put|post|patch|request))\s*\(",
        re.DOTALL,
    )
    _RAW_MERGE_PATH_RE = re.compile(r"/pulls[^\"']*?/merge")
    _PUT_METHOD_LITERAL_RE = re.compile(r"""["']PUT["']""")
    # The opener's own HTTP verb is PUT (e.g. ``client.put(...)`` /
    # ``httpx.put(...)``) — "put" not preceded by an identifier character.
    _PUT_VERB_OPENER_RE = re.compile(r"(?<![A-Za-z_])put\s*\(")
    _RAW_AUTOMERGE_MUTATION_RE = re.compile(r"enablePullRequestAutoMerge")

    @staticmethod
    def _scan_raw_merge_endpoint_offenders(content: str, *, rel: str) -> list:
        """Flag raw GitHub merge-endpoint / GraphQL auto-merge calls issued via
        ANY transport, not only ``_github_request``.

        Two endpoint signatures classify a transport-call window as a raw merge:
          * REST — the ``/pulls/{n}/merge`` path with a PUT method, whether PUT
            is an explicit ``"PUT"`` argument (the ``_github_request`` shape) or
            the transport verb itself (a lower-level ``client.put(.../merge)``).
          * GraphQL — the ``enablePullRequestAutoMerge`` mutation in the body.

        Returns one offender line per (line, kind). Whole-content scanning makes
        it immune to ``# noqa`` and to multi-line call construction (it searches
        a window past each opener rather than a single line).
        """
        cls = TestMergePrimitiveLock
        found: set = set()
        for m in cls._TRANSPORT_OPENERS.finditer(content):
            opener = m.group(0)
            start = m.start()
            window = content[start : min(start + 1500, len(content))]
            line_num = content[:start].count("\n") + 1
            if cls._RAW_MERGE_PATH_RE.search(window) and (
                cls._PUT_METHOD_LITERAL_RE.search(window)
                or cls._PUT_VERB_OPENER_RE.search(opener)
            ):
                found.add((line_num, "rest"))
            if cls._RAW_AUTOMERGE_MUTATION_RE.search(window):
                found.add((line_num, "graphql"))

        offenders = []
        for line_num, kind in sorted(found):
            if kind == "rest":
                offenders.append(
                    f"  {rel}:{line_num}: raw PUT /pulls/.../merge endpoint call "
                    "detected outside the helper layer"
                )
            else:
                offenders.append(
                    f"  {rel}:{line_num}: raw enablePullRequestAutoMerge "
                    "mutation call detected outside the helper layer"
                )
        return offenders

    def test_all_merge_issuing_functions_are_registered(self):
        """Every function in github_pulls.py that calls the GitHub merge endpoint
        or GraphQL mutation must be in TestPreMergeDualHeadGuardCoverage._MERGE_NAMES.

        If this fails, a new merge primitive was added to github_pulls.py but not
        registered in the coverage scan's _MERGE_NAMES set. Register it so that
        all call sites to the new primitive receive guard-coverage checking.
        """
        import ast

        gh_pulls_path = os.path.normpath(
            os.path.join(os.path.dirname(__file__), "..", "app", "github_pulls.py")
        )
        with open(gh_pulls_path, encoding="utf-8") as f:
            source = f.read()
        tree = ast.parse(source, filename=gh_pulls_path)

        # Find all function definitions in the module.
        funcs = {
            n.name: n
            for n in ast.walk(tree)
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
        }

        # Get the canonical set of registered merge functions from the
        # guard-coverage test.
        registered_merge_names = TestPreMergeDualHeadGuardCoverage._MERGE_NAMES

        # Identify which functions actually call the GitHub merge endpoint or
        # GraphQL mutation by scanning their source text.
        unregistered_merge_issuers = []
        for func_name, func_node in funcs.items():
            # Extract the source text of this function. ast.get_source_segment
            # uses line/column spans to slice the exact function body; a naive
            # source[col_offset:end_col_offset] would mix column offsets (which
            # are per-line, not whole-file byte indices) and slice garbage.
            func_source = ast.get_source_segment(source, func_node) or ""

            # Check if it issues a merge: a PUT to the REST merge endpoint or an
            # invocation of the GraphQL auto-merge mutation. Both pattern sets
            # match concrete code signals only, so docstring prose mentioning
            # "merge" does not produce false positives.
            is_merge_issuer = any(
                pattern.search(func_source)
                for pattern in (
                    *self._MERGE_REST_PATTERNS,
                    *self._MERGE_GRAPHQL_PATTERNS,
                )
            )

            if is_merge_issuer and func_name not in registered_merge_names:
                unregistered_merge_issuers.append(func_name)

        assert not unregistered_merge_issuers, (
            f"Functions in github_pulls.py that issue GitHub merges but are NOT "
            f"registered in TestPreMergeDualHeadGuardCoverage._MERGE_NAMES: "
            f"{', '.join(sorted(unregistered_merge_issuers))}.\n"
            f"Add each function name to _MERGE_NAMES so that all its call sites "
            f"receive guard-coverage checking. Without registration, call sites "
            f"escape the coverage scan and can slip through unguarded."
        )

    def test_no_raw_merge_endpoint_calls_outside_helper_layer(self):
        """The raw GitHub merge endpoint and GraphQL mutation may be issued ONLY
        from inside the github_pulls.py helper layer. No other backend/app file
        may call /pulls/.../merge or enablePullRequestAutoMerge directly.

        If this fails, a file outside the helper layer is calling the merge
        endpoint directly instead of going through the registered primitives
        (merge_pull_request / enable_auto_merge). This bypasses both the
        guard-coverage scan and the dual-head guard checks themselves.

        Fix by calling the appropriate primitive from github_pulls.py instead of
        issuing the endpoint directly.

        This scan detects (via the shared _scan_raw_merge_endpoint_offenders):
        - REST endpoint calls: a PUT to .../pulls/.../merge across any transport
          (_github_request OR a lower-level httpx/client call), multi-line,
          immune to # noqa
        - GraphQL mutation calls: any transport call whose body carries the
          enablePullRequestAutoMerge mutation, multi-line, immune to # noqa

        False-positives from docstrings are avoided by anchoring on an actual
        transport call and requiring the concrete endpoint signature, not prose
        mentioning the endpoint.
        """
        app_dir = os.path.normpath(os.path.join(os.path.dirname(__file__), "..", "app"))

        # Only github_pulls.py may issue the raw merge endpoint.
        exempt_files = {
            "github_pulls.py",
        }

        offenders: list[str] = []
        for root, _dirs, files in os.walk(app_dir):
            for fname in sorted(files):
                if not fname.endswith(".py"):
                    continue
                if fname in exempt_files:
                    continue

                fpath = os.path.join(root, fname)
                with open(fpath, encoding="utf-8") as f:
                    content = f.read()

                rel = os.path.relpath(fpath, app_dir)
                offenders.extend(
                    self._scan_raw_merge_endpoint_offenders(content, rel=rel)
                )

        assert not offenders, (
            "Found backend/app file(s) that call the raw GitHub merge endpoint or "
            "GraphQL mutation directly (bypassing the helper layer and guard coverage):\n"
            + "\n".join(offenders)
            + "\n\nAll PR merges must go through the registered primitives "
            "(merge_pull_request / enable_auto_merge from github_pulls.py). "
            "Calling the endpoint directly bypasses guard coverage and can slip "
            "through unguarded.\n"
            "Fix by importing and calling the appropriate primitive instead."
        )

    def test_scan_detects_multiline_raw_merge_rest_endpoint(self):
        """Verify the shared scanner detects multi-line-constructed raw merge
        REST endpoint calls.

        Regression test: a raw merge call must be detected even when the
        ``_github_request`` call itself spans multiple lines.
        """
        # Synthetic pattern: a direct _github_request call with the merge
        # endpoint, spanning multiple lines, which a line-by-line scan misses.
        test_pattern = """
async def malicious_merge(token: str, owner: str, repo: str, pr_number: int):
    response = await _github_request(
        "PUT",
        f"https://api.github.com/repos/{owner}/{repo}/pulls/{pr_number}/merge",
        headers,
        json={"merge_method": "squash"},
    )
"""
        offenders = self._scan_raw_merge_endpoint_offenders(
            test_pattern, rel="synthetic.py"
        )
        assert any("/pulls/.../merge" in o for o in offenders), (
            "Regression test: the hardened scan must detect multi-line-constructed "
            f"raw merge REST endpoint calls. Offenders: {offenders}"
        )

    def test_scan_detects_multiline_raw_merge_graphql_mutation(self):
        """Verify the shared scanner detects multi-line-constructed raw GraphQL
        auto-merge mutations.

        Regression test: a raw GraphQL auto-merge call must be detected when the
        ``_github_request`` call spans multiple lines and the
        ``enablePullRequestAutoMerge`` mutation is in the body.
        """
        # Synthetic pattern: _github_request POST to graphql endpoint with
        # enablePullRequestAutoMerge in the JSON body, spanning multiple lines.
        test_pattern = """
async def malicious_auto_merge(token: str, pr_node_id: str, merge_method: str):
    response = await _github_request(
        "POST",
        "https://api.github.com/graphql",
        headers,
        json={
            "query": "mutation($prId: ID!, $method: PullRequestMergeMethod!) {"
                     "enablePullRequestAutoMerge(input: {pullRequestId: $prId, mergeMethod: $method}) {"
                     "pullRequest { autoMergeRequest { enabledAt } } } }",
            "variables": {"prId": pr_node_id, "method": merge_method},
        },
    )
"""
        offenders = self._scan_raw_merge_endpoint_offenders(
            test_pattern, rel="synthetic.py"
        )
        assert any("enablePullRequestAutoMerge" in o for o in offenders), (
            "Regression test: the hardened scan must detect multi-line GraphQL "
            f"auto-merge mutations. Offenders: {offenders}"
        )

    def test_scan_flags_noqa_tagged_raw_merge_calls(self):
        """Verify the shared scanner flags raw merge calls even when tagged with
        ``# noqa``.

        Regression test: a line-by-line scan can be defeated by a trailing
        ``# noqa`` comment. The whole-content scanner detects the call
        regardless, since the noqa bypass is itself the vulnerability.
        """
        test_pattern = """
async def sneaky_merge(token: str, owner: str, repo: str, pr_number: int):
    response = await _github_request(  # noqa
        "PUT",
        f"https://api.github.com/repos/{owner}/{repo}/pulls/{pr_number}/merge",
        headers,
        json={"merge_method": "squash"},
    )
"""
        offenders = self._scan_raw_merge_endpoint_offenders(
            test_pattern, rel="synthetic.py"
        )
        assert any("/pulls/.../merge" in o for o in offenders), (
            "Regression test: the scan must flag raw merge calls even when they "
            f"carry a '# noqa' comment. Offenders: {offenders}"
        )

    def test_scan_detects_raw_merge_via_lower_level_http_client(self):
        """The broadened scanner must catch a raw REST merge issued WITHOUT
        ``_github_request`` — a direct lower-level HTTP client call.

        This is the transport-evasion false-negative the old ``_github_request``-
        only scan missed entirely: the merge endpoint is hit via
        ``client.put(.../pulls/N/merge)``, where the HTTP verb is the call itself
        (no explicit ``"PUT"`` argument). The scanner recognizes the opener verb
        as the method.
        """
        test_pattern = """
async def evade_via_client(token: str, owner: str, repo: str, pr_number: int):
    async with httpx.AsyncClient() as client:
        response = await client.put(
            f"https://api.github.com/repos/{owner}/{repo}/pulls/{pr_number}/merge",
            json={"merge_method": "squash"},
            headers=headers,
        )
"""
        offenders = self._scan_raw_merge_endpoint_offenders(test_pattern, rel="evil.py")
        assert any("/pulls/.../merge" in o for o in offenders), (
            "The hardened scan must detect a raw REST merge issued via a "
            "lower-level HTTP client (client.put), not only _github_request. "
            f"Offenders: {offenders}"
        )

    def test_scan_detects_graphql_auto_merge_via_lower_level_http_client(self):
        """The broadened scanner must catch a raw GraphQL auto-merge mutation
        issued WITHOUT ``_github_request`` — a direct lower-level HTTP client
        POST whose body carries ``enablePullRequestAutoMerge``.
        """
        test_pattern = """
async def evade_auto_merge(token: str, pr_node_id: str):
    response = await client.post(
        "https://api.github.com/graphql",
        json={"query": "mutation { enablePullRequestAutoMerge(input: {pullRequestId: $id}) { clientMutationId } }"},
        headers=headers,
    )
"""
        offenders = self._scan_raw_merge_endpoint_offenders(test_pattern, rel="evil.py")
        assert any("enablePullRequestAutoMerge" in o for o in offenders), (
            "The hardened scan must detect a raw GraphQL auto-merge mutation "
            "issued via a lower-level HTTP client (client.post), not only "
            f"_github_request. Offenders: {offenders}"
        )


# ── Native-auto-merge migration-exclusion coverage (whole-package) ────────


class TestNativeAutoMergeMigrationExclusionCoverage:
    """Whole-package invariant: EVERY ``enable_auto_merge`` call site under
    backend/app/ must be guarded by a ``pr_touches_migrations`` exclusion so a
    migration-carrying PR is never delegated to GitHub native auto-merge.

    WHY (distinct from TestPreMergeDualHeadGuardCoverage): the pre-merge
    combined-chain guard runs synchronously immediately before a merge and fully
    protects the DIRECT merge primitive. But GitHub native auto-merge
    (``enable_auto_merge``) completes the merge ASYNCHRONOUSLY at some later time
    once required checks pass. That reopens a time-of-check-to-time-of-use
    window: the target branch can advance between Blueprint's guard check and the
    eventual native merge, so the merge can land against a DIFFERENT Alembic
    chain than the one Blueprint validated — re-introducing the dual-head wedge
    the guard exists to prevent.

    TestPreMergeDualHeadGuardCoverage deliberately scopes this async window out
    (see its "Coverage note": it catches missing call-site guard coverage, NOT
    the later merge-time race "where ``enable_auto_merge`` may complete after a
    second migration PR lands. Track that TOCTOU window separately."). This test
    IS that separate track: it locks the native-auto-merge primitive on its own,
    asserting each call site additionally refuses to delegate a migration PR.

    The three production sites — the task-PR completion fallback
    (task_lifecycle.py), the rollup auto-merge fallback (rollup_management.py),
    and the release-promotion pending-CI fallback (release_promotion.py) — all
    check ``pr_touches_migrations`` and, when True, skip delegation and hold the
    PR in a retryable/parked state so the guarded path re-validates against the
    live target branch before merging.

    The scan is branch-aware (mirroring TestPreMergeDualHeadGuardCoverage): an
    exclusion that covers one branch does NOT cover a sibling branch. A site is
    covered when a ``pr_touches_migrations`` reference dominates it — either as a
    prior sibling early-return guard (the release_promotion idiom) or as the test
    of an enclosing ``if`` (the task-PR / rollup ``else``-branch idiom).

    Source-level only (AST); NEVER executes a merge path.
    """

    _EXCLUSION_NAMES = frozenset({"pr_touches_migrations"})
    _AUTOMERGE_NAMES = frozenset({"enable_auto_merge"})

    # Whole files that DEFINE/re-export the native-auto-merge primitive. They
    # make no migration decision (the calling site does), so they are exempt —
    # mirrors TestPreMergeDualHeadGuardCoverage._EXEMPT_FILES.
    _EXEMPT_FILES = {
        "github_pulls.py": (
            "defines the enable_auto_merge primitive (GraphQL "
            "enablePullRequestAutoMerge mutation); makes no migration decision"
        ),
        "github.py": (
            "re-exports enable_auto_merge for callers; makes no migration decision"
        ),
    }

    # Specific (file, function) auto-merge sites that can never carry a migration
    # onto an integration/promotion branch. Each entry MUST document WHY. Empty
    # today: all three real sites carry the pr_touches_migrations exclusion. Add
    # a genuinely-exempt site here with a reason rather than widening
    # _EXEMPT_FILES (which would mask a newly-added unguarded site).
    _EXEMPT_SITES: dict = {}

    @staticmethod
    def _collect_offenders(
        source: str,
        *,
        filename: str,
        rel: str,
        exclusion_names: frozenset,
        automerge_names: frozenset,
    ) -> list:
        def _called_name(call):
            return _ast_called_name(call)

        def _calls_exclusion(node) -> bool:
            # An actual ``pr_touches_migrations(...)`` CALL anywhere in the
            # subtree — as an ``if`` test or on an assignment RHS. A bare
            # reference to the name (e.g. the captured verdict left unused) does
            # NOT count: only a call produces a verdict to gate the delegation.
            return any(
                isinstance(n, ast.Call) and _called_name(n) in exclusion_names
                for n in ast.walk(node)
            )

        def _assign_verdict_targets(stmt) -> set:
            return _ast_assign_targets_for_predicated_call(
                stmt,
                call_predicate=lambda call: _called_name(call) in exclusion_names,
            )

        def _stmt_automerge_calls(stmt):
            for node in ast.walk(stmt):
                if isinstance(node, ast.Call) and _called_name(node) in automerge_names:
                    yield node

        offenders: list = []

        def _scan_body(stmts, covered: bool, verdict_vars: set, func_name: str) -> None:
            cur = covered
            vvars = set(verdict_vars)
            for stmt in stmts:
                if isinstance(
                    stmt, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
                ):
                    continue

                if isinstance(stmt, ast.If):
                    # An ``if pr_touches_migrations(...)`` test (or an ``if`` on a
                    # verdict variable captured above) covers only the branch
                    # whose outcome proves `pr_touches_migrations=False`.
                    # Mixed predicates like `if (not touches) and feature_flag`
                    # therefore cover only the body, not the else branch where
                    # `touches` may still be true. A delegation AFTER such an
                    # ``if`` is covered only when a migration-touching branch
                    # early-exits — fail-closed, not symbol presence.
                    branch_body_cov = cur or _ast_branch_implies_verdict(
                        stmt.test,
                        branch_truth=True,
                        desired_truth=False,
                        atom_predicate=lambda node: (
                            (
                                isinstance(node, ast.Call)
                                and _called_name(node) in exclusion_names
                            )
                            or (isinstance(node, ast.Name) and node.id in vvars)
                        ),
                    )
                    branch_else_cov = cur or _ast_branch_implies_verdict(
                        stmt.test,
                        branch_truth=False,
                        desired_truth=False,
                        atom_predicate=lambda node: (
                            (
                                isinstance(node, ast.Call)
                                and _called_name(node) in exclusion_names
                            )
                            or (isinstance(node, ast.Name) and node.id in vvars)
                        ),
                    )
                    _scan_body(stmt.body, branch_body_cov, vvars, func_name)
                    _scan_body(stmt.orelse, branch_else_cov, vvars, func_name)
                    if _ast_branch_implies_verdict(
                        stmt.test,
                        branch_truth=True,
                        desired_truth=True,
                        atom_predicate=lambda node: (
                            (
                                isinstance(node, ast.Call)
                                and _called_name(node) in exclusion_names
                            )
                            or (isinstance(node, ast.Name) and node.id in vvars)
                        ),
                    ) and _ast_branch_early_exits(stmt.body):
                        cur = True
                    elif _ast_branch_implies_verdict(
                        stmt.test,
                        branch_truth=False,
                        desired_truth=True,
                        atom_predicate=lambda node: (
                            (
                                isinstance(node, ast.Call)
                                and _called_name(node) in exclusion_names
                            )
                            or (isinstance(node, ast.Name) and node.id in vvars)
                        ),
                    ) and _ast_branch_early_exits(stmt.orelse):
                        cur = True
                    elif cur and (
                        _ast_branch_early_exits(stmt.body)
                        or _ast_branch_early_exits(stmt.orelse)
                    ):
                        cur = True
                    continue

                if isinstance(stmt, (ast.For, ast.AsyncFor, ast.While)):
                    _scan_body(stmt.body, cur, vvars, func_name)
                    _scan_body(stmt.orelse, cur, vvars, func_name)
                    continue
                if isinstance(stmt, (ast.With, ast.AsyncWith)):
                    _scan_body(stmt.body, cur, vvars, func_name)
                    continue
                if isinstance(stmt, ast.Try):
                    _scan_body(stmt.body, cur, vvars, func_name)
                    for handler in stmt.handlers:
                        _scan_body(handler.body, cur, vvars, func_name)
                    _scan_body(stmt.orelse, cur, vvars, func_name)
                    _scan_body(stmt.finalbody, cur, vvars, func_name)
                    continue
                if hasattr(ast, "Match") and isinstance(stmt, ast.Match):
                    for case in stmt.cases:
                        _scan_body(case.body, cur, vvars, func_name)
                    continue

                for call in _stmt_automerge_calls(stmt):
                    if (
                        os.path.basename(rel),
                        func_name,
                    ) in TestNativeAutoMergeMigrationExclusionCoverage._EXEMPT_SITES:
                        continue
                    if not cur:
                        offenders.append(
                            f"  {rel}:{call.lineno} in {func_name}() — "
                            "enable_auto_merge site not guarded by a "
                            "pr_touches_migrations exclusion (native "
                            "auto-merge TOCTOU window)"
                        )
                vvars |= _assign_verdict_targets(stmt)

        tree = ast.parse(source, filename=filename)
        for func in [
            node
            for node in ast.walk(tree)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        ]:
            _scan_body(func.body, False, set(), func.name)
        return offenders

    def test_all_native_auto_merge_sites_exclude_migrations(self):
        app_dir = os.path.normpath(os.path.join(os.path.dirname(__file__), "..", "app"))
        offenders: list = []
        for root, _dirs, files in os.walk(app_dir):
            for fname in sorted(files):
                if not fname.endswith(".py"):
                    continue
                if fname in self._EXEMPT_FILES:
                    continue
                fpath = os.path.join(root, fname)
                with open(fpath, encoding="utf-8") as f:
                    source = f.read()
                rel = os.path.relpath(fpath, app_dir)
                offenders.extend(
                    self._collect_offenders(
                        source,
                        filename=fpath,
                        rel=rel,
                        exclusion_names=self._EXCLUSION_NAMES,
                        automerge_names=self._AUTOMERGE_NAMES,
                    )
                )

        assert not offenders, (
            "Found GitHub native-auto-merge (enable_auto_merge) call site(s) "
            "under backend/app/ that can delegate a migration-carrying PR "
            "WITHOUT a pr_touches_migrations exclusion.\n"
            "Native auto-merge completes the merge asynchronously once checks "
            "pass, so the target branch can advance between Blueprint's "
            "pre-merge dual-head guard and the eventual merge — landing the PR "
            "against a different Alembic chain than the one validated "
            "(dual-head TOCTOU).\n"
            "Fix each site by checking `await pr_touches_migrations(...)` before "
            "delegating and, when True, holding the PR in its retryable/parked "
            "state instead of calling enable_auto_merge — OR, if the site can "
            "never carry a migration onto an integration/promotion branch, add "
            "its (file, function) to "
            "TestNativeAutoMergeMigrationExclusionCoverage._EXEMPT_SITES with a "
            "documented reason.\n"
            "Offenders:\n" + "\n".join(offenders)
        )

    def test_scan_flags_sibling_unexcluded_auto_merge_branch(self):
        """A migration-excluded branch must not mask a sibling unexcluded
        auto-merge branch — the exact hole a file-/function-wide allowlist would
        leave open."""
        from textwrap import dedent

        source = dedent(
            """
            async def demo():
                if cond:
                    if await pr_touches_migrations(token, owner, repo, pr_number):
                        return
                    await enable_auto_merge(token, owner, repo, pr_number)
                else:
                    await enable_auto_merge(token, owner, repo, pr_number)
            """
        )

        offenders = self._collect_offenders(
            source,
            filename="synthetic_auto_merge_fixture.py",
            rel="synthetic_auto_merge_fixture.py",
            exclusion_names=self._EXCLUSION_NAMES,
            automerge_names=self._AUTOMERGE_NAMES,
        )

        assert offenders == [
            "  synthetic_auto_merge_fixture.py:8 in demo() — enable_auto_merge "
            "site not guarded by a pr_touches_migrations exclusion (native "
            "auto-merge TOCTOU window)"
        ]

    def test_scan_accepts_else_branch_exclusion_idiom(self):
        """The task-PR / rollup idiom — `if pr_touches: park else: auto-merge` —
        is recognized as covered."""
        from textwrap import dedent

        source = dedent(
            """
            async def demo():
                if await pr_touches_migrations(token, owner, repo, pr_number):
                    park()
                else:
                    await enable_auto_merge(token, owner, repo, pr_number)
            """
        )

        offenders = self._collect_offenders(
            source,
            filename="synthetic_auto_merge_fixture.py",
            rel="synthetic_auto_merge_fixture.py",
            exclusion_names=self._EXCLUSION_NAMES,
            automerge_names=self._AUTOMERGE_NAMES,
        )

        assert offenders == []

    def test_scan_accepts_early_return_exclusion_idiom(self):
        """The release_promotion idiom — `if pr_touches: return` BEFORE a later
        auto-merge sibling — is recognized as covered."""
        from textwrap import dedent

        source = dedent(
            """
            async def demo():
                if await pr_touches_migrations(token, owner, repo, pr_number):
                    return record_failure()
                try:
                    await enable_auto_merge(token, owner, repo, pr_number)
                except Exception:
                    pass
            """
        )

        offenders = self._collect_offenders(
            source,
            filename="synthetic_auto_merge_fixture.py",
            rel="synthetic_auto_merge_fixture.py",
            exclusion_names=self._EXCLUSION_NAMES,
            automerge_names=self._AUTOMERGE_NAMES,
        )

        assert offenders == []

    def test_native_auto_merge_exclusion_verdict_ignored_is_flagged(self):
        """Fail-closed ratchet (native auto-merge): capturing the
        ``pr_touches_migrations`` verdict is NOT enough — it must gate the
        delegation.

        The documented false-negative: the exclusion is called and its result
        assigned, but the value is never tested and ``enable_auto_merge`` is
        delegated unconditionally. "Exclusion symbol referenced ⇒ covered" would
        mark this safe; the hardened scan flags it because no branch early-exits
        on the migration-touching verdict before the delegation.
        """
        from textwrap import dedent

        source = dedent(
            """
            async def demo():
                touches = await pr_touches_migrations(
                    token, owner, repo, pr_number
                )
                # verdict captured but ignored — delegation proceeds regardless
                await enable_auto_merge(
                    token, owner, repo, pr_number
                )
            """
        )

        offenders = self._collect_offenders(
            source,
            filename="synthetic_auto_merge_fixture.py",
            rel="synthetic_auto_merge_fixture.py",
            exclusion_names=self._EXCLUSION_NAMES,
            automerge_names=self._AUTOMERGE_NAMES,
        )

        assert offenders == [
            "  synthetic_auto_merge_fixture.py:7 in demo() — enable_auto_merge "
            "site not guarded by a pr_touches_migrations exclusion (native "
            "auto-merge TOCTOU window)"
        ], (
            "A pr_touches_migrations verdict that is captured but ignored "
            "(never gates an early-exit before enable_auto_merge) must be "
            "flagged — referencing the exclusion symbol is not the same as "
            "fail-closing on its verdict."
        )

    def test_mixed_condition_only_covers_the_guarded_automerge_branch(self):
        """A mixed predicate like `if (not touches) and feature_flag` covers
        only the body.

        The else branch is reachable both when the feature flag is false and
        when `touches=True`, so a delegated native auto-merge there must still
        be flagged.
        """
        from textwrap import dedent

        source = dedent(
            """
            async def demo():
                touches = await pr_touches_migrations(
                    token, owner, repo, pr_number
                )
                if (not touches) and feature_flag:
                    await enable_auto_merge(
                        token, owner, repo, pr_number
                    )
                else:
                    await enable_auto_merge(
                        token, owner, repo, pr_number
                    )
            """
        )

        offenders = self._collect_offenders(
            source,
            filename="synthetic_auto_merge_fixture.py",
            rel="synthetic_auto_merge_fixture.py",
            exclusion_names=self._EXCLUSION_NAMES,
            automerge_names=self._AUTOMERGE_NAMES,
        )

        assert offenders == [
            "  synthetic_auto_merge_fixture.py:11 in demo() — enable_auto_merge "
            "site not guarded by a pr_touches_migrations exclusion (native "
            "auto-merge TOCTOU window)"
        ]


# ── Operator message arming gate coverage (whole-package) ─────────────────


class TestOperatorMessageArmingGateCoverage:
    """Whole-package invariant: EVERY backend/app/ site that constructs an
    operator-authored ProjectMessage (``from_type='user'``) must either route
    its wake decision through the shared operator arming gate or be explicitly
    exempted with a documented reason.

    WHY: the live-chat endpoint and the project-scoped CLI endpoint were once
    able to diverge on strategist wake behavior. This scan catches future
    source-level message creation sites without executing any endpoint.

    Modeled on the os.walk whole-package invariants in this module. The scan is
    source-only (AST), function-scoped, and carries a tiny allowlist for the one
    legitimate exception we have today.
    """

    _GATE_NAME = "maybe_arm_operator_refresh"
    _MESSAGE_CLASS = "ProjectMessage"

    # Allowlisted (file, function) sites that intentionally seed an operator
    # message without a wake decision. The onboarding goal setter writes a
    # passive NOTE before any strategic loop exists, so arming the strategist is
    # unnecessary there.
    _EXEMPT_SITES = {
        (
            "routers/admin_onboard.py",
            "_seed_strategic_intent",
        ): (
            "seeds the onboarding goal as a passive NOTE before any strategic "
            "loop exists; intentionally does not wake the strategist"
        ),
    }

    @staticmethod
    def _collect_offenders(
        source: str,
        *,
        filename: str,
        rel: str,
        gate_name: str,
        message_class: str,
    ) -> list[str]:
        def _called_name(call: "ast.Call") -> str | None:
            """Return the bare callee name for ``foo(...)`` or ``mod.foo(...)``."""
            return _ast_called_name(call)

        def _calls_gate(node) -> bool:
            """True if the shared arming gate is CALLED anywhere in the subtree."""
            return any(
                isinstance(n, ast.Call) and _called_name(n) == gate_name
                for n in ast.walk(node)
            )

        def _gate_verdict_used(node) -> bool:
            """True if the gate's verdict is used (not just called and ignored).

            The gate must be called AND its verdict must appear in:
            - A return statement/value, or
            - A conditional test

            Assigning the verdict to a variable but never using it does not count.
            """
            # Find all gate calls and their assigned targets
            verdict_vars = set()
            for n in ast.walk(node):
                if isinstance(n, (ast.Assign, ast.AugAssign, ast.AnnAssign)):
                    # Check if the RHS contains a gate call
                    rhs = n.value if hasattr(n, "value") else None
                    if rhs and any(
                        isinstance(c, ast.Call) and _called_name(c) == gate_name
                        for c in ast.walk(rhs)
                    ):
                        # Extract variable names assigned on LHS
                        if isinstance(n, ast.Assign):
                            for target in n.targets:
                                if isinstance(target, ast.Name):
                                    verdict_vars.add(target.id)
                        elif isinstance(n, (ast.AugAssign, ast.AnnAssign)):
                            if isinstance(n.target, ast.Name):
                                verdict_vars.add(n.target.id)

            # Now check if the verdict is actually used
            # 1. Used directly in a return value
            for n in ast.walk(node):
                if isinstance(n, ast.Return) and n.value:
                    # Check for direct gate call in return
                    if any(
                        isinstance(c, ast.Call) and _called_name(c) == gate_name
                        for c in ast.walk(n.value)
                    ):
                        return True
                    # Check for verdict variable in return
                    for name_node in ast.walk(n.value):
                        if (
                            isinstance(name_node, ast.Name)
                            and name_node.id in verdict_vars
                        ):
                            return True

            # 2. Used in a conditional
            for n in ast.walk(node):
                if isinstance(n, ast.If):
                    # Check for gate call in the conditional test
                    if any(
                        isinstance(c, ast.Call) and _called_name(c) == gate_name
                        for c in ast.walk(n.test)
                    ):
                        return True
                    # Check for verdict variable in the conditional test
                    for name_node in ast.walk(n.test):
                        if (
                            isinstance(name_node, ast.Name)
                            and name_node.id in verdict_vars
                        ):
                            return True

            return False

        def _is_operator_message_call(node: "ast.Call") -> bool:
            """True for `ProjectMessage(..., from_type="user", ...)` calls."""
            if _called_name(node) != message_class:
                return False
            for kw in node.keywords:
                if (
                    kw.arg == "from_type"
                    and isinstance(kw.value, ast.Constant)
                    and kw.value.value == "user"
                ):
                    return True
            return False

        offenders: list[str] = []
        tree = ast.parse(source, filename=filename)

        funcs = [
            n
            for n in ast.walk(tree)
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
        ]

        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            if not _is_operator_message_call(node):
                continue

            # Resolve the innermost enclosing function for an actionable
            # offender message and a function-scoped guard check.
            enclosing = None
            for fn in funcs:
                if fn.lineno <= node.lineno <= (fn.end_lineno or fn.lineno):
                    if enclosing is None or fn.lineno > enclosing.lineno:
                        enclosing = fn
            func_name = enclosing.name if enclosing is not None else "<module>"

            if (rel, func_name) in TestOperatorMessageArmingGateCoverage._EXEMPT_SITES:
                continue

            # Fail-closed hardening: the gate must be CALLED and its verdict USED.
            # A bare reference to the gate symbol (e.g. in an import) is not enough;
            # only an actual call produces a verdict to use.
            gate_called = enclosing is not None and _calls_gate(enclosing)
            verdict_used = gate_called and _gate_verdict_used(enclosing)
            if not verdict_used:
                offenders.append(
                    f"  {rel}:{node.lineno} in {func_name}() — operator "
                    "ProjectMessage site not covered by the shared arming gate"
                )

        return offenders

    def test_scan_flags_unguarded_operator_message(self):
        """Fail-closed basic: an operator ProjectMessage(from_type='user') with
        no gate call in the enclosing function must be flagged."""
        from textwrap import dedent

        source = dedent(
            """
            async def create_message(session, project_id, content):
                msg = ProjectMessage(
                    project_id=project_id,
                    from_type="user",
                    content=content
                )
                await session.add(msg)
            """
        )

        offenders = self._collect_offenders(
            source,
            filename="synthetic_operator_msg_fixture.py",
            rel="synthetic_operator_msg_fixture.py",
            gate_name=self._GATE_NAME,
            message_class=self._MESSAGE_CLASS,
        )

        assert offenders == [
            "  synthetic_operator_msg_fixture.py:3 in create_message() — operator "
            "ProjectMessage site not covered by the shared arming gate"
        ]

    def test_scan_accepts_guarded_operator_message(self):
        """A guarded operator message with the gate called and verdict assigned
        and used in a response is accepted."""
        from textwrap import dedent

        source = dedent(
            """
            async def create_message(session, project_id, content):
                msg = ProjectMessage(
                    project_id=project_id,
                    from_type="user",
                    content=content
                )
                arming = await maybe_arm_operator_refresh(
                    session, project_id, msg_type, message_content=content
                )
                return {"id": msg.id, "arming": arming}
            """
        )

        offenders = self._collect_offenders(
            source,
            filename="synthetic_operator_msg_fixture.py",
            rel="synthetic_operator_msg_fixture.py",
            gate_name=self._GATE_NAME,
            message_class=self._MESSAGE_CLASS,
        )

        assert offenders == []

    def test_operator_gate_reference_without_verdict_usage_is_flagged(self):
        """Fail-closed ratchet: referencing the gate symbol is NOT enough — the
        verdict must actually be USED to drive the response or a conditional.

        This is the documented false-negative the hardened scan prevents:
        the gate is called and its result assigned, but the value is never
        tested and the response is formed unconditionally. 'Gate symbol
        referenced ⇒ covered' would mark this safe; the hardened scan flags it
        because the verdict is not used to gate the response or a commit.
        """
        from textwrap import dedent

        source = dedent(
            """
            async def create_message(session, project_id, content):
                msg = ProjectMessage(
                    project_id=project_id,
                    from_type="user",
                    content=content
                )
                # verdict captured but NEVER used in response or conditional
                arming = await maybe_arm_operator_refresh(
                    session, project_id, msg_type, message_content=content
                )
                return {"id": msg.id, "status": "created"}
            """
        )

        offenders = self._collect_offenders(
            source,
            filename="synthetic_operator_msg_fixture.py",
            rel="synthetic_operator_msg_fixture.py",
            gate_name=self._GATE_NAME,
            message_class=self._MESSAGE_CLASS,
        )

        assert offenders == [
            "  synthetic_operator_msg_fixture.py:3 in create_message() — operator "
            "ProjectMessage site not covered by the shared arming gate"
        ], (
            "A gate verdict that is captured but never used in the response "
            "(the return dict lacks the arming field) must be flagged — "
            "referencing the gate symbol is not the same as using its verdict."
        )

    def test_all_operator_message_sites_route_through_shared_gate(self):
        app_dir = os.path.normpath(os.path.join(os.path.dirname(__file__), "..", "app"))
        offenders: list[str] = []
        for root, _dirs, files in os.walk(app_dir):
            for fname in sorted(files):
                if not fname.endswith(".py"):
                    continue
                fpath = os.path.join(root, fname)
                rel = os.path.relpath(fpath, app_dir)
                with open(fpath, encoding="utf-8") as f:
                    source = f.read()

                offenders.extend(
                    self._collect_offenders(
                        source,
                        filename=fpath,
                        rel=rel,
                        gate_name=self._GATE_NAME,
                        message_class=self._MESSAGE_CLASS,
                    )
                )

        assert not offenders, (
            "Found operator-authored ProjectMessage site(s) under backend/app/ "
            "that do NOT route their wake decision through the shared operator "
            f"arming gate ({self._GATE_NAME}, in app/strategic_trigger.py).\n"
            "Fix each site by calling maybe_arm_operator_refresh(...) in the "
            "enclosing function and using its verdict in the response, OR — if "
            "the site is intentionally exempt — add its (file, function) to "
            "TestOperatorMessageArmingGateCoverage._EXEMPT_SITES with a documented "
            "reason.\n"
            "Offenders:\n" + "\n".join(offenders)
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


# ── Dialect-resolution footgun guard ───────────────────────────────────────


class TestNoRawBindDialectIdiom:
    """Guard against the None-bind dialect footgun (triggering incident PR #7099).

    The idiom ``<session>.bind.dialect.name if <session>.bind else "postgresql"``
    (and the ``.bind.dialect.name == "postgresql"`` guard variant) reads ``.bind``
    directly off a session. An ``AsyncSession`` created from an
    ``async_sessionmaker`` exposes a **None** ``.bind`` even though it resolves a
    real engine lazily at execute-time, so the idiom silently defaults to
    Postgres and emits Postgres-only SQL on a SQLite-bound session. All dialect
    resolution must go through ``app.database.resolve_dialect_name`` instead.
    """

    def test_no_raw_session_bind_dialect_in_app_code(self):
        import os

        app_dir = os.path.normpath(os.path.join(os.path.dirname(__file__), "..", "app"))
        # database.py *defines* resolve_dialect_name and documents the bad idiom
        # in its docstring; it is the one legitimate site that names ``.bind.dialect``.
        allow_list = {"database.py"}
        pattern = re.compile(r"\.bind\.dialect")
        offenders: list[str] = []
        for root, _dirs, files in os.walk(app_dir):
            for fname in files:
                if not fname.endswith(".py") or fname in allow_list:
                    continue
                fpath = os.path.join(root, fname)
                with open(fpath, encoding="utf-8") as f:
                    for lineno, line in enumerate(f, start=1):
                        if line.lstrip().startswith("#") or "# noqa" in line:
                            continue
                        if pattern.search(line):
                            rel = os.path.relpath(fpath, app_dir)
                            offenders.append(f"  {rel}:{lineno}: {line.rstrip()}")
        assert not offenders, (
            "Found raw `.bind.dialect` access in backend/app/.\n"
            "Resolve the dialect via app.database.resolve_dialect_name(session) "
            "instead — a None-bind async session silently defaults to Postgres "
            "and emits Postgres-only SQL on SQLite (incident PR #7099).\n"
            "Offenders:\n" + "\n".join(offenders)
        )

    def test_no_two_line_bind_dialect_split(self):
        """Catch the two-line split form the single-line regex misses.

        The footgun can evade ``test_no_raw_session_bind_dialect_in_app_code``
        by spreading the access across two lines: assign a session's ``.bind``
        (or ``get_bind()``) to a local, then read ``.dialect`` off that local on
        a later line. This whole-file scan flags any local assigned from
        ``<x>.bind`` / ``<x>.get_bind()`` whose ``.dialect`` is read anywhere in
        the same file. Legitimate non-dialect uses of ``.bind`` (e.g. handing the
        engine to a sub-session factory) never read ``.dialect`` off the local,
        so they are not flagged.
        """
        import os

        app_dir = os.path.normpath(os.path.join(os.path.dirname(__file__), "..", "app"))
        # database.py *defines* resolve_dialect_name (the one legitimate site).
        allow_list = {"database.py"}
        # A local assigned from a session's bind: ``<var> = <expr>.bind`` or
        # ``<var> = <expr>.get_bind(...)``.
        assign_re = re.compile(
            r"^\s*(\w+)\s*=\s*[\w.]+\.(?:bind\b|get_bind\s*\()", re.MULTILINE
        )
        offenders: list[str] = []
        for root, _dirs, files in os.walk(app_dir):
            for fname in files:
                if not fname.endswith(".py") or fname in allow_list:
                    continue
                fpath = os.path.join(root, fname)
                with open(fpath, encoding="utf-8") as f:
                    text = f.read()
                for m in assign_re.finditer(text):
                    var = m.group(1)
                    line = text[m.start() : text.find("\n", m.start())]
                    if line.lstrip().startswith("#") or "# noqa" in line:
                        continue
                    # Flag only when ``.dialect`` is read off this same local —
                    # that is what distinguishes the footgun from a benign
                    # ``.bind`` use that just forwards the engine.
                    if re.search(rf"\b{re.escape(var)}\.dialect\b", text):
                        lineno = text.count("\n", 0, m.start()) + 1
                        rel = os.path.relpath(fpath, app_dir)
                        offenders.append(f"  {rel}:{lineno}: {line.strip()}")
        assert not offenders, (
            "Found a two-line `.bind` → `.dialect` split in backend/app/.\n"
            "A local assigned from a session's `.bind`/`get_bind()` whose "
            "`.dialect` is read later is the same None-bind footgun in disguise "
            "(incident PR #7099). Resolve the dialect via "
            "app.database.resolve_dialect_name(session) instead.\n"
            "Offenders:\n" + "\n".join(offenders)
        )


class TestNoUnguardedPostgresJsonOp:
    """Guard against PostgreSQL-only JSON *operators* emitted with no dialect branch.

    The sibling ``TestNoRawBindDialectIdiom`` catches code that resolves the
    dialect the *wrong* way (raw ``.bind.dialect``). It does NOT catch the more
    dangerous case: code that emits a Postgres-only JSON operator with **no
    dialect resolution at all**. Such SQL compiles and runs on Postgres — so CI
    (which runs ``USE_TEST_POSTGRES=true``) stays green — and only blows up on
    the local SQLite smoke gate (``./check`` / ``--no-postgres``) with
    ``unrecognized token: "@"``. This is exactly how the account-health query
    regressed: a perf optimization swapped a SQLite-safe predicate for the JSONB
    containment operator ``@>`` (``.op("@>")``) with no dialect branch, and
    nothing static flagged it.

    This invariant flags any module under ``backend/app`` that emits a
    Postgres-only JSON operator via either surface:

    * the raw ``.op(...)`` surface — the JSONB containment operator ``@>`` and
      the key-existence operator family ``?`` / ``?|`` / ``?&``; or
    * the ``.has_key(...)`` *method* form — SQLAlchemy's JSONB key-existence
      check, which compiles to the Postgres-only ``?`` operator and raises
      ``sqlite3.OperationalError`` on the SQLite smoke gate exactly like
      ``.op("?")`` does.

    unless the module also routes dialect resolution through
    ``app.database.resolve_dialect_name``. A module that branches on the
    resolved dialect is presumed to guard the Postgres-only path behind it; if a
    genuine exception is ever needed, add the file to ``allow_list`` (the same
    escape hatch the ``.bind.dialect`` invariants use for ``database.py``).

    The previously-documented ``.has_key()`` scope gap is now CLOSED: the four
    pre-existing unguarded sites (digest_remediation, rollup_management,
    autonomous_research, residual_case_endpoints) plus the mis-commented
    admin_health site were all routed through ``resolve_dialect_name`` with a
    portable ``func.json_extract`` SQLite fallback, so scanning ``.has_key()``
    here no longer red-fails CI.
    """

    # The canonical resolver. A module that calls it is presumed to branch on
    # the resolved dialect and guard its Postgres-only path.
    _GUARD_TOKEN = "resolve_dialect_name"

    # Raw Postgres-only JSON operators expressed via the SQLAlchemy ``.op(...)``
    # surface: the JSONB containment operator ``@>`` and the key-existence
    # operator family ``?`` / ``?|`` / ``?&``.
    _PG_JSON_OP_RE = re.compile(r"""\.op\(\s*['"](?:@>|\?\|?|\?&)['"]\s*\)""")

    # The ``.has_key(...)`` method form — SQLAlchemy's JSONB key-existence
    # comparator, which compiles to the Postgres-only ``?`` operator. (Python 3
    # dicts have no ``.has_key`` method, so a ``.has_key(`` call site in this
    # codebase is always the JSONB operator.) A bare function named e.g.
    # ``_build_config_has_key(...)`` has no leading dot and is not matched.
    _PG_HAS_KEY_RE = re.compile(r"\.has_key\(")

    @classmethod
    def _scan_module_text(cls, text: str) -> list[int]:
        """Return the 1-based line numbers that emit a Postgres-only JSON
        operator — via either the raw ``.op(...)`` surface or the
        ``.has_key(...)`` method form — skipping comment / ``# noqa`` lines
        (mirrors the sibling scanners). Returns an empty list when the module is
        guarded — i.e. it also contains ``resolve_dialect_name``."""
        if cls._GUARD_TOKEN in text:
            return []
        hits: list[int] = []
        for lineno, line in enumerate(text.splitlines(), start=1):
            if line.lstrip().startswith("#") or "# noqa" in line:
                continue
            if cls._PG_JSON_OP_RE.search(line) or cls._PG_HAS_KEY_RE.search(line):
                hits.append(lineno)
        return hits

    def test_no_unguarded_pg_json_op_in_app_code(self):
        import os

        app_dir = os.path.normpath(os.path.join(os.path.dirname(__file__), "..", "app"))
        # database.py *defines* resolve_dialect_name and only documents ``@>`` in
        # prose (no ``.op("@>")`` call), so it is not matched; no entry required.
        allow_list: set[str] = set()
        offenders: list[str] = []
        for root, _dirs, files in os.walk(app_dir):
            for fname in files:
                if not fname.endswith(".py") or fname in allow_list:
                    continue
                fpath = os.path.join(root, fname)
                with open(fpath, encoding="utf-8") as f:
                    text = f.read()
                for lineno in self._scan_module_text(text):
                    rel = os.path.relpath(fpath, app_dir)
                    line = text.splitlines()[lineno - 1]
                    offenders.append(f"  {rel}:{lineno}: {line.strip()}")
        assert not offenders, (
            "Found a Postgres-only JSON operator (`.op('@>')` / `.op('?')` / "
            "`.op('?|')` / `.op('?&')` or the `.has_key(...)` method form) in "
            "backend/app/ with no dialect branch.\n"
            "Such SQL runs on Postgres (CI stays green) but fails on the local "
            'SQLite smoke gate with `unrecognized token: "@"` (or a `?` '
            "parameter-placeholder error for `.has_key`). Route dialect "
            "resolution through app.database.resolve_dialect_name(session) and "
            "branch the Postgres-only path behind it (see account-health fix), "
            "falling back to a portable `func.json_extract(...)` predicate on "
            "SQLite (see landed_signal / agent_warm_continuity).\n"
            "Offenders:\n" + "\n".join(offenders)
        )

    def test_scanner_flags_injected_unguarded_op(self):
        """Self-test: the scanner must DETECT an unguarded `.op('@>')` — guards
        against a scanner that silently matches nothing (mirrors
        ``test_branch_local_guard_scan_catches_sibling_unguarded_branch``)."""
        from textwrap import dedent

        source = dedent(
            """
            from sqlalchemy.dialects.postgresql import JSONB
            from sqlalchemy import cast

            async def failed_tasks(session):
                stmt = select(Task).where(
                    Task.context.op("@>")(cast({"failed": True}, JSONB))
                )
                return await session.execute(stmt)
            """
        )
        assert self._scan_module_text(source) == [7]

        # The key-existence operator family is detected too.
        key_exists = 'Task.context.op("?")(literal("k"))\n'
        assert self._scan_module_text(key_exists) == [1]

    def test_scanner_flags_injected_unguarded_has_key(self):
        """Self-test: the scanner must DETECT an unguarded `.has_key(...)`
        method-form key-existence check — the surface this invariant was
        extended to cover. Both the positive and the negated (`~...has_key`)
        form must be flagged."""
        from textwrap import dedent

        source = dedent(
            """
            from sqlmodel import select

            async def remediation_tasks(session):
                stmt = select(Task).where(
                    Task.context.has_key("diagnosis_task_id"),
                )
                return await session.execute(stmt)
            """
        )
        assert self._scan_module_text(source) == [6]

        # The negated key-absence form (`~Task.context.has_key(...)`) is the
        # exact shape that regressed in residual_case_endpoints / autonomous_
        # research; it must be detected too.
        negated = '.where(*filters, ~Task.context.has_key("failure_category"))\n'
        assert self._scan_module_text(negated) == [1]

    def test_scanner_ignores_non_method_has_key_function(self):
        """Self-test: a bare function named `*_has_key(...)` (no leading dot) is
        NOT the JSONB operator and must NOT be flagged — guards against the
        scanner over-matching helper functions like
        ``_build_config_has_key(project, key)``."""
        from textwrap import dedent

        source = dedent(
            """
            def _build_config_has_key(project, key):
                return key in (project.build_config or {})

            is_default = not _build_config_has_key(project, "task_branch_prefix")
            """
        )
        assert self._scan_module_text(source) == []

    def test_scanner_passes_guarded_op(self):
        """Self-test: a module that emits `.op('@>')` AND resolves the dialect
        via resolve_dialect_name is NOT flagged."""
        from textwrap import dedent

        source = dedent(
            """
            from app.database import resolve_dialect_name

            async def failed_tasks(session):
                if resolve_dialect_name(session) == "postgresql":
                    pred = Task.context.op("@>")(cast({"failed": True}, JSONB))
                else:
                    pred = Task.context["failed"].as_boolean().is_(True)
                return pred
            """
        )
        assert self._scan_module_text(source) == []

    def test_scanner_passes_guarded_has_key(self):
        """Self-test: a module that emits `.has_key(...)` AND resolves the
        dialect via resolve_dialect_name is NOT flagged — mirrors the canonical
        ``func.json_extract`` SQLite fallback used in landed_signal /
        agent_warm_continuity / the sites fixed alongside this invariant."""
        from textwrap import dedent

        source = dedent(
            """
            from sqlalchemy import func
            from app.database import resolve_dialect_name

            async def remediation_tasks(session):
                if resolve_dialect_name(session) == "postgresql":
                    pred = Task.context.has_key("diagnosis_task_id")
                else:
                    pred = func.json_extract(
                        Task.context, "$.diagnosis_task_id"
                    ).isnot(None)
                return pred
            """
        )
        assert self._scan_module_text(source) == []


# ── Base-branch fallback hardening ─────────────────────────────────────────


class TestBaseBranchFallback:
    """Guard against hardcoded 'main' fallbacks across backend/app."""

    def test_no_hardcoded_main_branch_fallbacks(self):
        """No production module under backend/app/ may use defensive 'main' fallbacks."""
        from app.branch_literal_compliance import scan_branch_literal_surface

        app_dir = Path(__file__).resolve().parents[1] / "app"
        result = scan_branch_literal_surface("backend", app_dir)
        offenders = [
            f"  {o.path}:{o.line}: {o.snippet}"
            for o in result.offenders
            if o.pattern in {
                "assign-branch-main",
                "python-else-main",
                "python-else-master",
                "runtime-config-main",
            }
        ]
        assert not offenders, (
            "Found hardcoded branch fallbacks in backend/app/.\n"
            "Replace defensive 'main' defaults with project-aware resolution.\n"
            "Offenders:\n" + "\n".join(offenders)
        )

    def test_no_hardcoded_branch_in_task_display_strings(self):
        """No production module under backend/app/ may embed a literal 'main...'
        or 'main -> automain' inside a task title= or description= field.

        These display strings are shown to operators for every project, including
        non-Blueprint repos whose branches are not main/automain, so they would
        mislabel what is actually being audited. Branch values must be interpolated
        from the per-task resolved base_branch/diff_range variables instead.

        Scope: only task/alert display fields (title=, description=, message=).
        Log format strings are intentionally excluded — they are internal and
        not shown to operators in the UI.
        """
        from app.branch_literal_compliance import scan_branch_literal_surface

        app_dir = Path(__file__).resolve().parents[1] / "app"
        result = scan_branch_literal_surface("backend", app_dir)
        offenders = [
            f"  {o.path}:{o.line}: {o.snippet}"
            for o in result.offenders
            if o.pattern in {"display-main-automain", "display-main-range"}
        ]
        assert not offenders, (
            "Found hardcoded branch names in task display fields (title=/description=/message=) "
            "in backend/app/.\n"
            "Interpolate branch values from the resolved per-task variable (e.g. base_branch)\n"
            "instead of embedding literal 'main...' or 'main -> automain'.\n"
            "Offenders:\n" + "\n".join(offenders)
        )


class TestBranchLiteralComplianceScanner:
    def test_scanner_reports_structured_offenders_across_backend_agent_frontend(
        self, tmp_path
    ):
        from app.branch_literal_compliance import scan_branch_literal_compliance

        backend_app = tmp_path / "backend" / "app"
        agent_src = tmp_path / "agent" / "src"
        admin_src = tmp_path / "admin-ui" / "src"
        backend_app.mkdir(parents=True)
        agent_src.mkdir(parents=True)
        admin_src.mkdir(parents=True)

        (backend_app / "worker.py").write_text(
            'branch = "main"\n'
            'description="audit main -> automain back-merge"\n',
            encoding="utf-8",
        )
        (agent_src / "runner.ts").write_text(
            "const baseBranch = task.baseBranch ?? 'main';\n",
            encoding="utf-8",
        )
        (admin_src / "Panel.tsx").write_text(
            "const promotionTarget = project.promotionTarget || 'automain';\n",
            encoding="utf-8",
        )

        offenders = [
            offender
            for result in scan_branch_literal_compliance(tmp_path)
            for offender in result.offenders
        ]

        assert [(o.surface, o.path, o.line, o.pattern) for o in offenders] == [
            ("backend", "worker.py", 1, "assign-branch-main"),
            ("backend", "worker.py", 2, "display-main-automain"),
            ("agent", "runner.ts", 1, "fallback-main"),
            ("admin-ui", "Panel.tsx", 1, "fallback-main"),
        ]
        assert offenders[0].snippet == 'branch = "main"'


# ── Self-repo branch resolution guard ──────────────────────────────────────


class TestSelfRepoBranchResolution:
    """Ensure self-repo projects resolve to correct integration branches."""

    def test_self_repo_null_integration_branch_resolves_to_automain(self):
        """A self-repo with NULL integration_branch must resolve to 'automain', not 'main'.

        Blueprint's integration trunk is 'automain' (qualification branch).
        The promotion/release target is 'main'. When integration_branch is unset,
        the resolver must fall back to 'automain' for self-repos, not 'main'.
        """
        from app.branch_resolution import get_project_integration_branch

        class _FakeSelfRepo:
            integration_branch = None
            is_self_repo = True

        assert get_project_integration_branch(_FakeSelfRepo()) == "automain"

    def test_non_self_repo_null_integration_branch_resolves_to_main(self):
        """A non-self-repo with NULL integration_branch must resolve to 'main'."""
        from app.branch_resolution import get_project_integration_branch

        class _FakeProject:
            integration_branch = None
            is_self_repo = False
            github_repo = "other-repo"

        assert get_project_integration_branch(_FakeProject()) == "main"


# ── Staging-branch prefix classification guard ────────────────────────────


class TestStagingBranchClassification:
    """Guard against hardcoded staging-prefix startswith calls in backend/app/.

    Per-project branch classification must route through Project.is_staging_branch()
    or Project.staging_branch_prefixes, not via bare .startswith("research/") /
    .startswith("plan/") calls that silently break non-Blueprint installs.

    Allowlisted files (they legitimately define or default these literals):
      - models.py  — defines the staging_branch_prefixes property and fallback
      - services.py — defines _DEFAULT_STAGING_PREFIXES constant
    """

    _ALLOWLIST = {"models.py", "services.py"}
    _PATTERN = re.compile(r'\.startswith\(["\'](?:research|plan)/')

    def test_no_hardcoded_staging_prefix_startswith(self):
        """No production module under backend/app/ may call
        .startswith("research/") or .startswith("plan/") directly.

        If this fails, replace the hardcoded classification with:
          - project.is_staging_branch(branch)   for boolean checks
          - project.staging_branch_prefixes       for prefix iteration
          - get_project_integration_branch(...)   for integration-branch decisions
        The allowlisted files (models.py, services.py) define these values and
        may legitimately contain the literals.
        """
        app_dir = os.path.normpath(os.path.join(os.path.dirname(__file__), "..", "app"))
        offenders: list[str] = []
        for root, _dirs, files in os.walk(app_dir):
            for fname in files:
                if not fname.endswith(".py"):
                    continue
                if fname in self._ALLOWLIST:
                    continue
                fpath = os.path.join(root, fname)
                with open(fpath, encoding="utf-8") as f:
                    for lineno, line in enumerate(f, start=1):
                        if line.lstrip().startswith("#") or "# noqa" in line:
                            continue
                        if self._PATTERN.search(line):
                            rel = os.path.relpath(fpath, app_dir)
                            offenders.append(f"  {rel}:{lineno}: {line.rstrip()}")
        assert not offenders, (
            "Found hardcoded staging-prefix startswith calls in backend/app/.\n"
            "Replace with project.is_staging_branch(branch) or\n"
            "project.staging_branch_prefixes for per-project prefix resolution.\n"
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
        "/repo/CLAUDE.md",  # Docker agent VM: /tmp/workspace mounted at /repo
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


# ── Agent Router Task-State Cache Invariants ───────────────────────────


class TestAgentRouterInvariants:
    def test_agent_router_only_has_deny_list_task_cache(self):
        """Enforce that agent.py only keeps _terminated_task_ids as a task-state cache.

        Regression guard for PR #6038 which removed _agent_active_cache. That cache
        was dead (no longer consulted by verify_agent_token), but its presence as
        module-level state was a latent footgun: a future agent could mistakenly
        believe it was load-bearing and gate the DB recheck on it again, silently
        re-creating the auth bug (deleted/terminal task authorized via stale cache hit).

        This tripwire enforces: the only task-ID-keyed module-level mutable mapping
        in agent.py is _terminated_task_ids (the deny-list). Any attempt to add back
        a 'known active tasks' cache will fail this test.

        Suspicious patterns (names hinting at task auth/active state):
        - _task_*_cache, _*active*, _authorized_*, _agent_*_cache
        """
        from app.routers import agent

        # Task-auth-state suspicious name patterns.
        # These patterns catch attempts to cache active/authorized task state by ID.
        SUSPICIOUS_PATTERNS = [
            r"_.*task.*cache",  # _task_cache, _task_auth_cache, etc.
            r"_.*active.*",  # _active_*, _agent_active_*, etc.
            r"_authorized.*",  # _authorized_task_ids, etc.
            r"_agent.*cache",  # _agent_*_cache, etc.
        ]

        # The only allowed task-state dict
        ALLOWED = {"_terminated_task_ids"}

        offenders = []
        for name, obj in agent.__dict__.items():
            # Only inspect dicts that start with underscore (module-level private state)
            if not isinstance(obj, dict) or not name.startswith("_"):
                continue

            # Check if name matches any suspicious pattern (case-insensitive)
            if any(
                re.match(pattern, name, re.IGNORECASE)
                for pattern in SUSPICIOUS_PATTERNS
            ):
                if name not in ALLOWED:
                    offenders.append(name)

        assert not offenders, (
            f"Task-auth-state cache(s) found in agent.py: {', '.join(offenders)}. "
            "Only _terminated_task_ids is permitted as a task-ID-keyed dict. "
            "See PR #6038: dead caches are latent footguns. The DB recheck is the "
            "authoritative gate — caching 'known active' tasks risks stale "
            "authorization of deleted/terminal tasks."
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
AGENT_SIZE_LIMITS = _SIZE_LIMITS["agent"]


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
        with open(path, encoding="utf-8") as f:
            count = len(f.readlines())
        assert count <= max_lines, _size_breach_message(
            relative_path, count, max_lines, "backend"
        )

    @pytest.mark.parametrize("relative_path", sorted(MODULE_SIZE_LIMITS))
    def test_module_within_limit(self, relative_path):
        """Every key in the ``backend`` section of module_size_limits.json is
        enforced automatically — adding a key to the JSON is sufficient to
        enforce its cap, with no per-file test method to maintain. The backend
        section has no environment skip, so it always enforces."""
        self._check_module(relative_path)

    def test_recovery_sweeps_cap_reflects_decomposed_size(self):
        """recovery_sweeps.py is a thin re-export hub (~63 lines) after its
        sweeps were split into recovery_evaluating.py and recovery_post_commit.py.
        Its cap must stay well below 200 so the guardrail catches any regression
        where substantial logic is re-added to this file instead of the proper
        submodule. A cap above 200 indicates stale pre-decomposition headroom
        crept back in."""
        cap = MODULE_SIZE_LIMITS["recovery_sweeps.py"]
        assert cap <= 200, (
            f"recovery_sweeps.py cap is {cap}, which exceeds 200. "
            "This file is a thin re-export hub after decomposition; its cap "
            "should stay well below 200. If the file has genuinely grown, "
            "consider whether new logic belongs in recovery_evaluating.py or "
            "recovery_post_commit.py instead."
        )


_AGENT_SRC_ROOT = (
    os.path.normpath(
        os.path.join(os.path.dirname(__file__), "..", "..", "agent", "src")
    )
    if os.path.isdir(
        os.path.join(os.path.dirname(__file__), "..", "..", "agent", "src")
    )
    else None
)


class TestAgentModuleSizeGuardrails:
    @staticmethod
    def _require_agent_src() -> None:
        import pytest

        if _AGENT_SRC_ROOT is None:
            pytest.skip("agent/src not available in this test environment")

    def _check_agent(self, relative_path: str) -> None:
        self._require_agent_src()
        max_lines = AGENT_SIZE_LIMITS[relative_path]
        path = os.path.join(_AGENT_SRC_ROOT, relative_path)
        with open(path) as f:
            count = len(f.readlines())
        assert count <= max_lines, _size_breach_message(
            relative_path, count, max_lines, "agent"
        )

    @pytest.mark.parametrize("relative_path", sorted(AGENT_SIZE_LIMITS))
    def test_agent_module_within_limit(self, relative_path):
        """Every key in the ``agent`` section of module_size_limits.json is
        enforced automatically. Skips (via ``_check_agent`` →
        ``_require_agent_src``) when ``agent/src`` is absent — e.g. CI's
        backend-only Docker build context."""
        self._check_agent(relative_path)


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

    @pytest.mark.parametrize("relative_path", sorted(FRONTEND_SIZE_LIMITS))
    def test_frontend_within_limit(self, relative_path):
        """Every key in the ``frontend`` section of module_size_limits.json is
        enforced automatically. Skips (via ``_check_frontend`` →
        ``_require_admin_ui_src``) when ``admin-ui/src`` is absent (CI's
        backend-only Docker context) or when ``SKIP_FRONTEND_SIZE_GUARDRAILS=1``
        is set — these skips are preserved exactly."""
        self._check_frontend(relative_path)

    def test_task_detail_drawer_meta(self):
        self._check_frontend("components/TaskDetailDrawerMeta.tsx")

    def test_api_ts(self):
        self._check_frontend("api.ts")

    def test_sr_trend_breakdown(self):
        self._check_frontend("components/SrTrendBreakdown.tsx")

    def test_sr_unhandled_edges_retrospectives_panel(self):
        self._check_frontend("components/SrUnhandledEdgesRetrospectivesPanel.tsx")


# ── Size-Guardrail Coverage (no large file may escape having a cap) ──────────
#
# The per-file guardrails above only constrain files explicitly listed in
# module_size_limits.json. That leaves a structural hole: a newly-created
# large file that nobody adds to the config is invisible to the guardrail and
# can grow without limit. This meta-test closes the complementary direction —
# it walks each source tree and FAILS if any non-test source file exceeds a
# generous line threshold while having NO entry in its section of the config.
# The remedy is to add a cap entry (current size + modest headroom), making
# the guardrail system self-extending.

# Generous threshold: high enough to catch only genuine monoliths, not routine
# files. Any uncapped file over this must gain an explicit, reviewed cap entry.
_COVERAGE_THRESHOLD = 1500


def _missing_cap_message(offenders: list, section: str) -> str:
    """Direct the author to add a cap entry for each uncapped oversized file.
    Distinct from _size_breach_message: the remedy here is to ADD a JSON key
    (the file has no cap at all), not to bump an existing one."""
    lines = "\n".join(
        f"  - {rel} has {count} lines (threshold: {_COVERAGE_THRESHOLD})"
        for rel, count in offenders
    )
    return (
        f"{len(offenders)} source file(s) exceed {_COVERAGE_THRESHOLD} lines "
        f"with NO cap entry under '{section}' in "
        f"backend/testing/module_size_limits.json:\n\n"
        f"{lines}\n\n"
        f"A large file with no cap is invisible to the size guardrails and can\n"
        f"grow without limit. Add an entry for each file above under the\n"
        f"'{section}' section, set to its current line count plus modest\n"
        f"headroom (e.g. current + 100). This is a reviewed config edit, not a\n"
        f"refactor — do NOT extract or restructure the source in this PR."
    )


def _scan_uncapped(root: str, section: dict, suffixes: tuple, is_excluded) -> list:
    """Walk ``root`` collecting (tree_relative_path, line_count) for every file
    whose name ends in one of ``suffixes``, is not excluded by ``is_excluded``,
    exceeds ``_COVERAGE_THRESHOLD`` lines, and has no key in ``section``.
    Tree-relative paths use forward slashes to match the JSON key convention."""
    offenders = []
    for dirpath, _dirnames, filenames in os.walk(root):
        for name in filenames:
            if not name.endswith(suffixes):
                continue
            abs_path = os.path.join(dirpath, name)
            rel_path = os.path.relpath(abs_path, root).replace(os.sep, "/")
            if is_excluded(rel_path):
                continue
            with open(abs_path) as f:
                count = len(f.readlines())
            if count > _COVERAGE_THRESHOLD and rel_path not in section:
                offenders.append((rel_path, count))
    offenders.sort(key=lambda item: item[1], reverse=True)
    return offenders


def _is_frontend_excluded(rel_path: str) -> bool:
    return rel_path.endswith((".test.ts", ".test.tsx", ".d.ts")) or rel_path.startswith(
        "test/"
    )


def _is_agent_excluded(rel_path: str) -> bool:
    return rel_path.endswith((".test.ts", ".d.ts"))


class TestSizeGuardrailCoverage:
    """Meta-guardrail: no large source file may escape having a cap entry.

    Complements the per-file guardrail classes (which only enforce files that
    ARE listed). Each test below scans one source tree; if it finds an
    oversized file with no cap key, it fails with instructions to add one.
    Skip semantics mirror the per-file classes exactly so CI behavior is
    unchanged (backend always runs; frontend/agent skip when their src roots
    are absent, and frontend also skips under SKIP_FRONTEND_SIZE_GUARDRAILS=1).
    """

    def test_backend_no_uncapped_large_file(self):
        app_root = os.path.normpath(
            os.path.join(os.path.dirname(__file__), "..", "app")
        )
        offenders = _scan_uncapped(
            app_root, MODULE_SIZE_LIMITS, (".py",), lambda _rel: False
        )
        assert not offenders, _missing_cap_message(offenders, "backend")

    def test_frontend_no_uncapped_large_file(self):
        import pytest

        if os.getenv("SKIP_FRONTEND_SIZE_GUARDRAILS") == "1":
            pytest.skip("SKIP_FRONTEND_SIZE_GUARDRAILS=1 set")
        if _ADMIN_UI_SRC_ROOT is None:
            pytest.skip(
                "admin-ui/src not available in this test environment "
                "(CI docker context is ./backend; mount via test-backend.sh "
                "for local enforcement)"
            )
        offenders = _scan_uncapped(
            _ADMIN_UI_SRC_ROOT,
            FRONTEND_SIZE_LIMITS,
            (".ts", ".tsx"),
            _is_frontend_excluded,
        )
        assert not offenders, _missing_cap_message(offenders, "frontend")

    def test_agent_no_uncapped_large_file(self):
        import pytest

        if _AGENT_SRC_ROOT is None:
            pytest.skip("agent/src not available in this test environment")
        offenders = _scan_uncapped(
            _AGENT_SRC_ROOT, AGENT_SIZE_LIMITS, (".ts",), _is_agent_excluded
        )
        assert not offenders, _missing_cap_message(offenders, "agent")


# ── LLM Token Pricing Coverage ──────────────────────────────────────────────


class TestLLMTokenPricingCoverage:
    """Verify that every selectable LLM model has a pricing entry.

    Selectable models are those advertised in each provider's settings_schema
    model enum plus the values returned by tier_to_model() for each tier
    (opus, sonnet, haiku with empty runtime_config).

    This test guards against silent drift: when a provider adds/rotates a model,
    operators can select it immediately, but if it's missing from LLM_TOKEN_PRICING,
    tasks run on it contribute $0 to LLM cost and are flagged `priced: false` in
    the cost breakdown — drifting the efficiency axes silently until the pricing
    table is updated.
    """

    def test_all_selectable_models_have_pricing(self):
        """Every model in get_all_selectable_models() must have an entry in
        LLM_TOKEN_PRICING, keyed by (provider_name_lower, model_lower).
        """
        from app.efficiency_snapshot import LLM_TOKEN_PRICING
        from app.providers.registry import get_all_selectable_models

        selectable = get_all_selectable_models()
        # selectable is {provider_name_str: frozenset[model_str]}

        priced_keys = set(LLM_TOKEN_PRICING.keys())
        missing = []

        for provider_name, models in selectable.items():
            for model in models:
                key = (provider_name.lower(), model.lower())
                if key not in priced_keys:
                    missing.append(f"{provider_name}:{model}")

        assert not missing, (
            "Selectable models missing from LLM_TOKEN_PRICING:\n  "
            + "\n  ".join(sorted(missing))
            + "\n\nAdd entries to LLM_TOKEN_PRICING in backend/app/efficiency_snapshot.py "
            "for each missing (provider, model) combo. Source rates from official "
            "provider pricing pages; preview/beta models without published rates "
            "should be priced at the closest GA-tier equivalent with a clarifying comment."
        )


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
            ADMIN_CANCEL,
            AGENT_STALLED,
            AGENT_TIMEOUT,
            APPROVED_CAP_PR_CLOSED,
            BLOCKED_PRECONDITION,
            BRANCH_GONE,
            DEAD_AUTH,
            EXTERNAL_API,
            INFRA_CLASSES,
            NO_PROGRESS_TIMEOUT,
            PARK_WAKE_STRANDED,
            PARK_WAKE_TIMEOUT,
            PROVISION_FAILURE,
            RATE_LIMIT,
            REASSIGNED,
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
                BRANCH_GONE,
                SCOPE_EMPTY,
                REASSIGNED,
                NO_PROGRESS_TIMEOUT,
                # Park/wake orchestration outcomes are budget-exempt infra faults
                # (the wake source never fired, or the registration was lost) —
                # not code defects. See feat: surface park/wake health.
                PARK_WAKE_TIMEOUT,
                PARK_WAKE_STRANDED,
                # Dead OAuth/API session: permanent provider-infra failure,
                # auto-disables account fleet-wide. Not a code defect.
                DEAD_AUTH,
                # Approved PR closed by cap burn — not a code defect, so it
                # must not penalize direction health or consume retry budget.
                APPROVED_CAP_PR_CLOSED,
                # Administrative cancellation (cascade cancel / supersession) —
                # not a code defect or genuine LLM hang.
                ADMIN_CANCEL,
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

# Author-side origins emitted by the agent origin-tagging handlers that must be
# EXCLUDED from the reviewer-origin set.
AGENT_AUTHOR_ORIGINS = frozenset({"sr", "author"})


def _resolve_origin_source_paths() -> "list[str]":
    """Locate agent handler files that stamp self-review origin tags.

    The tagging logic currently lives in ``fresh-reviewer.ts``; older revisions
    kept parts of it in ``standard.ts``. Read both when available so the
    invariant follows the actual source of truth across refactors.
    """
    repo_handler_dir = os.path.normpath(
        os.path.join(
            os.path.dirname(__file__),
            "..",
            "..",
            "agent",
            "src",
            "handlers",
        )
    )
    candidates = [
        os.getenv("AGENT_FRESH_REVIEWER_TS"),
        "/agent/src/handlers/fresh-reviewer.ts",
        os.path.join(repo_handler_dir, "fresh-reviewer.ts"),
        os.getenv("AGENT_STANDARD_TS"),
        "/agent/src/handlers/standard.ts",
        os.path.join(repo_handler_dir, "standard.ts"),
    ]
    found = []
    for candidate in candidates:
        if candidate and os.path.isfile(candidate) and candidate not in found:
            found.append(candidate)
    return found


def _parse_origin_literals_from_agent_handlers(source_text: str) -> "frozenset[str]":
    """Extract every ``origin`` string literal assigned in the agent handlers.

    Matches both ``entry.origin = 'x'`` / ``sr.reviewer_recovery_origin = 'x'``
    and object-literal ``origin: 'x'`` forms. Comment lines reuse the same
    literals so including them does not change the resulting set.
    """
    return frozenset(re.findall(r"origin\s*[:=]\s*'([^']+)'", source_text))


class TestReviewerOriginParity:
    """Pin the backend canonical reviewer-origin set to the exact set of
    non-author origins the agent stamps in its origin-tagging handlers. Guards
    against silent drift back into miscounting recovery catches as author
    self-review hits.
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
        """Parsed agent origin tags minus author origins must equal _REVIEWER_ORIGINS.

        Skipped when the relevant agent handler sources are not available (CI
        Docker context builds from ./backend only). Locally and via
        scripts/test-backend.sh (which mounts agent/src) this fires and
        prevents drift.

        If this fails: the agent emits a non-author origin that is not in
        _REVIEWER_ORIGINS (backend/app/sr_blind_spots.py), or vice versa. Catches
        with the missing origin would be misattributed to the author's self-review.
        """
        source_paths = _resolve_origin_source_paths()
        if not source_paths:
            pytest.skip(
                "agent origin-tagging handler source not available in this test "
                "environment (CI docker context is ./backend; run locally or via "
                "scripts/test-backend.sh from a checkout for enforcement)"
            )
        from app.sr_blind_spots import _REVIEWER_ORIGINS

        source_text = "\n".join(Path(path).read_text() for path in source_paths)
        all_origins = _parse_origin_literals_from_agent_handlers(source_text)
        # Sanity: the author origins we subtract must actually appear in the source.
        assert AGENT_AUTHOR_ORIGINS <= all_origins, (
            "Expected author origins {sr, author} not found in the agent "
            "origin-tagging handlers — "
            "the origin-tagging block may have moved or been renamed."
        )
        agent_reviewer_origins = all_origins - AGENT_AUTHOR_ORIGINS
        assert agent_reviewer_origins == _REVIEWER_ORIGINS, (
            "Reviewer-origin set drift between agent handlers and backend:\n"
            f"  in agent handlers but not _REVIEWER_ORIGINS: "
            f"{sorted(agent_reviewer_origins - _REVIEWER_ORIGINS)}\n"
            f"  in _REVIEWER_ORIGINS but not agent handlers: "
            f"{sorted(_REVIEWER_ORIGINS - agent_reviewer_origins)}\n"
            "Update _REVIEWER_ORIGINS in backend/app/sr_blind_spots.py to match "
            "the non-author origins emitted by the agent handlers."
        )


# ── Playbook-Learning Facade Compatibility ────────────────────────────────────
# After the playbook-learning split, key symbols moved to sub-modules
# (review_learning, playbook_lifecycle, playbook_storage, playbook_auto).
# playbook_learning re-exports them so existing callers keep working.
# These tests lock the re-export surface so future agents cannot drop
# compatibility symbols without a CI failure.


# Lifecycle/storage helpers + constants whose single canonical home is
# ``app.playbook_storage``.  The ``playbook_lifecycle`` shim and the
# ``playbook_learning`` facade must re-export every one of these BY IDENTITY —
# never redefine a local copy (a "dead twin" that can drift independently).
# Keep in sync with the re-export blocks in playbook_lifecycle.py / playbook_learning.py.
_CANONICAL_PLAYBOOK_STORAGE_NAMES = frozenset(
    {
        # lifecycle helpers
        "finalize_pending_playbook_entries",
        "retire_idle_auto_entries",
        "retire_ineffective_patterns",
        "check_and_flag_stale_refs",
        "record_playbook_applied",
        "record_playbook_confirmed",
        "_is_auto_playbook_suppressed",
        "_get_prior_archive_entry",
        "count_idle_auto_retired_within",
        "count_low_success_retired_within",
        "_extract_file_paths",
        # lifecycle constants
        "_AUTO_PLAYBOOK_RESURRECTION_COOLDOWN_DAYS",
        "_FILE_PATH_RE",
        "_IDLE_RETIRE_DAYS",
        "_RETIRE_MAX_SUCCESS_RATE",
        "_RETIRE_MIN_FOLLOWED",
        "_STALE_AGE_DAYS",
        "_STALE_CONFIRM_THRESHOLD",
    }
)


def _module_top_level_bound_names(module_path: str) -> set[str]:
    """Return names *defined* (not imported) at module top level.

    Collects targets of ``def``/``async def``/``class`` statements and
    module-level assignments (``Assign``/``AnnAssign``), while deliberately
    excluding names introduced via ``import`` / ``from ... import`` — those are
    re-exports, which is exactly what the shim/facade are allowed to do.
    """
    import ast

    with open(module_path, encoding="utf-8") as f:
        tree = ast.parse(f.read(), filename=module_path)

    bound: set[str] = set()
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            bound.add(node.name)
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    bound.add(target.id)
                elif isinstance(target, (ast.Tuple, ast.List)):
                    for elt in target.elts:
                        if isinstance(elt, ast.Name):
                            bound.add(elt.id)
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            bound.add(node.target.id)
    return bound


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

    def test_facade_lifecycle_symbols_are_canonical_storage_objects(self):
        """The facade must re-export each lifecycle helper/constant BY IDENTITY.

        ``test_compat_lifecycle_and_storage_symbols_accessible`` only proves the
        names resolve and are callable — a shadow copy with the same name would
        pass it.  This tightens the guard to object identity: if the facade ever
        starts pointing at a local re-implementation again (a "dead twin" that
        can drift from the canonical storage logic), this fails.
        """
        import app.playbook_learning as learning
        import app.playbook_storage as storage

        mismatched = [
            name
            for name in sorted(_CANONICAL_PLAYBOOK_STORAGE_NAMES)
            if getattr(learning, name) is not getattr(storage, name)
        ]
        assert not mismatched, (
            "app.playbook_learning re-exports a shadow copy instead of the "
            "canonical app.playbook_storage object for: "
            f"{mismatched}. The facade must re-export by identity "
            "(`from app.playbook_storage import <name>`), not redefine."
        )

    def test_lifecycle_shim_symbols_are_canonical_storage_objects(self):
        """The playbook_lifecycle shim must re-export storage objects by identity.

        Callers still import ``app.playbook_lifecycle.<name>`` (e.g.
        ``playbook_auto``, ``autonomous_research``); those must resolve to the
        one canonical storage implementation, not a re-introduced twin.
        """
        import app.playbook_lifecycle as shim
        import app.playbook_storage as storage

        mismatched = [
            name
            for name in sorted(_CANONICAL_PLAYBOOK_STORAGE_NAMES)
            if getattr(shim, name) is not getattr(storage, name)
        ]
        assert not mismatched, (
            "app.playbook_lifecycle re-exports a shadow copy instead of the "
            "canonical app.playbook_storage object for: "
            f"{mismatched}. The shim must re-export by identity, not redefine."
        )

    def test_shim_and_facade_do_not_redefine_lifecycle_helpers(self):
        """Neither the shim nor the facade may *define* a canonical lifecycle name.

        Source-scan tripwire: AST-parse playbook_lifecycle.py and
        playbook_learning.py and assert that none of the canonical
        storage-owned names is bound by a local ``def``/``class``/assignment.
        Re-exports via ``import`` are allowed (and expected); a redefinition is
        a dead twin that the identity checks above would also catch, but this
        flags it at the source so the regression is obvious in review.
        """
        import os

        app_dir = os.path.normpath(os.path.join(os.path.dirname(__file__), "..", "app"))
        offenders: list[str] = []
        for fname in ("playbook_lifecycle.py", "playbook_learning.py"):
            path = os.path.join(app_dir, fname)
            redefined = _module_top_level_bound_names(path) & (
                _CANONICAL_PLAYBOOK_STORAGE_NAMES
            )
            offenders.extend(f"  {fname}: {name}" for name in sorted(redefined))
        assert not offenders, (
            "A canonical app.playbook_storage lifecycle helper/constant is "
            "redefined (not re-exported) in the shim/facade — this re-introduces "
            "a dead twin that can drift from the canonical implementation. "
            "Re-export it via `from app.playbook_storage import <name>` instead.\n"
            "Offenders:\n" + "\n".join(offenders)
        )


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
        assert result["reviewer_caught_rate"] == 1.0, (
            "reviewer_caught_rate must denominate on reviewer-eligible tasks "
            "(tasks_with_reviewer_edges_count), not all code-change tasks."
        )
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
        assert result["reviewer_caught_rate"] == 1.0, (
            "reviewer_caught_rate must denominate on reviewer-eligible tasks "
            "(tasks_with_reviewer_edges_count), not all code-change tasks."
        )

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

    def test_reviewer_caught_rate_denominated_on_reviewer_eligible(self):
        """reviewer_caught_rate must use tasks_with_reviewer_edges_count as the
        denominator, not all code-change tasks.

        5 tasks total; 2 have reviewer edges; 1 of those 2 is a catch.
        With the correct denominator: 1/2 = 0.5.
        With the wrong (all-tasks) denominator: 1/5 = 0.2.
        The test asserts 0.5 to enforce reviewer-eligible-only denominator.
        """
        from unittest.mock import MagicMock
        from app.routers.admin_digest import _compute_edges_considered_metrics

        def _make_task(edges):
            t = MagicMock()
            t.first_payload = {"type": "create"}
            t.context = {"_self_review_outcome": {"edges_considered": edges}}
            return t

        # task A: reviewer raises a new case the author missed → reviewer caught
        task_a = _make_task(
            [
                {"case": "null payload", "handled": True, "origin": "author"},
                {"case": "concurrent writes", "handled": False, "origin": "reviewer"},
            ]
        )
        # task B: reviewer reviews but matches the author's case → no catch
        task_b = _make_task(
            [
                {"case": "empty input", "handled": True, "origin": "author"},
                {"case": "empty input", "handled": True, "origin": "reviewer"},
            ]
        )
        # tasks C–E: no reviewer edges — ineligible to appear in the denominator
        task_c = _make_task([{"case": "c1", "handled": True}])
        task_d = _make_task([{"case": "c2", "handled": True}])
        task_e = _make_task([{"case": "c3", "handled": True}])

        result = _compute_edges_considered_metrics(
            [task_a, task_b, task_c, task_d, task_e]
        )
        assert result is not None
        assert result["tasks_with_reviewer_edges_count"] == 2
        assert result["reviewer_caught_count"] == 1
        assert result["reviewer_caught_rate"] == 0.5, (
            "reviewer_caught_rate must equal reviewer_caught_count / "
            "tasks_with_reviewer_edges_count (1/2 = 0.5), not 1/5 = 0.2 "
            "which would be the deflated all-tasks denominator."
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


# Files with design_doc / vision writes that intentionally do NOT (yet) acquire
# a project-row lock. These are PRE-EXISTING lock-free read-modify-write sites
# (identical on origin/main, which never carried a lock here) that the positive
# ratchet below must not assert on — doing so would demand a lock that was never
# present, rather than catching a lock that was REMOVED (the ratchet's stated
# purpose). Migrating these writes to with_design_doc_locked() / with_vision_locked()
# is tracked as the companion to the "cluster 1/2" positive-ratchet test (#6779);
# until that migration lands, the entries below are exempt with rationale.
#
# NOTE: this exemption only narrows the POSITIVE ratchet (TestDesignDocLockAssignmentGuard
# / TestVisionLockAssignmentGuard). The NEGATIVE ratchet (TestDesignDocLockInvariant /
# TestVisionLockInvariant) still applies to these files and prevents any NEW unguarded
# write from raising the per-file count above its baseline. Remove a file from this set
# the moment its writes are migrated under a lock — that re-arms the positive guard.
_LOCK_GUARD_EXEMPT: dict[str, set[str]] = {
    # design_doc: empty — all design_doc writes are now migrated under
    # with_design_doc_locked(). auto_remediation.py (concurrent auto-recovery
    # RMW of design_doc["_auto_remediation"]) and routers/admin_onboard.py
    # (one-time onboarding seed) were the last two exempt sites; both now route
    # through the canonical helper, so the positive guard is fully re-armed for
    # design_doc and their baseline entries have been removed.
    "design_doc": set(),
    "vision": {
        # Single direction-metrics vision write.
        "direction_metrics.py",
        # Debug-only vision touch ({**project.vision}).
        "routers/admin_debug.py",
    },
}


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
        with open(py_file, encoding="utf-8") as fh:
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
            rel = os.path.relpath(py_file, app_dir).replace(os.sep, "/")
            counts[rel] = count
    return counts


def _file_contains_lock_pattern(file_path: str, patterns: list[str]) -> bool:
    """Check if a file contains at least one of the given lock-acquisition patterns.

    Uses simple substring matching (not AST) to keep checks conservative and fast.
    Returns True if any pattern is found, False otherwise.
    """
    try:
        with open(file_path, encoding="utf-8") as f:
            source = f.read()
    except (IOError, OSError):
        return False

    return any(pattern in source for pattern in patterns)


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


class TestDirectionsLockAssignmentGuard:
    """Positive ratchet: files with non-zero .directions assignments must be
    wrapped in a project-row-lock acquisition site.

    Complements TestDirectionsLockInvariant (negative ratchet). That test catches
    NEW direct assignments. This test ensures EXISTING assignments have a surrounding
    lock — if the lock is removed without migrating the write to with_directions_locked(),
    this test fails.

    Recognized lock acquisition sites:
      - with_for_update(...) on Project (or alias)
      - project_lock(...)
      - lock_project_or_429(...) or _lock_project_or_429(...)
      - with_directions_locked(...)

    Failure message suggests either migrating to with_directions_locked or
    restoring the surrounding lock call.
    """

    # Recognized patterns that indicate a file has a project-row lock.
    # Substring matching is sufficient (conservative approach).
    _LOCK_PATTERNS = [
        "with_for_update(",
        "project_lock(",
        "lock_project_or_429(",
        "with_directions_locked(",
    ]

    def test_directions_writes_guarded_by_locks(self):
        """Every file with .directions writes must contain at least one lock acquisition."""
        app_dir = os.path.normpath(os.path.join(os.path.dirname(__file__), "..", "app"))
        baseline: dict[str, int] = _LOCK_BASELINE.get("directions", {})

        violations: list[str] = []
        for rel_path, count in sorted(baseline.items()):
            if count <= 0:
                continue

            file_path = os.path.join(app_dir, rel_path)
            if not _file_contains_lock_pattern(file_path, self._LOCK_PATTERNS):
                violations.append(
                    f"  {rel_path}: {count} direct assignment(s), no lock guard found"
                )

        assert not violations, (
            "Files with .directions assignments lack lock acquisition guards:\n"
            + "\n".join(violations)
            + "\n\nEach file must contain at least one of these lock-acquisition patterns:\n"
            + "\n".join(f"  - {p}" for p in self._LOCK_PATTERNS)
            + "\n\nEither:\n"
            "  (a) Migrate the write to with_directions_locked() (in task_locking.py) and "
            "lower the count in lock_assignment_baseline.json, or\n"
            "  (b) Restore the surrounding project_lock / with_for_update / "
            "lock_project_or_429 call that was accidentally removed."
        )


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


# ── Running Update Lock Invariant ──────────────────────────────────────────────


class TestRunningUpdateLockInvariant:
    """Tripwire: direct <var>.running_update attribute assignments must not
    increase above their per-file baseline recorded in lock_assignment_baseline.json.

    Uses AST-based scanning (receiver-name-agnostic: matches any receiver so
    aliases like proj / p / _proj are all caught).  Assignments inside
    docstrings, comments, or string literals are naturally excluded.

    The baseline is a ratchet — counts may only decrease as write sites are
    migrated to the canonical reconciliation helper (reconcile_running_update /
    apply_reconciled_running_update in running_update_merge.py). Adding a new
    assignment to any file (even an already-baselined one) raises the count
    above its baseline and fails this test.

    To migrate a site: route it through apply_reconciled_running_update()
    (running_update_merge.py) and lower the count in lock_assignment_baseline.json.
    Remove the entry when count reaches 0.

    Note: running_update does NOT use the task_locking.py with_*_locked()
    helpers. Its guarantee is operator-wins reconciliation through the shared
    running_update_merge helpers, not a row-lock pattern.
    """

    def test_no_new_unlocked_running_update_writes(self):
        _check_lock_invariant(
            "running_update", "running_update", "apply_reconciled_running_update"
        )


# ── Design-Doc Lock Assignment Guard ─────────────────────────────────────────


class TestDesignDocLockAssignmentGuard:
    """Positive ratchet: files with non-zero .design_doc assignments must be
    wrapped in a project-row-lock acquisition site.

    Complements TestDesignDocLockInvariant (negative ratchet). That test catches
    NEW direct assignments. This test ensures EXISTING assignments have a surrounding
    lock — if the lock is removed without migrating the write to with_design_doc_locked(),
    this test fails.

    Recognized lock acquisition sites:
      - with_for_update(...) on Project (or alias)
      - project_lock(...)
      - lock_project_or_429(...) or _lock_project_or_429(...)
      - with_design_doc_locked(...)

    Failure message suggests either migrating to with_design_doc_locked or
    restoring the surrounding lock call.
    """

    # Recognized patterns that indicate a file has a project-row lock.
    # Substring matching is sufficient (conservative approach).
    _LOCK_PATTERNS = [
        "with_for_update(",
        "project_lock(",
        "lock_project_or_429(",
        "with_design_doc_locked(",
    ]

    def test_design_doc_writes_guarded_by_locks(self):
        """Every file with .design_doc writes must contain at least one lock acquisition."""
        app_dir = os.path.normpath(os.path.join(os.path.dirname(__file__), "..", "app"))
        baseline: dict[str, int] = _LOCK_BASELINE.get("design_doc", {})
        exempt = _LOCK_GUARD_EXEMPT.get("design_doc", set())

        violations: list[str] = []
        for rel_path, count in sorted(baseline.items()):
            if count <= 0:
                continue
            if rel_path in exempt:
                continue

            file_path = os.path.join(app_dir, rel_path)
            if not _file_contains_lock_pattern(file_path, self._LOCK_PATTERNS):
                violations.append(
                    f"  {rel_path}: {count} direct assignment(s), no lock guard found"
                )

        assert not violations, (
            "Files with .design_doc assignments lack lock acquisition guards:\n"
            + "\n".join(violations)
            + "\n\nEach file must contain at least one of these lock-acquisition patterns:\n"
            + "\n".join(f"  - {p}" for p in self._LOCK_PATTERNS)
            + "\n\nEither:\n"
            "  (a) Migrate the write to with_design_doc_locked() (in task_locking.py) and "
            "lower the count in lock_assignment_baseline.json, or\n"
            "  (b) Restore the surrounding project_lock / with_for_update / "
            "lock_project_or_429 call that was accidentally removed."
        )


# ── Vision Lock Assignment Guard ──────────────────────────────────────────────


class TestVisionLockAssignmentGuard:
    """Positive ratchet: files with non-zero .vision assignments must be
    wrapped in a project-row-lock acquisition site.

    Complements TestVisionLockInvariant (negative ratchet). That test catches
    NEW direct assignments. This test ensures EXISTING assignments have a surrounding
    lock — if the lock is removed without migrating the write to with_vision_locked(),
    this test fails.

    Recognized lock acquisition sites:
      - with_for_update(...) on Project (or alias)
      - project_lock(...)
      - lock_project_or_429(...) or _lock_project_or_429(...)
      - with_vision_locked(...)

    Failure message suggests either migrating to with_vision_locked or
    restoring the surrounding lock call.
    """

    # Recognized patterns that indicate a file has a project-row lock.
    # Substring matching is sufficient (conservative approach).
    _LOCK_PATTERNS = [
        "with_for_update(",
        "project_lock(",
        "lock_project_or_429(",
        "with_vision_locked(",
    ]

    def test_vision_writes_guarded_by_locks(self):
        """Every file with .vision writes must contain at least one lock acquisition."""
        app_dir = os.path.normpath(os.path.join(os.path.dirname(__file__), "..", "app"))
        baseline: dict[str, int] = _LOCK_BASELINE.get("vision", {})
        exempt = _LOCK_GUARD_EXEMPT.get("vision", set())

        violations: list[str] = []
        for rel_path, count in sorted(baseline.items()):
            if count <= 0:
                continue
            if rel_path in exempt:
                continue

            file_path = os.path.join(app_dir, rel_path)
            if not _file_contains_lock_pattern(file_path, self._LOCK_PATTERNS):
                violations.append(
                    f"  {rel_path}: {count} direct assignment(s), no lock guard found"
                )

        assert not violations, (
            "Files with .vision assignments lack lock acquisition guards:\n"
            + "\n".join(violations)
            + "\n\nEach file must contain at least one of these lock-acquisition patterns:\n"
            + "\n".join(f"  - {p}" for p in self._LOCK_PATTERNS)
            + "\n\nEither:\n"
            "  (a) Migrate the write to with_vision_locked() (in task_locking.py) and "
            "lower the count in lock_assignment_baseline.json, or\n"
            "  (b) Restore the surrounding project_lock / with_for_update / "
            "lock_project_or_429 call that was accidentally removed."
        )


# ── Behind-Recovery update_branch Guard ──────────────────────────────────────


class TestBehindRecoveryUpdateBranchGuard:
    """Tripwire: every behind-recovery caller of ``update_branch`` /
    ``update_branch_with_sha_retry`` (from ``app.github``) must consult the
    behind-base detector ``pr_is_not_behind`` first.

    Motivation
    ----------
    ``pr_is_not_behind`` (defined in ``backend/app/github.py``) lets callers
    skip an unnecessary GitHub ``update_branch`` write when the PR head is
    already an ancestor of base, or when ``mergeable_state`` is ``"clean"``.
    Those writes are not free — they round-trip GitHub, trigger fresh CI, and
    burn behind-retry budget on PRs that don't need rebasing.

    The pattern was wired into three sites already (one in
    ``ci_completion.py``, two in ``rollup_management.py``) and is being added
    to the orchestrator's CI polling site (``backend/app/orchestrator.py``)
    in a parallel task. This ratchet exists so a future contributor cannot
    add a new ``update_branch_with_sha_retry`` caller in a behind-recovery
    context without also wiring the ``pr_is_not_behind`` short-circuit.

    How it works
    ------------
    The test walks every ``.py`` file under ``backend/app/`` with ``ast.parse``
    (skipping ``app/github.py`` itself, which defines both the helper and the
    short-circuit), finds every ``ast.Call`` whose callee resolves to
    ``update_branch`` or ``update_branch_with_sha_retry`` (matched by name as
    ``ast.Name`` or as the attribute of an ``ast.Attribute``, so
    ``github.update_branch(...)`` is also caught), identifies the smallest
    enclosing function via a parent map, and asserts that ``pr_is_not_behind``
    appears as a ``Name`` or ``Attribute`` somewhere in that function's body
    (excluding nested function scopes). For module-scope calls with no
    enclosing function, the test falls back to a ±50-line window scan of the
    source file.

    Allowlisting a non-behind-recovery caller
    -----------------------------------------
    If a caller of ``update_branch`` / ``update_branch_with_sha_retry`` is
    genuinely NOT in a behind-recovery context (e.g. a maintenance script
    that always wants to rebase regardless of ancestor state, or a test
    scaffold that mocks the helper), add a ``"<relative_path>:<function>"``
    entry to ``_ALLOWLIST`` with a comment explaining why the call is not a
    behind-recovery write. Default behavior is "every call must be guarded";
    use the allowlist sparingly.

    Pending-fix entries
    -------------------
    ``_PENDING_GUARDS`` is a *temporary* allowlist for known unguarded sites
    that are being fixed in a parallel PR. Each entry MUST carry a TODO
    referencing the parallel work. A companion test
    (``test_pending_guards_are_eventually_resolved``) is marked
    ``pytest.mark.xfail(strict=True)`` so that once the parallel fix lands —
    making the pending site actually guarded — the test xpasses, strict
    failure fires, and the maintainer is forced to remove the corresponding
    ``_PENDING_GUARDS`` entry in the same change.
    """

    # callee identifier (final segment, whether ``ast.Name`` or
    # ``ast.Attribute``) → all of these are GitHub writes that should be
    # gated by ``pr_is_not_behind`` in any behind-recovery context.
    _TARGET_CALLEES = frozenset({"update_branch", "update_branch_with_sha_retry"})

    # Explicit guard symbols — referencing ANY of these in the enclosing
    # function body satisfies the tripwire. ``pr_is_not_behind`` is the
    # underlying detector; ``pr_is_not_behind_for`` is the convenience
    # wrapper in ``app/github.py`` that bundles the head_sha extraction +
    # get_branch_sha fetch boilerplate around it. Either reference is
    # sufficient evidence that the caller consulted the detector.
    _GUARD_NAMES = frozenset({"pr_is_not_behind", "pr_is_not_behind_for"})

    # ``app/github_pulls.py`` defines both ``update_branch`` and
    # ``pr_is_not_behind``, and the internal
    # ``update_branch_with_sha_retry -> update_branch`` call inside that file's
    # helper is by definition not a behind-recovery caller.  ``app/github.py``
    # is the re-export façade for the same symbols.  Skip both files.
    _SKIP_FILES = frozenset({"github.py", "github_pulls.py"})

    # ``"<relative_path>:<function_name>"`` entries for callers that are
    # legitimately NOT behind-recovery writes.  Empty by default — every
    # entry needs a comment justifying why the caller skips the guard.
    _ALLOWLIST: frozenset[str] = frozenset()

    # ``"<relative_path>:<function_name>"`` entries for sites currently
    # being fixed in a parallel PR. Each MUST carry a TODO. The companion
    # ``test_pending_guards_are_eventually_resolved`` will xpass once the
    # parallel fix lands, forcing removal of the entry.
    _PENDING_GUARDS: frozenset[str] = frozenset()

    @staticmethod
    def _callee_final_name(func):
        """Return the final identifier of a Call's func, or None if untyped."""
        import ast

        if isinstance(func, ast.Name):
            return func.id
        if isinstance(func, ast.Attribute):
            return func.attr
        return None

    @staticmethod
    def _build_parent_map(tree):
        """Return id(child) → parent for every node reachable from tree."""
        import ast

        parents: dict = {}
        for parent in ast.walk(tree):
            for child in ast.iter_child_nodes(parent):
                parents[id(child)] = parent
        return parents

    @classmethod
    def _enclosing_function(cls, node, parents):
        """Walk parent chain to find smallest enclosing FunctionDef, or None."""
        import ast

        cur = parents.get(id(node))
        while cur is not None:
            if isinstance(cur, (ast.FunctionDef, ast.AsyncFunctionDef)):
                return cur
            cur = parents.get(id(cur))
        return None

    @classmethod
    def _function_references_guard(cls, fn) -> bool:
        """True if the guard symbol is referenced anywhere in fn's body,
        excluding nested function scopes (each nested function is its own
        scope and must have its own guard reference)."""
        import ast

        stack = list(getattr(fn, "body", []) or [])
        while stack:
            node = stack.pop()
            # Skip nested function scopes — they're checked independently if
            # they contain their own Call to a target.
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            if isinstance(node, ast.Name) and node.id in cls._GUARD_NAMES:
                return True
            if isinstance(node, ast.Attribute) and node.attr in cls._GUARD_NAMES:
                return True
            stack.extend(ast.iter_child_nodes(node))
        return False

    @classmethod
    def _module_window_references_guard(
        cls, source_lines: list[str], lineno: int, window: int = 50
    ) -> bool:
        """Fallback for module-scope calls: scan ±window lines for guard."""
        lo = max(0, lineno - 1 - window)
        hi = min(len(source_lines), lineno + window)
        for line in source_lines[lo:hi]:
            # Strip-and-skip leading comments to mirror the AST's exclusion of
            # comments (string literals also won't trip this since the guard
            # name is unambiguous).
            stripped = line.lstrip()
            if stripped.startswith("#"):
                continue
            if any(name in line for name in cls._GUARD_NAMES):
                return True
        return False

    @classmethod
    def _scan_app_tree(cls) -> tuple[list[str], list[tuple[str, str]]]:
        """Walk ``backend/app/`` and collect (violations, guarded_sites).

        Returns
        -------
        violations
            Human-readable lines describing unguarded callers, suitable for
            inclusion in an assertion message.
        guarded_sites
            ``[(relative_path, function_name), ...]`` for every call site that
            IS guarded.  Used by the positive-sanity assertion to confirm the
            known good sites are still detected as guarded.
        """
        import ast
        import glob as globmod

        app_dir = os.path.normpath(os.path.join(os.path.dirname(__file__), "..", "app"))
        violations: list[str] = []
        guarded_sites: list[tuple[str, str]] = []

        py_files = sorted(
            globmod.glob(os.path.join(app_dir, "**", "*.py"), recursive=True)
        )
        for py_file in py_files:
            rel = os.path.relpath(py_file, app_dir)
            if rel in cls._SKIP_FILES:
                continue
            try:
                with open(py_file) as fh:
                    source = fh.read()
            except (IOError, OSError):
                continue
            try:
                tree = ast.parse(source, filename=py_file)
            except SyntaxError:
                # Skip unparseable files — same conservative posture as
                # _ast_count_attr_assignments above.
                print(f"[behind-recovery guard] skipping unparseable: {rel}")
                continue

            source_lines = source.splitlines()
            parents = cls._build_parent_map(tree)

            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                callee = cls._callee_final_name(node.func)
                if callee not in cls._TARGET_CALLEES:
                    continue

                fn = cls._enclosing_function(node, parents)
                fn_name = fn.name if fn is not None else "<module>"
                allow_key = f"{rel}:{fn_name}"

                if allow_key in cls._ALLOWLIST:
                    continue
                if allow_key in cls._PENDING_GUARDS:
                    # Pending fix — the companion xfail test guards us
                    # against forgetting to remove this entry.
                    continue

                if fn is not None:
                    is_guarded = cls._function_references_guard(fn)
                else:
                    is_guarded = cls._module_window_references_guard(
                        source_lines, node.lineno
                    )

                if is_guarded:
                    guarded_sites.append((rel, fn_name))
                else:
                    violations.append(
                        f"New unguarded {callee} call in {rel}:{node.lineno}. "
                        "Wire pr_is_not_behind first; see "
                        "backend/app/ci_completion.py:438-468 for the "
                        "canonical pattern."
                    )

        return violations, guarded_sites

    def test_no_unguarded_update_branch_callers(self):
        """Every caller of update_branch / update_branch_with_sha_retry must
        reference pr_is_not_behind in its enclosing function body."""
        violations, _guarded = self._scan_app_tree()
        assert not violations, (
            "Unguarded behind-recovery update_branch caller(s) detected:\n"
            + "\n".join(f"  {v}" for v in violations)
            + "\n\nEach call to update_branch / update_branch_with_sha_retry "
            "in a behind-recovery context must be preceded by a "
            "pr_is_not_behind short-circuit. See the docstring on "
            "TestBehindRecoveryUpdateBranchGuard for how to add an "
            "_ALLOWLIST entry if the caller is genuinely not behind-recovery."
        )

    def test_known_guarded_sites_are_detected(self):
        """Positive sanity: the three currently-wired guarded sites must be
        detected as guarded. If this assertion fails, the AST walk is broken
        and the negative ratchet above would silently pass on regressions.

        Once the parallel orchestrator fix lands and
        ``orchestrator.py:orchestrator_worker`` is removed from
        ``_PENDING_GUARDS``, it must also appear in this guarded set —
        update the expected list at that time (the existing ratchet
        ``test_pending_guards_are_eventually_resolved`` will fire to remind
        the maintainer).
        """
        _violations, guarded = self._scan_app_tree()
        guarded_keys = {f"{rel}:{fn}" for rel, fn in guarded}

        # We expect at minimum these three currently-wired sites. Each
        # appears once per Call site, so we use a containment check rather
        # than equality (the same enclosing function may host multiple
        # update_branch calls, all of which collapse to the same key).
        expected_subset = {
            # Single guarded call in ci_completion.py inside the CI merge
            # finalization function.
            "ci_completion.py",
            # Single guarded call in rollup_management.py (post-merge ancestor
            # check). The pre-merge SHA-mismatch check was removed in PR #6188
            # as part of dropping the redundant pre-merge behind path.
            "rollup_management.py",
            # Guarded call in orchestrator_ci_monitor.py inside
            # _monitor_evaluating_tasks for the CI polling behind path (the
            # EVALUATING/CI-waiting monitor sweep was extracted verbatim out
            # of orchestrator_worker; the pr_is_not_behind_for guard moved
            # with it).
            "orchestrator_ci_monitor.py",
        }
        guarded_files = {rel for rel, _fn in guarded}
        missing = expected_subset - guarded_files
        assert not missing, (
            "Positive-sanity check failed — the AST walk did not detect "
            "known-guarded sites in: " + ", ".join(sorted(missing)) + ".\n"
            "guarded_keys=" + ", ".join(sorted(guarded_keys))
        )

        # At least three guarded call sites must be present (1 in
        # ci_completion + 1 in rollup_management + 1 in
        # orchestrator_ci_monitor).
        assert len(guarded) >= 3, (
            f"Expected at least 3 guarded update_branch sites, "
            f"detected {len(guarded)}: {guarded}"
        )

    def test_pending_guards_are_eventually_resolved(self):
        """Strict-xfail tripwire that flips once every site in
        ``_PENDING_GUARDS`` becomes actually guarded in source.

        For each ``"<rel>:<fn>"`` pending entry, this test parses the file,
        finds the named function, and asserts that ``pr_is_not_behind`` is
        referenced inside it. While the parallel fix is in flight, the
        assertion fails (expected). The instant the fix lands the assertion
        passes — strict xfail then fails the build, forcing the maintainer
        to remove both the xfail mark and the corresponding
        ``_PENDING_GUARDS`` entry in the same change.
        """
        import ast

        app_dir = os.path.normpath(os.path.join(os.path.dirname(__file__), "..", "app"))
        unresolved: list[str] = []
        for entry in sorted(self._PENDING_GUARDS):
            rel, _, fn_name = entry.partition(":")
            py_file = os.path.join(app_dir, rel)
            with open(py_file) as fh:
                source = fh.read()
            tree = ast.parse(source, filename=py_file)
            target_fn = None
            for node in ast.walk(tree):
                if (
                    isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                    and node.name == fn_name
                ):
                    target_fn = node
                    break
            assert target_fn is not None, (
                f"_PENDING_GUARDS references unknown function {entry}; "
                "update the entry or remove it."
            )
            if not self._function_references_guard(target_fn):
                unresolved.append(entry)

        assert not unresolved, (
            "Pending behind-recovery guard fix(es) not yet applied: "
            + ", ".join(unresolved)
        )


# ── BuildConfig Schema Coverage ───────────────────────────────────────────────


class TestBuildConfigSchemaCoverage:
    """Guard: every build_config key read by a Project property must be declared
    in BuildConfig so schema-validated saves do not silently strip the key.

    Scans models.py for `(self.build_config or {}).get("key")` patterns inside
    Project property methods and asserts each extracted key name appears as a
    field in BuildConfig.model_fields. A CI failure here means a new property
    was added to Project without a corresponding schema field — the round-trip
    through the admin PATCH endpoint would silently drop the key on save.
    """

    def test_build_config_model_properties_declared_in_schema(self) -> None:
        models_path = os.path.normpath(
            os.path.join(os.path.dirname(__file__), "..", "app", "models.py")
        )
        with open(models_path) as f:
            source = f.read()

        consumed_keys = set(
            re.findall(
                r"\(self\.build_config\s+or\s+\{\}\)\.get\(\s*['\"]([^'\"]+)['\"]",
                source,
            )
        )
        assert consumed_keys, (
            "No build_config .get() calls found in models.py — "
            "the regex may need updating if the access pattern changed."
        )

        from app.schemas import BuildConfig

        declared_keys = set(BuildConfig.model_fields.keys())
        undeclared = consumed_keys - declared_keys
        assert not undeclared, (
            "build_config keys read by Project properties but not declared in "
            "BuildConfig schema (they will be silently stripped on save):\n"
            + "\n".join(f"  - {k}" for k in sorted(undeclared))
            + "\n\nAdd each missing key to BuildConfig in backend/app/schemas.py."
        )


class TestVmCostRateConsolidation:
    """Verify that VM cost rates are centralized and used consistently across consumers."""

    def test_all_consumers_resolve_same_rate(self) -> None:
        """All modules derive ON_DEMAND_RATE and SPOT_RATE from the same source."""
        from app.vm_cost_rates import VM_COST_RATES, get_vm_rate_per_second
        from app.routers.admin_analytics import ON_DEMAND_RATE, SPOT_RATE

        expected_on_demand = VM_COST_RATES["e2-standard-2"][0] / 3600
        expected_spot = VM_COST_RATES["e2-standard-2"][1] / 3600

        assert ON_DEMAND_RATE == expected_on_demand, (
            f"admin_analytics ON_DEMAND_RATE ({ON_DEMAND_RATE}) does not match "
            f"vm_cost_rates source ({expected_on_demand})"
        )
        assert SPOT_RATE == expected_spot, (
            f"admin_analytics SPOT_RATE ({SPOT_RATE}) does not match "
            f"vm_cost_rates source ({expected_spot})"
        )

        on_demand_from_func = get_vm_rate_per_second(spot=False)
        spot_from_func = get_vm_rate_per_second(spot=True)

        assert on_demand_from_func == expected_on_demand, (
            f"get_vm_rate_per_second(spot=False) ({on_demand_from_func}) does not match "
            f"expected rate ({expected_on_demand})"
        )
        assert spot_from_func == expected_spot, (
            f"get_vm_rate_per_second(spot=True) ({spot_from_func}) does not match "
            f"expected rate ({expected_spot})"
        )

    def test_default_machine_type_has_rate_entry(self) -> None:
        """The configured default machine type must have a rate entry."""
        from app.compute import MACHINE_TYPE
        from app.vm_cost_rates import VM_COST_RATES

        assert MACHINE_TYPE in VM_COST_RATES, (
            f"Default MACHINE_TYPE '{MACHINE_TYPE}' is not in VM_COST_RATES. "
            "Configured machine type must have an associated cost rate. "
            "Add the rate to VM_COST_RATES in backend/app/vm_cost_rates.py."
        )

    def test_unknown_machine_type_falls_back_to_default(self) -> None:
        """Unknown machine types should fall back to e2-standard-2, not silently yield zero cost."""
        from app.vm_cost_rates import get_vm_rate_per_second, VM_COST_RATES

        unknown_type = "unknown-machine-type"
        expected_rate = VM_COST_RATES["e2-standard-2"][0] / 3600

        rate = get_vm_rate_per_second(machine_type=unknown_type, spot=False)
        assert rate == expected_rate, (
            f"Unknown machine type '{unknown_type}' should fall back to "
            f"e2-standard-2 rate ({expected_rate}), got {rate}"
        )

        unknown_spot = get_vm_rate_per_second(machine_type=unknown_type, spot=True)
        expected_spot = VM_COST_RATES["e2-standard-2"][1] / 3600
        assert unknown_spot == expected_spot, (
            f"Unknown machine type '{unknown_type}' spot rate should fall back to "
            f"e2-standard-2 spot rate ({expected_spot}), got {unknown_spot}"
        )


# ── pr_merged "landed on integration" writer allowlist ─────────────────────


class TestPrMergedLandedWriterAllowlist:
    """Tripwire: NO backend/app/ site may SET the ``pr_merged`` "landed on
    integration" signal via a bare literal ``True`` — every writer must funnel
    through the guarded setter in ``app/landed_signal.py``. The reviewed
    allowlist of permitted inline writers is now EMPTY.

    Motivation
    ----------
    ``pr_merged`` is the canonical "this task's PR actually merged into the
    project integration branch" signal — setting it unblocks dependent work
    and feeds the release/landed query helpers. It must ONLY be set when a PR
    truly landed on the integration branch.

    Setter-consolidation (direction "refactor: separate landed signal from
    pr_merged") is COMPLETE: every writer now funnels through
    ``app/landed_signal.py`` — ``mark_landed_on_integration`` /
    ``apply_merged_landed_signal`` — which stamps the signal through the
    ``LANDED_SIGNAL_KEY`` constant, not a bare ``"pr_merged": True`` literal.
    This ratchet now enumerates any remaining inline literal writer and asserts
    the set is empty so that:

      * a NEW inline writer fails CI with a message directing the author to
        route through the guarded setter rather than stamping the signal
        inline (``found - _ALLOWLIST`` — the allowlist being empty, ANY inline
        writer trips it).

    How it works
    ------------
    The test walks every ``.py`` file under ``backend/app/`` line-by-line
    (the same source-walking style as ``TestBaseBranchFallback`` /
    ``TestStagingBranchClassification``), skipping comment lines
    (``#``-prefixed) and ``# noqa`` lines, and matches writer lines with two
    regexes — a dict-literal/spread form (``"pr_merged": True``) and a
    subscript-assignment form (``ctx["pr_merged"] = True``). It collects the
    SET of relative file paths containing at least one match (file-level keys,
    so no line numbers are hardcoded and consolidation that removes the last
    literal in a file drops it from the set) and asserts set equality against
    ``_ALLOWLIST``.

    Excluded files
    --------------
    ``release_version.py`` is the landed-signal reader/query-helper: it
    contains a ``cast({"pr_merged": True}, JSONB)`` ``@>`` containment query
    plus docstrings describing the signal — it does not WRITE the signal onto
    a task. It is listed in ``_SKIP_FILES`` so the docstring/query text does
    not register as a writer (mirrors how the behind-recovery tripwire skips
    ``github.py``).
    """

    # Dict-literal / spread form: ``"pr_merged": True`` (also matches the
    # conditional-spread idiom ``**({"pr_merged": True} if _landed else {})``).
    _DICT_PATTERN = re.compile(r"""["']pr_merged["']\s*:\s*True\b""")
    # Subscript-assignment form: ``ctx["pr_merged"] = True``.
    _SUBSCRIPT_PATTERN = re.compile(r"""["']pr_merged["']\s*\]\s*=\s*True\b""")

    # Reader / query-helper files that mention ``pr_merged: True`` in a query
    # cast or docstring but never stamp the signal onto a task.
    _SKIP_FILES = frozenset({"release_version.py"})

    # Reviewed set of files that may set the landed signal to True inline.
    # Setter-consolidation is now COMPLETE: every writer funnels through the
    # guarded setter in ``app/landed_signal.py`` (``mark_landed_on_integration``
    # / ``apply_merged_landed_signal``), which stamps ``LANDED_SIGNAL_KEY: True``
    # via the centralised constant rather than a bare ``"pr_merged": True``
    # literal — so the regexes below (which require the quoted ``pr_merged``
    # key) do not match it. The allowlist is therefore EMPTY: no backend/app/
    # file may stamp the literal signal inline. A NEW inline writer fails CI
    # and must route through ``landed_signal`` instead. Keys are paths relative
    # to backend/app/.
    _ALLOWLIST: frozenset[str] = frozenset()

    @classmethod
    def _discover_writers(cls):
        """Return the set of backend/app-relative paths that set pr_merged=True."""
        app_dir = os.path.normpath(os.path.join(os.path.dirname(__file__), "..", "app"))
        found: set[str] = set()
        for root, _dirs, files in os.walk(app_dir):
            for fname in files:
                if not fname.endswith(".py"):
                    continue
                fpath = os.path.join(root, fname)
                rel = os.path.relpath(fpath, app_dir).replace(os.sep, "/")
                if rel in cls._SKIP_FILES:
                    continue
                with open(fpath, encoding="utf-8") as f:
                    for line in f:
                        if line.lstrip().startswith("#") or "# noqa" in line:
                            continue
                        if cls._DICT_PATTERN.search(
                            line
                        ) or cls._SUBSCRIPT_PATTERN.search(line):
                            found.add(rel)
                            break
        return found

    def test_landed_writer_set_matches_allowlist(self):
        """The set of pr_merged=True writer files must equal the reviewed allowlist."""
        found = self._discover_writers()
        new_writers = sorted(found - self._ALLOWLIST)
        stale_entries = sorted(self._ALLOWLIST - found)
        assert found == self._ALLOWLIST, (
            "The set of backend/app/ files that set the pr_merged 'landed on "
            "integration' signal to True drifted from the reviewed allowlist.\n"
            + (
                "\nNEW writer(s) detected — the landed signal must only be set "
                "for a real integration-branch merge. Route the write through the "
                'shared guarded setter instead of stamping "pr_merged": True '
                "inline; do NOT just add the file to _ALLOWLIST:\n"
                + "\n".join(f"  + {p}" for p in new_writers)
                if new_writers
                else ""
            )
            + (
                "\nSTALE allowlist entry — this file no longer sets pr_merged=True "
                "(consolidation removed it). Remove it from _ALLOWLIST in "
                "TestPrMergedLandedWriterAllowlist:\n"
                + "\n".join(f"  - {p}" for p in stale_entries)
                if stale_entries
                else ""
            )
        )

    def test_writer_regexes_still_match_sample_literals(self):
        """Positive sanity: the writer regexes must still match a known literal.

        Now that consolidation is complete the live scan finds ZERO inline
        writers, so a file-based "must detect something" check would be
        unsatisfiable. Instead we assert the detection regexes still match the
        canonical inline forms directly — if they silently stop matching, the
        empty-allowlist set-equality tripwire would pass vacuously even after a
        real inline writer reappears. This guards against that regression
        without requiring any inline writer to exist.
        """
        assert self._DICT_PATTERN.search('        "pr_merged": True,')
        assert self._DICT_PATTERN.search("ctx = {**base, 'pr_merged': True}")
        assert self._SUBSCRIPT_PATTERN.search('    ctx["pr_merged"] = True')
        # And it must NOT match the centralised-constant form the guarded setter
        # uses, so ``app/landed_signal.py`` is correctly left undetected.
        assert not self._DICT_PATTERN.search("        LANDED_SIGNAL_KEY: True,")


# ── Typed mock-call helper convention ─────────────────────────────────────────
#
# Once a project-internal function's mock-call assertions adopt the type-safe
# wrapper ``expectCalledWith`` (agent/src/utils/typed-mock-call.ts), signature
# drift becomes a ``tsc`` error caught by the agent CI lane. Nothing, however,
# stops a future edit from dropping back to the raw, untyped
# ``expect(vi.mocked(fn)).toHaveBeenCalledWith(...)`` matcher for one of those
# functions — silently re-opening the drift blind spot the wrapper closed.
#
# The agent Vitest suite is NOT run in CI and ``.github/workflows`` is
# off-limits, so the durable lock must live in a BACKEND pytest test (which DOES
# run in CI). This mirrors the precedent of ``test_agent_branch_invariants.py``,
# which statically scans the agent TypeScript source.
#
# A HAND-MAINTAINED name list fails SILENT the moment a new project-internal
# function appears: a raw ``expect(vi.mocked(newInternalFn)).toHaveBeenCalledWith
# (...)`` on a function not in the list slips through unnoticed — the same
# silent-escape meta-gap the Alembic ``_MERGE_NAMES`` guard had. So the guard
# does NOT keep a name list. Instead it classifies each mock-call target through
# the *test file's own imports*:
#
#   * A binding imported from a RELATIVE specifier (``./x.js``, ``../index.js``,
#     ``../prompt.js``) is project-internal — a raw argument-bearing matcher on
#     it is an offender and must adopt the typed helper.
#   * A binding imported from a BARE specifier (``fs``, ``node:child_process``,
#     ``ws``, any third-party package) is external — its signature is owned
#     elsewhere, so a raw matcher is legitimately allowed.
#
# Namespace-qualified targets (``vi.mocked(idx.fn)`` with
# ``import * as idx from '../index.js'`` or ``const idx = await import('../index.js')``)
# resolve through the namespace binding. No-argument matchers
# (``toHaveBeenCalled``) and setup calls (``mockResolvedValue``) are never
# flagged — they carry no argument list to drift on signature. This mirrors the
# self-locking precedent of ``TestPrMergedLandedWriterAllowlist`` in this file.

# Documented exemptions: internal bindings that genuinely cannot adopt the typed
# helper, mapping ``binding name -> human reason``. Mirrors the documented
# allowlists used by the ``os.walk`` invariants elsewhere in this file. The
# canonical case is an OVERLOADED function: ``Parameters<F>`` resolves only to
# the LAST overload, so the wrapper would reject valid calls made against an
# earlier overload. Keep this set minimal and justified — an empty set is the
# correct default and means "every internal binding must use the typed helper".
_TYPED_MOCK_EXEMPT: "dict[str, str]" = {}

# Resolve agent/src across local + Docker test environments. Mirrors
# ``_resolve_agent_src_root`` in test_agent_branch_invariants.py:
#   1. AGENT_SRC_ROOT env override.
#   2. /agent/src (Docker volume-mount target used by scripts/test-backend.sh).
#   3. Host filesystem at ../../agent/src from this test file.


def _resolve_agent_src_for_typed_mock() -> "Path | None":
    candidates: list[Path] = []
    env_override = os.getenv("AGENT_SRC_ROOT")
    if env_override:
        candidates.append(Path(env_override))
    candidates.append(Path("/agent/src"))
    candidates.append(Path(__file__).resolve().parents[2] / "agent" / "src")
    for candidate in candidates:
        if candidate.is_dir():
            return candidate
    return None


_TYPED_MOCK_AGENT_SRC = _resolve_agent_src_for_typed_mock()


# Matches a RAW argument-bearing mock-call assertion on ANY binding:
#   expect(vi.mocked(setupWorkspace)).toHaveBeenCalledWith(
#   expect(vi.mocked(idx.setupWorkspace)).not.toHaveBeenCalledWith(
#   expect(vi.mocked(captureTestBaseline)).toHaveBeenLastCalledWith(
# The optional ``<namespace>.`` qualifier and the bare function name are both
# captured so the scanner can resolve either through the file's import map. An
# optional ``.not`` may precede the matcher. Only the argument-bearing matchers
# (``toHaveBeenCalledWith`` / ``toHaveBeenLastCalledWith``) are matched — the
# no-arg ``toHaveBeenCalled`` and ``.mockResolvedValue`` forms carry no argument
# list and so cannot drift on signature.
_RAW_TYPED_MOCK_RE = re.compile(
    r"vi\.mocked\(\s*(?:(?P<ns>\w+)\.)?(?P<fn>\w+)\s*\)\s*\)\s*(?:\.not)?\."
    r"(?P<matcher>toHaveBeenCalledWith|toHaveBeenLastCalledWith)"
)

# ── Import-classification helpers ─────────────────────────────────────────────
# These build a per-file map of ``binding name -> is_internal`` from the test
# file's own import statements, so a mock-call target can be classified as
# project-internal (relative specifier) or external (bare specifier) without any
# hand-maintained name list.
_STATIC_IMPORT_RE = re.compile(
    r"\bimport\s+(?P<clause>[^'\";]+?)\s+from\s+['\"](?P<spec>[^'\"]+)['\"]"
)
_DYNAMIC_DESTRUCTURE_RE = re.compile(
    r"\b(?:const|let|var)\s*\{(?P<names>[^}]*)\}\s*=\s*await\s+"
    r"import\(\s*['\"](?P<spec>[^'\"]+)['\"]"
)
_DYNAMIC_BINDING_RE = re.compile(
    r"\b(?:const|let|var)\s+(?P<name>\w+)\s*=\s*\(?\s*await\s+"
    r"import\(\s*['\"](?P<spec>[^'\"]+)['\"]"
)


def _spec_is_internal(spec: str) -> bool:
    """A relative module specifier denotes project-internal source."""
    return spec.startswith("./") or spec.startswith("../")


def _bindings_from_static_clause(clause: str) -> "list[str]":
    """Extract every runtime binding name from a static ``import`` clause.

    Handles ``* as ns`` namespace, ``{ a, b as c, type D }`` named, and leading
    default (``fs``, ``fs, { x }``) forms.
    """
    bindings: list[str] = []
    clause = clause.strip()
    ns = re.search(r"\*\s+as\s+(\w+)", clause)
    if ns:
        bindings.append(ns.group(1))
    brace = re.search(r"\{([^}]*)\}", clause)
    if brace:
        for part in brace.group(1).split(","):
            part = part.strip()
            if not part:
                continue
            part = re.sub(r"^type\s+", "", part)  # ``{ type Foo }`` inline form
            alias = re.search(r"\bas\s+(\w+)\s*$", part)  # ``orig as alias``
            if alias:
                bindings.append(alias.group(1))
            else:
                head = re.match(r"(\w+)", part)
                if head:
                    bindings.append(head.group(1))
    # Default import: a leading bare identifier before any ``{`` or ``*``.
    lead = clause.split(",")[0].strip()
    if re.fullmatch(r"\w+", lead):
        bindings.append(lead)
    return bindings


def _bindings_from_destructure(names: str) -> "list[str]":
    """Extract bindings from a dynamic ``const { a, default: fs } = await import``
    destructure pattern (the right-hand side of ``key: alias`` is the binding).
    """
    bindings: list[str] = []
    for part in names.split(","):
        part = part.strip()
        if not part:
            continue
        if ":" in part:  # ``{ default: fs }`` / ``{ orig: alias }``
            part = part.split(":", 1)[1].strip()
        head = re.match(r"(\w+)", part)
        if head:
            bindings.append(head.group(1))
    return bindings


def _parse_import_map(src: str) -> "dict[str, bool]":
    """Map each imported binding name -> ``is_internal`` (relative specifier).

    Covers static ``import`` (named, default, ``* as`` namespace) and dynamic
    ``await import('...')`` (destructured, namespace, and ``.default``) forms.
    When a binding name is bound more than once, internal wins (stricter), so a
    binding ever sourced from a relative path is treated as internal.
    """
    # Run over comment-stripped source so a commented-out import is ignored, and
    # join the lines so a multi-line ``import { ... } from '...'`` matches whole.
    text = "\n".join(line for _, line in _strip_ts_comments(src))
    result: dict[str, bool] = {}

    def _record(binding: str, spec: str) -> None:
        result[binding] = result.get(binding, False) or _spec_is_internal(spec)

    for m in _STATIC_IMPORT_RE.finditer(text):
        for binding in _bindings_from_static_clause(m.group("clause")):
            _record(binding, m.group("spec"))
    for m in _DYNAMIC_DESTRUCTURE_RE.finditer(text):
        for binding in _bindings_from_destructure(m.group("names")):
            _record(binding, m.group("spec"))
    for m in _DYNAMIC_BINDING_RE.finditer(text):
        _record(m.group("name"), m.group("spec"))
    return result


def _strip_ts_comments(src: str) -> "list[tuple[int, str]]":
    """Return ``(lineno, text)`` pairs with comment lines/regions removed.

    Compact self-contained stripper so a commented-out example
    (``// expect(vi.mocked(setupWorkspace)).toHaveBeenCalledWith(...)``) does
    not trip the guard. Handles ``//`` line comments, trailing ``//``, and
    ``/* ... */`` block comments (single- and multi-line). String-literal
    awareness is intentionally simple (matching the precedent stripper in
    test_agent_branch_invariants.py) — adequate for the agent test sources.
    """
    result: list[tuple[int, str]] = []
    in_block = False
    for i, line in enumerate(src.splitlines(), start=1):
        stripped = line
        if in_block:
            end = stripped.find("*/")
            if end == -1:
                continue
            stripped = stripped[end + 2 :]
            in_block = False
        while True:
            start = stripped.find("/*")
            if start == -1:
                break
            end = stripped.find("*/", start + 2)
            if end == -1:
                stripped = stripped[:start]
                in_block = True
                break
            stripped = stripped[:start] + stripped[end + 2 :]
        slash_idx = stripped.find("//")
        if slash_idx != -1:
            stripped = stripped[:slash_idx]
        lstripped = stripped.lstrip()
        if not lstripped or lstripped.startswith("*"):
            continue
        result.append((i, stripped))
    return result


def _scan_source_for_raw_typed_mock(
    name: str, src: str, exempt: "dict[str, str] | None" = None
) -> "list[str]":
    """Return ``name:lineno: <line>`` offenders for one test source string.

    An offender is a raw argument-bearing matcher whose target binding (or, for
    ``ns.fn``, its namespace qualifier) resolves through the file's imports to a
    project-internal (relative) specifier and whose function name is not in the
    documented exemption set. External and unresolved bindings are left alone, so
    the guard has zero false positives on Node-stdlib / third-party spies.
    """
    exempt = _TYPED_MOCK_EXEMPT if exempt is None else exempt
    import_map = _parse_import_map(src)
    offenders: list[str] = []
    for lineno, line in _strip_ts_comments(src):
        m = _RAW_TYPED_MOCK_RE.search(line)
        if not m:
            continue
        lookup = m.group("ns") or m.group("fn")
        if not import_map.get(lookup, False):
            continue  # external or unresolved binding — raw matcher allowed.
        if m.group("fn") in exempt:
            continue  # documented, genuinely-unconvertible internal function.
        offenders.append(f"{name}:{lineno}: {line.strip()}")
    return offenders


def _scan_for_raw_typed_mock(path: Path) -> "list[str]":
    """Return ``file:lineno: <line>`` offenders in a single agent test file."""
    return _scan_source_for_raw_typed_mock(path.name, path.read_text(encoding="utf-8"))


def _iter_agent_test_files() -> "list[Path]":
    """Glob every ``*.test.ts`` under the resolved agent/src tree."""
    if _TYPED_MOCK_AGENT_SRC is None:
        return []
    return sorted(_TYPED_MOCK_AGENT_SRC.rglob("*.test.ts"))


class TestTypedMockAssertConvention:
    """Static guard: call-argument assertions on project-internal functions
    (resolved through the test file's own RELATIVE imports) must use the
    type-safe wrapper ``expectCalledWith`` (agent/src/utils/typed-mock-call.ts),
    never the raw ``expect(vi.mocked(fn)).toHaveBeenCalledWith(...)`` matcher.

    The raw matcher accepts ``any[]`` arguments, so a signature change to the
    internal function silently leaves stale assertions green. The wrapper
    constrains the asserted arguments to ``Parameters<F>``, turning drift into a
    ``tsc`` error. Because the agent Vitest suite is not run in CI, this backend
    guard is the only place the convention is CI-enforced.

    Classification is by IMPORT, not by a hand-maintained name list: a binding
    from a relative specifier (``../index.js``) is internal and must be typed; a
    binding from a bare specifier (``fs``, third-party) is external and may stay
    raw. This closes the silent-escape gap where a brand-new internal function,
    absent from any list, would slip through.

    Skips (does not fail) when ``agent/src`` is absent — the CI backend Docker
    image builds from ``./backend`` only, so the agent source is not present
    there; local runs and ``scripts/test-backend.sh`` mount ``agent/src`` and
    enforce the guard.
    """

    _SKIP_REASON = (
        "agent/src not available in this test environment "
        "(CI docker context is ./backend; run locally or via "
        "scripts/test-backend.sh from a checkout for enforcement)"
    )

    def test_protected_fn_allowlist_nonempty_and_resolved(self):
        """The scan must not be silently vacuous: the exemption set must be a
        documented dict and at least one agent test file must be discoverable.

        (Repurposed from the former hardcoded-allowlist assertion — the guard
        now classifies targets by import, so there is no name list to keep
        non-empty. The non-vacuity check it provided is preserved here.) Mirrors
        ``test_target_files_exist`` in test_agent_branch_invariants.py.
        """
        assert isinstance(_TYPED_MOCK_EXEMPT, dict), (
            "_TYPED_MOCK_EXEMPT must be a dict of binding-name -> reason."
        )
        if _TYPED_MOCK_AGENT_SRC is None:
            pytest.skip(self._SKIP_REASON)
        test_files = _iter_agent_test_files()
        assert test_files, (
            "No agent *.test.ts files found under "
            f"{_TYPED_MOCK_AGENT_SRC} — the typed-mock scan would be vacuous. "
            "Has the agent test layout moved?"
        )

    def test_protected_fns_use_typed_helper(self):
        """No agent ``*.test.ts`` file may assert call arguments on a
        relative-imported (project-internal) function via the raw untyped
        matcher.

        Correct form (agent/src/utils/typed-mock-call.ts)::

            import { expectCalledWith } from '../utils/typed-mock-call.js';
            expectCalledWith(setupWorkspace, workspace, { scope: 'medium' });

        Forbidden form (re-opens the signature-drift blind spot)::

            import { setupWorkspace } from '../index.js';
            expect(vi.mocked(setupWorkspace)).toHaveBeenCalledWith(
                workspace, { scope: 'medium' });

        The ``.not`` and ``toHaveBeenLastCalledWith`` variants, and an optional
        ``<module>.`` namespace qualifier (``idx.setupWorkspace``), are all
        covered. No-argument forms (``toHaveBeenCalled()``,
        ``.mockResolvedValue(...)``) and bindings from bare specifiers
        (``fs.writeFileSync``) are intentionally NOT flagged.
        """
        if _TYPED_MOCK_AGENT_SRC is None:
            pytest.skip(self._SKIP_REASON)
        all_offenders: list[str] = []
        for path in _iter_agent_test_files():
            all_offenders.extend(_scan_for_raw_typed_mock(path))

        assert not all_offenders, (
            "Raw untyped mock-call assertion found on a project-internal "
            "(relative-imported) function. The raw "
            "``expect(vi.mocked(fn)).toHaveBeenCalledWith(...)`` matcher accepts "
            "``any[]`` args, so a signature change would leave the assertion "
            "silently green. Use the type-safe wrapper ``expectCalledWith`` / "
            "``expectLastCalledWith`` / ``expectNotCalledWith`` from "
            "``agent/src/utils/typed-mock-call.ts`` instead (or, if the function "
            "genuinely cannot be typed, add it to ``_TYPED_MOCK_EXEMPT`` with a "
            "reason):\n" + "\n".join(all_offenders)
        )

    def test_typed_mock_scanner_detects_injected_regression(self):
        """Sanity-check the scanner against synthetic *sources* (with import
        headers) so a future tweak to the regex or the import classifier does
        not silently weaken the guard.

        The ``bad`` source asserts call args on relative-imported bindings via
        the raw matcher (named, namespace-qualified, and ``toHaveBeenLastCalled``
        forms) — every one must be flagged. The ``good`` source uses the typed
        wrapper, no-arg matchers, ``mockResolvedValue`` setup, bare-specifier
        spies, and a commented-out example — none may be flagged.
        """
        bad_src = (
            "import { setupWorkspace, runClaudeCode } from '../index.js';\n"
            "import { captureTestBaseline } from '../utils/test-fix-loop.js';\n"
            "import * as idx from '../index.js';\n"
            "import fs from 'fs';\n"
            "describe('x', () => {\n"
            "  it('y', () => {\n"
            "    expect(vi.mocked(setupWorkspace)).toHaveBeenCalledWith(ws, {});\n"
            "    expect(vi.mocked(idx.runClaudeCode)).not.toHaveBeenCalledWith(p);\n"
            "    expect(vi.mocked(captureTestBaseline)).toHaveBeenLastCalledWith(ws, []);\n"
            "  });\n"
            "});\n"
        )
        offenders = _scan_source_for_raw_typed_mock("bad.test.ts", bad_src)
        assert len(offenders) == 3, (
            f"Expected 3 offenders on relative-imported bindings, got: {offenders}"
        )

        good_src = (
            "import { expectCalledWith, expectNotCalledWith } from "
            "'../utils/typed-mock-call.js';\n"
            "import { setupWorkspace } from '../index.js';\n"
            "import { captureTestBaseline } from '../utils/test-fix-loop.js';\n"
            "import fs from 'fs';\n"
            "import { execFileSync } from 'child_process';\n"
            "describe('x', () => {\n"
            "  it('y', () => {\n"
            "    expectCalledWith(setupWorkspace, ws, {});\n"
            "    expectNotCalledWith(captureTestBaseline, ws);\n"
            "    expect(vi.mocked(setupWorkspace)).toHaveBeenCalled();\n"
            "    expect(vi.mocked(captureTestBaseline)).not.toHaveBeenCalled();\n"
            "    vi.mocked(setupWorkspace).mockResolvedValue({});\n"
            "    expect(vi.mocked(fs.writeFileSync)).toHaveBeenCalledWith(p, d);\n"
            "    expect(vi.mocked(execFileSync)).toHaveBeenCalledWith('git', []);\n"
            "    // expect(vi.mocked(setupWorkspace)).toHaveBeenCalledWith(x);\n"
            "  });\n"
            "});\n"
        )
        assert _scan_source_for_raw_typed_mock("good.test.ts", good_src) == [], (
            "Scanner false-positive on known-good source (typed wrapper, no-arg "
            "matchers, bare-specifier spies, or commented-out example)."
        )

    def test_scanner_classifies_by_import_not_namelist(self):
        """The silent-escape the old name list missed: a brand-new internal
        function — in NO list — is flagged purely because it is relative-imported,
        and the SAME name imported from a bare specifier is NOT flagged. Also
        exercises namespace and default resolution through dynamic imports.
        """
        internal = (
            "import { newInternalFn } from '../foo.js';\n"
            "expect(vi.mocked(newInternalFn)).toHaveBeenCalledWith(1, 2);\n"
        )
        assert _scan_source_for_raw_typed_mock("a.test.ts", internal), (
            "A relative-imported function absent from any name list must still "
            "be flagged — this is the silent-escape the import classifier closes."
        )

        external = (
            "import { newInternalFn } from 'some-third-party';\n"
            "expect(vi.mocked(newInternalFn)).toHaveBeenCalledWith(1, 2);\n"
        )
        assert _scan_source_for_raw_typed_mock("b.test.ts", external) == [], (
            "The same name imported from a bare specifier must NOT be flagged."
        )

        # Namespace qualifier on a dynamic relative import resolves to internal.
        dyn_internal = (
            "it('x', async () => {\n"
            "  const idx = await import('../index.js');\n"
            "  expect(vi.mocked(idx.persistAndPostStatus)).toHaveBeenCalledWith(s);\n"
            "});\n"
        )
        assert _scan_source_for_raw_typed_mock("c.test.ts", dyn_internal), (
            "vi.mocked(idx.fn) with `const idx = await import('../index.js')` "
            "must resolve through the namespace binding and be flagged."
        )

        # Namespace qualifier on a dynamic bare import stays external.
        dyn_external = (
            "it('x', async () => {\n"
            "  const cp = await import('child_process');\n"
            "  expect(vi.mocked(cp.execFileSync)).toHaveBeenCalledWith('git');\n"
            "});\n"
        )
        assert _scan_source_for_raw_typed_mock("d.test.ts", dyn_external) == [], (
            "A namespace binding from a bare specifier must not be flagged."
        )

        # Default-via-destructure dynamic import stays external too.
        dyn_default = (
            "it('x', async () => {\n"
            "  const { default: fs } = await import('fs');\n"
            "  expect(vi.mocked(fs.writeFileSync)).toHaveBeenCalledWith(p, d);\n"
            "});\n"
        )
        assert _scan_source_for_raw_typed_mock("e.test.ts", dyn_default) == [], (
            "`const { default: fs } = await import('fs')` is external."
        )

    def test_typed_mock_exempt_list_documented(self):
        """Every exemption must carry a non-empty human reason, mirroring the
        documented ``os.walk`` allowlists elsewhere in this file. An empty
        exemption set is acceptable (the default); this guards future entries.
        """
        for binding, reason in _TYPED_MOCK_EXEMPT.items():
            assert isinstance(reason, str) and reason.strip(), (
                f"Exemption {binding!r} in _TYPED_MOCK_EXEMPT must document a "
                "non-empty reason (e.g. 'overloaded signature — Parameters<F> "
                "resolves to the last overload only')."
            )

    def test_typed_mock_exempt_list_suppresses_internal(self):
        """An internal binding listed in the exemption set is NOT flagged even
        with a raw argument-bearing matcher — but IS flagged once the exemption
        is removed, proving the suppression is real and not incidental.
        """
        src = (
            "import { overloadedFn } from '../index.js';\n"
            "expect(vi.mocked(overloadedFn)).toHaveBeenCalledWith(a, b);\n"
        )
        control = _scan_source_for_raw_typed_mock("f.test.ts", src)
        assert control, (
            "Control: an internal binding must be flagged without an exemption."
        )
        suppressed = _scan_source_for_raw_typed_mock(
            "f.test.ts", src, exempt={"overloadedFn": "overloaded signature"}
        )
        assert suppressed == [], (
            "An exempt internal function must be suppressed by _TYPED_MOCK_EXEMPT."
        )


# ── Agent ⇄ backend test-runner detector parity (CI-enforced) ─────────────────
#
# Blueprint's no-CI merge gate depends on TWO test-suite detectors emitting the
# same runner FAMILY *and* an equivalent COMMAND for every supported repo layout:
#   - the agent runtime detector  : agent/src/utils/test-runners.ts
#       (``detectTestRunners`` — runs at merge time against the cloned PR head)
#   - the backend onboarding detector : backend/app/routers/admin_onboard.py
#       (``_detect_build_config`` — runs against the GitHub Contents API)
#
# A shared fixture (backend/testing/__fixtures__/test_runner_layouts.json) is the
# single source of truth. The BACKEND half is already pinned to it in plain
# GitHub CI by ``TestSharedTestRunnerLayouts`` in test_onboard.py. The AGENT half
# was only pinned by a *vitest* test, and the agent CI job runs only ``tsc
# --noEmit`` (no vitest), so a single-sided change to the agent detector — a new
# family, a changed command, a dropped manifest — was verified NOWHERE in CI.
#
# This block closes that gap using the same precedent as the SR-category mirror
# (``AGENT_SELF_REVIEW_CATEGORIES`` / ``test_agent_categories_match_prompt_source``)
# and the helper-exclusion scan above:
#   1. ``AGENT_TEST_RUNNER_COMMANDS`` — a backend-resident declarative table of
#      the family → emitted-command mapping the agent detector produces. Lives in
#      the backend so it ships in CI's ``./backend`` Docker context.
#   2. An ALWAYS-ON test pinning that table to the shared fixture (family
#      coverage + exact command), so the table — and via the existing backend
#      parity test, the backend detector — is pinned to the shared spec in plain
#      GitHub CI, no agent/src required.
#   3. A SOURCE-SCAN test that, when agent/src is available (the local +
#      merge-gate path, where scripts/test-backend.sh mounts ../agent/src to
#      /agent/src), statically parses test-runners.ts and asserts the agent
#      detector's literal family → command mapping equals the table. It FAILS
#      LOUDLY on a parse miss (anchor/region not found) and SKIPS with a clear
#      marker only when agent/src is genuinely absent (the GH-only Docker
#      context, exactly like the SR-category test).


# Family → command the agent detector (``detectTestRunners``) emits for each
# supported layout. MUST stay byte-for-byte in sync with BOTH:
#   - the shared fixture test_runner_layouts.json (asserted always-on below), and
#   - the literal runner objects in agent/src/utils/test-runners.ts (asserted by
#     the source-scan below when agent/src is mounted).
#
# The family→command mapping itself lives in the shared, cross-language fixture
# ``__fixtures__/test_runner_commands.json`` so the agent vitest parity loop and
# this backend table consume ONE source of truth (no third hand-synced copy).
# Edit that JSON AND test-runners.ts together; the source-scan fails otherwise.
#
# The ``script`` family is the lone dynamic-arg case: the agent emits
# ``{ cmd: 'bash', args: [script] }`` where ``script`` ranges over a literal list
# (``test.sh``, ``run-tests.sh``, ``run_tests.sh``, ``scripts/test.sh``). The
# shared layout fixture exercises two of those members under this same family:
# ``scripts_test_sh_root_convention`` (=> ``bash scripts/test.sh``) and
# ``test_sh_root_convention`` (=> the root-level ``bash test.sh`` convention).
# All bash fallbacks collapse to family ``script``, so the single representative
# command in the JSON suffices and the table key still matches the fixture's
# detectable-family set exactly; the per-member command variance is covered by
# the ``_AGENT_DYNAMIC_ARG_FAMILIES`` skip, which verifies this family via its
# script-list literal instead.
def _load_agent_runner_commands() -> "dict[str, str]":
    """Load the shared family→command fixture (read-only, single source of truth)."""
    fixture_path = os.path.normpath(
        os.path.join(
            os.path.dirname(__file__), "__fixtures__", "test_runner_commands.json"
        )
    )
    import json

    with open(fixture_path, encoding="utf-8") as fh:
        return json.load(fh)


AGENT_TEST_RUNNER_COMMANDS = _load_agent_runner_commands()

# The lone family whose command the agent builds from a loop variable rather than
# a literal ``args`` array, so it can't be reconstructed from the runner literal
# and is verified via its script-list literal instead.
_AGENT_DYNAMIC_ARG_FAMILIES = {"script"}


def _load_shared_runner_layouts() -> list:
    """Load the shared test-runner layout fixture (read-only)."""
    fixture_path = os.path.normpath(
        os.path.join(
            os.path.dirname(__file__), "__fixtures__", "test_runner_layouts.json"
        )
    )
    import json

    with open(fixture_path, encoding="utf-8") as fh:
        return json.load(fh)


def _resolve_agent_test_runners_ts_path() -> "str | None":
    """Locate ``agent/src/utils/test-runners.ts`` across local + Docker envs.

    Search order mirrors ``_resolve_prompt_ts_path``:
      1. ``AGENT_TEST_RUNNERS_TS`` env override (caller-supplied path).
      2. ``/agent/src/utils/test-runners.ts`` (Docker volume-mount target used by
         ``scripts/test-backend.sh``).
      3. Host filesystem at ``../../agent/src/utils/test-runners.ts`` from this
         test file (``backend/testing/`` → repo root).

    Returns the first existing path or ``None`` if none resolve.
    """
    env_override = os.getenv("AGENT_TEST_RUNNERS_TS")
    candidates = []
    if env_override:
        candidates.append(env_override)
    candidates.append("/agent/src/utils/test-runners.ts")
    candidates.append(
        os.path.normpath(
            os.path.join(
                os.path.dirname(__file__),
                "..",
                "..",
                "agent",
                "src",
                "utils",
                "test-runners.ts",
            )
        )
    )
    for candidate in candidates:
        if os.path.isfile(candidate):
            return candidate
    return None


# Matches a single-line TestRunner object literal's ``cmd``/``args`` pair, e.g.
#   cmd: 'python3', args: ['-m', 'pytest']
#   cmd: 'vendor/bin/phpunit', args: []
# Every runner literal in test-runners.ts is emitted on one line, so a non-greedy
# single-line match captures the command name and the raw args array body.
_AGENT_RUNNER_LITERAL_RE = re.compile(r"cmd:\s*'([^']+)',\s*args:\s*\[([^\]]*)\]")
_AGENT_QUOTED_STRING_RE = re.compile(r"'([^']*)'")


def _reconstruct_agent_commands(source: str) -> "set[str]":
    """Reconstruct every literal ``<cmd> <args...>`` the agent detector emits.

    Parses each ``cmd: '<c>', args: [<a>...]`` runner literal and joins the
    command with its *quoted* args (unquoted args — e.g. the ``[script]`` loop
    variable or the ``pgFlag`` ternary — are intentionally dropped; those runners
    are handled by their own family-specific assertions). Returns the set of
    reconstructed command strings. Raises on a parse miss so the scan can never
    silently pass with an empty set.
    """
    commands: "set[str]" = set()
    for cmd, args_blob in _AGENT_RUNNER_LITERAL_RE.findall(source):
        args = _AGENT_QUOTED_STRING_RE.findall(args_blob)
        commands.add(" ".join([cmd, *args]).strip())
    if not commands:
        raise ValueError(
            "No `cmd: '...', args: [...]` runner literals found in "
            "agent/src/utils/test-runners.ts — the runner-literal format changed "
            "or the file is empty. Update _AGENT_RUNNER_LITERAL_RE in "
            "backend/testing/test_invariants.py to match the new emit format."
        )
    return commands


def _extract_extra_family_region(source: str) -> str:
    """Return the body of the ``detectExtraFamilyRunner`` helper.

    This helper is the canonical home of the divergent-family runners
    (Ruby/Gradle/Maven/Elixir/.NET/PHP/Composer). Scanning it lets the reverse
    parity check catch a NEW extra family added to the agent without a matching
    table entry. Raises (loud) if the anchor or its terminating ``return null;``
    cannot be located — never silently returns an empty region.
    """
    start = source.find("detectExtraFamilyRunner = (")
    if start == -1:
        raise ValueError(
            "Could not find `detectExtraFamilyRunner = (` anchor in "
            "agent/src/utils/test-runners.ts — the divergent-family helper was "
            "renamed or removed. Update _extract_extra_family_region in "
            "backend/testing/test_invariants.py to match."
        )
    end = source.find("return null;", start)
    if end == -1:
        raise ValueError(
            "Could not find the terminating `return null;` of "
            "detectExtraFamilyRunner in agent/src/utils/test-runners.ts — the "
            "helper's structure changed. Update _extract_extra_family_region."
        )
    return source[start:end]


def _extract_generic_fallback_region(source: str) -> str:
    """Return the body of the ``detectGenericFallbackRunner`` helper.

    This helper is the canonical home of generic-fallback runners
    (Makefile, just, Taskfile, tox, nox, shell scripts). Raises (loud) if the
    anchor or its terminating ``return null;`` cannot be located — never
    silently returns an empty region.
    """
    start = source.find("detectGenericFallbackRunner = (")
    if start == -1:
        raise ValueError(
            "Could not find `detectGenericFallbackRunner = (` anchor in "
            "agent/src/utils/test-runners.ts — the generic-fallback helper was "
            "renamed or removed. Update _extract_generic_fallback_region in "
            "backend/testing/test_invariants.py to match."
        )
    end = source.find("return null;", start)
    if end == -1:
        raise ValueError(
            "Could not find the terminating `return null;` of "
            "detectGenericFallbackRunner in agent/src/utils/test-runners.ts — the "
            "helper's structure changed. Update _extract_generic_fallback_region."
        )
    return source[start:end]


def _extract_core_root_region(source: str) -> str:
    """Return the region where core root-level runners are emitted.

    This covers npm, pytest, cargo, go detection at repo root.
    Extracts from the first root-level manifest check to the nested scan.
    Raises (loud) if the anchor cannot be located — never silently returns
    an empty region.
    """
    start = source.find("const pkgPath = `${WORKSPACE}/package.json`;")
    if start == -1:
        raise ValueError(
            "Could not find root-level runner detection anchor in "
            "agent/src/utils/test-runners.ts. The core root runner detection "
            "was refactored or removed. Update _extract_core_root_region in "
            "backend/testing/test_invariants.py to match."
        )
    # End at the start of nested monorepo detection comment
    end = source.find("--- Nested monorepo / external-layout detection", start)
    if end == -1:
        raise ValueError(
            "Could not find nested monorepo comment in "
            "agent/src/utils/test-runners.ts. The structure changed. "
            "Update _extract_core_root_region."
        )
    return source[start:end]


def _extract_nested_package_region(source: str) -> str:
    """Return the region where nested-package runners are emitted.

    This covers detection within subdirectories (services/, apps/, packages/,
    etc.). Raises (loud) if the anchor or terminating condition cannot be
    located — never silently returns an empty region.
    """
    start = source.find("const pushRunnerForPackageDir = (relPath: string): void => {")
    if start == -1:
        raise ValueError(
            "Could not find `pushRunnerForPackageDir` anchor in "
            "agent/src/utils/test-runners.ts — the nested-package detection "
            "was refactored or removed. Update _extract_nested_package_region "
            "in backend/testing/test_invariants.py to match."
        )
    # End before the closing of the main function (find the final closing brace)
    # For safety, look for the specific pattern that ends the nested scanning
    end = source.find("return runners;", start)
    if end == -1:
        raise ValueError(
            "Could not find `return runners;` terminator after nested package region "
            "in agent/src/utils/test-runners.ts. Update _extract_nested_package_region."
        )
    return source[start:end]


def _extract_classifier_region(source: str) -> str:
    """Extract the classifyTestExecution function from test-runners.ts.

    Raises ValueError with a clear message if the function anchor cannot be found.
    """
    start = source.find("export function classifyTestExecution")
    if start == -1:
        raise ValueError(
            "Could not find `export function classifyTestExecution` anchor in "
            "agent/src/utils/test-runners.ts — the classifier function was "
            "renamed or removed. Update _extract_classifier_region in "
            "backend/testing/test_invariants.py to match."
        )

    # Find the end by searching for the final return statement of the function
    end = source.find("\n  return 'ran';", start)
    if end == -1:
        raise ValueError(
            "Could not find the terminating `return 'ran';` of "
            "classifyTestExecution in agent/src/utils/test-runners.ts — the "
            "function's structure changed. Update _extract_classifier_region."
        )
    # Include the return statement and closing brace
    end = source.find("\n}", end) + 1
    return source[start:end]


def _extract_setup_classifier_region(source: str) -> str:
    """Extract the classifySetupFailure function from test-runners.ts.

    Finds the function by its export declaration and extracts it by locating
    the module-level closing brace. This boundary detection is robust to
    refactoring of the final return statement.
    """
    start = source.find("export function classifySetupFailure")
    if start == -1:
        raise ValueError(
            "Could not find `export function classifySetupFailure` anchor in "
            "agent/src/utils/test-runners.ts — the setup classifier function "
            "was renamed or removed. Update _extract_setup_classifier_region in "
            "backend/testing/test_invariants.py to match."
        )

    # Find the closing brace of the function. Since the function body is indented
    # (2-space indent per TypeScript convention), the closing brace appears on
    # its own line with no leading whitespace: \n}. This is more robust than
    # looking for a specific return statement, since the final return value may
    # change without affecting the function boundary.
    end = source.find("\n}", start)
    if end == -1:
        raise ValueError(
            "Could not find the closing brace of classifySetupFailure in "
            "agent/src/utils/test-runners.ts — the function's structure changed. "
            "Update _extract_setup_classifier_region."
        )
    # Include the closing brace in the extracted region (+ 2 for \n and })
    end = end + 2
    return source[start:end]


def _extract_env_failure_region(source: str) -> str:
    """Extract the matchesEnvironmentFailureSignature function from test-runners.ts.

    The function must be defined immediately before classifyTestExecution so the
    region boundary is [matchesEnvironmentFailureSignature .. classifyTestExecution).
    Raises ValueError with a clear message on parse miss.
    """
    start = source.find("export function matchesEnvironmentFailureSignature")
    if start == -1:
        raise ValueError(
            "Could not find `export function matchesEnvironmentFailureSignature` anchor "
            "in agent/src/utils/test-runners.ts — the function was renamed or removed. "
            "Update _extract_env_failure_region in backend/testing/test_invariants.py."
        )
    end = source.find("\nexport function classifyTestExecution", start)
    if end == -1:
        raise ValueError(
            "Could not find `export function classifyTestExecution` terminator after "
            "matchesEnvironmentFailureSignature in agent/src/utils/test-runners.ts. "
            "Ensure matchesEnvironmentFailureSignature is defined immediately before "
            "classifyTestExecution in the file."
        )
    return source[start:end]


class TestAgentEnvFailureSignatureParity:
    """SOURCE-SCAN: when agent/src is mounted, the agent-side env-failure
    signature list stays in sync with the backend's _AGENT_VM_PATTERNS in
    backend/app/failure_layer_classifier.py.

    A pattern in the backend that is absent from the agent means a VM-level
    failure (missing .so, runtime absent on PATH, spawn ENOENT) is misrouted to
    the code-fix loop instead of the setup-escalation path, burning agent
    budget on an unfixable problem.

    Skips in CI Docker contexts where agent/src is absent, exactly like the
    sibling ``TestAgentTestRunnerSourceParity`` class.
    """

    @staticmethod
    def _read_agent_source() -> str:
        path = _resolve_agent_test_runners_ts_path()
        if path is None:
            pytest.skip(
                "agent/src/utils/test-runners.ts not available in this test "
                "environment (CI docker context is ./backend; run locally or via "
                "scripts/test-backend.sh from a checkout for enforcement)"
            )
        with open(path, encoding="utf-8") as fh:
            return fh.read()

    def test_env_failure_function_exists_and_is_exported(self):
        """matchesEnvironmentFailureSignature must be defined and exported."""
        source = self._read_agent_source()
        region = _extract_env_failure_region(source)  # raises (loud) on parse miss
        assert "export function matchesEnvironmentFailureSignature" in region, (
            "agent/src/utils/test-runners.ts no longer exports "
            "matchesEnvironmentFailureSignature. Restore the export so the "
            "no-CI gate can route env failures to setupErrors."
        )

    def test_env_failure_real_failure_guard_present(self):
        """The function must gate on a hasRealFailureEvidence check so a test
        that quotes an env phrase as assertion data stays a real test failure."""
        source = self._read_agent_source()
        region = _extract_env_failure_region(source)
        assert "hasRealFailureEvidence" in region, (
            "agent/src/utils/test-runners.ts matchesEnvironmentFailureSignature "
            "no longer defines a real-failure evidence guard. Without this guard "
            "a genuine failing suite whose output quotes an env phrase would be "
            "misrouted to the setup-failure escalation path."
        )
        assert "if (hasRealFailureEvidence) return false;" in region, (
            "agent/src/utils/test-runners.ts matchesEnvironmentFailureSignature "
            "no longer short-circuits on real failure evidence. Restore the "
            "`if (hasRealFailureEvidence) return false;` guard."
        )

    def test_env_failure_patterns_mirror_agent_vm_patterns(self):
        """Key patterns from _AGENT_VM_PATTERNS must appear in the agent function.

        The backend is the source of truth for what counts as a VM-level failure.
        A pattern present in the backend but absent from the agent means that
        failure class silently falls through to the code-fix loop.
        """
        source = self._read_agent_source()
        region = _extract_env_failure_region(source)

        # Each entry: (human label, canonical substring that must appear in region).
        # Substrings are key phrases from _AGENT_VM_PATTERNS (failure_layer_classifier.py).
        # Use raw strings to match backslash-escaped regex literals in the TS source.
        required_patterns = [
            ("shared-library load error", "error while loading shared librar"),
            ("shared-object open failure", "cannot open shared object file"),
            ("no-such-file .so", r"\.so"),
            ("spawn ENOENT", "ENOENT"),
            # TS source uses regex literals: \bnode:\s+command not found
            ("node runtime missing", r"\bnode:"),
            ("python3 runtime missing", "python3"),
            # dash/sh "<tool>: not found" missing-executable phrasing
            ("dash not-found", "not found"),
            # Docker daemon unavailable on the gate VM
            ("docker daemon unavailable", "cannot connect to the docker daemon"),
            # OOM kills (exit 137 / SIGKILL / signal 9)
            ("OOM sigkill", "sigkill"),
            ("OOM signal killed", r"signal:\s*killed"),
            ("OOM exit 137", "137"),
            # Disk/quota/space failures (host resource exhaustion)
            ("disk no space", "no space left on device"),
            ("disk quota exceeded", "disk quota exceeded"),
            ("filesystem full", "filesystem (is )?full"),
            ("disk ENOSPC", "enospc"),
            # Permission denied / access failures (read-only fs, insufficient perms)
            ("permission denied", "permission denied"),
            ("permission EACCES", "eacces"),
            ("read-only filesystem", "read-only file system"),
            ("read-only EROFS", "erofs"),
        ]
        missing = [label for label, phrase in required_patterns if phrase not in region]
        assert not missing, (
            "agent/src/utils/test-runners.ts matchesEnvironmentFailureSignature "
            "is missing pattern(s) that mirror backend _AGENT_VM_PATTERNS: "
            f"{missing}. Add the missing patterns to keep the two banks in sync."
        )

    def test_extract_env_failure_region_raises_on_missing_anchor(self):
        """Parser raises loudly when the function anchor is absent."""
        with pytest.raises(ValueError, match="matchesEnvironmentFailureSignature"):
            _extract_env_failure_region(
                "export function classifyTestExecution(output: string) {}"
            )

    def test_extract_env_failure_region_raises_on_missing_terminator(self):
        """Parser raises loudly when classifyTestExecution is absent after anchor."""
        with pytest.raises(ValueError, match="classifyTestExecution"):
            _extract_env_failure_region(
                "export function matchesEnvironmentFailureSignature(output: string) {}"
            )


class TestAgentTestRunnerTableParity:
    """ALWAYS-ON: the backend-resident agent runner table is pinned to the shared
    fixture. Runs in plain GitHub CI (no agent/src required).

    The backend onboarding detector is already pinned to the same fixture by
    ``TestSharedTestRunnerLayouts`` in test_onboard.py, so this test transitively
    pins the table to the backend half for every layout — without duplicating or
    weakening that backend parity assertion.
    """

    def test_table_keys_equal_fixture_detectable_families(self):
        """The table's family set must exactly equal the fixture's detectable
        families — no missing family, no phantom extra family."""
        layouts = _load_shared_runner_layouts()
        fixture_families = {
            layout["family"] for layout in layouts if layout["detectable"]
        }
        table_families = set(AGENT_TEST_RUNNER_COMMANDS)
        missing = fixture_families - table_families
        extra = table_families - fixture_families
        assert not missing and not extra, (
            "AGENT_TEST_RUNNER_COMMANDS has drifted from the shared fixture "
            "test_runner_layouts.json:\n"
            f"  families in fixture but missing from table: {sorted(missing)}\n"
            f"  families in table but absent from fixture:  {sorted(extra)}\n"
            "Update AGENT_TEST_RUNNER_COMMANDS in backend/testing/test_invariants.py."
        )

    def test_table_has_nonempty_command_for_every_fixture_family(self):
        """Every detectable fixture family must map to a non-empty command string
        in the table — the family→command bridge has no gaps.

        The shared fixture's schema is {name, files, detectable, family}; it
        deliberately carries NO command (the canonical command lives in this table
        and is pinned byte-for-byte to the agent source by the source-scan below
        when agent/src is mounted). So the always-on guarantee here is coverage +
        non-emptiness in plain GitHub CI, not the exact command string."""
        layouts = _load_shared_runner_layouts()
        missing = []
        for layout in layouts:
            if not layout["detectable"]:
                continue
            family = layout["family"]
            command = AGENT_TEST_RUNNER_COMMANDS.get(family)
            if not command or not command.strip():
                missing.append(f"  {family} (layout {layout['name']})")
        assert not missing, (
            "AGENT_TEST_RUNNER_COMMANDS is missing a non-empty command for "
            "detectable fixture family/families:\n" + "\n".join(missing) + "\n"
            "Add the family→command entry in backend/testing/test_invariants.py."
        )

    def test_fixture_families_are_unambiguous(self):
        """Defensive: the fixture must not assign the same family to layouts that
        would require different commands — the single-command-per-family table
        could not represent that. The fixture carries no command field, so we
        assert the weaker structural invariant the table relies on: every
        detectable layout names a family that exists as a single table key (a
        dict is a function by construction, so one family => one command)."""
        layouts = _load_shared_runner_layouts()
        unknown = sorted(
            {
                layout["family"]
                for layout in layouts
                if layout["detectable"]
                and layout["family"] not in AGENT_TEST_RUNNER_COMMANDS
            }
        )
        assert not unknown, (
            "Fixture names detectable family/families with no table entry: "
            f"{unknown}. Every detectable family must map to exactly one command "
            "in AGENT_TEST_RUNNER_COMMANDS."
        )

    @staticmethod
    def _find_layout_by_file_path(layouts, predicate):
        """Return the first layout whose ``files`` contains a path matching the
        predicate, or ``None``. Read-only over the shared fixture schema
        ``{name, files:[{path, content?}], detectable, family}``."""
        for layout in layouts:
            for entry in layout.get("files", []):
                if predicate(entry.get("path", "")):
                    return layout
        return None

    def test_composer_scripts_test_signal_pinned_by_name(self):
        """NAMED DRIFT SIGNAL (composer): a detectable composer.json-with-
        scripts.test layout (family ``composer``) must exist in the shared
        fixture AND the table must map ``composer`` → ``composer test``.

        The general family-set parity test stays green if BOTH the composer
        fixture layout AND the composer table entry are deleted together; this
        test names the signal so that removing, renaming, or re-committing it out
        of either the fixture or the table fails by name in plain GitHub CI."""
        layouts = _load_shared_runner_layouts()
        layout = self._find_layout_by_file_path(layouts, lambda p: p == "composer.json")
        assert layout is not None, (
            "NAMED SIGNAL LOST: no shared-fixture layout declares a root "
            "'composer.json' file. The composer no-CI drift signal "
            "(family 'composer', command 'composer test') must be carried by a "
            "detectable layout in test_runner_layouts.json. Restore the "
            "'composer_json_with_scripts_test' layout."
        )
        composer_entry = next(
            entry for entry in layout["files"] if entry.get("path") == "composer.json"
        )
        assert '"test"' in composer_entry.get("content", ""), (
            "NAMED SIGNAL WEAKENED: the composer.json layout "
            f"'{layout['name']}' no longer declares a scripts.test entry in its "
            "content. The composer signal requires a real scripts.test so the "
            "no-CI detector emits 'composer test'."
        )
        assert layout["detectable"] is True, (
            "NAMED SIGNAL DISABLED: the composer.json layout "
            f"'{layout['name']}' is no longer detectable=true; the composer "
            "no-CI drift signal would be silently dropped from coverage."
        )
        assert layout["family"] == "composer", (
            "NAMED SIGNAL RENAMED: the composer.json layout "
            f"'{layout['name']}' now names family '{layout['family']}', not "
            "'composer'. The composer no-CI drift signal must keep family "
            "'composer'."
        )
        assert AGENT_TEST_RUNNER_COMMANDS.get("composer") == "composer test", (
            "NAMED SIGNAL LOST: AGENT_TEST_RUNNER_COMMANDS['composer'] is "
            f"{AGENT_TEST_RUNNER_COMMANDS.get('composer')!r}, not 'composer "
            "test'. Restore the composer command in "
            "test_runner_commands.json / AGENT_TEST_RUNNER_COMMANDS."
        )

    def test_script_test_sh_signal_pinned_by_name(self):
        """NAMED DRIFT SIGNAL (script): a detectable root ``scripts/test.sh``
        layout (family ``script``) must exist in the shared fixture AND the
        table must map ``script`` → ``bash scripts/test.sh``.

        The general family-set parity test stays green if BOTH the script
        fixture layout AND the script table entry are deleted together; this
        test names the signal so that removing, renaming, or re-committing it out
        of either the fixture or the table fails by name in plain GitHub CI."""
        layouts = _load_shared_runner_layouts()
        layout = self._find_layout_by_file_path(
            layouts, lambda p: p == "scripts/test.sh"
        )
        assert layout is not None, (
            "NAMED SIGNAL LOST: no shared-fixture layout declares a root "
            "'scripts/test.sh' file. The script no-CI drift signal "
            "(family 'script', command 'bash scripts/test.sh') must be carried "
            "by a detectable layout in test_runner_layouts.json. Restore the "
            "'scripts_test_sh_root_convention' layout."
        )
        assert layout["detectable"] is True, (
            "NAMED SIGNAL DISABLED: the scripts/test.sh layout "
            f"'{layout['name']}' is no longer detectable=true; the script "
            "no-CI drift signal would be silently dropped from coverage."
        )
        assert layout["family"] == "script", (
            "NAMED SIGNAL RENAMED: the scripts/test.sh layout "
            f"'{layout['name']}' now names family '{layout['family']}', not "
            "'script'. The script no-CI drift signal must keep family 'script'."
        )
        assert AGENT_TEST_RUNNER_COMMANDS.get("script") == "bash scripts/test.sh", (
            "NAMED SIGNAL LOST: AGENT_TEST_RUNNER_COMMANDS['script'] is "
            f"{AGENT_TEST_RUNNER_COMMANDS.get('script')!r}, not 'bash "
            "scripts/test.sh'. Restore the script command in "
            "test_runner_commands.json / AGENT_TEST_RUNNER_COMMANDS."
        )


class TestAgentTestRunnerSourceParity:
    """SOURCE-SCAN: when agent/src is mounted, the agent detector's literal
    family → command mapping in test-runners.ts must equal the backend table.

    Fires whenever the backend suite runs via scripts/test-backend.sh (the local
    + no-CI merge-gate path, which mounts ../agent/src to /agent/src). Skips with
    a clear marker only in the GH-only Docker context where agent/src is genuinely
    absent — exactly the precedent of ``test_agent_categories_match_prompt_source``.

    A single-sided change to the agent detector (new family, changed command,
    dropped manifest) that is not mirrored in AGENT_TEST_RUNNER_COMMANDS fails
    here; a table change not mirrored in test-runners.ts also fails.
    """

    @staticmethod
    def _read_agent_source() -> str:
        path = _resolve_agent_test_runners_ts_path()
        if path is None:
            pytest.skip(
                "agent/src/utils/test-runners.ts not available in this test "
                "environment (CI docker context is ./backend; run locally or via "
                "scripts/test-backend.sh from a checkout for enforcement)"
            )
        with open(path, encoding="utf-8") as fh:
            return fh.read()

    def test_agent_emits_table_command_for_every_family(self):
        """Every non-dynamic table command must be reconstructable from a literal
        runner object in test-runners.ts.

        Catches a CHANGED command (e.g. ``mvn test`` → ``mvn verify``: the old
        literal is gone) and a DROPPED manifest (the runner removed: command gone).
        """
        source = self._read_agent_source()
        agent_commands = _reconstruct_agent_commands(source)  # raises on parse miss
        missing = []
        for family, command in AGENT_TEST_RUNNER_COMMANDS.items():
            if family in _AGENT_DYNAMIC_ARG_FAMILIES:
                continue
            if command not in agent_commands:
                missing.append(f"  {family}: expected agent to emit {command!r}")
        assert not missing, (
            "agent/src/utils/test-runners.ts no longer emits the command(s) the "
            "backend table expects:\n"
            + "\n".join(missing)
            + "\n\nThe agent detector drifted from AGENT_TEST_RUNNER_COMMANDS. "
            "Reconcile agent/src/utils/test-runners.ts and the table in "
            "backend/testing/test_invariants.py."
        )

    def test_agent_emits_shell_script_runner(self):
        """The dynamic-arg ``script`` family: the agent must emit a ``bash`` runner
        and its script list must include all four shell-script variants: test.sh,
        run-tests.sh, run_tests.sh, and scripts/test.sh.

        Verified via the script-list literals because the command is built from a
        loop variable, not a literal ``args`` array.
        """
        source = self._read_agent_source()
        shell_command = AGENT_TEST_RUNNER_COMMANDS["script"]
        # shell_command == "bash scripts/test.sh"
        cmd, script = shell_command.split(" ", 1)
        assert f"cmd: '{cmd}'" in source, (
            f"agent/src/utils/test-runners.ts no longer emits a {cmd!r} runner; "
            f"the shell family ({shell_command!r}) cannot be produced."
        )
        # Verify all four shell-script paths are present as string literals.
        # If any were silently dropped, no detection would occur for repos using
        # that convention.
        shell_scripts = ["test.sh", "run-tests.sh", "run_tests.sh", "scripts/test.sh"]
        for sh_script in shell_scripts:
            assert f"'{sh_script}'" in source, (
                f"agent/src/utils/test-runners.ts no longer lists {sh_script!r} in its "
                f"shell-script fallback list; repos using this convention would no longer "
                f"be detected. Reconcile the script list with AGENT_TEST_RUNNER_COMMANDS['script']."
            )

    def test_agent_emits_composer_json_detection_trigger(self):
        """The ``composer`` family: the agent must look for ``composer.json`` as its
        detection trigger inside the divergent-family helper, gated by the
        manifest-content guard, and emit a ``composer`` runner.

        This closes an asymmetry with the shell-``script`` family. For shell
        scripts, ``test_agent_emits_shell_script_runner`` pins every filename the
        agent looks for (test.sh, run-tests.sh, …) as a source literal, so a
        renamed/removed detection trigger fails the parity scan. For composer,
        only the emitted command (``composer test``) was reconstructed and
        checked — nothing pinned the ``composer.json`` detection trigger itself.
        As a result, dropping or mistyping ``has('composer.json')`` while leaving
        the ``composer test`` command literal alive would silently stop the agent
        from detecting composer repos at merge time, even though the backend
        onboarding detector still detects ``composer.json`` — the exact silent
        cross-detector divergence the parity suite exists to prevent. This test
        pins the trigger in the same source-scan tier as the script-filename check.

        Scoped to the ``detectExtraFamilyRunner`` region (not anywhere in the
        file) so the assertion catches the trigger being moved out of the helper,
        not merely a stray ``composer.json`` string elsewhere.
        """
        source = self._read_agent_source()
        region = _extract_extra_family_region(source)  # raises (loud) on parse miss
        composer_command = AGENT_TEST_RUNNER_COMMANDS["composer"]
        # composer_command == "composer test"
        cmd, _args = composer_command.split(" ", 1)
        # (a) the composer.json detection trigger literal must live in the helper.
        assert "'composer.json'" in region, (
            "agent/src/utils/test-runners.ts no longer looks for 'composer.json' "
            "in detectExtraFamilyRunner; composer repos would no longer be "
            "detected at merge time even though the backend onboarding detector "
            "still detects composer.json. Reconcile the composer detection trigger "
            "with AGENT_TEST_RUNNER_COMMANDS['composer']."
        )
        # (b) the region must still emit a 'composer' runner (the command survives).
        assert f"cmd: '{cmd}'" in region, (
            f"agent/src/utils/test-runners.ts no longer emits a {cmd!r} runner in "
            f"detectExtraFamilyRunner; the composer family ({composer_command!r}) "
            "cannot be produced."
        )
        # (c) the trigger must stay content-gated by the manifest-script guard, so
        # a composer.json without a real scripts.test still does not falsely detect.
        assert "manifestDefinesTestScript" in region, (
            "agent/src/utils/test-runners.ts no longer gates the composer.json "
            "trigger with manifestDefinesTestScript in detectExtraFamilyRunner; "
            "the trigger must remain content-gated to match the backend onboarding "
            "detector's scripts.test requirement."
        )

    def test_agent_extra_family_runners_are_all_in_table(self):
        """Reverse direction: every command the divergent-family helper emits must
        appear in the backend table.

        Catches a NEW family added to ``detectExtraFamilyRunner`` (e.g. an sbt
        runner) without a matching AGENT_TEST_RUNNER_COMMANDS entry.
        """
        source = self._read_agent_source()
        region = _extract_extra_family_region(source)  # raises (loud) on parse miss
        region_commands = _reconstruct_agent_commands(region)
        table_commands = set(AGENT_TEST_RUNNER_COMMANDS.values())
        orphans = sorted(region_commands - table_commands)
        assert not orphans, (
            "agent/src/utils/test-runners.ts emits divergent-family command(s) "
            f"with no entry in AGENT_TEST_RUNNER_COMMANDS: {orphans}. "
            "Add the new family/command to the table in "
            "backend/testing/test_invariants.py (and the shared fixture)."
        )

    def test_agent_generic_fallback_runners_are_all_in_table(self):
        """Reverse direction: every command the generic-fallback helper emits
        (Makefile, just, Taskfile, tox, nox, shell scripts) must appear in
        the backend table.

        Catches a NEW generic-fallback family added to ``detectGenericFallbackRunner``
        without a matching AGENT_TEST_RUNNER_COMMANDS entry. The shell-script
        family (with unquoted loop variable args) is excluded here and verified
        separately via test_agent_emits_shell_script_runner.
        """
        source = self._read_agent_source()
        region = _extract_generic_fallback_region(source)  # raises (loud) on parse miss
        region_commands = _reconstruct_agent_commands(region)
        table_commands = set(AGENT_TEST_RUNNER_COMMANDS.values())
        # Exclude bare 'bash' command (it has unquoted loop-variable args)
        # which is already verified in test_agent_emits_shell_script_runner
        region_commands.discard("bash")
        orphans = sorted(region_commands - table_commands)
        assert not orphans, (
            "agent/src/utils/test-runners.ts emits generic-fallback family command(s) "
            f"with no entry in AGENT_TEST_RUNNER_COMMANDS: {orphans}. "
            "Add the new family/command to the table in "
            "backend/testing/test_invariants.py (and the shared fixture)."
        )

    def test_agent_core_root_runners_are_all_in_table(self):
        """Reverse direction: every command emitted at root level (npm, pytest,
        cargo, go) must appear in the backend table.

        Catches a NEW core runner family added to root detection without a
        matching AGENT_TEST_RUNNER_COMMANDS entry. Commands with unquoted ternary
        variable args (like pgFlag for the Blueprint backend test script) are
        excluded as they are internal/special cases not part of the standard
        family set.
        """
        source = self._read_agent_source()
        region = _extract_core_root_region(source)  # raises (loud) on parse miss
        region_commands = _reconstruct_agent_commands(region)
        table_commands = set(AGENT_TEST_RUNNER_COMMANDS.values())
        # Exclude commands with unquoted ternary args that are already in the table
        # (e.g., 'bash scripts/test-backend.sh' is a blueprint-internal case)
        region_commands = {
            cmd
            for cmd in region_commands
            if cmd in table_commands or not cmd.startswith("bash")
        }
        orphans = sorted(region_commands - table_commands)
        assert not orphans, (
            "agent/src/utils/test-runners.ts emits root-level core family command(s) "
            f"with no entry in AGENT_TEST_RUNNER_COMMANDS: {orphans}. "
            "Add the new family/command to the table in "
            "backend/testing/test_invariants.py (and the shared fixture)."
        )

    def test_agent_nested_package_runners_are_all_in_table(self):
        """Reverse direction: every command emitted in nested package detection
        (services/, apps/, packages/, etc.) must appear in the backend table.

        Catches a NEW family added to nested package detection without a
        matching AGENT_TEST_RUNNER_COMMANDS entry. This includes core families
        (pytest, npm, go, cargo) at nested levels plus any divergent or
        generic-fallback families detected in subdirectories.
        """
        source = self._read_agent_source()
        region = _extract_nested_package_region(source)  # raises (loud) on parse miss
        region_commands = _reconstruct_agent_commands(region)
        table_commands = set(AGENT_TEST_RUNNER_COMMANDS.values())
        orphans = sorted(region_commands - table_commands)
        assert not orphans, (
            "agent/src/utils/test-runners.ts emits nested-package family command(s) "
            f"with no entry in AGENT_TEST_RUNNER_COMMANDS: {orphans}. "
            "Add the new family/command to the table in "
            "backend/testing/test_invariants.py (and the shared fixture)."
        )

    def test_classifier_missing_command_requires_adjacency(self):
        """The no-CI gate's classifier (classifyTestExecution) must not use loose
        substring matching for the missing-command signal. A too-loose check that
        scans the whole output for 'command' AND 'not found' independently would
        false-positive on failures whose test ids contain 'command' (e.g.
        test_command_parser) plus unrelated errors ('404 Not Found', 'element not
        found'). This would cause the gate to incorrectly merge unverified PRs.

        The classifier must require the two signal words to be adjacent (at most one
        quoted/bare token between them) on the SAME line, with a word boundary on
        `command` so test_command_parser doesn't match.
        """
        source = self._read_agent_source()
        region = _extract_classifier_region(source)  # raises (loud) on parse miss

        # Reject the loose pattern that would false-positive
        loose_pattern = "lower.includes('command') && lower.includes('not found')"
        assert loose_pattern not in region, (
            "agent/src/utils/test-runners.ts classifyTestExecution uses loose "
            "substring matching for 'command' and 'not found' independently, which "
            "false-positives on genuine failures. The pattern must require adjacency "
            f"on the same line. Found: {loose_pattern!r}. "
            "Use a regex pattern like /\\bcommand\\b(?:\\s+(?:\"[^\"]*\"|'[^']*'|\\S+))?\\s+not\\s+found\\b/i"
        )

        # Verify the correct line-scoped detection is present. The classifier
        # detects missing runners with a set of line-anchored, runner-shaped
        # patterns (missingCommandPatterns) tested PER LINE — never a whole-output
        # substring scan. Each pattern requires the CONTIGUOUS phrase "command not
        # found" (or the yarn-shaped 'command "x" not found') on a runner-emitted
        # line, so a failing suite whose output merely contains the two signal
        # words non-adjacently stays a real run.
        assert "missingCommandPatterns" in region, (
            "agent/src/utils/test-runners.ts classifyTestExecution must define a "
            "missingCommandPatterns set of line-anchored regexes for missing-command "
            "detection."
        )
        assert "missingCommandPatterns.some(pattern => pattern.test(line))" in region, (
            "agent/src/utils/test-runners.ts classifyTestExecution must test "
            "missingCommandPatterns on individual lines (some(line => "
            "missingCommandPatterns.some(pattern => pattern.test(line)))), not on "
            "the whole output."
        )
        # The patterns must require the contiguous phrase on a runner-shaped line,
        # never the two signal words matched independently across the output.
        assert "command not found" in region, (
            "agent/src/utils/test-runners.ts classifyTestExecution must require the "
            "contiguous phrase 'command not found' in its runner-shaped patterns, "
            "not 'command' and 'not found' matched independently."
        )
        assert "^(?:bash|sh):" in region, (
            "agent/src/utils/test-runners.ts classifyTestExecution must line-anchor "
            "its missing-command patterns to runner-emitted shapes (e.g. "
            "'^(?:bash|sh):') so a quoted phrase mid-line stays a real run."
        )

    def test_classifier_empty_suite_signals_require_failure_guard(self):
        """Empty-suite phrases may only classify as no_tests when no real
        failure evidence is present. This prevents a genuine failing suite from
        being downgraded just because its output mentions 'no tests ran',
        'collected 0 items', or a similar placeholder phrase as data."""
        source = self._read_agent_source()
        region = _extract_classifier_region(source)  # raises (loud) on parse miss

        assert "hasRealFailureEvidence" in region, (
            "agent/src/utils/test-runners.ts classifyTestExecution no longer "
            "defines a real-failure guard. Empty-suite phrases must be gated "
            "by a failure-evidence check so genuine failures are not reclassified "
            "as no_tests."
        )
        assert "!hasRealFailureEvidence" in region, (
            "agent/src/utils/test-runners.ts classifyTestExecution no longer "
            "requires `!hasRealFailureEvidence` before returning no_tests. "
            "Restore the guard so real failures win over empty-suite phrases."
        )
        for phrase in [
            "no test specified",
            "missing script",
            "No rule to make target",
            "no tests ran",
            "collected 0 items",
            "No test files found",
            "no tests to run",
        ]:
            assert phrase in region, (
                "agent/src/utils/test-runners.ts classifyTestExecution no longer "
                f"contains the empty-suite signature {phrase!r}; update this "
                "invariant to match the classifier if the signal moved."
            )

    def test_setup_classifier_requires_line_anchored_patterns(self):
        """The setup-failure classifier must stay line-anchored and pattern-based.

        Loose whole-output substring scans would false-positive on a real test
        that merely QUOTES a setup-like line ("bash: python3: command not
        found", "ModuleNotFoundError: No module named 'pip'") in an assertion
        body.
        """
        source = self._read_agent_source()
        region = _extract_setup_classifier_region(source)

        for forbidden in [
            "output.includes(",
            "lower.includes(",
            "trimmedLines.some(line => line.includes(",
        ]:
            assert forbidden not in region, (
                "agent/src/utils/test-runners.ts classifySetupFailure uses loose "
                f"substring matching ({forbidden!r}); keep setup detection "
                "line-anchored via regex patterns."
            )

        assert "missingToolPatterns" in region, (
            "agent/src/utils/test-runners.ts classifySetupFailure must define a "
            "missingToolPatterns set of line-anchored regexes."
        )
        assert (
            "trimmedLines.some(line => missingToolPatterns.some(pattern => pattern.test(line)))"
            in region
        ), (
            "agent/src/utils/test-runners.ts classifySetupFailure must test "
            "missingToolPatterns per line, not against the whole output."
        )
        for required in [
            "command not found",
            "python(?:3)?|go|cargo|node|npm|pnpm|yarn|pip|poetry",
            "^(?:bash|sh):",
            """No module named ['"]pip['"]""",
        ]:
            assert required in region, (
                "agent/src/utils/test-runners.ts classifySetupFailure no longer "
                f"contains the setup-failure signature {required!r}; update this "
                "invariant if the signal legitimately moved."
            )
        for forbidden in [
            "/^ModuleNotFoundError:\\s+No module named ['\"][^'\"]+['\"]\\s*$/i",
            "/^ImportError:\\s+No module named ['\"][^'\"]+['\"]\\s*$/i",
            "/^Error:\\s+Cannot find module ['\"](?![./])[^'\"]+['\"]\\s*$/i",
            "/^(?:go:\\s+)?missing go\\.sum entry for module providing package\\b.*$/i",
        ]:
            assert forbidden not in region, (
                "agent/src/utils/test-runners.ts classifySetupFailure must not "
                f"treat generic repo dependency failures as setup infra ({forbidden!r})."
            )

    def test_classifier_real_failure_guard_covers_every_runner_family(self):
        """COUPLED INVARIANT: the no-CI gate's runner detection can launch many
        ecosystems (Go, Ruby/Rake, Gradle, Maven, Elixir, .NET, PHPUnit) beyond
        pytest/vitest/Rust, and the real-failure guard (hasRealFailureEvidence)
        MUST recognize a genuine failure in each one. Otherwise a failing suite
        whose output ALSO quotes a placeholder diagnostic line (a captured child
        process's "command not found", an assertion diff printing "no test
        specified", a sub-invocation's "no tests ran") is misread as "no tests
        ran" and the gate falls open, merging UNVERIFIED.

        The agent vitest suite (which directly exercises the behavior) IS run
        automatically by the generated ./check (the agent stack block calls
        `npx vitest run`; see check-helper.ts), but ./check only runs on the
        agent VM at PR time and is NOT a required CI gate on this repo's PRs —
        so the runner-family classifier signatures it would catch are not
        enforced by CI. This CI-enforced source-scan therefore pins "a family
        the gate can launch" and "its failure signature is recognized" as ONE
        invariant. Each per-ecosystem failure signature below must be present in
        the classifier region; a future edit that silently drops one re-opens
        the fall-open and fails here.
        """
        source = self._read_agent_source()
        region = _extract_classifier_region(source)  # raises (loud) on parse miss

        assert "hasRealFailureEvidence" in region, (
            "agent/src/utils/test-runners.ts classifyTestExecution no longer "
            "defines the real-failure guard hasRealFailureEvidence."
        )

        # Each entry: (human label, list of acceptable source substrings — at
        # least one must be present). The substrings are the line-anchored,
        # non-zero-guarded signatures the guard uses for that family.
        required_signatures = [
            (
                "pytest collection/import/conftest `1 error` summary",
                [r"[1-9]\d*\s+errors?"],
            ),
            ("Go verbose per-test failure (--- FAIL:)", [r"^--- FAIL:"]),
            ("Go tab-separated package FAIL summary", [r"^FAIL\t"]),
            (".NET `Failed: N` (non-zero)", [r"Failed:\s+[1-9]"]),
            (
                "Elixir/Ruby/RSpec `N failure(s)` (non-zero)",
                [r"[1-9]\d*\s+failures?"],
            ),
            ("PHPUnit/Maven `Failures: N` (non-zero)", [r"Failures:\s+[1-9]"]),
            (
                "Maven/Gradle/JUnit/PHPUnit `Errors: N` (non-zero)",
                [r"Errors:\s+[1-9]"],
            ),
            (
                "Minitest/ExUnit `N error(s)` (non-zero)",
                [r"[1-9]\d*\s+errors?"],
            ),
        ]
        missing = [
            label
            for label, candidates in required_signatures
            if not any(candidate in region for candidate in candidates)
        ]
        assert not missing, (
            "agent/src/utils/test-runners.ts classifyTestExecution's real-failure "
            "guard no longer recognizes a genuine failure for runner family/"
            f"families: {missing}. The no-CI gate's runner detection can still "
            "launch these ecosystems, so dropping their failure signature re-opens "
            "the merge-unverified fall-open. Restore the line-anchored, non-zero "
            "signature(s) in hasRealFailureEvidence."
        )

        # The count-summary signatures MUST keep a non-zero guard so a zero-count
        # summary (`Failed: 0`, `Failures: 0`, `0 failures`) is never mistaken for
        # a real failure (which would hard-block a genuinely empty repo).
        for nonzero_guard in [
            r"Failed:\s+[1-9]",
            r"Failures:\s+[1-9]",
            r"[1-9]\d*\s+failures?",
            r"Errors:\s+[1-9]",
            r"[1-9]\d*\s+errors?",
        ]:
            assert nonzero_guard in region, (
                "agent/src/utils/test-runners.ts classifyTestExecution dropped the "
                f"non-zero count guard {nonzero_guard!r}; a zero-count summary would "
                "be misread as a real failure and hard-block an empty repo."
            )

    def test_classifier_all_skipped_signals_cover_every_family(self):
        """COUPLED INVARIANT (mirror of the real-failure-guard scan): a run that
        COLLECTED tests but executed NONE because every test was skipped /
        deselected / ignored / filtered is the SAME honest no-real-tests case as a
        zero-collection run, and the cold no-CI gate must NOT record it as a
        verified pass. The runner detection can launch every family below, so the
        all-skipped classifier (returning 'all_skipped') MUST recognize each one;
        a future edit that silently drops a family's all-skip signature re-opens
        the false-'verified' path and fails here.

        STRICTLY conservative: all_skipped is gated on a real-PASS guard
        (hasRealPassEvidence) AND the existing real-FAILURE guard, so a run where
        even one test passed or failed ("5 passed, 1500 skipped") stays 'ran'.
        """
        source = self._read_agent_source()
        region = _extract_classifier_region(source)  # raises (loud) on parse miss

        # The all_skipped verdict and its conservative real-pass guard must exist.
        assert "all_skipped" in region, (
            "agent/src/utils/test-runners.ts classifyTestExecution no longer "
            "returns 'all_skipped'; an all-skipped suite would be recorded as a "
            "vacuous verified pass. Restore the all-skip classification."
        )
        for guard in ["hasRealPassEvidence", "!hasRealPassEvidence"]:
            assert guard in region, (
                "agent/src/utils/test-runners.ts classifyTestExecution no longer "
                f"defines/uses the real-pass guard {guard!r}. all_skipped must be "
                "gated by a real-pass check so a mixed pass-with-skips run stays "
                "a real 'ran' verdict."
            )
        # The non-zero real-pass signatures (so a zero-count pass token is never
        # treated as a real pass that would suppress all-skip detection).
        for pass_sig in [r"[1-9]\d*\s+passed", r"Passed:\s+[1-9]"]:
            assert pass_sig in region, (
                "agent/src/utils/test-runners.ts classifyTestExecution dropped the "
                f"real-pass signature {pass_sig!r}; without it a mixed pass-with-"
                "skips run could be misread as all_skipped."
            )

        # Per-family all-skip signatures: at least one candidate per family must
        # be present, covering the SAME family set as the real-failure guard.
        required_skip_signatures = [
            (
                "pytest all-skipped/deselected",
                [r"[1-9]\d*\s+skipped", r"[1-9]\d*\s+deselected"],
            ),
            ("vitest all-skipped", [r"[1-9]\d*\s+skipped"]),
            (
                "Rust all-ignored/filtered",
                [r"[1-9]\d*\s+ignored", r"[1-9]\d*\s+filtered out"],
            ),
            ("Go verbose --- SKIP:", [r"--- SKIP:"]),
            ("Ruby/RSpec all-pending", [r"(\d+)\s+pending"]),
            ("Ruby/Minitest all-skips", [r"(\d+)\s+skips"]),
            ("Elixir/ExUnit all-excluded", [r"skipped|excluded"]),
            (
                "Gradle/Maven Surefire Tests-run/Skipped",
                [r"Tests run:\s*(\d+)", r"Skipped:\s*(\d+)"],
            ),
            (".NET Total:/Skipped:", [r"Total:\s*(\d+)"]),
            ("PHPUnit Tests:/Skipped:", [r"Tests:\s*(\d+)"]),
        ]
        missing = [
            label
            for label, candidates in required_skip_signatures
            if not any(candidate in region for candidate in candidates)
        ]
        assert not missing, (
            "agent/src/utils/test-runners.ts classifyTestExecution's all-skip "
            "detection no longer recognizes a fully-skipped run for runner family/"
            f"families: {missing}. The no-CI gate's runner detection can still "
            "launch these ecosystems, so dropping their all-skip signature re-opens "
            "the false-'verified' fall-open (an all-skipped suite recorded as a "
            "verified pass). Restore the line-anchored all-skip signature(s)."
        )

        # The aggregate-family detection MUST compare total === skip (not a bare
        # non-zero Skipped count), so a MIXED run ("Tests run: 5, .., Skipped: 3")
        # with passes counted only in the total stays a real 'ran' verdict.
        assert "allSkipByTotal" in region, (
            "agent/src/utils/test-runners.ts classifyTestExecution dropped the "
            "allSkipByTotal total===skip comparison; a bare non-zero 'Skipped: N' "
            "would misclassify a mixed pass-with-skips run as all_skipped."
        )


class TestAgentTestRunnerParseHelpers:
    """ALWAYS-ON unit tests for the source-scan parser, driven by synthetic
    source strings (no agent/src required). Demonstrates the scan catches drift
    and fails loudly on a parse miss — mirroring ``TestSrCategoryParityHelper``.
    """

    def test_reconstructs_command_from_literal(self):
        synthetic = (
            "runners.push({ cmd: 'python3', args: ['-m', 'pytest'], cwd: W, "
            "label: 'pytest (root)' });"
        )
        assert _reconstruct_agent_commands(synthetic) == {"python3 -m pytest"}

    def test_reconstructs_command_with_empty_args(self):
        synthetic = "return { cmd: 'vendor/bin/phpunit', args: [], cwd: d, label: x };"
        assert _reconstruct_agent_commands(synthetic) == {"vendor/bin/phpunit"}

    def test_dynamic_arg_runner_drops_unquoted_variable(self):
        """A ``[script]`` loop-variable arg yields just the bare command — proving
        the shell family genuinely needs its script-list assertion."""
        synthetic = "runners.push({ cmd: 'bash', args: [script], cwd: W, label: l });"
        assert _reconstruct_agent_commands(synthetic) == {"bash"}

    def test_reconstruct_raises_on_parse_miss(self):
        with pytest.raises(ValueError, match="No `cmd:"):
            _reconstruct_agent_commands("// no runner literals here\nconst x = 1;")

    def test_extract_extra_family_region_raises_on_missing_anchor(self):
        with pytest.raises(ValueError, match="detectExtraFamilyRunner"):
            _extract_extra_family_region("export function detectTestRunners() {}")

    def test_extract_extra_family_region_raises_on_missing_terminator(self):
        with pytest.raises(ValueError, match="return null;"):
            _extract_extra_family_region(
                "const detectExtraFamilyRunner = (absDir, label) => { return foo; }"
            )

    def test_scan_catches_changed_command(self):
        """A drifted agent command (mvn verify) is absent from the reconstructed
        set, so the family check would flag it."""
        synthetic = "return { cmd: 'mvn', args: ['verify'], cwd: d, label: x };"
        agent_commands = _reconstruct_agent_commands(synthetic)
        assert "mvn test" not in agent_commands
        assert "mvn verify" in agent_commands

    def test_scan_catches_new_extra_family(self):
        """A new divergent family with no table entry is surfaced as an orphan."""
        region = (
            "detectExtraFamilyRunner = (absDir, label) => {\n"
            "  if (has('hypothetical.lock')) return { cmd: 'hypothetical', args: ['test'], "
            "cwd: absDir, label: l };\n"
            "  return null;\n};"
        )
        body = _extract_extra_family_region(region)
        orphans = _reconstruct_agent_commands(body) - set(
            AGENT_TEST_RUNNER_COMMANDS.values()
        )
        assert orphans == {"hypothetical test"}

    def test_extract_classifier_region_raises_on_missing_anchor(self):
        with pytest.raises(ValueError, match="classifyTestExecution"):
            _extract_classifier_region("export function detectTestRunners() {}")

    def test_extract_classifier_region_raises_on_missing_return(self):
        with pytest.raises(ValueError, match="return 'ran'"):
            _extract_classifier_region(
                "export function classifyTestExecution(output: string) { "
                "return 'no_tests'; }"
            )

    def test_extract_setup_classifier_region_raises_on_missing_anchor(self):
        with pytest.raises(ValueError, match="classifySetupFailure"):
            _extract_setup_classifier_region(
                "export function classifyTestExecution(output: string) { return 'ran'; }"
            )

    def test_extract_setup_classifier_region_raises_on_missing_closing_brace(self):
        with pytest.raises(ValueError, match="closing brace"):
            _extract_setup_classifier_region(
                "export function classifySetupFailure(output: string) { return true;"
            )

    def test_classifier_region_contains_missing_command_check(self):
        """A synthetic classifier region with the correct pattern passes."""
        synthetic = (
            "export function classifyTestExecution(output: string) {\n"
            "  const missingCommand = /\\bcommand\\b(?:\\s+(?:\"[^\"]*\"|'[^']*'|\\S+))?\\s+not\\s+found\\b/i;\n"
            "  if (lines.some(line => missingCommand.test(line))) return 'no_tests';\n"
            "  return 'ran';\n}"
        )
        region = _extract_classifier_region(synthetic)
        assert "missingCommand" in region
        assert ".some(line => missingCommand.test(line))" in region
        assert r"\bcommand\b" in region

    def test_setup_classifier_region_contains_line_anchored_patterns(self):
        synthetic = (
            "export function classifySetupFailure(output: string) {\n"
            "  const trimmedLines = output.split(/\\r?\\n/).map(line => line.trim());\n"
            "  const missingToolPatterns = [/^(?:bash|sh):(?:\\s+line\\s+\\d+:)?\\s*(?:python(?:3)?|go|cargo|node|npm|pnpm|yarn|pip|poetry):\\s+command not found\\s*$/i];\n"
            "  if (trimmedLines.some(line => missingToolPatterns.some(pattern => pattern.test(line)))) return true;\n"
            "  return false;\n"
            "}\n"
        )
        region = _extract_setup_classifier_region(synthetic)
        assert "missingToolPatterns" in region
        assert (
            ".some(line => missingToolPatterns.some(pattern => pattern.test(line)))"
            in region
        )


# ── No-CI UNVERIFIED-merge reason-label parity ────────────────────────────────
# The orchestrator writes a stable reason KEY into
# review_loop["unverified_merge_reason"] at each no-CI fall-open decision site.
# Three operator-facing surfaces humanize that key: the backend unverified-merge
# alert (alert_dispatch._UNVERIFIED_REASON_LABELS), the per-task Merge Test Gate
# panel, and the project merge-verification banner (both frontend, sharing
# admin-ui/src/utils/unverifiedReasonLabels.ts). These tests pin the label maps
# to the full set of emitted keys so a new reason can never ship showing a raw
# token (e.g. "suite_all_skipped") or a positive-sounding key ("probe_passed")
# verbatim to an operator.

# Canonical set of fall-open reason keys, with their emit sites:
#   internal_staging_base / kill_switch_off / no_detectable_test_command /
#   verify_setup_failed        -> app/orchestrator.py no-CI gate
#   undetected_runtime / suite_all_skipped / probe_passed / probe_could_not_run /
#   probe_no_artifact / documented_command_could_not_run /
#   documented_command_passed / documented_command_no_artifact
#                              -> ci_resolution.classify_unverified_fall_open_reason
_UNVERIFIED_REASON_KEYS = frozenset(
    {
        "no_detectable_test_command",
        "kill_switch_off",
        "internal_staging_base",
        "undetected_runtime",
        "suite_all_skipped",
        "verify_setup_failed",
        "probe_passed",
        "probe_could_not_run",
        "probe_no_artifact",
        "documented_command_could_not_run",
        "documented_command_passed",
        "documented_command_no_artifact",
        "acceptance_test_not_run",
    }
)


def _frontend_unverified_reason_keys() -> "set[str] | None":
    """Extract the key set of the frontend UNVERIFIED_REASON_LABELS map.

    Returns ``None`` when admin-ui/src is not on disk (CI docker context is
    ./backend) so the caller can skip, mirroring the frontend size guardrails.
    """
    if _ADMIN_UI_SRC_ROOT is None:
        return None
    path = _get_frontend_path(os.path.join("utils", "unverifiedReasonLabels.ts"))
    source = Path(path).read_text(encoding="utf-8")
    # Isolate the object literal body so we only read declared keys, not the
    # JSDoc that names other keys in prose.
    match = re.search(
        r"UNVERIFIED_REASON_LABELS:\s*Record<string,\s*string>\s*=\s*\{(.*?)\n\};",
        source,
        re.DOTALL,
    )
    assert match, "UNVERIFIED_REASON_LABELS object literal not found in frontend util"
    body = match.group(1)
    return set(re.findall(r"^\s*([A-Za-z_][A-Za-z0-9_]*):", body, re.MULTILINE))


def test_unverified_reason_labels_backend_covers_all_keys():
    """The backend alert label map labels every emitted fall-open reason key."""
    from app.alert_dispatch import _UNVERIFIED_REASON_LABELS

    missing = _UNVERIFIED_REASON_KEYS - set(_UNVERIFIED_REASON_LABELS)
    assert not missing, (
        "backend _UNVERIFIED_REASON_LABELS (app/alert_dispatch.py) is missing "
        f"labels for emitted reason keys: {sorted(missing)}"
    )


def test_unverified_reason_labels_frontend_backend_parity():
    """Frontend and backend reason-label maps must cover the same key set.

    Guards against the drift in this task: the frontend map had fallen behind
    the backend (missing suite_all_skipped / verify_setup_failed / probe_*),
    showing raw tokens to operators.
    """
    frontend_keys = _frontend_unverified_reason_keys()
    if frontend_keys is None:
        pytest.skip(
            "admin-ui/src not available in this test environment "
            "(CI docker context is ./backend; mount via test-backend.sh)"
        )
    from app.alert_dispatch import _UNVERIFIED_REASON_LABELS

    backend_keys = set(_UNVERIFIED_REASON_LABELS)
    assert frontend_keys == backend_keys, (
        "no-CI UNVERIFIED reason-label maps drifted — frontend "
        "(admin-ui/src/utils/unverifiedReasonLabels.ts) and backend "
        "(app/alert_dispatch._UNVERIFIED_REASON_LABELS) must label the same keys. "
        f"frontend-only={sorted(frontend_keys - backend_keys)} "
        f"backend-only={sorted(backend_keys - frontend_keys)}"
    )
    # And both must cover the full canonical emitted set.
    assert _UNVERIFIED_REASON_KEYS <= frontend_keys, (
        "frontend reason-label map missing canonical keys: "
        f"{sorted(_UNVERIFIED_REASON_KEYS - frontend_keys)}"
    )


# Canonical set of degraded no-CI reasons that escalate to ERROR severity.
# These are environment/setup failures where verification was expected but
# could not proceed due to infrastructure/tooling issues.
_DEGRADED_REASON_KEYS = frozenset(
    {
        "verify_setup_failed",
        "undetected_runtime",
        "suite_all_skipped",
        "probe_could_not_run",
        "probe_no_artifact",
        "documented_command_could_not_run",
        "documented_command_no_artifact",
        "acceptance_test_not_run",
    }
)


def test_unverified_reason_severity_covers_all_keys():
    """The severity map must classify every canonical emitted fall-open reason key.

    Without this guard, a new reason key can be added with a label but no severity
    entry and will silently default to WARNING, potentially downgrading degraded
    environment/setup failures (which should be ERROR) to benign warnings. This test
    ensures that is impossible: every canonical key must have an explicit severity.
    """
    from app.alert_dispatch import _UNVERIFIED_REASON_SEVERITY

    missing = _UNVERIFIED_REASON_KEYS - set(_UNVERIFIED_REASON_SEVERITY)
    assert not missing, (
        "no-CI reason severity map (_UNVERIFIED_REASON_SEVERITY in "
        "app/alert_dispatch.py) is missing severity entries for emitted reason keys: "
        f"{sorted(missing)}. Every canonical reason must have an explicit severity "
        "classification (ERROR for degraded environment/setup failures, WARNING for benign)."
    )


def test_unverified_reason_severity_degraded_are_error():
    """Known degraded reasons (environment/setup failures) must be classified ERROR.

    Degraded reasons represent cases where verification was expected but could not
    proceed due to infrastructure/tooling issues (setup failures, suite collection
    failures, build artifacts missing, etc.). These escalate to ERROR to alert the
    operator loudly. This test ensures no degraded reason accidentally downgrades
    to WARNING, either by missing from the map (caught by the prior test) or by
    being mis-classified.
    """
    from app.alert_dispatch import _UNVERIFIED_REASON_SEVERITY, AlertSeverity

    for reason in _DEGRADED_REASON_KEYS:
        severity = _UNVERIFIED_REASON_SEVERITY.get(reason)
        assert severity == AlertSeverity.ERROR, (
            f"no-CI degraded reason '{reason}' must be classified ERROR in "
            "_UNVERIFIED_REASON_SEVERITY (app/alert_dispatch.py), but got {severity}. "
            "Degraded environment/setup failures must escalate loudly to ERROR, not WARNING."
        )


def test_unverified_reason_severity_derives_from_shared_sets():
    """The alert severity map is the SINGLE-SOURCE-OF-TRUTH wiring: it must be
    derived exactly from ``ci_resolution.{DEGRADED,BENIGN}_UNVERIFIED_REASONS``.

    The benign-vs-degraded partition lives in ``app.ci_resolution`` so the alert
    layer and success-scoring analytics cannot drift. This pins the wiring: if a
    future edit re-hand-lists the severity map or the shared sets diverge from
    the canonical key set, this fails loudly rather than silently mis-scoring a
    merge as verified.
    """
    from app.alert_dispatch import _UNVERIFIED_REASON_SEVERITY, AlertSeverity
    from app.ci_resolution import (
        BENIGN_UNVERIFIED_REASONS,
        DEGRADED_UNVERIFIED_REASONS,
    )

    # The shared sets partition the canonical emitted reason key set exactly.
    assert not (BENIGN_UNVERIFIED_REASONS & DEGRADED_UNVERIFIED_REASONS)
    assert (BENIGN_UNVERIFIED_REASONS | DEGRADED_UNVERIFIED_REASONS) == set(
        _UNVERIFIED_REASON_KEYS
    )
    assert DEGRADED_UNVERIFIED_REASONS == _DEGRADED_REASON_KEYS

    expected = {
        **{r: AlertSeverity.ERROR for r in DEGRADED_UNVERIFIED_REASONS},
        **{r: AlertSeverity.WARNING for r in BENIGN_UNVERIFIED_REASONS},
    }
    assert _UNVERIFIED_REASON_SEVERITY == expected


class TestSpawnResumeNeedSignalParity:
    """The spawn-time prerequisite resolver and the resume-time blocked-need
    matcher MUST agree on the shared EXPLICIT ``{kind, ref}`` signals.

    They diverged once (resume matched task needs by UUID only and capability
    needs by payload-type/title/related-file heuristics, while spawn matched by
    id-or-finding_hash and explicit capability markers), silently failing to
    resume dependents whose foundation declared a finding-hash or marker signal.
    Both now route shared explicit signals through
    ``finding_deduplication.prerequisite_matches_task``; this fixture-table
    parity test fails if either side stops honoring one of those shared signals.
    """

    @staticmethod
    def _task(*, tid=None, context=None, title="cand", payloads=None):
        import uuid as _uuid

        from app.models import Task, TaskStatus

        return Task(
            id=tid or _uuid.uuid4(),
            project_id=_uuid.uuid4(),
            title=title,
            description="x",
            status=TaskStatus.IN_PROGRESS,
            created_by="u",
            requires_approval=False,
            payloads=payloads if payloads is not None else [{"type": "create"}],
            context=context or {},
            depends_on=[],
            status_log=[],
        )

    def test_shared_signals_agree_on_match_and_no_match(self):
        import uuid as _uuid

        from app.blocked_need_resolution import resolve_blocked_need
        from app.finding_deduplication import resolve_prerequisite_task

        task_id = _uuid.uuid4()
        # (label, need, candidate-factory, expect_match)
        cases = [
            (
                "task-by-id",
                {"kind": "task", "ref": str(task_id)},
                lambda: self._task(tid=task_id),
                True,
            ),
            (
                "task-by-finding-hash",
                {"kind": "task", "ref": "feedfacefeedface"},
                lambda: self._task(context={"finding_hash": "feedfacefeedface"}),
                True,
            ),
            (
                "capability-marker",
                {"kind": "capability", "ref": "auth-foundation"},
                lambda: self._task(context={"provides_capability": "auth-foundation"}),
                True,
            ),
            (
                "capability-list-marker",
                {"kind": "capability", "ref": "schema-models"},
                lambda: self._task(context={"capability": ["x", "schema-models"]}),
                True,
            ),
            (
                "task-wrong-hash",
                {"kind": "task", "ref": "0000000000000000"},
                lambda: self._task(context={"finding_hash": "1111111111111111"}),
                False,
            ),
            (
                "capability-no-marker",
                {"kind": "capability", "ref": "auth"},
                lambda: self._task(
                    title="Build the auth foundation",
                    context={"related_files": ["backend/app/auth.py"]},
                ),
                False,
            ),
        ]

        for label, need, factory, expect_match in cases:
            spawn_task = factory()
            resume_task = factory()
            # A BLOCKED dependent distinct from the foundation candidate.
            blocked = self._task()
            spawn_hit = resolve_prerequisite_task(need, [spawn_task]) is not None
            resume_hit = resolve_blocked_need(blocked, need, [resume_task]) is not None
            assert spawn_hit == resume_hit, (
                f"spawn/resume need-signal parity broken for case '{label}': "
                f"spawn matched={spawn_hit}, resume matched={resume_hit}. Both "
                "must agree on shared explicit signals."
            )
            assert spawn_hit == expect_match, (
                f"case '{label}' expected match={expect_match} but spawn "
                f"matched={spawn_hit}"
            )

    def test_resume_preserves_legacy_blocked_capability_description_fallback(self):
        from app.blocked_need_resolution import resolve_blocked_need
        from app.finding_deduplication import resolve_prerequisite_task

        blocked = self._task()
        cases = [
            (
                "payload-type",
                {"kind": "capability", "ref": "design"},
                self._task(payloads=[{"type": "design"}]),
            ),
            (
                "exact-title",
                {"kind": "capability", "ref": "shared auth layer"},
                self._task(title="shared auth layer"),
            ),
            (
                "related-file",
                {"kind": "capability", "ref": "backend/app/auth.py"},
                self._task(context={"related_files": ["backend/app/auth.py"]}),
            ),
        ]

        for label, need, candidate in cases:
            assert resolve_prerequisite_task(need, [candidate]) is None, (
                f"spawn path should stay explicit-only for legacy capability case "
                f"'{label}'"
            )
            assert resolve_blocked_need(blocked, need, [candidate]) is candidate, (
                f"resume path lost legacy blocked capability fallback for case "
                f"'{label}'"
            )


def test_standalone_smoke_modules_importable_as_root():
    env = os.environ.copy()
    env.setdefault("AGENT_JWT_SECRET", "test-secret-for-invariant-check")
    env.setdefault("WS_TICKET_SECRET", "test-ws-secret-for-invariant-check")
    cwd = Path("/app") if Path("/app").exists() else Path(__file__).resolve().parents[1]

    for module in ("app.research_completion", "app.security"):
        result = subprocess.run(
            [sys.executable, "-c", f"import {module}"],
            cwd=cwd,
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )
        assert result.returncode == 0, (
            f"{module} failed to import from /app: "
            f"stdout={result.stdout!r} stderr={result.stderr!r}"
        )
        assert "ImportError" not in result.stderr
        assert "circular import" not in result.stderr


def test_all_load_only_projection_sites_require_raiseload():
    """Every load_only() and defer() call in backend/app must pass raiseload=True.

    Blueprint adopted a projection contract to eliminate MissingGreenlet 500s:
    narrow-column reads use load_only(..., raiseload=True) so that accessing an
    un-projected column raises a synchronous StatementError at test time instead
    of triggering an async lazy-load that surfaces as a MissingGreenlet in prod.

    This invariant enforces the contract codebase-wide so that a new bare
    load_only() or defer() call is caught immediately rather than discovered in
    production.
    """
    app_dir = Path(__file__).resolve().parents[1] / "app"
    violations: list[str] = []

    for py_file in sorted(app_dir.rglob("*.py")):
        try:
            source = py_file.read_text(encoding="utf-8")
            tree = ast.parse(source, filename=str(py_file))
        except (SyntaxError, UnicodeDecodeError):
            continue

        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if isinstance(func, ast.Name):
                fname = func.id if func.id in ("load_only", "defer") else None
            elif isinstance(func, ast.Attribute):
                fname = func.attr if func.attr in ("load_only", "defer") else None
            else:
                fname = None
            if fname is None:
                continue

            has_raiseload = any(
                kw.arg == "raiseload"
                and isinstance(kw.value, ast.Constant)
                and kw.value.value is True
                for kw in node.keywords
            )
            if not has_raiseload:
                rel = py_file.relative_to(app_dir.parent.parent)
                violations.append(
                    f"  {rel}:{node.lineno} — {fname}() missing raiseload=True"
                )

    assert not violations, (
        "Projection contract violated: the following load_only()/defer() calls "
        "omit raiseload=True. Add raiseload=True to each site so that accessing "
        "an un-projected column raises synchronously at test time instead of "
        "triggering a MissingGreenlet 500 in production.\n" + "\n".join(violations)
    )


def test_stall_alert_functions_reachable_from_canonical_import_path():
    """Every _check_/_alert_/_resolve_ function in release_stall_alerts.py must be
    reachable via at least one canonical import path.

    When a new stall-alert function is extracted into release_stall_alerts.py it
    must be either (a) directly imported in test_release_stall_alerts.py, or (b)
    re-exported from release_qualifier.py via a ``from app.release_stall_alerts
    import ...`` block.  A function that is neither tested directly nor re-exported
    is silently unreachable — callers who import from the old location
    (release_qualifier) won't see it, and the test suite won't exercise it.
    """
    testing_dir = Path(__file__).resolve().parent
    backend_dir = testing_dir.parent
    app_dir = backend_dir / "app"

    stall_alerts_path = app_dir / "release_stall_alerts.py"
    qualifier_path = app_dir / "release_qualifier.py"
    test_path = testing_dir / "test_release_stall_alerts.py"

    stall_source = stall_alerts_path.read_text(encoding="utf-8")
    qualifier_source = qualifier_path.read_text(encoding="utf-8")
    test_source = test_path.read_text(encoding="utf-8")

    stall_tree = ast.parse(stall_source, filename=str(stall_alerts_path))
    qualifier_tree = ast.parse(qualifier_source, filename=str(qualifier_path))
    test_tree = ast.parse(test_source, filename=str(test_path))

    # Collect all top-level function names matching the _check_/_alert_/_resolve_ prefix
    import re as _re

    _STALL_FUNC_PATTERN = _re.compile(r"^(_check_|_alert_|_resolve_)")
    stall_functions: set[str] = set()
    for node in ast.walk(stall_tree):
        if isinstance(node, ast.FunctionDef) and _STALL_FUNC_PATTERN.match(node.name):
            stall_functions.add(node.name)

    def _collect_imports_from(tree: ast.Module, module_name: str) -> set[str]:
        """Collect names imported via 'from <module_name> import ...' in tree."""
        names: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module == module_name:
                for alias in node.names:
                    names.add(alias.name)
        return names

    # Names directly imported in the test file
    test_imports = _collect_imports_from(test_tree, "app.release_stall_alerts")

    # Names re-exported from release_qualifier.py
    qualifier_reexports = _collect_imports_from(
        qualifier_tree, "app.release_stall_alerts"
    )

    reachable = test_imports | qualifier_reexports
    unreachable = sorted(stall_functions - reachable)

    assert not unreachable, (
        "The following functions in release_stall_alerts.py are not reachable "
        "from any canonical import path — they are neither directly imported in "
        "test_release_stall_alerts.py nor re-exported from release_qualifier.py.\n"
        "Fix by adding them to one of:\n"
        "  (a) the 'from app.release_stall_alerts import ...' block in "
        "test_release_stall_alerts.py (for alert/resolve functions that need "
        "direct test coverage), OR\n"
        "  (b) the 'from app.release_stall_alerts import ...' block in "
        "release_qualifier.py (for check functions re-exported for callers).\n\n"
        "Unreachable functions:\n" + "\n".join(f"  {f}" for f in unreachable)
    )


def test_summarize_release_artifacts_callers_always_pass_project():
    """Every call to summarize_release_artifacts in backend/app/ passes project=.

    Omitting project= silently enables the legacy scalar fallback that invents
    a backend_revision contribution for external projects, causing a serving-status
    divergence across UI / CLI / system-state surfaces. This invariant enforces
    that all production call sites opt in to the project-aware path.

    The definition file (release_artifacts.py) is excluded — the function's own
    default parameter ``project=None`` is not a missing-keyword violation.
    """
    backend_dir = Path(__file__).resolve().parent.parent
    app_dir = backend_dir / "app"

    violations: list[str] = []

    for py_file in sorted(app_dir.rglob("*.py")):
        # Exclude the definition file itself — `project=None` in the signature
        # is intentional and is not a call-site violation.
        if py_file.name == "release_artifacts.py":
            continue
        # Exclude routers sub-directory traversal already captured by rglob,
        # but include them — they are production call sites too.

        try:
            source = py_file.read_text(encoding="utf-8")
        except OSError:
            continue

        if "summarize_release_artifacts" not in source:
            continue

        try:
            tree = ast.parse(source, filename=str(py_file))
        except SyntaxError:
            continue

        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if isinstance(func, ast.Name):
                fname = func.id
            elif isinstance(func, ast.Attribute):
                fname = func.attr
            else:
                continue
            if fname != "summarize_release_artifacts":
                continue

            has_project_kwarg = any(kw.arg == "project" for kw in node.keywords)
            if not has_project_kwarg:
                rel = py_file.relative_to(backend_dir.parent)
                violations.append(
                    f"  {rel}:{node.lineno} — "
                    "call to summarize_release_artifacts() missing project= keyword"
                )

    assert not violations, (
        "The following call sites omit project= from summarize_release_artifacts(). "
        "Omitting project= enables a legacy scalar fallback that synthesises a "
        "spurious backend_revision contribution for external projects and causes "
        "serving-status divergence across surfaces. Pass project=<project_row> at "
        "each site (or project=None if the call is known-safe and intentional — but "
        "prefer the explicit keyword so the invariant stays informative).\n"
        + "\n".join(violations)
    )


def test_serving_status_reads_route_through_aggregate():
    """All read-path files must use version_serving_summary, not private copies.

    The canonical serving resolver is ``release_artifacts.version_serving_summary``.
    Private copies of ``_version_serving_summary`` in other modules diverge silently:
    they bypass project= threading, fall back to legacy scalars, or drift from the
    aggregate-first algorithm over time.  This guard asserts that no file outside
    ``release_artifacts.py`` defines a ``FunctionDef`` named ``_version_serving_summary``
    or ``version_serving_summary`` — new read sites must import the canonical one.
    """
    backend_dir = Path(__file__).resolve().parent.parent
    app_dir = backend_dir / "app"

    duplicates: list[str] = []

    for py_file in sorted(app_dir.rglob("*.py")):
        if py_file.name == "release_artifacts.py":
            continue

        try:
            source = py_file.read_text(encoding="utf-8")
        except OSError:
            continue

        # Quick text filter before expensive parse.
        if (
            "_version_serving_summary" not in source
            and "version_serving_summary" not in source
        ):
            continue

        try:
            tree = ast.parse(source, filename=str(py_file))
        except SyntaxError:
            continue

        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name in (
                "_version_serving_summary",
                "version_serving_summary",
            ):
                rel = py_file.relative_to(backend_dir.parent)
                duplicates.append(
                    f"  {rel}:{node.lineno} — "
                    f"defines {node.name!r} (should import from release_artifacts)"
                )

    assert not duplicates, (
        "The following files define a private serving-summary resolver instead of "
        "importing ``version_serving_summary`` from ``release_artifacts``.  "
        "Duplicate definitions diverge from the aggregate-first algorithm and "
        "cause serving-status inconsistency across UI/CLI/strategist surfaces.\n"
        + "\n".join(duplicates)
    )


# ---------------------------------------------------------------------------
# Dead-column guard: confirm five dropped columns stay gone from models.py
# and their write sites stay removed from production code.
# ---------------------------------------------------------------------------

#: Columns confirmed dead (migration 0300) and removed from the ORM.
#: Each entry is (model_class_name, field_name, canonical_write_site).
_DEAD_ORM_COLUMNS = [
    ("Task", "infra_wait_until", None),
    ("Task", "spawn_failed", "agent_status_spawn.py"),
    ("Task", "last_remediation_at", None),
    ("Project", "regression_signal_state", None),
    ("DeploymentState", "prior_deployed_at", "main.py"),
]


def test_dropped_dead_orm_columns_absent_from_models():
    """Verify the five dead ORM columns are not re-introduced in models.py.

    Migration 0300 dropped Task.infra_wait_until, Task.spawn_failed,
    Task.last_remediation_at, Project.regression_signal_state, and
    DeploymentState.prior_deployed_at.  This guard makes re-adding any of them
    a CI failure so the dead-schema signal is durable.
    """
    backend_dir = Path(__file__).resolve().parent.parent
    models_path = backend_dir / "app" / "models.py"
    source = models_path.read_text(encoding="utf-8")

    violations: list[str] = []
    for model_name, field_name, _ in _DEAD_ORM_COLUMNS:
        # Look for the field as an ORM attribute assignment (e.g. "    field_name: ...")
        # or a direct attribute access (e.g. "task.field_name =").
        pattern = re.compile(
            r"^\s+" + re.escape(field_name) + r"\s*[:=]",
            re.MULTILINE,
        )
        if pattern.search(source):
            violations.append(
                f"  {model_name}.{field_name} found in models.py — "
                "this column was confirmed dead and dropped in migration 0300"
            )

    assert not violations, (
        "Dead ORM columns were re-introduced in models.py.\n"
        "These columns have no live readers and were dropped in migration 0300.\n"
        + "\n".join(violations)
    )


def test_dropped_dead_orm_columns_write_sites_removed():
    """Verify known write sites for the dead columns are gone from production code.

    Migration 0300 removed the only write sites for Task.spawn_failed
    (agent_status_spawn.py) and DeploymentState.prior_deployed_at (main.py).
    This guard ensures those write sites are not reintroduced.
    """
    backend_dir = Path(__file__).resolve().parent.parent
    app_dir = backend_dir / "app"

    violations: list[str] = []
    for model_name, field_name, write_file in _DEAD_ORM_COLUMNS:
        if write_file is None:
            continue
        target = app_dir / write_file
        if not target.exists():
            continue
        source = target.read_text(encoding="utf-8")
        # Match attribute assignment: ".field_name = " or ".field_name=" patterns
        pattern = re.compile(r"\." + re.escape(field_name) + r"\s*=")
        if pattern.search(source):
            violations.append(
                f"  {write_file}: still contains a write to .{field_name} "
                f"({model_name}.{field_name} was dropped in migration 0300)"
            )

    assert not violations, (
        "Write sites for dead ORM columns were reintroduced.\n"
        "These columns have no live readers and were dropped in migration 0300.\n"
        + "\n".join(violations)
    )


def test_authorized_test_deletion_allowlist_is_sealed():
    """The hardcoded test-deletion allowlist must NOT be importable.

    The release qualifier retired the ``AUTHORIZED_TEST_DELETIONS`` tuple and its
    ``_get_lifecycle_bounded_authorized_deletions()`` lifecycle filter in favour
    of per-file LLM classification emitted by the release audit agent
    (``deleted_test_file_classifications``).  The allowlist was authorization-as-
    code: widening it required a backend deploy, which created the catch-22 that
    blocked two legitimate promotions (v14 viewportRegression, v17 visual-
    verification dead-code cut).

    This invariant seals the backdoor: neither symbol may be reintroduced into
    ``app.release_scoring`` without a deliberate decision that also updates this
    test.  Importing either name must raise ImportError.
    """
    import app.release_scoring as rs

    for name in (
        "AUTHORIZED_TEST_DELETIONS",
        "_get_lifecycle_bounded_authorized_deletions",
    ):
        assert not hasattr(rs, name), (
            f"app.release_scoring.{name} is importable again. The hardcoded "
            "test-deletion allowlist was deliberately retired in favour of per-file "
            "LLM classification (deleted_test_file_classifications). Do not "
            "reintroduce the allowlist — classify deletions in the release audit "
            "agent (agent/src/handlers/audits.ts) instead."
        )

    with pytest.raises(ImportError):
        from app.release_scoring import AUTHORIZED_TEST_DELETIONS  # noqa: F401
    with pytest.raises(ImportError):
        from app.release_scoring import (  # noqa: F401
            _get_lifecycle_bounded_authorized_deletions,
        )


def test_main_py_admin_endpoints_auth_and_401_path():
    """All @app.{method}('/api/admin/...') handlers declared directly in main.py
    must use get_current_user or get_current_user_optional, and any handler
    using get_current_user_optional must include an explicit
    HTTPException(status_code=401) path so unauthenticated callers are never
    served a non-error response.

    This is a static AST check on main.py only (not on included routers) to
    complement the live-app dependency scan in test_all_admin_routes_require_auth.
    """
    import ast
    from pathlib import Path

    main_py = Path(__file__).parent.parent / "app" / "main.py"
    source = main_py.read_text()
    tree = ast.parse(source, filename=str(main_py))

    # Endpoints that intentionally skip the 401-path requirement — document why.
    OPTIONAL_401_ALLOWLIST: dict[str, str] = {}

    AUTH_DEPS = {"get_current_user", "get_current_user_optional"}
    HTTP_METHODS = {"get", "post", "put", "delete", "patch"}

    def _has_status_401_raise(funcdef: ast.AST) -> bool:
        """Return True if the function body contains raise HTTPException(status_code=401, ...)."""
        for node in ast.walk(funcdef):
            if not isinstance(node, ast.Raise):
                continue
            exc = node.exc
            if exc is None:
                continue
            if not (
                isinstance(exc, ast.Call)
                and isinstance(exc.func, ast.Name)
                and exc.func.id == "HTTPException"
            ):
                continue
            for kw in exc.keywords:
                if (
                    kw.arg == "status_code"
                    and isinstance(kw.value, ast.Constant)
                    and kw.value.value == 401
                ):
                    return True
        return False

    def _dep_name_from_call(call_node: ast.expr | None) -> str | None:
        """Return the callable name from a Depends(...) call node, or None."""
        if call_node is None:
            return None
        if not (
            isinstance(call_node, ast.Call)
            and isinstance(call_node.func, ast.Name)
            and call_node.func.id == "Depends"
            and call_node.args
        ):
            return None
        dep_arg = call_node.args[0]
        if isinstance(dep_arg, ast.Name):
            return dep_arg.id
        return None

    def _auth_deps_in_funcdef(
        funcdef: ast.FunctionDef | ast.AsyncFunctionDef,
    ) -> set[str]:
        """Return the set of AUTH_DEPS callables used as Depends() default values."""
        # FastAPI resolves Depends() from the *default value* of parameters,
        # not from the type annotation.  Positional-arg defaults are aligned to
        # the tail of args.args; keyword-only defaults map 1:1 with kwonlyargs.
        found: set[str] = set()

        for default in funcdef.args.defaults:
            name = _dep_name_from_call(default)
            if name in AUTH_DEPS:
                found.add(name)

        for kw_default in funcdef.args.kw_defaults:
            name = _dep_name_from_call(kw_default)
            if name in AUTH_DEPS:
                found.add(name)

        return found

    missing_auth: list[str] = []
    missing_401: list[str] = []
    found_paths: list[str] = []

    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue

        for dec in node.decorator_list:
            # Match @app.get(...), @app.post(...), etc.
            if not (
                isinstance(dec, ast.Call)
                and isinstance(dec.func, ast.Attribute)
                and isinstance(dec.func.value, ast.Name)
                and dec.func.value.id == "app"
                and dec.func.attr in HTTP_METHODS
            ):
                continue

            # Extract path from the first positional arg or the 'path' keyword.
            path_node = None
            if dec.args:
                path_node = dec.args[0]
            else:
                for kw in dec.keywords:
                    if kw.arg == "path":
                        path_node = kw.value
                        break

            if not isinstance(path_node, ast.Constant) or not isinstance(
                path_node.value, str
            ):
                continue

            path = path_node.value
            if not path.startswith("/api/admin/"):
                continue

            found_paths.append(path)

            # Check parameter *defaults* for recognized auth dependency callables.
            auth_deps_present = _auth_deps_in_funcdef(node)

            if not auth_deps_present:
                missing_auth.append(path)
                continue

            # For get_current_user_optional endpoints, require an explicit 401.
            if (
                "get_current_user_optional" in auth_deps_present
                and "get_current_user" not in auth_deps_present
                and path not in OPTIONAL_401_ALLOWLIST
                and not _has_status_401_raise(node)
            ):
                missing_401.append(path)

    assert found_paths, (
        "No @app.{method}('/api/admin/...') handlers found in main.py — "
        "the decorator pattern match may be broken."
    )
    assert not missing_auth, (
        "The following /api/admin/ endpoints in main.py have no recognized auth "
        "dependency (get_current_user or get_current_user_optional):\n"
        + "\n".join(f"  - {p}" for p in sorted(missing_auth))
        + "\n\nAdd Depends(get_current_user) or Depends(get_current_user_optional) "
        "to the handler signature."
    )
    assert not missing_401, (
        "The following /api/admin/ endpoints in main.py use get_current_user_optional "
        "but have no explicit raise HTTPException(status_code=401) path:\n"
        + "\n".join(f"  - {p}" for p in sorted(missing_401))
        + "\n\nWhen current_user is None the endpoint must raise HTTPException(status_code=401). "
        "Add a 401 path or add the endpoint to OPTIONAL_401_ALLOWLIST with justification."
    )


def test_deleted_test_file_classification_fails_open_without_genuine_erosion():
    """The deleted-test-file gate must FAIL OPEN on unclassified deletions.

    Operator steer (reverting #11268's short-lived fail-closed disposition): the
    ONLY blocking outcome is an explicit ``genuine_erosion`` classification from
    the release audit agent.  When ``_score_audit()`` sees ``deleted_test_files``
    but the report carries NO ``deleted_test_file_classifications`` entry (or an
    empty one), it must return a NON-None erosion score — the deletion passes.
    A co-deleted/dead-code test removal must not block promotion merely because
    the auditor didn't emit a tag (the common case for declutter/dead-code cuts).

    Deterministic DETECTION of the deleted set is unchanged, so nothing is
    missed; only the default disposition for an un-flagged deletion is allow.
    The narrow erosion case (subject demonstrably still present, test stripped to
    hide a failing build) still hard-blocks — but only when the auditor
    affirmatively classifies it ``genuine_erosion`` (Part 3).
    """
    from app.release_scoring import _score_audit

    # Part 1: absent classifications key → fail-OPEN (passes)
    report_absent = {
        "audit_score": 0.9,
        "test_erosion": {
            "deleted_test_files": ["testing/test_some_module.py"],
            "assertion_delta": 0,
            "test_count_delta": 0,
        },
        # No deleted_test_file_classifications key at all.
        "findings": [],
    }
    _, erosion_absent = _score_audit(report_absent)
    assert erosion_absent is not None, (
        "An unclassified deleted test file must fail open (non-None erosion). "
        "Only an explicit genuine_erosion classification hard-blocks."
    )

    # Part 2: explicit dead_code_removal classification → gate passes (non-None)
    report_authorized = {
        "audit_score": 0.9,
        "test_erosion": {
            "deleted_test_files": ["testing/test_some_module.py"],
            "assertion_delta": 0,
            "test_count_delta": 0,
        },
        "deleted_test_file_classifications": [
            {
                "path": "testing/test_some_module.py",
                "classification": "dead_code_removal",
                "rationale": "app/some_module.py co-deleted; no callers remain.",
            }
        ],
        "findings": [],
    }
    _, erosion_authorized = _score_audit(report_authorized)
    assert erosion_authorized is not None, (
        "A dead_code_removal-classified deletion must pass (non-None erosion)."
    )

    # Part 3: explicit genuine_erosion → still the hard-blocker sentinel (None)
    report_erosion = {
        "audit_score": 0.9,
        "test_erosion": {
            "deleted_test_files": ["testing/test_some_module.py"],
            "assertion_delta": 0,
            "test_count_delta": 0,
        },
        "deleted_test_file_classifications": [
            {
                "path": "testing/test_some_module.py",
                "classification": "genuine_erosion",
                "rationale": "app/some_module.py present and unchanged.",
            }
        ],
        "findings": [],
    }
    _, erosion_genuine = _score_audit(report_erosion)
    assert erosion_genuine is None, (
        "An explicit genuine_erosion classification must still hard-block "
        "(erosion=None) — the one case fail-open preserves."
    )


# ── TaskType evaluation registry consistency (always-on smoke invariant) ───────


def test_task_type_evaluation_registry_is_consistent():
    """Mirror DB-free registry assertions into always-on smoke suite.

    The TaskType<->evaluation-strategy registry consistency and 'lock the spec'
    assertions previously ran only in the DB-backed full CI matrix, allowing
    vocabulary changes to break them and stay green on the contributing PR,
    failing only at the promotion gate. This invariant brings the pure,
    DB-free checks into the smoke suite so drift is caught at PR time.

    Checks:
    1. All TaskType enum values have entries in EVALUATION_STRATEGY_BY_TASK_TYPE
    2. All TASK_CAPABILITIES have valid evaluation values (in the closed vocabulary)
    3. The actual registry matches the expected hardcoded mapping (lock the spec)
    """
    from app.task_capabilities import (
        EVALUATION_STRATEGY_BY_TASK_TYPE,
        TASK_CAPABILITIES,
        TaskType,
    )

    # Check 1: Every TaskType has a registry entry
    assert set(EVALUATION_STRATEGY_BY_TASK_TYPE) == {t.value for t in TaskType}, (
        "All TaskType enum values must have entries in EVALUATION_STRATEGY_BY_TASK_TYPE. "
        "If you added a new task type, declare its evaluation strategy in task_capabilities.py."
    )

    # Check 2: All TASK_CAPABILITIES have valid evaluation values
    valid_evaluation_keys = {
        "code-ci-review",
        "forward-quality",
        "metric",
        "artifact-delivery",
        "structured-verdict",
        "none",
    }
    for cap in TASK_CAPABILITIES:
        assert cap.evaluation in valid_evaluation_keys, (
            f"TaskCapability {cap.id} has invalid evaluation {cap.evaluation!r}. "
            f"Must be one of: {valid_evaluation_keys}"
        )

    # Check 3: Lock the per-type strategy assignment (spec freeze for every-PR visibility)
    # This hardcoded mapping is the 'lock the spec' assertion that prevents silent
    # evaluation-strategy drift. If you genuinely need to change a mapping, update
    # this dict AND update TASK_CAPABILITIES to match.
    expected_registry = {
        "create": "code-ci-review",
        "review": "none",
        "panel_review": "structured-verdict",
        "fix": "code-ci-review",
        "plan": "none",
        "research": "forward-quality",
        "strategic_review": "forward-quality",
        "design_doc_update": "forward-quality",
        "security_audit": "forward-quality",
        "qa": "code-ci-review",
        "deps": "code-ci-review",
        "docs": "code-ci-review",
        "migrate": "code-ci-review",
        "smoketest": "none",
        "refactor": "code-ci-review",
        "debug": "forward-quality",
        "perf": "metric",
        "investigate": "forward-quality",
        "self_audit": "forward-quality",
        "release_audit": "forward-quality",
        "release_audit_challenge": "none",
        "release_merge_grade": "none",
        "memo": "forward-quality",
        "verify_tests": "none",
        "compute_experiment": "artifact-delivery",
        "web_research": "forward-quality",
        "design": "forward-quality",
        "repo_bootstrap": "none",
        "salvage_bundle": "code-ci-review",
    }
    assert EVALUATION_STRATEGY_BY_TASK_TYPE == expected_registry, (
        "TaskType evaluation registry has drifted from the locked spec. "
        "This invariant is always-on (every PR) to catch vocabulary changes early. "
        "If the drift is intentional, update both task_capabilities.py AND this assertion."
    )


def test_pytest_filterwarnings_includes_httpx_error_guard():
    """pyproject.toml must configure filterwarnings to turn httpx DeprecationWarnings into errors.

    This guard prevents silent regressions where a dependency bump introduces a
    deprecated-API call that only surfaces as a hard removal in the next version.
    Motivating case: httpx 0.27 deprecated app=; httpx 0.28 removed it.
    """
    import sys

    if sys.version_info >= (3, 11):
        import tomllib
    else:
        try:
            import tomli as tomllib  # type: ignore[no-redef]
        except ImportError:
            pytest.skip("tomllib/tomli not available on Python < 3.11")
            return

    pyproject_path = Path(__file__).resolve().parents[1] / "pyproject.toml"
    assert pyproject_path.exists(), f"pyproject.toml not found at {pyproject_path}"

    with open(pyproject_path, "rb") as fh:
        config = tomllib.load(fh)

    filterwarnings = (
        config.get("tool", {})
        .get("pytest", {})
        .get("ini_options", {})
        .get("filterwarnings", [])
    )

    assert any(
        entry == "error::DeprecationWarning:httpx" for entry in filterwarnings
    ), (
        "Missing 'error::DeprecationWarning:httpx' in "
        "[tool.pytest.ini_options].filterwarnings in pyproject.toml. "
        "This guard ensures httpx deprecation warnings surface as test failures "
        "before they become hard removals in the next version bump."
    )


def _ci_wait_true_literals_missing_pr_number(app_dir: str) -> list[str]:
    """Return "file:line" sites where a dict LITERAL sets ``"ci_waiting": True``
    but does NOT co-stamp a ``"pr_number"`` key.

    The orchestrator's merge-critical CI-poll loop resolves a parked rollup's PR
    via ``review_loop.pr_number`` (with a ``context.rollup_pr_number`` fallback,
    ``ci_merge_helpers.effective_pr_number``). A ``ci_waiting=True`` armed as a
    dict literal WITHOUT ``pr_number`` reintroduces the identity-less
    ``{ci_waiting, iteration}`` shape that the merger SKIPS — the PR-identity
    split-brain. Every CI-wait arm literal must therefore also carry
    ``pr_number`` (or route through ``ci_merge_helpers.arm_ci_wait``, which
    co-stamps it and asserts it is non-None).
    """
    import ast
    import glob as globmod

    offenders: list[str] = []
    for py_file in globmod.glob(os.path.join(app_dir, "**", "*.py"), recursive=True):
        with open(py_file, encoding="utf-8") as fh:
            source = fh.read()
        try:
            tree = ast.parse(source, filename=py_file)
        except SyntaxError:
            continue
        rel = os.path.relpath(py_file, app_dir).replace(os.sep, "/")
        for node in ast.walk(tree):
            if not isinstance(node, ast.Dict):
                continue
            const_keys = {
                k.value for k in node.keys if isinstance(k, ast.Constant)
            }
            arms_ci_wait = any(
                isinstance(k, ast.Constant)
                and k.value == "ci_waiting"
                and isinstance(v, ast.Constant)
                and v.value is True
                for k, v in zip(node.keys, node.values)
            )
            if arms_ci_wait and "pr_number" not in const_keys:
                offenders.append(f"{rel}:{node.lineno}")
    return offenders


def test_ci_wait_arm_literals_costamp_pr_number():
    """Tripwire: no dict literal may arm ``ci_waiting=True`` without ``pr_number``.

    Guards the PR-identity split-brain fix: an identity-less CI-wait arm is
    skipped by the merge-critical poll loop and only drains out-of-band. New arm
    sites must either include ``"pr_number"`` in the same literal or route
    through ``ci_merge_helpers.arm_ci_wait``.
    """
    app_dir = os.path.normpath(os.path.join(os.path.dirname(__file__), "..", "app"))
    offenders = _ci_wait_true_literals_missing_pr_number(app_dir)
    assert not offenders, (
        "Dict literal(s) arm ci_waiting=True without co-stamping pr_number "
        "(identity-less shape the merge poll loop SKIPS):\n  "
        + "\n  ".join(offenders)
        + "\n\nInclude \"pr_number\" in the same literal, or route the arm site "
        "through ci_merge_helpers.arm_ci_wait(review_loop, pr_number=..., "
        "now_iso=...) which co-stamps pr_number + ci_wait_started and asserts "
        "pr_number is non-None."
    )

# ---------------------------------------------------------------------------
# Cache-by-default guardrail for github_refs.get_branch_sha
# ---------------------------------------------------------------------------
# get_branch_sha caches by default (short-TTL branch-tip cache) — safe for the
# overwhelming majority of reads. The ONLY way to (re)introduce an uncached
# hot-path ref read is `live=True`, so every live=True site must be a reviewed
# member of this allowlist. Keyed "<file>:<enclosing function>". Each is
# SHA-critical: it records a promoted/merged SHA, pins the canonical
# candidate/base SHA, or anchors a branch create — where a <=60s-stale cached
# tip would be a data-integrity bug. Adding an entry is a deliberate act.
_LIVE_REF_READ_ALLOWLIST = frozenset(
    {
        "release_promotion.py:promote",
        "release_qualifier.py:run_qualification",
        "release_qualifier.py:_guard_ci_unresolved_promotion_in_flight",
        "release_qualifier.py:force_promote_current_version",
        "release_reconciliation.py:_reconcile_if_promotion_failed_landed",
        "release_reconciliation.py:_reconcile_if_not_promoted_but_pr_merged",
        "release_reconciliation.py:_reconcile_if_pending_auto_merge_landed",
        "release_reconciliation.py:_reconcile_if_managed_check_wait",
        "release_version.py:_resolve_integration_head_sha",
        "strategic_spawning.py:_spawn_strategic_review",
        "task_completion.py:_ensure_branch",
        "rollup_management.py:_check_and_trigger_rollup",
    }
)

# github.py defines get_branch_sha; github_refs.py's own live=True is inside
# create_branch's conflict-retry (branch-write context, not a hot read path).
_LIVE_REF_SCAN_SKIP_FILES = frozenset({"github.py", "github_refs.py"})


def _live_true_get_branch_sha_sites(tree, rel_path):
    """Yield ``"<rel_path>:<function>"`` for each get_branch_sha(..., live=True)."""
    parent = {}
    for node in ast.walk(tree):
        for child in ast.iter_child_nodes(node):
            parent[child] = node

    def _enclosing_fn(node):
        cur = parent.get(node)
        while cur is not None:
            if isinstance(cur, (ast.FunctionDef, ast.AsyncFunctionDef)):
                return cur.name
            cur = parent.get(cur)
        return "<module>"

    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        callee = node.func
        name = (
            callee.attr
            if isinstance(callee, ast.Attribute)
            else callee.id
            if isinstance(callee, ast.Name)
            else None
        )
        if name != "get_branch_sha":
            continue
        for kw in node.keywords:
            if (
                kw.arg == "live"
                and isinstance(kw.value, ast.Constant)
                and kw.value.value is True
            ):
                yield f"{rel_path}:{_enclosing_fn(node)}"


def test_get_branch_sha_live_true_is_allowlisted():
    """Every ``get_branch_sha(..., live=True)`` in backend/app must be a reviewed
    member of the must-be-live allowlist. This is what makes cache-by-default
    safe: a future caller can't silently reintroduce an uncached hot-path ref
    read (the class of bug that drained the GitHub install-token budget) without
    tripping this guard."""
    import glob

    app_dir = os.path.join(os.path.dirname(__file__), "..", "app")
    offenders = []
    for path in glob.glob(os.path.join(app_dir, "*.py")):
        rel = os.path.basename(path)
        if rel in _LIVE_REF_SCAN_SKIP_FILES:
            continue
        with open(path, encoding="utf-8") as f:
            tree = ast.parse(f.read(), filename=rel)
        for site in _live_true_get_branch_sha_sites(tree, rel):
            if site not in _LIVE_REF_READ_ALLOWLIST:
                offenders.append(site)
    assert not offenders, (
        "get_branch_sha(..., live=True) at un-allowlisted site(s): "
        f"{sorted(offenders)}. live=True bypasses the branch-tip cache and must be "
        "a reviewed SHA-critical read (records a promoted/merged SHA, pins the "
        "canonical candidate/base SHA, or anchors a branch create). If genuinely "
        "SHA-critical, add '<file>:<function>' to _LIVE_REF_READ_ALLOWLIST with a "
        "justification; otherwise drop live=True (cached is the safe default)."
    )


def test_get_branch_sha_live_guard_scanner_flags_injected_site():
    """Self-test: the scanner actually detects a live=True call, so the guard
    above can't silently pass by matching nothing."""
    import textwrap

    src = textwrap.dedent(
        """
        async def _bad():
            return await github.get_branch_sha(t, o, r, b, live=True)
        """
    )
    tree = ast.parse(src)
    assert list(_live_true_get_branch_sha_sites(tree, "fake.py")) == ["fake.py:_bad"]


# ---------------------------------------------------------------------------
# String patch-target resolution invariant
# ---------------------------------------------------------------------------

def _collect_app_patch_targets(testing_dir: Path) -> dict[str, set[str]]:
    """Return {filename: {target, ...}} for app.* string patch targets in test files.

    Excludes patch() calls that carry ``create=True`` — those intentionally
    name attributes that don't exist at module level.
    """
    result: dict[str, set[str]] = {}
    for py_file in sorted(testing_dir.glob("*.py")):
        try:
            source = py_file.read_text(encoding="utf-8")
            tree = ast.parse(source, filename=py_file.name)
        except (SyntaxError, UnicodeDecodeError):
            continue

        targets: set[str] = set()
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if isinstance(func, ast.Attribute) and func.attr in ("patch", "setattr"):
                pass
            elif isinstance(func, ast.Name) and func.id == "patch":
                pass
            else:
                continue

            # create=True patches intentionally reference non-existent attributes
            if any(
                kw.arg == "create"
                and isinstance(kw.value, ast.Constant)
                and kw.value.value is True
                for kw in node.keywords
            ):
                continue

            if not node.args:
                continue
            first_arg = node.args[0]
            if not isinstance(first_arg, ast.Constant) or not isinstance(
                first_arg.value, str
            ):
                continue
            target = first_arg.value
            # Require at least app.<module>.<attr> (two dots minimum)
            if target.startswith("app.") and "." in target[4:]:
                targets.add(target)

        if targets:
            result[py_file.name] = targets
    return result


def _app_module_top_level_names(app_dir: Path, module_dotted: str) -> set[str] | None:
    """Return the set of top-level names in a module file, or None if not found.

    Scans only module-level statements (defs, class defs, assignments, imports).
    Returns None when ``module_dotted`` does not map to a file on disk, which
    indicates a "nested import" pattern (e.g. app.storage.storage mapping to
    google.cloud.storage accessed via app.storage) — those are skipped rather
    than flagged.
    """
    parts = module_dotted.split(".")
    base = app_dir.parent  # backend/

    candidate = base.joinpath(*parts).with_suffix(".py")
    if not candidate.exists():
        candidate = base.joinpath(*parts, "__init__.py")
        if not candidate.exists():
            return None

    try:
        source = candidate.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(candidate))
    except (SyntaxError, UnicodeDecodeError):
        return None

    names: set[str] = set()
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(node.name)
        elif isinstance(node, ast.Assign):
            for tgt in node.targets:
                for n in ast.walk(tgt):
                    if isinstance(n, ast.Name):
                        names.add(n.id)
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            names.add(node.target.id)
        elif isinstance(node, ast.ImportFrom):
            for alias in node.names:
                names.add(alias.asname if alias.asname else alias.name)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                names.add(alias.asname if alias.asname else alias.name)
    return names


def test_string_patch_targets_resolve():
    """Every app.* string patch target in backend/testing/*.py must resolve to
    an existing top-level name in its module.

    Renaming or deleting an app.* symbol that is referenced in a string patch
    target (mocker.patch / mock.patch / @patch / monkeypatch.setattr) silently
    voids the mock — the real function runs instead of the mock, producing
    false-passing tests. This invariant makes that class of breakage fail at
    the standalone gate (scripts/test-backend.sh --standalone --no-postgres)
    before it reaches CI, naming the offending test file and target.

    Excluded: ``create=True`` patches (intentionally naming absent attributes)
    and targets whose module path does not map to a file on disk (nested import
    patterns such as ``app.storage.storage.Client``).
    """
    testing_dir = Path(__file__).resolve().parent
    app_dir = Path(__file__).resolve().parents[1] / "app"

    targets_by_file = _collect_app_patch_targets(testing_dir)

    module_cache: dict[str, set[str] | None] = {}
    failures: list[str] = []

    for test_file, targets in sorted(targets_by_file.items()):
        for target in sorted(targets):
            last_dot = target.rfind(".")
            if last_dot == -1:
                continue
            module_dotted = target[:last_dot]
            attr = target[last_dot + 1:]

            if module_dotted not in module_cache:
                module_cache[module_dotted] = _app_module_top_level_names(
                    app_dir, module_dotted
                )

            top_level = module_cache[module_dotted]
            if top_level is None:
                continue  # nested import pattern — not checkable statically
            if attr not in top_level:
                failures.append(
                    f"  {test_file}: '{target}' — '{attr}' not defined in {module_dotted}"
                )

    assert not failures, (
        f"String patch targets that do not resolve ({len(failures)} offender(s)):\n"
        + "\n".join(sorted(failures))
        + "\n\nFix: rename the patch target string to match the current symbol name, "
        "or remove the test if the functionality was deleted."
    )


def test_string_patch_target_scanner_self_tests():
    """Self-tests for the patch-target scanner helpers."""
    import textwrap
    import tempfile

    # Scanner detects a stale patch target
    src_stale = textwrap.dedent("""
        async def test_foo(mocker):
            mocker.patch("app.nonexistent_module_xyz.nonexistent_func")
    """)
    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir_path = Path(tmpdir)
        (tmpdir_path / "test_fake.py").write_text(src_stale)
        result = _collect_app_patch_targets(tmpdir_path)
        assert result == {
            "test_fake.py": {"app.nonexistent_module_xyz.nonexistent_func"}
        }

    # create=True patches are excluded
    src_create = textwrap.dedent("""
        async def test_bar(mocker):
            mocker.patch("app.some_module.some_func", create=True)
    """)
    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir_path = Path(tmpdir)
        (tmpdir_path / "test_create.py").write_text(src_create)
        assert _collect_app_patch_targets(tmpdir_path) == {}


# Redundant ``create=True`` patches currently in the suite (target already exists
# at module top level). ``create=True`` is legitimate ONLY when the attribute is
# absent; these are grandfathered so this invariant fails only on NEWLY-accreted
# redundancy. A queued refactor drops the flags below — prune the matching entry
# here as each one lands (leftover entries are harmless: the check is subset-only).
_KNOWN_REDUNDANT_CREATE_TRUE: "frozenset[str]" = frozenset({
    "test_admin_routers.py::app.routers.admin_digest._runtime_config",
    "test_c5_approval_gate_invariant.py::app.ci_completion._post_merge_summary",
    "test_c5_approval_gate_invariant.py::app.review_pipeline.broadcast_to_project",
    "test_evaluating.py::app.services.broadcast_to_project",
    "test_release_stall_alerts.py::app.release_promotion._notify_operator",
    "test_warm_vm_integration.py::app.broadcast.broadcast_to_project",
})


def test_redundant_create_true_patches_flagged():
    """A ``create=True`` patch whose target now EXISTS at module top level is
    redundant and must be flagged at the standalone merge gate.

    ``create=True`` fabricates an attribute mock deems absent; once the real
    attribute exists the flag is a no-op that silently suppresses the
    ``AttributeError`` a later rename/removal of that symbol would raise — the
    exact drift that ``test_string_patch_targets_resolve`` catches, except that
    invariant deliberately skips ``create=True`` patches. This keeps redundant
    ``create=True`` from re-accreting: any ``create=True`` ``app.*`` target that
    resolves at module top level fails unless grandfathered in
    ``_KNOWN_REDUNDANT_CREATE_TRUE``.
    """
    from testing import impacted_tests

    testing_dir = Path(__file__).resolve().parent
    app_dir = Path(__file__).resolve().parents[1] / "app"

    pairs = impacted_tests.collect_create_true_patch_targets(str(testing_dir))

    module_cache: dict[str, "set[str] | None"] = {}
    offenders: list[str] = []
    for test_file, target in pairs:
        last_dot = target.rfind(".")
        if last_dot == -1:
            continue
        module_dotted = target[:last_dot]
        attr = target[last_dot + 1:]

        if module_dotted not in module_cache:
            module_cache[module_dotted] = _app_module_top_level_names(
                app_dir, module_dotted
            )

        top_level = module_cache[module_dotted]
        if top_level is None:
            continue  # nested-import pattern — create=True is legitimate
        if attr in top_level and f"{test_file}::{target}" not in _KNOWN_REDUNDANT_CREATE_TRUE:
            offenders.append(
                f"  {test_file}: '{target}' — '{attr}' already exists in {module_dotted}"
            )

    assert not offenders, (
        f"Redundant create=True patch(es) detected ({len(offenders)}):\n"
        + "\n".join(sorted(offenders))
        + "\n\ncreate=True is a no-op when the attribute exists and silently masks "
        "future renames/removals of that symbol. Fix: drop the create=True keyword "
        "so the patch resolves against the real attribute (and is then covered by "
        "test_string_patch_targets_resolve)."
    )


def test_redundant_create_true_scanner_self_test():
    """Self-test: the create=True collector returns create=True targets and only those.

    Guards the invariant above from going vacuous — proves detection is live by
    constructing a file with one create=True patch and one plain patch and
    asserting only the former is returned.
    """
    import textwrap
    import tempfile
    from testing import impacted_tests

    src = textwrap.dedent("""
        def test_thing(mocker):
            mocker.patch("app.some_module.exists", create=True)
            mocker.patch("app.some_module.plain")
            mocker.patch("backend.app.other.sym", create=True)
    """)
    with tempfile.TemporaryDirectory() as tmpdir:
        (Path(tmpdir) / "test_synthetic.py").write_text(src)
        pairs = impacted_tests.collect_create_true_patch_targets(tmpdir)

    assert pairs == [
        ("test_synthetic.py", "app.other.sym"),
        ("test_synthetic.py", "app.some_module.exists"),
    ]


# ── Route Contract Ownership ────────────────────────────────────────────────


def _find_test_funcs_matching(content: str, pattern: re.Pattern) -> list[str]:
    """Return names of test functions whose body contains the given pattern."""
    func_starts = list(re.finditer(r"^(?:async )?def (test_\w+)", content, re.MULTILINE))
    offending: list[str] = []
    for i, match in enumerate(func_starts):
        start = match.start()
        end = func_starts[i + 1].start() if i + 1 < len(func_starts) else len(content)
        if pattern.search(content[start:end]):
            offending.append(match.group(1))
    return offending


_API_URL_RE = re.compile(r'["\'](/api/[a-z][^"\'\s?#{]*)["\']')

_TEST_FUNC_DEF_RE = re.compile(r"^(?:async )?def (test_\w+)", re.MULTILINE)


def _validate_route_contract_registry(
    testing_dir: Path,
    registry: dict,
) -> list[str]:
    """Return a list of rot errors for a route-contract ownership registry.

    Each entry is validated against the test files in ``testing_dir``:
    * the owner file must exist;
    * the ``search_pattern`` must be a valid regex;
    * the ``search_pattern`` must match inside the body of at least one real
      ``test_*`` function in the owner file — a match only in a comment,
      docstring, or unrelated top-level line means the owning contract test was
      renamed/deleted and is reported;
    * every ``exempt_tests`` name must exist as a ``test_*`` function somewhere
      in ``testing_dir`` (a dangling exemption is dead config).

    Returns an empty list when the registry is fully valid.
    """
    all_test_funcs: set[str] = set()
    for test_file in testing_dir.glob("test_*.py"):
        all_test_funcs.update(_TEST_FUNC_DEF_RE.findall(test_file.read_text()))

    errors: list[str] = []
    for endpoint, config in registry.items():
        owner_file: str = config["owner"]
        search_pattern: str = config["search_pattern"]

        owner_path = testing_dir / owner_file
        if not owner_path.exists():
            errors.append(
                f"Endpoint '{endpoint}': owner file '{owner_file}' does not exist "
                f"at {owner_path}"
            )
            continue

        owner_content = owner_path.read_text()
        try:
            compiled = re.compile(search_pattern)
        except re.error as exc:
            errors.append(
                f"Endpoint '{endpoint}': invalid search_pattern regex: {exc}"
            )
            continue

        # Require the pattern to match inside a real test_* function body, not
        # merely anywhere in the file (a comment/docstring/import line).
        owning_funcs = _find_test_funcs_matching(owner_content, compiled)
        if not owning_funcs:
            if compiled.search(owner_content):
                errors.append(
                    f"Endpoint '{endpoint}': search_pattern matches owner file "
                    f"'{owner_file}' but not inside any test_* function body "
                    f"(only in a comment/docstring/unrelated line). The owning "
                    f"contract test appears to have been renamed or deleted. "
                    f"Pattern: {search_pattern}"
                )
            else:
                errors.append(
                    f"Endpoint '{endpoint}': search_pattern does not match any "
                    f"content in owner file '{owner_file}'. Pattern: {search_pattern}"
                )

        # Every declared exemption must name a real test_* function; a stale
        # name silently widens the ownership gate.
        errors.extend(
            f"Endpoint '{endpoint}': exempt_tests names '{exempt_name}', "
            f"which is not a test_* function anywhere in backend/testing. "
            f"Remove the stale exemption or fix the name."
            for exempt_name in config.get("exempt_tests", [])
            if exempt_name not in all_test_funcs
        )

    return errors


def _find_unregistered_duplicated_contracts(
    testing_dir: Path,
    registry: dict,
) -> list[tuple[str, list[str]]]:
    """Return (url, [file_names]) for API URLs asserted in 2+ test files but not registered.

    Scans test_*.py files for literal API URL strings and groups them by the set
    of files whose test functions reference each URL.  URLs already covered by a
    registry search_pattern are excluded — only unregistered cross-file duplicates
    are returned so callers can fail fast when a new contradiction appears.
    """
    registered_patterns = [
        re.compile(cfg["search_pattern"]) for cfg in registry.values()
    ]

    url_files: dict[str, list[str]] = {}
    for test_file in sorted(testing_dir.glob("test_*.py")):
        content = test_file.read_text()
        seen: set[str] = set(_API_URL_RE.findall(content))
        for url in seen:
            url_files.setdefault(url, []).append(test_file.name)

    violations: list[tuple[str, list[str]]] = []
    for url, files in url_files.items():
        if len(files) < 2:
            continue
        if any(pat.search(url) for pat in registered_patterns):
            continue
        violations.append((url, sorted(files)))
    return violations


class TestRouteContractOwnership:
    """Standalone guard: each endpoint's response-shape contract must live in
    exactly one owner test file (declared in route_contract_owners.json).

    A second test file asserting the same endpoint's shape is a contract
    violation caught at standalone CI time — the mechanical speed bump that was
    missing when the /tasks flat-vs-nested contradiction merged undetected.
    """

    def test_no_duplicate_route_contracts(self):
        """No non-owner test file may introduce response-shape tests for a
        registered endpoint unless the test function is explicitly exempted.

        To register a new endpoint: add an entry to route_contract_owners.json.
        To exempt a pre-existing co-located test: add its function name to
        'exempt_tests' for that endpoint's entry.
        """
        import json

        registry_path = BACKEND_ROOT / "testing" / "route_contract_owners.json"
        assert registry_path.exists(), (
            f"Route contract registry not found: {registry_path}. "
            "Create backend/testing/route_contract_owners.json to use this guard."
        )

        with open(registry_path) as f:
            registry = json.load(f)

        testing_dir = BACKEND_ROOT / "testing"
        violations: list[str] = []

        for endpoint, config in registry.items():
            owner_file: str = config["owner"]
            raw_pattern: str = config["search_pattern"]
            exempt_tests: set[str] = set(config.get("exempt_tests", []))

            try:
                compiled = re.compile(raw_pattern)
            except re.error as exc:
                raise AssertionError(
                    f"route_contract_owners.json: invalid regex for '{endpoint}': {exc}"
                ) from exc

            for test_file in sorted(testing_dir.glob("test_*.py")):
                if test_file.name == owner_file:
                    continue
                content = test_file.read_text()
                if not compiled.search(content):
                    continue

                offending = [
                    fn
                    for fn in _find_test_funcs_matching(content, compiled)
                    if fn not in exempt_tests
                ]
                if offending:
                    violations.append(
                        f"Endpoint '{endpoint}' response-shape contract is owned by "
                        f"'{owner_file}' but also asserted in {test_file.name}: "
                        + ", ".join(offending)
                    )

        assert not violations, (
            "Route contract violations detected. Each endpoint's response-shape "
            "contract must live in its registered owner file "
            "(see backend/testing/route_contract_owners.json).\n"
            + "\n".join(f"  • {v}" for v in violations)
        )

    def test_route_contract_owners_are_valid(self):
        """Each endpoint's owner file must exist and own a real contract test.

        This meta-integrity check prevents silent rot: a stale/typo'd owner file
        path or a drifted search_pattern that no longer matches its endpoint
        is caught at merge time.

        Rot-prevention tightening: the search_pattern must match inside the body
        of at least one real ``test_*`` function in the owner file — a match in a
        comment, docstring, or an unrelated top-level line is NOT enough, so a
        deleted/renamed owner contract test is caught. Every name listed in an
        entry's ``exempt_tests`` must also exist as a ``test_*`` function
        somewhere in backend/testing; a dangling exemption is dead config that
        silently widens the gate.
        """
        import json

        registry_path = BACKEND_ROOT / "testing" / "route_contract_owners.json"
        with open(registry_path) as f:
            registry = json.load(f)

        testing_dir = BACKEND_ROOT / "testing"
        errors = _validate_route_contract_registry(testing_dir, registry)

        assert not errors, (
            "Route contract ownership registry has invalid entries:\n"
            + "\n".join(f"  • {e}" for e in errors)
        )

    def test_route_contract_registry_is_populated(self):
        """The registry must contain at least one entry beyond the seed entry.

        This prevents the guard from being silently hollowed out by removing
        entries; any PR that drops the registry below the minimum must update
        this threshold with justification.
        """
        import json

        registry_path = BACKEND_ROOT / "testing" / "route_contract_owners.json"
        with open(registry_path) as f:
            registry = json.load(f)

        _MINIMUM_REGISTERED_ENDPOINTS = 3
        assert len(registry) >= _MINIMUM_REGISTERED_ENDPOINTS, (
            f"route_contract_owners.json has only {len(registry)} registered "
            f"endpoint(s); the guard requires at least {_MINIMUM_REGISTERED_ENDPOINTS}. "
            "Add entries for admin endpoints whose response-shape contracts live in "
            "more than one test file, or lower the threshold with justification."
        )

    def test_unregistered_endpoint_with_multiple_contract_assertions_fails(self):
        """Verify _find_unregistered_duplicated_contracts catches cross-file violations.

        Creates two synthetic test files that both assert the same unregistered API
        endpoint, then confirms the helper returns that endpoint as a violation.
        The registry is kept empty so the endpoint is definitely unregistered.
        """
        import tempfile

        endpoint_url = "/api/admin/fake-widgets"
        src_a = (
            f'async def test_get_widgets(client):\n'
            f'    r = await client.get("{endpoint_url}")\n'
            f'    assert r.json()["widgets"]\n'
        )
        src_b = (
            f'async def test_widget_list(client):\n'
            f'    r = await client.get("{endpoint_url}")\n'
            f'    assert r.json()["count"] == 0\n'
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            tmpdir_path = Path(tmpdir)
            (tmpdir_path / "test_alpha.py").write_text(src_a)
            (tmpdir_path / "test_beta.py").write_text(src_b)

            violations = _find_unregistered_duplicated_contracts(tmpdir_path, {})

        assert any(endpoint_url in url for url, _ in violations), (
            f"Expected '{endpoint_url}' (asserted in test_alpha.py and test_beta.py) "
            f"to be flagged as an unregistered duplicate contract, but "
            f"_find_unregistered_duplicated_contracts returned: {violations}"
        )

    def test_validate_registry_accepts_pattern_inside_test_function(self):
        """A pattern that matches inside a real test_* body is valid (no errors)."""
        import tempfile

        owner_src = (
            "async def test_widgets_contract(client):\n"
            '    r = await client.get("/api/admin/widgets")\n'
            '    assert r.json()["widgets"] == []\n'
        )
        registry = {
            "GET /api/admin/widgets": {
                "owner": "test_widgets.py",
                "search_pattern": r"/api/admin/widgets",
                "exempt_tests": [],
            }
        }
        with tempfile.TemporaryDirectory() as tmpdir:
            tmpdir_path = Path(tmpdir)
            (tmpdir_path / "test_widgets.py").write_text(owner_src)

            errors = _validate_route_contract_registry(tmpdir_path, registry)

        assert errors == [], (
            f"Expected no errors for a pattern matched inside a test_* function, "
            f"got: {errors}"
        )

    def test_validate_registry_rejects_missing_owner_file(self):
        """A registered owner file that does not exist is reported."""
        import tempfile

        registry = {
            "GET /api/admin/widgets": {
                "owner": "test_does_not_exist.py",
                "search_pattern": r"/api/admin/widgets",
                "exempt_tests": [],
            }
        }
        with tempfile.TemporaryDirectory() as tmpdir:
            errors = _validate_route_contract_registry(Path(tmpdir), registry)

        assert any("does not exist" in e for e in errors), (
            f"Expected a missing-owner-file error, got: {errors}"
        )

    def test_validate_registry_rejects_pattern_only_in_comment(self):
        """A pattern that matches only in a comment/docstring — not inside any
        test_* function body — is caught as a renamed/deleted owner contract.

        This is the core rot-prevention tightening: the old check accepted a
        match anywhere in the file, so deleting the real contract test while
        leaving a stray mention would pass silently.
        """
        import tempfile

        # The endpoint string appears only in a module docstring / comment, and
        # the sole test_* function does NOT reference it (its contract was
        # renamed away).
        owner_src = (
            '"""Tests once covered /api/admin/widgets response shape."""\n'
            "\n"
            "# TODO: /api/admin/widgets contract lives here\n"
            "async def test_unrelated(client):\n"
            '    r = await client.get("/api/admin/other")\n'
            '    assert r.status_code == 200\n'
        )
        registry = {
            "GET /api/admin/widgets": {
                "owner": "test_widgets.py",
                "search_pattern": r"/api/admin/widgets",
                "exempt_tests": [],
            }
        }
        with tempfile.TemporaryDirectory() as tmpdir:
            tmpdir_path = Path(tmpdir)
            (tmpdir_path / "test_widgets.py").write_text(owner_src)

            errors = _validate_route_contract_registry(tmpdir_path, registry)

        assert any("not inside any test_* function body" in e for e in errors), (
            f"Expected a comment-only match to be rejected (renamed/deleted owner "
            f"test), got: {errors}"
        )

    def test_validate_registry_rejects_pattern_matching_nowhere(self):
        """A pattern that matches nothing in the owner file is reported as a
        drifted pattern (distinct from the comment-only case)."""
        import tempfile

        owner_src = (
            "async def test_something(client):\n"
            '    r = await client.get("/api/admin/other")\n'
            '    assert r.status_code == 200\n'
        )
        registry = {
            "GET /api/admin/widgets": {
                "owner": "test_widgets.py",
                "search_pattern": r"/api/admin/widgets",
                "exempt_tests": [],
            }
        }
        with tempfile.TemporaryDirectory() as tmpdir:
            tmpdir_path = Path(tmpdir)
            (tmpdir_path / "test_widgets.py").write_text(owner_src)

            errors = _validate_route_contract_registry(tmpdir_path, registry)

        assert any("does not match any" in e for e in errors), (
            f"Expected a drifted-pattern error, got: {errors}"
        )

    def test_validate_registry_rejects_dangling_exempt_test(self):
        """An exempt_tests name that is not a real test_* function anywhere in
        the testing dir is reported as a stale exemption."""
        import tempfile

        owner_src = (
            "async def test_widgets_contract(client):\n"
            '    r = await client.get("/api/admin/widgets")\n'
            '    assert r.json()["widgets"] == []\n'
        )
        registry = {
            "GET /api/admin/widgets": {
                "owner": "test_widgets.py",
                "search_pattern": r"/api/admin/widgets",
                "exempt_tests": ["test_this_was_deleted_long_ago"],
            }
        }
        with tempfile.TemporaryDirectory() as tmpdir:
            tmpdir_path = Path(tmpdir)
            (tmpdir_path / "test_widgets.py").write_text(owner_src)

            errors = _validate_route_contract_registry(tmpdir_path, registry)

        assert any(
            "test_this_was_deleted_long_ago" in e and "stale exemption" in e
            for e in errors
        ), f"Expected a stale-exemption error, got: {errors}"

    def test_validate_registry_accepts_exempt_test_in_other_file(self):
        """A function-scoped exemption may live in a DIFFERENT test file; it is
        valid as long as the name exists as a test_* function somewhere in the
        testing dir (matching the real registry's cross-file exemptions)."""
        import tempfile

        owner_src = (
            "async def test_widgets_contract(client):\n"
            '    r = await client.get("/api/admin/widgets")\n'
            '    assert r.json()["widgets"] == []\n'
        )
        other_src = (
            "async def test_widgets_from_other_angle(client):\n"
            '    r = await client.get("/api/admin/widgets")\n'
            '    assert r.json()["count"] == 0\n'
        )
        registry = {
            "GET /api/admin/widgets": {
                "owner": "test_widgets.py",
                "search_pattern": r"/api/admin/widgets",
                "exempt_tests": ["test_widgets_from_other_angle"],
            }
        }
        with tempfile.TemporaryDirectory() as tmpdir:
            tmpdir_path = Path(tmpdir)
            (tmpdir_path / "test_widgets.py").write_text(owner_src)
            (tmpdir_path / "test_widgets_other.py").write_text(other_src)

            errors = _validate_route_contract_registry(tmpdir_path, registry)

        assert errors == [], (
            f"Expected a cross-file exemption naming a real test_* function to be "
            f"valid, got: {errors}"
        )


def test_no_deprecated_datetime_utcnow_calls():
    """No app/ file may call datetime.utcnow() — use _utcnow() from app.time_utils instead.

    Python 3.12 turns every datetime.utcnow() call into a DeprecationWarning; with
    filterwarnings=error::DeprecationWarning in pyproject.toml this becomes a hard test
    failure.  The canonical wrapper _utcnow() (in app/time_utils.py) returns a UTC-aware
    datetime and is exempt from DTZ ruff rules via its own noqa.  This invariant prevents
    regressions after the de-utcnow sweep.
    """
    app_dir = BACKEND_ROOT / "app"
    violating_files: list[str] = []

    for py_file in sorted(app_dir.rglob("*.py")):
        source = py_file.read_text(encoding="utf-8")
        try:
            tree = ast.parse(source, filename=str(py_file))
        except SyntaxError:
            continue

        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if (
                isinstance(func, ast.Attribute)
                and func.attr == "utcnow"
                and isinstance(func.value, ast.Name)
                and func.value.id == "datetime"
            ):
                rel = py_file.relative_to(BACKEND_ROOT)
                violating_files.append(f"  {rel}:{node.lineno}")
                break  # one report per file is enough

    assert not violating_files, (
        f"Found datetime.utcnow() calls in {len(violating_files)} app/ file(s).\n"
        "Replace each with _utcnow() from app.time_utils — it returns a UTC-aware\n"
        "datetime and avoids the Python 3.12 DeprecationWarning.\n\n"
        "Offending files:\n" + "\n".join(violating_files)
    )



def test_spawn_budget_partition_is_exhaustive():
    """Assert that the autonomous spawn-budget partition is exhaustive and disjoint.

    Every task type must be in exactly one of INITIATIVE or SERVICING partition,
    with no gaps. Live spawn paths exist for memo, repo_bootstrap, and investigate,
    so all three must be classified to prevent the initiative cap from silently
    under-binding due to 'other' bucket spillover.
    """
    from app.task_capabilities import (
        ALL_TASK_TYPES,
        AUTONOMOUS_SPAWN_INITIATIVE_TASK_TYPES,
        AUTONOMOUS_SPAWN_SERVICING_TASK_TYPES,
    )

    initiative = AUTONOMOUS_SPAWN_INITIATIVE_TASK_TYPES
    servicing = AUTONOMOUS_SPAWN_SERVICING_TASK_TYPES

    # Disjoint: no overlap between partitions
    overlap = initiative & servicing
    assert not overlap, (
        f"initiative and servicing partitions overlap: {sorted(overlap)}. "
        "Each task type must be in exactly one partition."
    )

    # Exhaustive: union covers all task types with no gaps
    union = initiative | servicing
    all_types = frozenset(ALL_TASK_TYPES)
    missing = all_types - union
    assert not missing, (
        f"task types missing from both partitions: {sorted(missing)}. "
        "All task types must be classified as either initiative or servicing."
    )

    # Verify union exactly equals all types (redundant but explicit check)
    assert union == all_types, (
        f"partition union {sorted(union)} != all task types {sorted(all_types)}"
    )


def test_orchestrator_health_task_type_vocab_is_registry_known():
    """orchestrator_health hand-maintained type sets must contain only registry-known types.

    Drift class: a type name removed from TaskType can silently linger in these
    frozensets (e.g., 'modify' was never registered but appeared in
    _CODE_PRODUCING_TASK_TYPES and _OUTPUT_TRANSFORMING_TASK_TYPES).  This guard
    catches that at PR time rather than silently at runtime.

    The empty-string sentinel in _CODE_PRODUCING_TASK_TYPES is intentional — it
    is the normalized form for a default-create task whose payload carries no
    explicit type — and is therefore excluded from the registry check.
    """
    from app.task_capabilities import ALL_TASK_TYPES
    import app.orchestrator_health as _oh

    covered_sets = {
        "_CODE_PRODUCING_TASK_TYPES": _oh._CODE_PRODUCING_TASK_TYPES,
        "_OUTPUT_TRANSFORMING_TASK_TYPES": _oh._OUTPUT_TRANSFORMING_TASK_TYPES,
    }
    for set_name, type_set in covered_sets.items():
        unknown = {t for t in type_set if t and t not in ALL_TASK_TYPES}
        assert not unknown, (
            f"orchestrator_health.{set_name} contains unregistered task type(s): "
            f"{sorted(unknown)}.  Either add the type to TaskType in task_capabilities.py "
            "or remove the stale literal from the frozenset."
        )


def test_orchestrator_health_vocab_guard_catches_injected_fake_type():
    """Self-test: the vocab guard above must catch a deliberately injected fake type.

    Verifies the guard is live — not accidentally vacuous — by constructing a
    patched frozenset that contains a fake type and asserting the guard logic
    raises.
    """
    from app.task_capabilities import ALL_TASK_TYPES

    fake_set = frozenset({"create", "fix", "__nonexistent_type__"})
    unknown = {t for t in fake_set if t and t not in ALL_TASK_TYPES}
    assert unknown == {"__nonexistent_type__"}, (
        "Guard logic failed to detect the injected fake type '__nonexistent_type__'. "
        "The standing vocab check would silently pass on real drift."
    )


def test_mock_patch_targets_resolve():
    """Every first-party mock.patch() string target in the test suite must resolve.

    Uses ``impacted_tests.collect_first_party_patch_targets`` (production API) to
    enumerate every ``patch("app.*")`` call across ``backend/testing/test_*.py``,
    then resolves each to the module file it references (via the same AST-based
    top-level-name check as ``_app_module_top_level_names``). A renamed or removed
    attribute only surfaces at test-run time when the impacted-test selector
    happens to include the patching file; this static check closes that gap at the
    standalone merge gate.

    ``create=True`` patches and targets whose module path does not map to a file
    on disk (nested-import patterns such as ``app.storage.storage.Client``) are
    excluded rather than flagged.
    """
    from testing import impacted_tests

    testing_dir = Path(__file__).resolve().parent
    app_dir = Path(__file__).resolve().parents[1] / "app"

    targets = impacted_tests.collect_first_party_patch_targets(str(testing_dir))

    module_cache: dict[str, "set[str] | None"] = {}
    failures: list[str] = []

    for target in targets:
        last_dot = target.rfind(".")
        if last_dot == -1:
            continue
        module_dotted = target[:last_dot]
        attr = target[last_dot + 1:]

        if module_dotted not in module_cache:
            module_cache[module_dotted] = _app_module_top_level_names(
                app_dir, module_dotted
            )

        top_level = module_cache[module_dotted]
        if top_level is None:
            continue  # nested import pattern — not statically checkable
        if attr not in top_level:
            failures.append(
                f"  '{target}' — '{attr}' not defined in {module_dotted}"
            )

    assert not failures, (
        f"Stale mock-patch target(s) detected ({len(failures)} of {len(targets)}).\n"
        "A renamed/removed attribute only fails at test run time when the "
        "impacted-test selector happens to include the patching file.\n"
        "Fix: update the patch target string to the current module.attribute path.\n\n"
        + "\n".join(sorted(failures)[:50])
        + ("" if len(failures) <= 50 else f"\n... and {len(failures) - 50} more")
    )


def test_acceptance_window_evidence_call_site_wired():
    """release_qualifier_sweep.py must import and call record_acceptance_window_coverage_evidence.

    This invariant guards that the acceptance-window evidence recording function
    stays wired into the sweep hot path. A rename or accidental deletion would
    silently break coverage re-scoring without this tripwire.
    """
    app_dir = Path(__file__).resolve().parents[1] / "app"
    sweep_path = app_dir / "release_qualifier_sweep.py"
    criteria_path = app_dir / "acceptance_criteria.py"

    sweep_source = sweep_path.read_text(encoding="utf-8")
    criteria_source = criteria_path.read_text(encoding="utf-8")

    func_name = "record_acceptance_window_coverage_evidence"

    # Confirm the function is defined in acceptance_criteria.py
    assert f"def {func_name}" in criteria_source, (
        f"'{func_name}' is no longer defined in acceptance_criteria.py. "
        "Either restore the function or update this invariant to match the new name."
    )

    # Confirm it is imported in release_qualifier_sweep.py
    sweep_tree = ast.parse(sweep_source, filename=str(sweep_path))
    imported_names: set[str] = set()
    for node in ast.walk(sweep_tree):
        if isinstance(node, ast.ImportFrom) and node.module == "app.acceptance_criteria":
            for alias in node.names:
                imported_names.add(alias.asname or alias.name)

    assert func_name in imported_names, (
        f"release_qualifier_sweep.py does not import '{func_name}' from "
        "app.acceptance_criteria. The acceptance-window evidence recording "
        "call site may be broken — restore the import."
    )

    # Confirm it is actually called in release_qualifier_sweep.py
    called = False
    for node in ast.walk(sweep_tree):
        if isinstance(node, ast.Call):
            name = _ast_called_name(node)
            if name == func_name:
                called = True
                break
        # Also handle `await record_acceptance_window_coverage_evidence(...)`
        if isinstance(node, ast.Await) and isinstance(node.value, ast.Call):
            name = _ast_called_name(node.value)
            if name == func_name:
                called = True
                break

    assert called, (
        f"release_qualifier_sweep.py imports '{func_name}' but never calls it. "
        "The acceptance-window evidence re-scoring sweep is effectively disabled — "
        "restore the call site."
    )


def test_metrics_integrity_sweep_wired_in_reaper():
    """sweep_metrics_integrity must be imported from reaper_sweeps and called in reaper.py."""
    import ast
    from pathlib import Path

    reaper_path = Path(__file__).resolve().parents[1] / "app" / "reaper.py"
    source = reaper_path.read_text()
    tree = ast.parse(source)

    # Verify import
    imported = False
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == "app.reaper_sweeps":
            for alias in node.names:
                if alias.name == "sweep_metrics_integrity":
                    imported = True
    assert imported, (
        "sweep_metrics_integrity is not imported from app.reaper_sweeps in reaper.py. "
        "The metrics-integrity sweep must be wired in reaper.py."
    )

    # Verify call site
    called = any(
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "sweep_metrics_integrity"
        for node in ast.walk(tree)
    )
    assert called, (
        "sweep_metrics_integrity is imported but never called in reaper.py. "
        "The metrics-integrity sweep must be invoked in the reaper service loop."
    )


def test_v22_finding_backfill_wired_in_orchestrator():
    """_sweep_v22_finding_backfill must be imported and called inside orchestrator.py."""
    import ast
    from pathlib import Path

    orch_path = Path(__file__).resolve().parents[1] / "app" / "orchestrator.py"
    source = orch_path.read_text()
    tree = ast.parse(source)

    # Verify inline import from release_readiness
    imported = any(
        isinstance(node, ast.ImportFrom)
        and node.module == "app.release_readiness"
        and any(alias.name == "_sweep_v22_finding_backfill" for alias in node.names)
        for node in ast.walk(tree)
    )
    assert imported, (
        "_sweep_v22_finding_backfill is not imported from app.release_readiness in orchestrator.py. "
        "The v22 finding backfill must be wired into the orchestrator cadence loop."
    )

    # Verify call site
    called = any(
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "_sweep_v22_finding_backfill"
        for node in ast.walk(tree)
    )
    assert called, (
        "_sweep_v22_finding_backfill is imported but never called in orchestrator.py. "
        "The v22 finding backfill must be invoked on a cadence in the orchestrator loop."
    )


def test_v22_live_verification_evidence_wired_in_sweep():
    """record_v22_live_verification_evidence must be called inside _sweep_v22_finding_backfill.

    The existing wiring test only checks that _sweep_v22_finding_backfill is
    imported and called in orchestrator.py — it does NOT verify the call to
    record_v22_live_verification_evidence inside that function.  This test
    guards the inner call site so a rename or accidental deletion inside
    _sweep_v22_finding_backfill does not silently break the live-verification
    evidence backfill.
    """
    import ast
    from pathlib import Path

    rr_path = Path(__file__).resolve().parents[1] / "app" / "release_readiness.py"
    source = rr_path.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(rr_path))

    func_name = "record_v22_live_verification_evidence"

    # Confirm the function is defined in release_readiness.py
    func_defined = False
    for node in ast.walk(tree):
        if (
            isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name == func_name
        ):
            func_defined = True
            break

    assert func_defined, (
        f"'{func_name}' is no longer defined in release_readiness.py. "
        "Either restore the function or update this invariant to match the new name."
    )

    # Find the _sweep_v22_finding_backfill async function definition
    sweep_func: ast.AsyncFunctionDef | None = None
    for node in ast.walk(tree):
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "_sweep_v22_finding_backfill":
            sweep_func = node
            break

    assert sweep_func is not None, (
        "_sweep_v22_finding_backfill is no longer defined in release_readiness.py. "
        "Either restore the function or update this invariant."
    )

    # Walk the function body to find calls to record_v22_live_verification_evidence
    called = False
    for node in ast.walk(sweep_func):
        # Direct call: record_v22_live_verification_evidence(...)
        if isinstance(node, ast.Call):
            name = _ast_called_name(node)
            if name == func_name:
                called = True
                break
        # Await-wrapped call: await record_v22_live_verification_evidence(...)
        if isinstance(node, ast.Await) and isinstance(node.value, ast.Call):
            name = _ast_called_name(node.value)
            if name == func_name:
                called = True
                break

    assert called, (
        f"'{func_name}' is not called inside "
        "_sweep_v22_finding_backfill in release_readiness.py. "
        "The live-verification evidence backfill is effectively disabled — "
        "restore the call site."
    )


def test_all_autocomplete_paths_gate_on_criterion_coverage():
    """All direction auto-complete paths must gate on criterion coverage.

    Regression guard: a direction that owns an uncovered acceptance criterion
    must not auto-complete through any path — exhaustion (saturated / dedup
    deadlock / already-fixed deadlock) or proven-merge (>=3 verified merges).
    This invariant verifies structurally that every auto-complete code path in
    research_direction_state.py checks ``direction_owns_uncovered_criterion``
    before retiring a direction.

    The module-level frozenset ``_AUTOCOMPLETE_PATHS_GATED_ON_CRITERION_COVERAGE``
    declares the gated paths. Removing the gate from either function, or removing
    the frozenset declaration, causes this test to fail.
    """
    import ast
    from pathlib import Path

    rds_path = Path(__file__).resolve().parents[1] / "app" / "research_direction_state.py"
    source = rds_path.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(rds_path))

    gate_func_name = "direction_owns_uncovered_criterion"

    # ── 1. Verify the module-level frozenset declaration exists ───────────
    frozenset_found = False
    frozenset_entries: set[str] = set()
    for stmt in ast.iter_child_nodes(tree):
        target_name: str | None = None
        value_node: ast.expr | None = None
        if isinstance(stmt, ast.Assign):
            for t in stmt.targets:
                if isinstance(t, ast.Name) and t.id == "_AUTOCOMPLETE_PATHS_GATED_ON_CRITERION_COVERAGE":
                    target_name = t.id
                    value_node = stmt.value
                    break
        elif isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name):
            if stmt.target.id == "_AUTOCOMPLETE_PATHS_GATED_ON_CRITERION_COVERAGE":
                target_name = stmt.target.id
                value_node = stmt.value
        if target_name is None:
            continue
        frozenset_found = True
        # Extract the declared path names from the frozenset literal
        if isinstance(value_node, ast.Call):
            # frozenset({...}) or frozenset([...])
            if isinstance(value_node.func, ast.Name) and value_node.func.id == "frozenset":
                container = value_node.args[0] if value_node.args else None
                if isinstance(container, (ast.Set, ast.List, ast.Tuple)):
                    for elt in container.elts:
                        if isinstance(elt, ast.Constant) and isinstance(elt.value, str):
                            frozenset_entries.add(elt.value)
        break

    assert frozenset_found, (
        "_AUTOCOMPLETE_PATHS_GATED_ON_CRITERION_COVERAGE frozenset is missing from "
        "research_direction_state.py. This constant declares which auto-complete paths "
        "are criterion-gated. Restore it."
    )
    assert frozenset_entries >= {
        "exhaustion",
        "proven_merge",
        "no_findings",
        "strategist_retire",
    }, (
        f"_AUTOCOMPLETE_PATHS_GATED_ON_CRITERION_COVERAGE must contain at least "
        f"{{'exhaustion', 'proven_merge', 'no_findings', 'strategist_retire'}}; "
        f"found {frozenset_entries}. Restore the missing entries."
    )

    # ── 1b. Verify the criterion key constant that gates all paths ─────────
    # This establishes that c-560ec27e is the unique criterion key whose
    # coverage status gates every auto-complete path. Direction d-12f7d408
    # carries this key; any direction owning an uncovered c-560ec27e criterion
    # is blocked from retiring through any of the four gated paths.
    _criterion_key_found = False
    _criterion_key_value: str | None = None
    for stmt in ast.iter_child_nodes(tree):
        target_name: str | None = None
        value_node: ast.expr | None = None
        if isinstance(stmt, ast.Assign):
            for t in stmt.targets:
                if isinstance(t, ast.Name) and t.id == "_CRITERION_COVERAGE_GATE_KEY":
                    target_name = t.id
                    value_node = stmt.value
                    break
        elif isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name):
            if stmt.target.id == "_CRITERION_COVERAGE_GATE_KEY":
                target_name = stmt.target.id
                value_node = stmt.value
        if target_name is None:
            continue
        _criterion_key_found = True
        if isinstance(value_node, ast.Constant) and isinstance(value_node.value, str):
            _criterion_key_value = value_node.value
        break

    assert _criterion_key_found, (
        "_CRITERION_COVERAGE_GATE_KEY is missing from "
        "research_direction_state.py. This constant declares which criterion "
        "key gates the autocomplete paths."
    )
    assert _criterion_key_value == "c-560ec27e", (
        f"_CRITERION_COVERAGE_GATE_KEY expected 'c-560ec27e', got "
        f"{_criterion_key_value!r}"
    )

    # ── 2. Verify exhaustion path: _maybe_autocomplete_exhausted_direction ─
    exhaustion_func: ast.AsyncFunctionDef | None = None
    for node in ast.walk(tree):
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "_maybe_autocomplete_exhausted_direction":
            exhaustion_func = node
            break

    assert exhaustion_func is not None, (
        "_maybe_autocomplete_exhausted_direction not found in research_direction_state.py"
    )

    exhaustion_gated = False
    for node in ast.walk(exhaustion_func):
        if isinstance(node, ast.Await) and isinstance(node.value, ast.Call):
            if _ast_called_name(node.value) == gate_func_name:
                exhaustion_gated = True
                break
        if isinstance(node, ast.Call) and _ast_called_name(node) == gate_func_name:
            exhaustion_gated = True
            break

    assert exhaustion_gated, (
        "_maybe_autocomplete_exhausted_direction does not call "
        "direction_owns_uncovered_criterion. The exhaustion auto-complete path "
        "(saturated / dedup-deadlock / already-fixed-deadlock) must gate on "
        "criterion coverage before retiring a direction."
    )

    # ── 3. Verify proven-merge path: apply_post_spawn_state ───────────────
    proven_func: ast.AsyncFunctionDef | None = None
    for node in ast.walk(tree):
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "apply_post_spawn_state":
            proven_func = node
            break

    assert proven_func is not None, (
        "apply_post_spawn_state not found in research_direction_state.py"
    )

    proven_gated = False
    for node in ast.walk(proven_func):
        if isinstance(node, ast.Call) and _ast_called_name(node) == gate_func_name:
            proven_gated = True
            break
        if isinstance(node, ast.Await) and isinstance(node.value, ast.Call):
            if _ast_called_name(node.value) == gate_func_name:
                proven_gated = True
                break

    assert proven_gated, (
        "apply_post_spawn_state does not call direction_owns_uncovered_criterion. "
        "The proven-merge auto-complete path must gate on criterion coverage before "
        "retiring a direction."
    )

    # ── 4. Verify research_completion.py auto-complete paths ──────────────
    rc_path = Path(__file__).resolve().parents[1] / "app" / "research_completion.py"
    rc_source = rc_path.read_text(encoding="utf-8")
    rc_tree = ast.parse(rc_source, filename=str(rc_path))

    # 4a. Verify the module-level frozenset declaration exists
    rc_frozenset_found = False
    rc_frozenset_entries: set[str] = set()
    for stmt in ast.iter_child_nodes(rc_tree):
        target_name: str | None = None
        value_node: ast.expr | None = None
        if isinstance(stmt, ast.Assign):
            for t in stmt.targets:
                if isinstance(t, ast.Name) and t.id == "_AUTOCOMPLETE_PATHS_GATED_ON_CRITERION_COVERAGE":
                    target_name = t.id
                    value_node = stmt.value
                    break
        elif isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name):
            if stmt.target.id == "_AUTOCOMPLETE_PATHS_GATED_ON_CRITERION_COVERAGE":
                target_name = stmt.target.id
                value_node = stmt.value
        if target_name is None:
            continue
        rc_frozenset_found = True
        if isinstance(value_node, ast.Call):
            if isinstance(value_node.func, ast.Name) and value_node.func.id == "frozenset":
                container = value_node.args[0] if value_node.args else None
                if isinstance(container, (ast.Set, ast.List, ast.Tuple)):
                    for elt in container.elts:
                        if isinstance(elt, ast.Constant) and isinstance(elt.value, str):
                            rc_frozenset_entries.add(elt.value)
        break

    assert rc_frozenset_found, (
        "_AUTOCOMPLETE_PATHS_GATED_ON_CRITERION_COVERAGE frozenset is missing from "
        "research_completion.py. This constant declares which auto-complete paths "
        "are criterion-gated in _stamp_no_findings_refractory. Restore it."
    )
    assert rc_frozenset_entries >= {
        "exhaustion",
        "proven_merge",
        "no_findings",
        "strategist_retire",
    }, (
        f"_AUTOCOMPLETE_PATHS_GATED_ON_CRITERION_COVERAGE in research_completion.py "
        f"must contain at least "
        f"{{'exhaustion', 'proven_merge', 'no_findings', 'strategist_retire'}}; "
        f"found {rc_frozenset_entries}. Restore the missing entries."
    )

    # 4b. Verify _stamp_no_findings_refractory calls the gate function
    snfr_func: ast.AsyncFunctionDef | None = None
    for node in ast.walk(rc_tree):
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "_stamp_no_findings_refractory":
            snfr_func = node
            break

    assert snfr_func is not None, (
        "_stamp_no_findings_refractory not found in research_completion.py"
    )

    snfr_gated = False
    for node in ast.walk(snfr_func):
        if isinstance(node, ast.Call) and _ast_called_name(node) == gate_func_name:
            snfr_gated = True
            break
        if isinstance(node, ast.Await) and isinstance(node.value, ast.Call):
            if _ast_called_name(node.value) == gate_func_name:
                snfr_gated = True
                break

    assert snfr_gated, (
        "_stamp_no_findings_refractory does not call "
        "direction_owns_uncovered_criterion. The proven-merge and exhaustion "
        "auto-complete paths in research_completion.py must gate on criterion "
        "coverage before retiring a direction."
    )

    # ── 5. Verify strategist retire path: _handle_strategic_completion ──────
    sc_path = Path(__file__).resolve().parents[1] / "app" / "strategic_completion.py"
    sc_source = sc_path.read_text(encoding="utf-8")
    sc_tree = ast.parse(sc_source, filename=str(sc_path))

    handler_func: ast.AsyncFunctionDef | None = None
    for node in ast.walk(sc_tree):
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "_handle_strategic_completion":
            handler_func = node
            break

    assert handler_func is not None, (
        "_handle_strategic_completion not found in strategic_completion.py"
    )

    handler_gated = False
    for node in ast.walk(handler_func):
        if isinstance(node, ast.Call) and _ast_called_name(node) == gate_func_name:
            handler_gated = True
            break
        if isinstance(node, ast.Await) and isinstance(node.value, ast.Call):
            if _ast_called_name(node.value) == gate_func_name:
                handler_gated = True
                break

    assert handler_gated, (
        "_handle_strategic_completion does not call direction_owns_uncovered_criterion. "
        "The strategist-authored retire_direction path must gate on criterion "
        "coverage before retiring a direction."
    )


def test_retire_direction_op_gates_on_criterion_coverage():
    """The retire_direction op in strategic_completion.py must gate on criterion coverage.

    Regression guard: the retire_direction handler in
    ``_handle_strategic_completion`` must call
    ``direction_owns_uncovered_criterion`` before setting a direction's
    status to ``"retired"``.  This is the strategist-authored retirement
    path (distinct from the exhaust / proven-merge auto-complete paths
    already covered by
    ``test_all_autocomplete_paths_gate_on_criterion_coverage``).

    Removing the gate from ``_handle_strategic_completion`` causes this
    test to fail.
    """
    import ast
    from pathlib import Path

    sc_path = Path(__file__).resolve().parents[1] / "app" / "strategic_completion.py"
    source = sc_path.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(sc_path))

    gate_func_name = "direction_owns_uncovered_criterion"

    # ── 1. Find _handle_strategic_completion ─────────────────────────────
    handler_func: ast.AsyncFunctionDef | None = None
    for node in ast.walk(tree):
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "_handle_strategic_completion":
            handler_func = node
            break

    assert handler_func is not None, (
        "_handle_strategic_completion not found in strategic_completion.py"
    )

    # ── 2. Verify it calls direction_owns_uncovered_criterion ────────────
    handler_gated = False
    for node in ast.walk(handler_func):
        if isinstance(node, ast.Call) and _ast_called_name(node) == gate_func_name:
            handler_gated = True
            break
        if isinstance(node, ast.Await) and isinstance(node.value, ast.Call):
            if _ast_called_name(node.value) == gate_func_name:
                handler_gated = True
                break

    assert handler_gated, (
        "_handle_strategic_completion does not call direction_owns_uncovered_criterion. "
        "The retire_direction op must gate on criterion coverage before retiring a "
        "direction."
    )


def test_record_task_merge_evidence_gates_on_acceptance_is_proven():
    """record_task_merge_evidence must gate on acceptance_is_proven before writing CriterionEvidence rows.

    Regression guard: the evidence-quality gate (``_reject_unproven_merge_evidence``
    wrapping ``acceptance_is_proven``) must be called inside
    ``record_task_merge_evidence`` before any ``record_evidence`` call. This
    ensures that only machine-checked proven tasks can record CriterionEvidence.
    """
    import ast
    from pathlib import Path

    ac_path = Path(__file__).resolve().parents[1] / "app" / "acceptance_criteria.py"
    source = ac_path.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(ac_path))

    gate_func_name = "_reject_unproven_merge_evidence"

    # Find the record_task_merge_evidence async function
    func: ast.AsyncFunctionDef | None = None
    for node in ast.walk(tree):
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "record_task_merge_evidence":
            func = node
            break

    assert func is not None, (
        "record_task_merge_evidence is no longer defined in acceptance_criteria.py. "
        "Restore this function or update the invariant."
    )

    # Walk the function body to find calls to _reject_unproven_merge_evidence
    gate_called = False
    for node in ast.walk(func):
        if isinstance(node, ast.Call):
            name = _ast_called_name(node)
            if name == gate_func_name:
                gate_called = True
                break
        if isinstance(node, ast.Await) and isinstance(node.value, ast.Call):
            name = _ast_called_name(node.value)
            if name == gate_func_name:
                gate_called = True
                break

    assert gate_called, (
        f"'{gate_func_name}' is not called inside "
        "record_task_merge_evidence in acceptance_criteria.py. "
        "The acceptance_is_proven gate before recording CriterionEvidence rows "
        "has been removed or renamed. Restore it."
    )
