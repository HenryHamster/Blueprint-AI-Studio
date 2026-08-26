"""Resolve the repo ``scripts/`` directory across the two real layouts.

``backend/`` and ``scripts/`` are siblings under the repo root in a developer
checkout, so ``<backend>/../scripts`` resolves correctly there. That relative
walk is WRONG in the Docker test environment, which is the layout every CI
backend shard and ``scripts/test-backend.sh`` run actually uses:

* the backend tree is mounted at ``/app`` with **no** ``scripts/`` sibling, so
  ``<backend>/../scripts`` evaluates to ``/scripts`` — a path that does not
  exist; and
* the repo root is bind-mounted read-only at ``/repo``, so the repo's scripts
  live at ``/repo/scripts``.

A fixed relative walk therefore silently produces a nonexistent directory in
one of the two environments, and every ``sys.path`` insert / import keyed off
it fails for a reason nothing names. :func:`resolve_scripts_dir` probes instead,
and when neither layout is present it returns a NAMED reason rather than a path
that does not exist, so callers can report *why* instead of surfacing a bare
``ModuleNotFoundError``.

Stdlib only, no imports from ``app`` or ``scripts``: this module is imported
during ``check_size_limits``'s own ``sys.path`` bootstrap, before the repo
``scripts/`` dir is importable at all.
"""

from __future__ import annotations

import os

# Probe file that identifies a directory as the repo's ``scripts/``. It is the
# shared verification-receipt writer rather than an arbitrary marker: it is the
# module the size-limits lane actually imports from there, so a directory that
# satisfies this predicate is by construction usable for the import that made
# us look. Kept as a literal (not derived from a caller) so the predicate a
# guard test pins is the predicate production resolution uses.
SCRIPTS_DIR_SENTINEL = "verification_receipt.py"

# Where the repo root is bind-mounted inside the Docker test image. Mirrors the
# `-v "$PWD:/repo:ro"` in both scripts/test-backend.sh and the CI shard matrix.
CONTAINER_REPO_ROOT = "/repo"

# Escape hatch for environments matching neither layout. Same variable name the
# other scripts/-importing test modules already honour.
SCRIPTS_DIR_ENV = "BLUEPRINT_SCRIPTS_DIR"


def resolve_scripts_dir(
    backend_dir: str,
    container_repo_root: str = CONTAINER_REPO_ROOT,
    env_override: str | None = None,
) -> tuple[str | None, str]:
    """Return ``(scripts_dir, unavailable_reason)`` — exactly one is truthy.

    Candidates, in order: an explicit env override, the ``/repo`` bind-mount
    (the Docker test layout), then ``<backend_dir>/../scripts`` (a developer
    checkout). A candidate counts only when it actually carries
    :data:`SCRIPTS_DIR_SENTINEL`, so a directory that happens to exist but is
    not the repo's ``scripts/`` is never returned.

    On a miss the reason names every candidate probed and both escape hatches,
    so the failure is diagnosable from the message alone.
    """
    candidates: list[str] = []
    if env_override:
        candidates.append(env_override)
    candidates.append(os.path.join(container_repo_root, "scripts"))
    candidates.append(os.path.normpath(os.path.join(backend_dir, "..", "scripts")))
    for candidate in candidates:
        if os.path.isfile(os.path.join(candidate, SCRIPTS_DIR_SENTINEL)):
            return os.path.normpath(candidate), ""
    return None, (
        f"repo scripts/ not found: no {SCRIPTS_DIR_SENTINEL} under any of "
        f"{', '.join(candidates)} — set {SCRIPTS_DIR_ENV}, or mount the repo "
        f"root at {container_repo_root} as the CI shards and "
        "scripts/test-backend.sh do"
    )


def resolve_scripts_dir_from_env(
    backend_dir: str,
    container_repo_root: str = CONTAINER_REPO_ROOT,
) -> tuple[str | None, str]:
    """:func:`resolve_scripts_dir` with the env override read from the process."""
    return resolve_scripts_dir(
        backend_dir,
        container_repo_root,
        env_override=os.getenv(SCRIPTS_DIR_ENV),
    )
