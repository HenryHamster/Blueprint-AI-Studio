"""Seam test for the extracted GitHub token resolver module.

Verifies that ``app.github_token_resolver`` and the legacy
``app.routers.agent`` re-export path expose the SAME objects (identical
function/cache/lock instances), so the behavior-preserving extraction did not
fork state, and that importing the resolver module does not require importing
the agent FastAPI router (no router-level coupling / circular import).
"""

import importlib
import subprocess
import sys


def test_resolver_symbols_are_same_objects_via_both_paths():
    import app.github_token_resolver as resolver
    import app.routers.agent as agent

    # Identical object identity across both import paths — the router merely
    # re-exports; it must not define its own copy that could drift.
    assert agent.get_github_token_for_task is resolver.get_github_token_for_task
    assert agent.invalidate_github_token_cache is resolver.invalidate_github_token_cache
    assert agent._github_token_cache is resolver._github_token_cache
    assert agent._github_token_cache_lock is resolver._github_token_cache_lock
    assert agent._GITHUB_TOKEN_TTL == resolver._GITHUB_TOKEN_TTL


def test_cache_is_shared_state_not_a_copy():
    """A mutation through one path is visible through the other (same dict)."""
    import app.github_token_resolver as resolver
    import app.routers.agent as agent

    sentinel_key = "__seam_test_sentinel_project__"
    try:
        resolver._github_token_cache[sentinel_key] = {"token": "x"}
        assert agent._github_token_cache.get(sentinel_key) == {"token": "x"}

        agent.invalidate_github_token_cache(sentinel_key)
        assert sentinel_key not in resolver._github_token_cache
    finally:
        resolver._github_token_cache.pop(sentinel_key, None)


def test_resolver_module_does_not_pull_in_agent_router():
    """The resolver must be importable WITHOUT importing app.routers.agent.

    This is the whole point of the extraction: the 15+ non-router modules that
    only need a GitHub token should not transitively depend on the agent
    FastAPI router. Run in a fresh interpreter so prior imports don't mask it.
    """
    code = (
        "import sys; import app.github_token_resolver; "
        "assert 'app.routers.agent' not in sys.modules, "
        "'github_token_resolver must not import app.routers.agent'"
    )
    result = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr


def test_no_circular_import_on_reimport():
    """Re-importing both modules in either order does not raise."""
    importlib.reload(importlib.import_module("app.github_token_resolver"))
    importlib.import_module("app.routers.agent")
