"""Shared helper for post-commit source inspection tests.

Provides a stage-module registry and a helper that concatenates the source of
``_process_post_commit`` with the source of every registered stage module.

The registry is initially empty; later extraction steps (S2-S12) will register
their sibling modules here so all three source-inspection test files can stay
pointing at ``_post_commit_sources()`` instead of the literal function body.

Precedent: ``_agent_testhelpers.py``, ``_compute_test_helpers.py``.
"""

from __future__ import annotations

import inspect
import types

# Stage modules registered here are appended to _post_commit_sources() output.
# Mutable list so extraction steps can append without re-importing this module.
_POST_COMMIT_STAGE_REGISTRY: list[types.ModuleType] = []


def _post_commit_sources() -> str:
    """Return concatenated source for _process_post_commit and all registered
    stage modules.

    With an empty registry this is identical to
    ``inspect.getsource(_process_post_commit)``, so all existing substring
    and ordering assertions pass unchanged.
    """
    from app.task_lifecycle import _process_post_commit  # lazy import

    parts = [inspect.getsource(_process_post_commit)]
    parts.extend(inspect.getsource(module) for module in _POST_COMMIT_STAGE_REGISTRY)
    return "\n".join(parts)
