#!/usr/bin/env python3
"""Standalone CLI entrypoint for the per-project compute-image build+publish.

The reusable, testable logic lives in ``backend/app/compute_image_build.py``
(stdlib-only, self-contained) so the backend can ``from app import
compute_image_build`` and the DB-backed test suite can exercise it. This wrapper
loads that module **by file path** (not via the ``app`` package, so no FastAPI
/ SQLAlchemy import is triggered) and hands off to its ``main()`` — keeping the
operator's invocation a single ``python3 scripts/build_compute_image.py`` with
no PYTHONPATH setup.

Trigger interface (for the operator wiring a CI job on top)
-----------------------------------------------------------
Invoke with the project repo checked out:

    python3 scripts/build_compute_image.py \
        --repo-dir /path/to/checkout \
        --registry us-docker.pkg.dev/PROJECT/compute-images \
        --image-name my-project \
        --dockerfile .blueprint/Dockerfile.compute \
        --dep-file requirements.txt --dep-file poetry.lock \
        [--parent python:3.11-slim] \
        [--build-arg KEY=VALUE ...] \
        [--dry-run]

On success the last stdout line is the digest-pinned reference
(``registry/repo@sha256:...``); a ``RESULT_JSON:`` line carries a
machine-readable summary. Exit 0 = built or already-present; 1 = build failure;
2 = validation error; 3 = credential leak (scanner runs BEFORE any push, so a
leaked token fails the build closed). The CI trigger should run this on every
push to the project's default branch that touches a declared dependency file or
the Dockerfile; the content-hash gate makes re-runs on unchanged deps a cheap
no-op (registry lookup, no build).
"""
from __future__ import annotations

import importlib.util
import os
import sys

_MODULE_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "backend",
    "app",
    "compute_image_build.py",
)


def _load_module():
    spec = importlib.util.spec_from_file_location("compute_image_build", _MODULE_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load compute-image build module from {_MODULE_PATH}")
    module = importlib.util.module_from_spec(spec)
    # Register before exec so dataclass type-resolution (which looks the module
    # up in sys.modules by __module__) succeeds for path-loaded modules.
    sys.modules["compute_image_build"] = module
    spec.loader.exec_module(module)
    return module


if __name__ == "__main__":
    raise SystemExit(_load_module().main(sys.argv[1:]))
